"""Build a league's knowledge-base SOURCES from nothing but its Sleeper league id.

Walks the season chain (`previous_league_id`) and writes, into the league's own knowledge base (league_ctx.kb_dir),
the same files the index (kb_db) and the readers consume for league #1:

    seasons_history.json       standings per season (+ the Sleeper league id of each season)
    franchises_history.json    roster_id -> manager/team history (franchise continuity is the roster id)
    matchups_history.json      every played game (regular season + playoffs)
    brackets_history.json      official Sleeper winners/losers brackets (stage of every post-season game)
    playoffs_history.json      divisions, overall standings, title and consolation results per season
    h2h_matrix.json            head-to-head matrix (h2h_builder)
    draft_history.json         every draft pick, exact_draft_slots.json the slot -> roster mapping
    trades_history.json        every completed trade (players, picks, FAAB)
    waivers_market_classified.json   every waiver / free-agent move (no quality tiers: those need curation)
    synced_transactions.json   trade ids already known, so the live monitor never re-announces history

What it does NOT produce (curated or chat-derived in league #1, readers degrade gracefully): manager psychological
profiles, the players lineage registry and waiver quality tiers.

    python3 kb_bootstrap.py [--home DIR] [--seasons N]      # runs for the current league (league_ctx)

`get` is injectable (tests feed a fake Sleeper); everything is idempotent: running it again rebuilds the files.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import league_ctx

logger = logging.getLogger("kb_bootstrap")
MAX_WEEK = 18
Getter = Callable[[str], Any]


# --------------------------------------------------------------------------- source files
def _write(kb_dir: str, name: str, data: Any, indent: Optional[int] = 1) -> str:
    os.makedirs(kb_dir, exist_ok=True)
    path = os.path.join(kb_dir, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent, sort_keys=False)
    os.replace(tmp, path)
    return path


def _default_get(ctx: "league_ctx.LeagueContext") -> Getter:
    sc = ctx.sleeper()
    return sc._get


# --------------------------------------------------------------------------- Sleeper readers
def season_chain(start_id: str, get: Getter, limit: int = 12) -> List[Dict[str, Any]]:
    """[league json of each season], oldest first, following previous_league_id from `start_id`."""
    chain: List[Dict[str, Any]] = []
    seen = set()
    cur = str(start_id)
    while cur and cur != "0" and cur not in seen and len(chain) < limit:
        seen.add(cur)
        lg = get(f"league/{cur}")
        if not isinstance(lg, dict) or not lg.get("league_id"):
            break
        chain.append(lg)
        cur = str(lg.get("previous_league_id") or "")
    chain.reverse()
    return chain


def _weeks_to_read(lg: Dict[str, Any], nfl_state: Dict[str, Any]) -> List[int]:
    """Weeks with finished games: all of a completed season, up to last week's for the running one."""
    if str(lg.get("status")) == "complete" or str(lg.get("season")) != str(nfl_state.get("season")):
        return list(range(1, MAX_WEEK + 1))
    return list(range(1, max(1, int(nfl_state.get("week") or 1))))


def _fmt_date(ms: Any, tz: Optional[ZoneInfo] = None) -> Tuple[str, str]:
    """(ISO date, dd/mm/yyyy HH:MM) of a Sleeper millisecond timestamp, in the league's time zone."""
    try:
        dt = datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).astimezone(tz or timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        dt = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%d/%m/%Y %H:%M")


class SeasonData:
    """Everything one season of the chain needs, fetched once."""

    def __init__(self, league: Dict[str, Any]):
        self.league = league
        self.id = str(league["league_id"])
        self.season = str(league.get("season"))
        self.settings = league.get("settings") or {}
        self.users: List[Dict[str, Any]] = []
        self.rosters: List[Dict[str, Any]] = []
        self.matchups: Dict[int, List[Dict[str, Any]]] = {}
        self.transactions: Dict[int, List[Dict[str, Any]]] = {}
        self.drafts: List[Dict[str, Any]] = []
        self.draft_picks: Dict[str, List[Dict[str, Any]]] = {}
        self.winners: List[Dict[str, Any]] = []
        self.losers: List[Dict[str, Any]] = []

    @property
    def playoff_start(self) -> int:
        return int(self.settings.get("playoff_week_start") or 15)

    def user_of(self, owner_id: Any) -> Dict[str, Any]:
        for u in self.users:
            if str(u.get("user_id")) == str(owner_id):
                return u
        return {}

    def roster_info(self) -> Dict[int, Dict[str, Any]]:
        """roster_id -> {owner_id, manager (display name), team, wins, losses, ties, pf, pa, division, players}."""
        out: Dict[int, Dict[str, Any]] = {}
        for r in self.rosters:
            rid = int(r.get("roster_id"))
            u = self.user_of(r.get("owner_id"))
            st = r.get("settings") or {}
            disp = str(u.get("display_name") or f"Time {rid}")
            out[rid] = {
                "owner_id": str(r.get("owner_id") or ""), "manager": disp,
                "team": str((u.get("metadata") or {}).get("team_name") or disp),
                "wins": int(st.get("wins") or 0), "losses": int(st.get("losses") or 0), "ties": int(st.get("ties") or 0),
                "pf": round(float(st.get("fpts") or 0) + float(st.get("fpts_decimal") or 0) / 100.0, 2),
                "pa": round(float(st.get("fpts_against") or 0) + float(st.get("fpts_against_decimal") or 0) / 100.0, 2),
                "division": int(st.get("division") or 1), "players": len(r.get("players") or []),
            }
        return out


def fetch_season(league: Dict[str, Any], nfl_state: Dict[str, Any], get: Getter, workers: int = 8) -> SeasonData:
    sd = SeasonData(league)
    lid = sd.id
    sd.users = get(f"league/{lid}/users") or []
    sd.rosters = get(f"league/{lid}/rosters") or []
    weeks = _weeks_to_read(league, nfl_state)

    def matchup(w: int):
        return w, get(f"league/{lid}/matchups/{w}") or []

    def tx(w: int):
        return w, get(f"league/{lid}/transactions/{w}") or []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for w, m in pool.map(matchup, weeks):
            if m:
                sd.matchups[w] = m
        for w, t in pool.map(tx, range(1, MAX_WEEK + 1) if str(league.get("status")) == "complete"
                             else range(1, max(2, int(nfl_state.get("week") or 1)) + 1)):
            if t:
                sd.transactions[w] = t
    sd.winners = get(f"league/{lid}/winners_bracket") or []
    sd.losers = get(f"league/{lid}/losers_bracket") or []
    drafts = get(f"league/{lid}/drafts") or []
    sd.drafts = [d for d in drafts if isinstance(d, dict)]
    for d in sd.drafts:
        did = str(d.get("draft_id"))
        full = get(f"draft/{did}") or {}
        d.update({k: v for k, v in full.items() if k in ("slot_to_roster_id", "draft_order", "metadata", "settings")})
        sd.draft_picks[did] = get(f"draft/{did}/picks") or []
    return sd


# --------------------------------------------------------------------------- builders
def build_seasons_history(seasons: List[SeasonData]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for sd in seasons:
        info = sd.roster_info()
        order = sorted(info, key=lambda r: (-info[r]["wins"], -info[r]["pf"]))
        rows = []
        for rank, rid in enumerate(order, 1):
            i = info[rid]
            rows.append({"roster_id": rid, "owner_id": i["owner_id"], "owner_name": i["manager"], "team_name": i["team"],
                         "wins": i["wins"], "losses": i["losses"], "ties": i["ties"], "pts_for": i["pf"],
                         "players_count": i["players"], "rank": rank})
        out[sd.season] = {"season": sd.season, "league_id": sd.id, "standings": rows}
    return out


def build_franchises(seasons: List[SeasonData]) -> Dict[str, Any]:
    fr: Dict[int, Dict[str, Any]] = {}
    for sd in seasons:                          # oldest -> newest: the latest values end up as "current"
        for rid, i in sd.roster_info().items():
            f = fr.setdefault(rid, {"roster_id": rid, "aliases": set(), "seasons": {}, "_owners": [], "_teams": []})
            f["canonical_manager"], f["real_name"], f["current_team"] = i["manager"], i["manager"], i["team"]
            f["aliases"].update(x.lower() for x in (i["manager"], i["team"]) if x)
            f["seasons"][sd.season] = {"owner_id": i["owner_id"], "sleeper_display_name": i["manager"],
                                       "team_name": i["team"], "wins": i["wins"], "losses": i["losses"],
                                       "ties": i["ties"], "points_for": i["pf"]}
            f["_owners"].append((sd.season, i["owner_id"], i["manager"]))
            f["_teams"].append((sd.season, i["team"]))
    out: Dict[str, Any] = {}
    for rid, f in sorted(fr.items()):
        def runs(items, key_idx):
            groups: List[Dict[str, Any]] = []
            for it in items:
                if groups and groups[-1]["_k"] == it[key_idx]:
                    groups[-1]["seasons"].append(it[0])
                else:
                    groups.append({"_k": it[key_idx], "seasons": [it[0]], "_it": it})
            return groups
        mh = [{"owner_id": g["_it"][1], "manager_name": g["_it"][2], "sleeper_user": g["_it"][2],
               "start_season": g["seasons"][0], "end_season": g["seasons"][-1], "seasons": g["seasons"]}
              for g in runs(f["_owners"], 1)]
        th = [{"team_name": g["_it"][1], "start_season": g["seasons"][0], "end_season": g["seasons"][-1],
               "seasons": g["seasons"]} for g in runs(f["_teams"], 1)]
        out[str(rid)] = {"roster_id": rid, "canonical_manager": f["canonical_manager"], "real_name": f["real_name"],
                         "current_team": f["current_team"], "aliases": sorted(f["aliases"]),
                         "managers_history": mh, "teams_history": th, "seasons": f["seasons"]}
    return out


def build_matchups(seasons: List[SeasonData]) -> List[Dict[str, Any]]:
    games: List[Dict[str, Any]] = []
    for sd in seasons:
        info = sd.roster_info()
        for week in sorted(sd.matchups):
            groups: Dict[Any, list] = {}
            for e in sd.matchups[week]:
                if e.get("matchup_id") is not None:
                    groups.setdefault(e["matchup_id"], []).append(e)
            for pair in groups.values():
                if len(pair) != 2:
                    continue
                a, b = sorted(pair, key=lambda x: int(x["roster_id"]))
                pa, pb = float(a.get("points") or 0.0), float(b.get("points") or 0.0)
                if pa == 0.0 and pb == 0.0:      # not played (bye weeks / future weeks)
                    continue
                ra, rb = int(a["roster_id"]), int(b["roster_id"])
                po = week >= sd.playoff_start
                games.append({"season": sd.season, "week": week, "is_playoff": po,
                              "phase": "Playoffs" if po else "Temporada Regular", "roster_a": ra, "roster_b": rb,
                              "manager_a": info.get(ra, {}).get("manager", f"Roster_{ra}"),
                              "manager_b": info.get(rb, {}).get("manager", f"Roster_{rb}"),
                              "team_a": info.get(ra, {}).get("team", f"Time {ra}"),
                              "team_b": info.get(rb, {}).get("team", f"Time {rb}"),
                              "points_a": round(pa, 2), "points_b": round(pb, 2)})
    games.sort(key=lambda g: (g["season"], g["week"], g["roster_a"]))
    return games


def _placements(bracket: List[Dict[str, Any]]) -> Dict[int, int]:
    """placement -> roster_id from the bracket games that carry a `p` (1 = final, 3 = third place game, ...)."""
    res: Dict[int, int] = {}
    for g in bracket or []:
        p, w, l = g.get("p"), g.get("w"), g.get("l")
        if p and w and l:
            res[int(p)], res[int(p) + 1] = int(w), int(l)
    return res


def build_playoffs(seasons: List[SeasonData]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for sd in seasons:
        info = sd.roster_info()
        names = {rid: f"{i['team']} ({i['manager']})" for rid, i in info.items()}
        order = sorted(info, key=lambda r: (-info[r]["wins"], -info[r]["pf"]))
        meta = sd.league.get("metadata") or {}
        div_names = {n: str(meta.get(f"division_{n}") or f"Divisão {n}") for n in
                     range(1, int(sd.settings.get("divisions") or 0) + 1)}
        divisions: Dict[str, Any] = {}
        overall = []
        for rank, rid in enumerate(order, 1):
            i = info[rid]
            row = {"roster_id": rid, "team_name": i["team"], "manager_name": i["manager"], "wins": i["wins"],
                   "losses": i["losses"], "ties": i["ties"], "pts_for": i["pf"], "pts_against": i["pa"]}
            overall.append({**row, "overall_rank": rank, "division_name": div_names.get(i["division"], f"Divisão {i['division']}")})
            d = divisions.setdefault(str(i["division"]), {"division_id": i["division"],
                                                          "division_name": div_names.get(i["division"], f"Divisão {i['division']}"),
                                                          "teams": []})
            d["teams"].append(row)
        for d in divisions.values():
            d["teams"].sort(key=lambda t: (-t["wins"], -t["pts_for"]))
            for n, t in enumerate(d["teams"], 1):
                t["division_rank"] = n
        win, lose = _placements(sd.winners), _placements(sd.losers)

        def nm(placed: Dict[int, int], p: int) -> Optional[str]:
            return names.get(placed.get(p)) if placed.get(p) else None
        title = {"champion": nm(win, 1) or "A Definir", "runner_up": nm(win, 2), "third_place": nm(win, 3),
                 "fourth_place": nm(win, 4), "fifth_place": nm(win, 5), "sixth_place": nm(win, 6)}
        toilet = {"competition_name": "Toilet Bowl (Consolation Bracket)", "toilet_champion": nm(lose, 1),
                  "toilet_runner_up": nm(lose, 2), "ninth_place": nm(lose, 3), "tenth_place": nm(lose, 4),
                  "eleventh_place": nm(lose, 5), "lanterna": nm(lose, 6) or (names.get(order[-1]) if order else None)}
        out[sd.season] = {"season": sd.season, "league_id": sd.id, "divisions": divisions, "overall_standings": overall,
                          "playoffs_title": title, "toilet_bowl": toilet}
    return out


def build_drafts(seasons: List[SeasonData]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    history: Dict[str, Any] = {}
    slots: Dict[str, Any] = {}
    for sd in seasons:
        done = [d for d in sd.drafts if sd.draft_picks.get(str(d.get("draft_id")))]
        if not done:
            continue
        d = sorted(done, key=lambda x: (str(x.get("status")) == "complete", int(x.get("start_time") or 0)))[-1]
        picks = sd.draft_picks[str(d["draft_id"])]
        name = (d.get("metadata") or {}).get("name") or f"Draft {sd.season}"
        history[sd.season] = {"draft_id": str(d["draft_id"]), "draft_name": name, "season": sd.season,
                              "total_picks": len(picks), "picks": picks}
        s2r = {str(k): v for k, v in (d.get("slot_to_roster_id") or {}).items() if str(k).isdigit() and v}
        order = {str(k): v for k, v in (d.get("draft_order") or {}).items() if k and v}
        slots[sd.season] = {"slot_to_roster_id": s2r, "draft_order": order}
    return history, slots


def build_trades(seasons: List[SeasonData], players: Dict[str, Dict[str, Any]],
                 drafts: Dict[str, Any], slots: Dict[str, Any],
                 tz: Optional[ZoneInfo] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(trades, ids): `Sleeper trade` -> the sides/assets shape of trades_history.json."""
    # (draft season, round, original roster) -> the pick that was made with it
    made: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
    for season, d in drafts.items():
        s2r = {int(k): int(v) for k, v in (slots.get(season) or {}).get("slot_to_roster_id", {}).items() if v}
        for p in d.get("picks", []):
            orig = s2r.get(int(p.get("draft_slot") or 0))
            if orig:
                made[(season, int(p.get("round") or 0), orig)] = p
    trades: List[Dict[str, Any]] = []
    ids: List[str] = []
    for sd in seasons:
        info = sd.roster_info()
        rows = []
        for w, txs in sd.transactions.items():
            for t in txs:
                if t.get("type") == "trade" and t.get("status") == "complete":
                    rows.append((int(t.get("status_updated") or t.get("created") or 0), w, t))
        for ts, w, t in sorted(rows, key=lambda r: r[0]):
            sides: Dict[int, Dict[str, Any]] = {}
            for rid in t.get("roster_ids") or []:
                i = info.get(int(rid), {})
                sides[int(rid)] = {"manager": i.get("manager", f"Roster_{rid}"), "team": i.get("team", f"Time {rid}"),
                                   "assets": []}
            for pid, rid in (t.get("adds") or {}).items():
                p = players.get(str(pid), {})
                sides.setdefault(int(rid), {"manager": info.get(int(rid), {}).get("manager", f"Roster_{rid}"),
                                            "team": info.get(int(rid), {}).get("team", f"Time {rid}"), "assets": []})
                sides[int(rid)]["assets"].append({"kind": "player", "player_id": str(pid),
                                                  "name": p.get("name") or f"Jogador {pid}", "pos": p.get("pos") or "?",
                                                  "team": p.get("team") or "FA"})
            for dp in t.get("draft_picks") or []:
                rcv = int(dp.get("owner_id"))
                orig = int(dp.get("roster_id"))
                year, rnd = str(dp.get("season")), int(dp.get("round") or 1)
                a: Dict[str, Any] = {"kind": "pick", "round": rnd, "year": year,
                                     "via": info.get(orig, {}).get("manager"), "status": "future"}
                used = made.get((year, rnd, orig))
                if used:
                    meta = used.get("metadata") or {}
                    nm = f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
                    a.update({"status": "used", "overall": used.get("pick_no"), "used_no": used.get("pick_no"),
                              "used_player": f"{nm} ({meta.get('position') or '?'} - {meta.get('team') or 'FA'})" if nm else None,
                              "used_by": info.get(int(used.get("roster_id") or 0), {}).get("manager")})
                sides.setdefault(rcv, {"manager": info.get(rcv, {}).get("manager", f"Roster_{rcv}"),
                                       "team": info.get(rcv, {}).get("team", f"Time {rcv}"), "assets": []})
                sides[rcv]["assets"].append(a)
            for wb in t.get("waiver_budget") or []:
                rcv = int(wb.get("receiver"))
                sides.setdefault(rcv, {"manager": info.get(rcv, {}).get("manager", f"Roster_{rcv}"),
                                       "team": info.get(rcv, {}).get("team", f"Time {rcv}"), "assets": []})
                sides[rcv]["assets"].append({"kind": "faab", "amount": int(wb.get("amount") or 0)})
            iso, _ = _fmt_date(ts, tz)
            tid = str(t.get("transaction_id") or "")
            ids.append(tid)
            trades.append({"tx_id": tid or None, "date": iso, "season": sd.season, "week": w,
                           "sides": [sides[k] for k in sorted(sides)]})
    return trades, ids


def build_waivers(seasons: List[SeasonData], players: Dict[str, Dict[str, Any]],
                  tz: Optional[ZoneInfo] = None) -> Dict[str, Any]:
    def asset(pid: Any) -> Dict[str, Any]:
        p = players.get(str(pid), {})
        name = p.get("name") or f"Jogador {pid}"
        pos, team = p.get("pos") or "?", p.get("team") or "FA"
        return {"player_id": str(pid), "name": name, "position": pos, "team": team, "label": f"{name} ({pos} - {team})"}
    rows: List[Dict[str, Any]] = []
    for sd in seasons:
        info = sd.roster_info()
        for w, txs in sd.transactions.items():
            for t in txs:
                if t.get("type") not in ("waiver", "free_agent", "commissioner"):
                    continue
                rid = int((t.get("roster_ids") or [0])[0] or 0)
                i = info.get(rid, {})
                ts = int(t.get("status_updated") or t.get("created") or 0)
                _, human = _fmt_date(ts, tz)
                rows.append({"transaction_id": str(t.get("transaction_id")), "season": sd.season, "week": w,
                             "date": human, "created_ts": ts / 1000.0, "type": t.get("type"),
                             "status": t.get("status"), "roster_id": rid, "manager": i.get("manager", f"Roster_{rid}"),
                             "real_name": i.get("manager", ""), "team_name": i.get("team", f"Time {rid}"),
                             "bid": int((t.get("settings") or {}).get("waiver_bid") or 0),
                             "quality_tier": None, "tier_badge": "", "quality_reason": "",
                             "adds": [asset(p) for p in (t.get("adds") or {})],
                             "drops": [asset(p) for p in (t.get("drops") or {})]})
    rows.sort(key=lambda r: r["created_ts"])
    return {"summary": {"source": "kb_bootstrap (Sleeper API)", "tiers": "not classified"}, "manager_profiles": {},
            "transactions": rows}


# --------------------------------------------------------------------------- orchestration
def bootstrap(ctx: Optional["league_ctx.LeagueContext"] = None, seasons: Optional[int] = None,
              get: Optional[Getter] = None, players: Optional[Dict[str, Dict[str, Any]]] = None,
              progress: Callable[[str], None] = lambda m: logger.info(m), workers: int = 8) -> Dict[str, Any]:
    """Write the league's knowledge-base sources. Returns {file: count} plus the seasons covered."""
    ctx = ctx or league_ctx.current()
    get = get or _default_get(ctx)
    t0 = time.monotonic()
    state = get("state/nfl") or {}
    chain = season_chain(ctx.league_id, get, limit=seasons or 12)
    if seasons:
        chain = chain[-seasons:]
    if not chain:
        raise RuntimeError(f"league {ctx.league_id} not found on Sleeper")
    progress(f"{len(chain)} temporada(s): " + ", ".join(str(l.get('season')) for l in chain))
    data: List[SeasonData] = []
    for lg in chain:
        progress(f"lendo {lg.get('season')} ({lg.get('league_id')})…")
        data.append(fetch_season(lg, state, get, workers=workers))
    if players is None:
        import players_db
        with league_ctx.use(ctx):
            players = players_db.load()
    kb = ctx.kb_dir
    report: Dict[str, Any] = {"seasons": [sd.season for sd in data]}
    hist = build_seasons_history(data)
    _write(kb, "seasons_history.json", hist)
    fr = build_franchises(data)
    _write(kb, "franchises_history.json", fr)
    games = build_matchups(data)
    _write(kb, "matchups_history.json", games, indent=2)
    report["matchups_history.json"] = len(games)
    po = build_playoffs(data)
    _write(kb, "playoffs_history.json", po)
    drafts, slots = build_drafts(data)
    _write(kb, "draft_history.json", drafts)
    _write(kb, "exact_draft_slots.json", slots)
    report["draft_history.json"] = sum(d.get("total_picks", 0) for d in drafts.values())
    tz = ZoneInfo(ctx.timezone)
    trades, trade_ids = build_trades(data, players, drafts, slots, tz)
    with open(os.path.join(kb, "trades_history.json"), "w", encoding="utf-8") as f:   # one trade per line, like kb_db
        f.write('{"version": 1, "trades": [\n')
        f.write(",\n".join(json.dumps(t, ensure_ascii=False, separators=(",", ":")) for t in trades))
        f.write("\n]}\n")
    report["trades_history.json"] = len(trades)
    wv = build_waivers(data, players, tz)
    _write(kb, "waivers_market_classified.json", wv, indent=None)
    report["waivers_market_classified.json"] = len(wv["transactions"])
    # the live monitor must not re-announce / re-record history (trades and waiver moves alike)
    known = set(trade_ids) | {r["transaction_id"] for r in wv["transactions"]}
    _write(kb, "synced_transactions.json", {"transactions": sorted(known),
                                           "last_update": datetime.now().isoformat()})
    # official brackets (stage of every post-season game) and the head-to-head matrix
    import h2h_builder
    try:
        ids = {sd.season: sd.id for sd in data}
        brackets = {"_meta": {"source": "Sleeper /winners_bracket + /losers_bracket", "updated": datetime.now().isoformat(),
                              "note": "só jogos decididos; semana = playoff_week_start + rodada - 1"}}
        for sd in data:
            brackets[sd.season] = h2h_builder.fetch_season(sd.season, sd.id, get)
        _write(kb, "brackets_history.json", brackets)
        _write(kb, "h2h_matrix.json", h2h_builder.build_matrix(games, {int(k): v for k, v in fr.items()}, brackets),
               indent=2)
        report["brackets_history.json"] = len(ids)
    except Exception as exc:  # leagues without brackets still get everything else
        logger.warning("brackets/h2h skipped: %s", exc)
    try:                                       # current rosters (starters, bench, taxi/IR, strengths/needs)
        with league_ctx.use(ctx):
            import build_teams_rosters_analysis
            build_teams_rosters_analysis.build_team_analysis(get)
        report["teams_rosters_analysis.json"] = 1
    except Exception as exc:
        logger.warning("roster dossier skipped: %s", exc)
    progress(f"pronto em {time.monotonic() - t0:.0f}s")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", help="league home (default: current league)")
    ap.add_argument("--seasons", type=int, help="only the last N seasons")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ctx = league_ctx.for_home(args.home) if args.home else league_ctx.current()
    with league_ctx.use(ctx):
        rep = bootstrap(ctx, seasons=args.seasons, progress=print)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
