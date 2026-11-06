#!/usr/bin/env python3
"""Data Lab — real NFL advanced stats for the High Stakes Fantasy league (all 12 teams).

Sources (free, public, weekly):
  * nflverse play-by-play (air yards, pass depth/side, YAC vs expected, EPA, CPOE, success)
  * NFL Next Gen Stats via nflverse (separation, cushion, share of intended air yards, YAC over
    expected, time to throw, aggressiveness, rush yards over expected)
  * nflverse snap counts (offense snap %) and PFR advanced (drops, broken tackles)
  * Sleeper (league rosters, weekly starters, PPR points, player id ↔ GSIS id)

Everything numeric is computed in code. Jev only picks the storyline angle per player (Choice);
the chat model (jev-router) only writes one acid line per team from the computed facts.

"Rotas": public data has no route-level charting, so we use the TARGET MAP as a route proxy:
target depth (curto <10 / médio 10–19 / profundo 20+ air yards) × side (esq/meio/dir).
"""

from __future__ import annotations

import csv
import difflib
import gzip
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "nfl_data")
os.makedirs(DATA_DIR, exist_ok=True)
import league_ctx


def _lid() -> str:
    """Sleeper id of the current league (the NFL data below is league-independent and shared)."""
    return league_ctx.current().league_id


NV = "https://github.com/nflverse/nflverse-data/releases/download"
UA = {"User-Agent": "Mozilla/5.0 (MrIrrelevant DataLab)"}
TEAM_FIX = {"LAR": "LA", "WSH": "WAS", "JAC": "JAX"}  # Sleeper/ESPN → nflverse abbreviations
SKILL = {"QB", "RB", "WR", "TE"}
PBP_COLS = ("game_id", "week", "posteam", "defteam", "pass_attempt", "rush_attempt", "complete_pass",
            "qb_dropback", "sack", "interception", "pass_touchdown", "rush_touchdown", "receiving_yards",
            "rushing_yards", "passing_yards", "yards_after_catch", "air_yards", "pass_location",
            "yardline_100", "epa", "success", "cpoe", "xyac_mean_yardage", "receiver_player_id",
            "rusher_player_id", "passer_player_id", "qb_scramble", "two_point_attempt", "season_type")


# ------------------------------------------------------------------ fetch / cache
def _fetch(name: str, url: str, max_age_h: float = 6.0) -> str:
    path = os.path.join(DATA_DIR, name)
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
        return path
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        tmp = path + ".tmp"
        open(tmp, "wb").write(data)
        os.replace(tmp, path)
    except Exception as exc:
        if not os.path.exists(path):
            raise RuntimeError(f"download failed {name}: {exc}")
    return path


def _csv(path: str) -> List[Dict[str, str]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _f(v: Any) -> float:
    try:
        return float(v) if v not in (None, "", "NA") else 0.0
    except ValueError:
        return 0.0


def _has(v: Any) -> bool:
    return v not in (None, "", "NA")


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


_MEMO: Dict[str, Any] = {}


def _memo(key: str, ttl: float, fn):
    hit = _MEMO.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _MEMO[key] = (time.time(), val)
    return val


def sleeper_map() -> Dict[str, Dict[str, Any]]:
    """sleeper_id -> {name, pos, team, gsis} (compact daily cache of /players/nfl)."""
    def build():
        path = os.path.join(DATA_DIR, "sleeper_ids.json")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 24 * 3600:
            return json.load(open(path, encoding="utf-8"))
        req = urllib.request.Request("https://api.sleeper.app/v1/players/nfl", headers=UA)
        raw = json.loads(urllib.request.urlopen(req, timeout=60).read())
        out = {pid: {"name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                     "pos": p.get("position"), "team": p.get("team"), "gsis": (p.get("gsis_id") or "").strip(),
                     "pfr": "", "espn": str(p.get("espn_id") or "")}
               for pid, p in raw.items() if p.get("position") in SKILL | {"K"}}
        # Sleeper lacks gsis_id for most recent players → DynastyProcess id crosswalk (sleeper↔gsis↔pfr).
        try:
            ids = _csv(_fetch("db_playerids.csv", "https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_playerids.csv", 24))
            for r in ids:
                sid = (r.get("sleeper_id") or "").split(".")[0]
                if sid in out:
                    if not out[sid]["gsis"] and _has(r.get("gsis_id")):
                        out[sid]["gsis"] = r["gsis_id"]
                    if _has(r.get("pfr_id")):
                        out[sid]["pfr"] = r["pfr_id"]
                    if not out[sid]["espn"] and _has(r.get("espn_id")):
                        out[sid]["espn"] = r["espn_id"].split(".")[0]
        except Exception as exc:
            print(f"[data_lab] id crosswalk failed: {exc}", file=sys.stderr)
        json.dump(out, open(path, "w", encoding="utf-8"))
        return out
    return _memo("sleeper_map", 3600, build)


def _season() -> str:
    from sleeper_client import SleeperClient
    return str(SleeperClient(_lid()).get_nfl_state().get("season") or datetime.now().year)


def espn_player_news(sleeper_id: str, max_age_h: float = 96) -> List[Dict[str, str]]:
    """ESPN athlete overview: Rotowire analysis blurb + ESPN headlines for ONE player (by espn id)."""
    espn = (sleeper_map().get(str(sleeper_id)) or {}).get("espn")
    if not espn:
        return []
    def build():
        url = f"https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{espn}/overview"
        try:
            d = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10).read())
        except Exception:
            return []
        out = []
        rw = d.get("rotowire") or {}
        if rw.get("headline"):
            out.append({"title": rw["headline"], "summary": (rw.get("story") or "")[:600],
                        "published_at": str(rw.get("published") or rw.get("lastModified") or ""),
                        "source": "Rotowire (ESPN)", "category": ""})
        cutoff = time.time() - max_age_h * 3600
        for n in (d.get("news") or [])[:6]:
            ts = n.get("published") or ""
            try:
                from datetime import datetime as _dt
                if _dt.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < cutoff:
                    continue
            except Exception:
                pass
            out.append({"title": n.get("headline", ""), "summary": (n.get("description") or "")[:300],
                        "published_at": ts, "source": "ESPN", "category": ""})
        return out
    return _memo(f"espnnews{espn}", 900, build)


def pbp(season: str) -> List[Dict[str, str]]:
    def build():
        rows = _csv(_fetch(f"pbp_{season}.csv.gz", f"{NV}/pbp/play_by_play_{season}.csv.gz"))
        return [{k: r.get(k, "") for k in PBP_COLS} for r in rows if r.get("season_type") == "REG"]
    return _memo(f"pbp{season}", 1800, build)


def ngs(kind: str, season: str) -> Dict[Tuple[str, int], Dict[str, str]]:
    def build():
        rows = _csv(_fetch(f"ngs_{kind}.csv.gz", f"{NV}/nextgen_stats/ngs_{kind}.csv.gz"))
        return {(r["player_gsis_id"], int(r["week"])): r for r in rows
                if r.get("season") == season and r.get("season_type") == "REG" and r.get("week", "0") != "0"}
    return _memo(f"ngs{kind}{season}", 1800, build)


def snaps(season: str) -> Dict[Tuple[str, int], float]:
    def build():
        rows = _csv(_fetch(f"snaps_{season}.csv.gz", f"{NV}/snap_counts/snap_counts_{season}.csv.gz"))
        out = {}
        for r in rows:
            if r.get("game_type") != "REG":
                continue
            pct = _f(r["offense_pct"])
            out[(r["pfr_player_id"], int(r["week"]))] = pct * 100 if pct <= 1.0 else pct
        return out
    return _memo(f"snaps{season}", 1800, build)


def pfr_rec(season: str) -> Dict[Tuple[str, int], Dict[str, str]]:
    def build():
        rows = _csv(_fetch(f"pfr_rec_{season}.csv.gz", f"{NV}/pfr_advstats/advstats_week_rec_{season}.csv.gz"))
        return {(r["pfr_player_id"], int(r["week"])): r for r in rows}
    return _memo(f"pfr{season}", 1800, build)


# ------------------------------------------------------------------ play-by-play index
def _new() -> Dict[str, float]:
    return defaultdict(float)


def build_index(season: str) -> Dict[str, Any]:
    """players[gsis][week] -> metrics ; teams[team][week] -> denominators ; games per week."""
    def build():
        P: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(lambda: defaultdict(_new))
        T: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(lambda: defaultdict(_new))
        D: Dict[str, Dict[int, Dict[str, float]]] = defaultdict(lambda: defaultdict(_new))  # defense allowed
        games: Dict[int, set] = defaultdict(set)
        opp: Dict[Tuple[str, int], str] = {}
        pteam: Dict[Tuple[str, int], str] = {}
        for r in pbp(season):
            if r["two_point_attempt"] == "1":
                continue
            w = int(_f(r["week"]))
            team, dteam = r["posteam"], r["defteam"]
            if not team:
                continue
            games[w].add(r["game_id"])
            opp[(team, w)] = dteam
            epa = _f(r["epa"])
            for pid_col in ("receiver_player_id", "rusher_player_id", "passer_player_id"):
                if _has(r[pid_col]):
                    pteam[(r[pid_col], w)] = team
            if r["pass_attempt"] == "1" and r["sack"] != "1" and _has(r["receiver_player_id"]):
                g = r["receiver_player_id"]
                m = P[g][w]
                air, ydl = _f(r["air_yards"]), _f(r["yardline_100"])
                m["tgt"] += 1
                m["air"] += air
                m["epa_tgt"] += epa
                m["rz_tgt"] += ydl <= 20
                m["ez_tgt"] += _has(r["air_yards"]) and air >= ydl
                m["short" if air < 10 else "mid" if air < 20 else "deep"] += 1
                side = r["pass_location"] or "?"
                m[f"side_{side}"] += 1
                T[team][w]["tgt"] += 1
                T[team][w]["air"] += air
                if r["complete_pass"] == "1":
                    m["rec"] += 1
                    m["rec_yd"] += _f(r["receiving_yards"])
                    m["yac"] += _f(r["yards_after_catch"])
                    if _has(r["xyac_mean_yardage"]):
                        m["xyac"] += _f(r["xyac_mean_yardage"])
                        m["yac_n"] += 1
                m["rec_td"] += r["pass_touchdown"] == "1"
            if r["rush_attempt"] == "1" and _has(r["rusher_player_id"]):
                g = r["rusher_player_id"]
                m = P[g][w]
                yd = _f(r["rushing_yards"])
                m["car"] += 1
                m["rush_yd"] += yd
                m["rush_td"] += r["rush_touchdown"] == "1"
                m["epa_rush"] += epa
                m["succ"] += r["success"] == "1"
                m["expl"] += yd >= 10
                m["rz_car"] += _f(r["yardline_100"]) <= 20
                if r["qb_scramble"] != "1":
                    T[team][w]["car"] += 1
                D[dteam][w]["rush_epa"] += epa
                D[dteam][w]["rush_n"] += 1
            if r["qb_dropback"] == "1" and _has(r["passer_player_id"]):
                g = r["passer_player_id"]
                m = P[g][w]
                m["db"] += 1
                m["epa_db"] += epa
                m["sacks"] += r["sack"] == "1"
                if r["pass_attempt"] == "1" and r["sack"] != "1":
                    m["att"] += 1
                    m["cmp"] += r["complete_pass"] == "1"
                    m["pass_yd"] += _f(r["passing_yards"])
                    m["pass_td"] += r["pass_touchdown"] == "1"
                    m["int"] += r["interception"] == "1"
                    if _has(r["air_yards"]):
                        m["att_air"] += _f(r["air_yards"])
                        m["deep_att"] += _f(r["air_yards"]) >= 20
                    if _has(r["cpoe"]):
                        m["cpoe"] += _f(r["cpoe"])
                        m["cpoe_n"] += 1
                D[dteam][w]["pass_epa"] += epa
                D[dteam][w]["pass_n"] += 1
        return {"P": P, "T": T, "D": D, "games": {w: len(g) for w, g in games.items()}, "opp": opp,
                "pteam": pteam}
    return _memo(f"idx{season}", 1800, build)


def last_complete_week(season: str) -> int:
    g = build_index(season)["games"]
    full = [w for w, n in g.items() if n >= 13]
    return max(full) if full else max(g or {1: 0})


def week_fully_loaded(season: str, week: int) -> bool:
    """True when nflverse PBP has every game ESPN scheduled for ``week`` (e.g. MNF included)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&season={season}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        n = len(json.loads(urllib.request.urlopen(req, timeout=15).read()).get("events", []))
    except Exception:
        return False
    return n > 0 and build_index(season)["games"].get(week, 0) >= n


def _sum(weeks: Dict[int, Dict[str, float]], ws) -> Dict[str, float]:
    out: Dict[str, float] = defaultdict(float)
    for w in ws:
        for k, v in (weeks.get(w) or {}).items():
            out[k] += v
    return out


# ------------------------------------------------------------------ league context
def league_context(season: str, week: int) -> Dict[str, Any]:
    from sleeper_client import SleeperClient
    s = SleeperClient(_lid())
    users = s.get_users()
    stats = s._get(f"stats/nfl/regular/{season}/{week}") or {}
    proj = s._get(f"projections/nfl/regular/{season}/{week}") or {}
    starters_by_roster: Dict[int, List[str]] = {}
    pair_by_roster: Dict[int, int] = {}
    # RAW endpoint (per roster): the week's real starters + matchup pairing. get_matchups() is pre-grouped.
    for m in s._get(f"league/{s.league_id}/matchups/{week}") or []:
        rid = m.get("roster_id")
        starters_by_roster[rid] = [str(x) for x in (m.get("starters") or []) if str(x) != "0"]
        if m.get("matchup_id") is not None:
            pair_by_roster[rid] = m["matchup_id"]
    teams = []
    for r in s.get_rosters():
        u = users.get(r.get("owner_id"), {})
        rid = r.get("roster_id")
        teams.append({"rid": rid, "team": u.get("team_name") or f"Time {rid}", "manager": u.get("display_name", ""),
                      "players": [str(p) for p in r.get("players") or []],
                      "starters": starters_by_roster.get(rid) or [str(p) for p in r.get("starters") or []],
                      "matchup_id": pair_by_roster.get(rid)})
    return {"teams": teams, "stats": stats, "proj": proj}


# ------------------------------------------------------------------ player profile
def profile(sid: str, season: str, week: int) -> Optional[Dict[str, Any]]:
    info = sleeper_map().get(sid)
    if not info or info["pos"] not in SKILL or not info["gsis"]:
        return None
    idx = build_index(season)
    g = info["gsis"]
    team = idx["pteam"].get((g, week)) or next((idx["pteam"][(g, w)] for w in range(week, 0, -1)
                                                if (g, w) in idx["pteam"]), TEAM_FIX.get(info["team"], info["team"]))
    pw = idx["P"].get(g, {})
    wk = _sum(pw, [week])
    ssn = _sum(pw, range(1, week + 1))
    tw = _sum(idx["T"].get(team, {}), [week])
    ts = _sum(idx["T"].get(team, {}), range(1, week + 1))
    pos = info["pos"]
    prof: Dict[str, Any] = {"sid": sid, "name": info["name"], "pos": pos, "team": team, "week": week}
    if pos in ("WR", "TE", "RB"):
        t = wk["tgt"]
        prof.update({
            "tgt": int(t), "rec": int(wk["rec"]), "rec_yd": int(wk["rec_yd"]), "rec_td": int(wk["rec_td"]),
            "tgt_share": wk["tgt"] / tw["tgt"] if tw["tgt"] else 0, "air_share": wk["air"] / tw["air"] if tw["air"] > 0 else 0,
            "adot": wk["air"] / t if t else 0, "yac": wk["yac"], "yacoe": (wk["yac"] - wk["xyac"]) if wk["yac_n"] else None,
            "epa_tgt": wk["epa_tgt"] / t if t else 0, "rz_tgt": int(wk["rz_tgt"]), "ez_tgt": int(wk["ez_tgt"]),
            "depth": (int(wk["short"]), int(wk["mid"]), int(wk["deep"])),
            "side": (int(wk["side_left"]), int(wk["side_middle"]), int(wk["side_right"])),
            "ssn_tgt_share": ssn["tgt"] / ts["tgt"] if ts["tgt"] else 0,
        })
        prof["wopr"] = 1.5 * prof["tgt_share"] + 0.7 * prof["air_share"]
    if pos in ("RB", "QB"):
        c = wk["car"]
        prof.update({"car": int(c), "rush_yd": int(wk["rush_yd"]), "rush_td": int(wk["rush_td"]),
                     "ypc": wk["rush_yd"] / c if c else 0, "epa_rush": wk["epa_rush"] / c if c else 0,
                     "succ": wk["succ"] / c if c else 0, "expl": int(wk["expl"]), "rz_car": int(wk["rz_car"]),
                     "car_share": c / tw["car"] if tw["car"] else 0})
    if pos == "QB":
        a = wk["att"]
        prof.update({"db": int(wk["db"]), "att": int(a), "cmp": int(wk["cmp"]), "pass_yd": int(wk["pass_yd"]),
                     "pass_td": int(wk["pass_td"]), "int": int(wk["int"]), "sacks": int(wk["sacks"]),
                     "epa_db": wk["epa_db"] / wk["db"] if wk["db"] else 0,
                     "cpoe": wk["cpoe"] / wk["cpoe_n"] if wk["cpoe_n"] else None,
                     "qb_adot": wk["att_air"] / a if a else 0, "deep_pct": wk["deep_att"] / a if a else 0})
    # Next Gen Stats
    n = ngs("receiving", season).get((g, week)) if pos in ("WR", "TE", "RB") else None
    if n:
        prof.update({"sep": _f(n["avg_separation"]), "cushion": _f(n["avg_cushion"]),
                     "iay_share": _f(n["percent_share_of_intended_air_yards"]),
                     "ngs_yacoe": _f(n["avg_yac_above_expectation"])})
    n = ngs("passing", season).get((g, week)) if pos == "QB" else None
    if n:
        prof.update({"ttt": _f(n.get("avg_time_to_throw")), "aggr": _f(n.get("aggressiveness")),
                     "ngs_cpoe": _f(n.get("completion_percentage_above_expectation"))})
    n = ngs("rushing", season).get((g, week)) if pos in ("RB", "QB") else None
    if n:
        prof.update({"ryoe": _f(n.get("rush_yards_over_expected_per_att")),
                     "box8": _f(n.get("percent_attempts_gte_eight_defenders"))})
    prof["snap"] = snaps(season).get((info.get("pfr", ""), week))
    pr = pfr_rec(season).get((info.get("pfr", ""), week))
    if pr:
        prof["drops"] = int(_f(pr.get("receiving_drop")))
        prof["btk"] = int(_f(pr.get("receiving_broken_tackles"))) + int(_f(pr.get("rushing_broken_tackles")))
    return prof


def _pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _pct_raw(x: Optional[float]) -> str:  # already in percent units (NGS / snaps)
    return "—" if x is None else f"{x:.0f}%"


def route_map(p: Dict[str, Any]) -> str:
    s, m, d = p.get("depth", (0, 0, 0))
    l, mi, r = p.get("side", (0, 0, 0))
    tot = s + m + d
    if not tot:
        return ""
    lr = l + mi + r or 1
    return (f"🗺️ Mapa de alvos (proxy de rotas): curto {s} · médio {m} · profundo {d} | "
            f"esq {l*100//lr}% · meio {mi*100//lr}% · dir {r*100//lr}%")


def fmt_profile(p: Dict[str, Any], pts: Optional[float] = None, proj: Optional[float] = None) -> List[str]:
    """3–4 dense lines with real numbers."""
    L: List[str] = []
    head = f"*{p['name']}* ({p['pos']}-{p['team']})"
    if pts is not None:
        head += f" — *{pts:.1f} pts*" + (f" (proj {proj:.1f})" if proj else "")
    if p.get("snap") is not None:
        head += f" | snaps {_pct_raw(p['snap'])}"
    L.append(head)
    pos = p["pos"]
    if pos == "QB":
        cp = p.get("ngs_cpoe") if p.get("ngs_cpoe") is not None else p.get("cpoe")
        L.append(f"• {p['cmp']}/{p['att']}, {p['pass_yd']} jds, {p['pass_td']} TD, {p['int']} INT, {p['sacks']} sacks | "
                 f"EPA/dropback {p['epa_db']:+.2f} | CPOE {cp:+.1f}" if cp is not None else
                 f"• {p['cmp']}/{p['att']}, {p['pass_yd']} jds, {p['pass_td']} TD, {p['int']} INT | EPA/db {p['epa_db']:+.2f}")
        extra = f"• aDOT {p['qb_adot']:.1f} | passes 20+ jds {_pct(p['deep_pct'])}"
        if p.get("ttt"):
            extra += f" | tempo p/ lançar {p['ttt']:.2f}s | agressividade {p['aggr']:.0f}%"
        if p.get("car"):
            extra += f" | 🏃 {p['car']}-{p['rush_yd']}-{p['rush_td']}"
        L.append(extra)
    if pos in ("WR", "TE", "RB") and (p.get("tgt") or pos != "RB"):
        L.append(f"• {p['rec']}/{p['tgt']} rec, {p['rec_yd']} jds, {p['rec_td']} TD | target share {_pct(p['tgt_share'])} | "
                 f"air yards share {_pct(p['air_share'])} | WOPR {p['wopr']:.2f}")
        adv = f"• aDOT {p['adot']:.1f}"
        if p.get("sep"):
            adv += f" | separação {p['sep']:.1f} jd (cushion {p['cushion']:.1f})"
        if p.get("yacoe") is not None:
            adv += f" | YAC {p['yac']:.0f} ({p['yacoe']:+.0f} vs esperado)"
        adv += f" | EPA/alvo {p['epa_tgt']:+.2f}"
        if p.get("rz_tgt"):
            adv += f" | RZ {p['rz_tgt']} (end zone {p['ez_tgt']})"
        if p.get("drops"):
            adv += f" | drops {p['drops']}"
        L.append(adv)
        rm = route_map(p)
        if rm:
            L.append(rm)
    if pos == "RB":
        run = (f"• 🏃 {p['car']} car ({_pct(p['car_share'])} do time), {p['rush_yd']} jds ({p['ypc']:.1f}/car), "
               f"{p['rush_td']} TD | EPA/car {p['epa_rush']:+.2f} | sucesso {_pct(p['succ'])} | explosivas {p['expl']}")
        if p.get("ryoe") is not None:
            run += f" | RYOE {p['ryoe']:+.2f}/car"
        if p.get("rz_car"):
            run += f" | RZ {p['rz_car']}"
        L.insert(1, run)
    return L


# ------------------------------------------------------------------ Jev + commentary
ANGLES = {
    "volume": "dominant target or carry volume / opportunity share drove the result",
    "big_play": "one or two explosive long plays drove the result",
    "red_zone": "touchdowns / red-zone and end-zone usage drove the result",
    "efficiency": "outstanding efficiency per play (EPA, YAC over expected, separation, CPOE) on normal volume",
    "deep_threat": "downfield usage: high aDOT and share of air yards",
    "bust": "a disappointing game well below projection, or empty volume without production",
    "dual_threat": "production came from both rushing and passing/receiving",
}
ANGLE_PT = {"volume": "📦 Volume", "big_play": "💥 Big play", "red_zone": "🎯 Red zone", "efficiency": "⚙️ Eficiência",
            "deep_threat": "🚀 Ameaça profunda", "bust": "🧊 Decepção", "dual_threat": "🔀 Dupla ameaça"}


def jev_angles(items: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """One Jev request: storyline angle per player (Choice fan-out)."""
    try:
        import jev
    except Exception:
        return {}
    state = {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()
                 if kk not in ("sid",)} for k, v in items.items()}
    qs = {k: {"type": "choice", "criteria": ANGLES,
              "instructions": f"For the fantasy-football week of `{k}` (stats in state), which storyline best explains "
                              f"the fantasy result? pts = PPR points, proj = projection."} for k in items}
    ans = jev.decide(state, qs, cache=True)
    return {k: jev.choice(ans, k)[0] for k in items if ans and jev.choice(ans, k)[0]}


def llm_one_liners(facts: Dict[str, str], task: str) -> Dict[str, str]:
    """LLM (Gemini, OpenRouter fallback) writes one acid PT-BR line per key, from facts only. {} on failure."""
    if not facts:
        return {}
    prompt = (f"Você é o {league_ctx.current().persona_name}, analista ácido de fantasy football numa liga "
              f"{league_ctx.current().profile.league_type} de veteranos que já "
              f"sabem ler estatística. {task} Para CADA chave do JSON abaixo escreva UMA frase (máx 25 palavras) em "
              "PT-BR que INTERPRETE os dados — o que o número revela (uso, sustentabilidade, sorte vs habilidade, "
              "risco/compra/venda) — com uma alfinetada no dono do time. NÃO repita a linha de estatística; cite no "
              "máximo 1 número. Não invente fatos fora do JSON. Responda APENAS um JSON {chave: frase}.\n\n"
              + json.dumps(facts, ensure_ascii=False))
    try:
        import llm
        txt = llm.chat(prompt, max_tokens=1500, timeout=60, json_mode=True)
        txt = txt[txt.find("{"): txt.rfind("}") + 1]
        out = json.loads(txt)
        return {k: str(v).strip() for k, v in out.items() if k in facts}
    except Exception as exc:
        print(f"[data_lab] comment LLM failed: {exc}", file=sys.stderr)
        return {}


# ------------------------------------------------------------------ SLOT 1: Raio-X da Rodada
def raio_x(week: Optional[int] = None, comments: bool = True) -> str:
    season = _season()
    week = week or last_complete_week(season)
    ctx = league_context(season, week)
    rows = []
    for t in ctx["teams"]:
        best = None
        for sid in t["starters"]:
            info = sleeper_map().get(sid)
            if not info or info["pos"] not in SKILL:
                continue
            pts = _f((ctx["stats"].get(sid) or {}).get("pts_ppr"))
            if best is None or pts > best[1]:
                best = (sid, pts)
        if not best:
            continue
        prof = profile(best[0], season, week)
        if not prof:
            continue
        proj = _f((ctx["proj"].get(best[0]) or {}).get("pts_ppr")) or None
        rows.append((t, prof, best[1], proj))
    # awards across ALL league starters
    all_p = []
    for t in ctx["teams"]:
        for sid in t["starters"]:
            p = profile(sid, season, week)
            if p:
                all_p.append((t, p))
    angles = jev_angles({f"{p['name']}": {**{k: v for k, v in p.items() if not isinstance(v, tuple)},
                                          "pts": pts, "proj": proj} for t, p, pts, proj in rows})
    facts = {t["team"]: f"{p['name']} {p['pos']} {pts:.1f} pts (proj {proj or 0:.1f}); "
                        + "; ".join(fmt_profile(p)[1:]).replace("*", "") for t, p, pts, proj in rows}
    lines_by_team = llm_one_liners(facts, "Comente o destaque de cada time da liga nesta rodada.") if comments else {}
    out = [f"🔬 *DATA LAB — RAIO-X DA RODADA {week}*",
           f"_O titular que mais pontuou em cada um dos {len(rows)} times, dissecado com dados reais (nflverse / Next Gen Stats)._",
           ""]
    for t, p, pts, proj in sorted(rows, key=lambda x: -x[2]):
        ang = ANGLE_PT.get(angles.get(p["name"], ""), "")
        out.append(f"🏈 *{t['team']}* ({t['manager']}){' — ' + ang if ang else ''}")
        out += fmt_profile(p, pts, proj)
        if lines_by_team.get(t["team"]):
            out.append(f"🎙️ _{lines_by_team[t['team']]}_")
        out.append("")
    # awards
    def top(key, pos=None, rev=True, fmt="{:.2f}", min_vol=None):
        cands = [(t, p) for t, p in all_p if p.get(key) is not None and (not pos or p["pos"] in pos)
                 and (not min_vol or p.get(min_vol[0], 0) >= min_vol[1])]
        if not cands:
            return None
        t, p = sorted(cands, key=lambda x: x[1][key], reverse=rev)[0]
        return f"{p['name']} ({t['team']}) {fmt.format(p[key])}"
    awards = [("🚀 Maior aDOT (mín. 4 alvos)", top("adot", {"WR", "TE"}, fmt="{:.1f} jds", min_vol=("tgt", 4))),
              ("🧲 Mais separação (NGS)", top("sep", {"WR", "TE"}, fmt="{:.1f} jd")),
              ("📦 Maior target share", top("tgt_share", {"WR", "TE", "RB"}, fmt="{:.0%}")),
              ("⚙️ Maior EPA/dropback (QB)", top("epa_db", {"QB"}, fmt="{:+.2f}", min_vol=("db", 15))),
              ("🏃 Mais RYOE/carregada (mín. 8)", top("ryoe", {"RB"}, fmt="{:+.2f}", min_vol=("car", 8))),
              ("🍗 YAC acima do esperado", top("yacoe", {"WR", "TE", "RB"}, fmt="{:+.0f} jds"))]
    out.append("🏆 *PRÊMIOS DATA LAB (entre todos os titulares da liga)*:")
    out += [f"• {a}: {v}" for a, v in awards if v]
    out.append("\n_Rotas = proxy pelo mapa de alvos (profundidade × lado). Fontes: nflverse PBP, NFL Next Gen Stats, Sleeper._")
    return "\n".join(out)


# ------------------------------------------------------------------ SLOT 2: Tendências de uso
def _usage(g: str, team: str, pos: str, w: int, idx) -> Optional[float]:
    pw = idx["P"].get(g, {}).get(w)
    tw = idx["T"].get(team, {}).get(w)
    if not tw:
        return None
    pw = pw or {}
    if pos in ("WR", "TE"):
        return pw.get("tgt", 0) / tw["tgt"] if tw.get("tgt") else None
    if pos == "RB":
        opp = pw.get("car", 0) + pw.get("tgt", 0)
        den = tw.get("car", 0) + tw.get("tgt", 0)
        return opp / den if den else None
    return None


def tendencias(week: Optional[int] = None) -> str:
    season = _season()
    week = week or last_complete_week(season)
    if week < 3:
        return ""
    idx = build_index(season)
    ctx = league_context(season, week)
    out = [f"📈 *DATA LAB — TENDÊNCIAS DE USO (Semana {week} vs média das 2 anteriores)*",
           "_Volume antecede ponto: share de alvos (WR/TE) e de oportunidades (RB = carregadas + alvos)._", ""]
    for t in sorted(ctx["teams"], key=lambda x: x["team"]):
        deltas = []
        for sid in t["players"]:
            info = sleeper_map().get(sid)
            if not info or info["pos"] not in ("WR", "TE", "RB") or not info["gsis"]:
                continue
            gs = info["gsis"]
            tm = lambda w: idx["pteam"].get((gs, w)) or TEAM_FIX.get(info["team"], info["team"])  # noqa: E731
            played = lambda w: (gs, w) in idx["pteam"] or (info.get("pfr"), w) in snaps(season)  # noqa: E731
            if not played(week):
                continue  # did not play this week (injury/inactive/bye) → not a usage trend
            now = _usage(gs, tm(week), info["pos"], week, idx)
            prev = [x for x in (_usage(gs, tm(w), info["pos"], w, idx) for w in (week - 1, week - 2) if played(w))
                    if x is not None]
            if now is None or not prev:
                continue
            base = sum(prev) / len(prev)
            if max(now, base) < 0.08:  # ignore irrelevant depth players
                continue
            sn = snaps(season)
            s_now = sn.get((info.get("pfr", ""), week))
            s_prev = [x for x in (sn.get((info.get("pfr", ""), w)) for w in (week - 1, week - 2)) if x is not None]
            deltas.append((now - base, info, now, base, s_now, sum(s_prev) / len(s_prev) if s_prev else None))
        if not deltas:
            continue
        deltas.sort(key=lambda x: x[0])
        up, down = deltas[-1], deltas[0]
        out.append(f"🏈 *{t['team']}* ({t['manager']})")
        for tag, d in (("⬆️", up), ("⬇️", down)):
            if tag == "⬇️" and d is up:
                continue
            snap = f" | snaps {d[5]:.0f}%→{d[4]:.0f}%" if d[4] is not None and d[5] is not None else ""
            out.append(f"{tag} {d[1]['name']} ({d[1]['pos']}-{d[1]['team']}): share {d[3]*100:.0f}% → {d[2]*100:.0f}% "
                       f"({d[0]*100:+.0f} p.p.){snap}")
        out.append("")
    out.append("_Subiu share + snaps = compra antes do mercado reagir. Caiu share com snaps estáveis = sinal amarelo._")
    return "\n".join(out)


# ------------------------------------------------------------------ SLOT 3: Matchup Lab
def _espn_opponents(week: int, season: str) -> Dict[str, str]:
    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?week={week}&season={season}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    fix = TEAM_FIX
    out = {}
    for e in data.get("events", []):
        cs = e.get("competitions", [{}])[0].get("competitors", [])
        if len(cs) == 2:
            a, b = (fix.get(c["team"]["abbreviation"], c["team"]["abbreviation"]) for c in cs)
            out[a], out[b] = b, a
    return out


def defense_profile(season: str, upto: int) -> Dict[str, Dict[str, Any]]:
    """Per NFL defense: PPR allowed per game by position + EPA/play allowed (pass, rush) + ranks."""
    from sleeper_client import SleeperClient
    s = SleeperClient(_lid())
    idx = build_index(season)
    smap = sleeper_map()
    allowed: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    games: Dict[str, set] = defaultdict(set)
    for w in range(1, upto + 1):
        st = s._get(f"stats/nfl/regular/{season}/{w}") or {}
        for sid, v in st.items():
            info = smap.get(sid)
            if not info or info["pos"] not in SKILL or not isinstance(v, dict) or not info["gsis"]:
                continue
            tm = idx["pteam"].get((info["gsis"], w))
            d = idx["opp"].get((tm, w)) if tm else None
            if not d:
                continue
            allowed[d][info["pos"]] += _f(v.get("pts_ppr"))
            games[d].add(w)
    prof = {}
    for d in allowed:
        n = len(games[d]) or 1
        dd = _sum(idx["D"].get(d, {}), range(1, upto + 1))
        prof[d] = {**{f"ppr_{p}": allowed[d][p] / n for p in SKILL},
                   "pass_epa": dd["pass_epa"] / dd["pass_n"] if dd["pass_n"] else 0,
                   "rush_epa": dd["rush_epa"] / dd["rush_n"] if dd["rush_n"] else 0}
    for key in ["ppr_QB", "ppr_RB", "ppr_WR", "ppr_TE", "pass_epa", "rush_epa"]:
        order = sorted(prof, key=lambda x: prof[x][key], reverse=True)  # rank 1 = most generous
        for i, d in enumerate(order, 1):
            prof[d][f"rank_{key}"] = i
    return prof


def matchup_lab(week: Optional[int] = None) -> str:
    season = _season()
    done = last_complete_week(season)
    week = week or done + 1
    ctx = league_context(season, week)
    opps = _espn_opponents(week, season)
    dprof = defense_profile(season, min(done, week - 1))
    idx = build_index(season)
    smap = sleeper_map()

    def key_player(t):
        best = None
        for sid in t["starters"]:
            info = smap.get(sid)
            if not info or info["pos"] not in SKILL or not info["gsis"]:
                continue
            from sleeper_client import SleeperClient  # noqa
            pw = idx["P"].get(info["gsis"], {})
            vol = sum((pw.get(w, {}).get("tgt", 0) + pw.get(w, {}).get("car", 0) + pw.get(w, {}).get("db", 0) * 0.3)
                      for w in range(1, done + 1))
            if best is None or vol > best[1]:
                best = (sid, vol, info)
        return best

    pairs: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for t in ctx["teams"]:
        pairs[t["matchup_id"]].append(t)
    out = [f"🎯 *DATA LAB — MATCHUP LAB (Semana {week})*",
           "_A peça de maior volume de cada titular da liga contra a defesa NFL que vai enfrentar "
           f"(PPR cedido/jogo e EPA/jogada cedido até a semana {done}; rank 1 = defesa mais generosa de 32)._", ""]
    for mid, ts in pairs.items():
        if mid is None or len(ts) != 2:
            continue
        out.append(f"⚔️ *{ts[0]['team']}* x *{ts[1]['team']}*")
        for t in ts:
            kp = key_player(t)
            if not kp:
                continue
            sid, _, info = kp
            my = next((idx["pteam"][(info["gsis"], w)] for w in range(done, 0, -1) if (info["gsis"], w) in idx["pteam"]),
                      TEAM_FIX.get(info["team"], info["team"]))
            d = opps.get(my)
            if not d or d not in dprof:
                out.append(f"• {info['name']} ({info['pos']}-{info['team']}): bye/sem dados")
                continue
            dp = dprof[d]
            side = "pass" if info["pos"] in ("QB", "WR", "TE") else "rush"
            ppr = dp[f"ppr_{info['pos']}"]
            rk = dp[f"rank_ppr_{info['pos']}"]
            verdict = "🟢 matchup favorável" if rk <= 8 else "🔴 matchup duro" if rk >= 25 else "🟡 neutro"
            out.append(f"• {info['name']} ({info['pos']}-{my}) vs *{d}*: cede {ppr:.1f} PPR/jogo a {info['pos']} "
                       f"(#{rk}) | EPA {side} cedido {dp[side + '_epa']:+.2f} (#{dp['rank_' + side + '_epa']}) → {verdict}")
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ liga tool query
def query(alvo: str, week: Optional[int] = None) -> str:
    season = _season()
    done = last_complete_week(season)
    week = min(week or done, done) if done else (week or 1)
    smap = sleeper_map()
    target = _norm(alvo)
    if not target:
        return raio_x(week, comments=False)
    ctx = league_context(season, week)
    for t in ctx["teams"]:  # team / manager
        if target in (_norm(t["team"]), _norm(t["manager"])) or (len(target) > 3 and (target in _norm(t["team"])
                                                                                      or target in _norm(t["manager"]))):
            out = [f"🔬 *Data Lab — {t['team']}* ({t['manager']}) — Semana {week}"]
            for sid in t["starters"]:
                p = profile(sid, season, week)
                if p:
                    out += fmt_profile(p, _f((ctx["stats"].get(sid) or {}).get("pts_ppr")))
            return "\n".join(out)
    names = {_norm(v["name"]): k for k, v in smap.items() if v.get("gsis")}
    m = difflib.get_close_matches(target, list(names), n=1, cutoff=0.75)
    if not m:
        return f"Não achei '{alvo}' (jogador ou time da liga)."
    sid = names[m[0]]
    out = []
    for w in (week, week - 1) if week > 1 else (week,):
        p = profile(sid, season, w)
        if p:
            out.append(f"— Semana {w} —")
            out += fmt_profile(p, _f((league_context(season, w)["stats"].get(sid) or {}).get("pts_ppr")))
    return "\n".join(out) or "Sem dados avançados para esse jogador ainda."


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "raio_x"
    wk = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
    t0 = time.time()
    print({"raio_x": lambda: raio_x(wk), "tendencias": lambda: tendencias(wk), "matchup": lambda: matchup_lab(wk),
           "query": lambda: query(" ".join(sys.argv[2:]))}[cmd]())
    print(f"[data_lab] {cmd} in {time.time() - t0:.1f}s", file=sys.stderr)
