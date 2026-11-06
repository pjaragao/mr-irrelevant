"""SQLite index of the league history (trades, games, drafts, waivers, players, profiles).

Why: the knowledge base used to be Markdown/JSON blobs that the LLM had to read and count by hand. Here the
sources stay where they are (versioned, written by the crons) and are loaded into `knowledge_base/league.db`,
a *rebuildable* index that every reader queries with SQL.

* Sources (tracked): trades_history.json, matchups_history.json, franchises_history.json, draft_history.json,
  exact_draft_slots.json, seasons_history.json, waivers_market_classified.json, players_registry.json,
  MANAGERS_PSYCHOLOGICAL_PROFILES.md.
* `connect()` rebuilds the DB when any source changed (size/mtime fingerprint, a few `stat` calls) — under a
  file lock, into a temp file, swapped atomically — so there is no hook to forget in the producers.
* Readers get a read-only connection; the DB can always be deleted (`python3 kb_db.py rebuild`).
* Week 0 = offseason: trades/waivers before the NFL kickoff are week 0 in the index (Sleeper calls it week 1).
* Trades are the only source that is *written* (append_trade): trades_history.json is their source of truth.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

import league_ctx

KB_DIR: Optional[str] = None          # override (tests/tools); None = the current league's knowledge base
TRADES_FILE: Optional[str] = None

logger = logging.getLogger("kb_db")
SCHEMA_VERSION = 6

# (file name, searched in knowledge_base/ first, then league/)
SOURCES = ("trades_history.json", "matchups_history.json", "brackets_history.json", "franchises_history.json",
           "draft_history.json", "exact_draft_slots.json", "seasons_history.json", "waivers_market_classified.json",
           "players_registry.json", "player_points.json", "MANAGERS_PSYCHOLOGICAL_PROFILES.md")

# Names whose trades weigh as "blockbusters" (kept from the old Markdown-based scorer).
STAR_PLAYERS = ("mahomes", "lamar", "josh allen", "jalen hurts", "burrow", "mccaffrey", "bijan", "gibbs",
                "jefferson", "ja'marr chase", "ceedee lamb", "puka", "amon-ra", "nabers", "bowers", "brock bowers",
                "mcbride", "jeanty", "saquon", "barkley", "jayden daniels", "drake maye", "stroud", "kelce")

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE franchises (roster_id INTEGER PRIMARY KEY, manager TEXT, real_name TEXT, current_team TEXT);
CREATE TABLE manager_names (name_lc TEXT PRIMARY KEY, roster_id INTEGER);
CREATE TABLE franchise_seasons (roster_id INTEGER, season TEXT, team_name TEXT, wins INTEGER, losses INTEGER,
    ties INTEGER, points_for REAL, PRIMARY KEY (roster_id, season));
CREATE TABLE trades (seq INTEGER PRIMARY KEY, tx_id TEXT, date TEXT, season TEXT, week INTEGER,
    n_players INTEGER, n_picks INTEGER, faab_total INTEGER, impact REAL, blockbuster INTEGER);
CREATE TABLE trade_sides (trade_seq INTEGER, roster_id INTEGER, manager TEXT, team TEXT,
    PRIMARY KEY (trade_seq, roster_id));
CREATE TABLE trade_assets (id INTEGER PRIMARY KEY, trade_seq INTEGER, roster_id INTEGER, kind TEXT, label TEXT,
    player_id TEXT, player_name TEXT, pos TEXT, nfl_team TEXT, pick_round INTEGER, pick_year TEXT, pick_via TEXT,
    pick_overall INTEGER, pick_status TEXT, used_no INTEGER, used_player TEXT, used_by TEXT, faab INTEGER);
CREATE INDEX ix_assets_trade ON trade_assets (trade_seq);
CREATE INDEX ix_assets_roster ON trade_assets (roster_id, kind);
CREATE TABLE games (season TEXT, week INTEGER, is_playoff INTEGER, phase TEXT, roster_id INTEGER, opp_roster_id INTEGER,
    manager TEXT, team TEXT, opp_manager TEXT, opp_team TEXT, pf REAL, pa REAL, result TEXT,
    bracket TEXT, stage TEXT, PRIMARY KEY (season, week, roster_id, opp_roster_id));
CREATE INDEX ix_games_roster ON games (roster_id, season);
CREATE TABLE draft_picks (season TEXT, pick_no INTEGER, round INTEGER, slot INTEGER, roster_id INTEGER,
    orig_roster_id INTEGER, manager TEXT, team TEXT, via_trade INTEGER, player_id TEXT, player_name TEXT,
    pos TEXT, nfl_team TEXT, draft_name TEXT, PRIMARY KEY (season, pick_no));
CREATE INDEX ix_picks_roster ON draft_picks (roster_id, season);
CREATE TABLE waiver_moves (tx_id TEXT PRIMARY KEY, season TEXT, week INTEGER, date TEXT, ts REAL, type TEXT,
    status TEXT, roster_id INTEGER, manager TEXT, team TEXT, bid INTEGER, tier INTEGER, tier_badge TEXT, reason TEXT,
    add_id TEXT, add_name TEXT, add_pos TEXT, add_team TEXT, add_label TEXT, drop_name TEXT, drop_label TEXT);
CREATE INDEX ix_waivers_roster ON waiver_moves (roster_id, season);
CREATE TABLE players (player_id TEXT PRIMARY KEY, name TEXT, pos TEXT, nfl_team TEXT, status_nfl TEXT,
    origin_type TEXT, origin_season TEXT, origin_round INTEGER, origin_pick INTEGER, origin_roster_id INTEGER,
    owner_roster_id INTEGER, owner_role TEXT);
CREATE TABLE profiles (roster_id INTEGER PRIMARY KEY, title TEXT, body TEXT);
CREATE TABLE player_weeks (season TEXT, week INTEGER, roster_id INTEGER, player_id TEXT, name TEXT, pos TEXT,
    nfl_team TEXT, pts REAL, started INTEGER, bye INTEGER, PRIMARY KEY (season, week, roster_id, player_id));
CREATE INDEX ix_pw_player ON player_weeks (player_id, season);
"""


# --------------------------------------------------------------------------- paths / freshness
def kb_dir() -> str:
    return KB_DIR or league_ctx.current().kb_dir


def trades_file() -> str:
    return TRADES_FILE or os.path.join(kb_dir(), "trades_history.json")


def db_path() -> str:
    ctx = league_ctx.current()
    if ctx.legacy_layout and os.environ.get("LEAGUE_DB"):   # dev/test override; never applied to a second league
        return os.environ["LEAGUE_DB"]
    return os.path.join(kb_dir(), "league.db")


def _source_path(name: str) -> Optional[str]:
    if KB_DIR:                                              # explicit directory (tests): only that one
        p = os.path.join(KB_DIR, name)
        return p if os.path.isfile(p) else None
    p = league_ctx.current().source_path(name)
    return p if os.path.isfile(p) else None


def _fingerprint() -> str:
    parts = [f"v{SCHEMA_VERSION}"]
    for name in SOURCES:
        p = _source_path(name)
        if p:
            st = os.stat(p)
            parts.append(f"{name}:{st.st_size}:{st.st_mtime_ns}")
        else:
            parts.append(f"{name}:-")
    return "|".join(parts)


def _stored_fingerprint(path: str) -> Optional[str]:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='fingerprint'").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def ensure_fresh() -> str:
    """Rebuild the DB if missing or stale. Returns its path."""
    path = db_path()
    fp = _fingerprint()
    if os.path.isfile(path) and _stored_fingerprint(path) == fp:
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # another process may have rebuilt it while we waited for the lock
        if os.path.isfile(path) and _stored_fingerprint(path) == _fingerprint():
            return path
        try:
            rebuild(path)
        except Exception:
            if not os.path.isfile(path):
                raise
            logger.exception("kb_db: rebuild failed; serving the previous index")
    return path


def connect(fresh: bool = True) -> sqlite3.Connection:
    """Read-only connection (rebuilds the index first when a source changed)."""
    path = ensure_fresh() if fresh else db_path()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# --------------------------------------------------------------------------- source readers
def _load(name: str) -> Any:
    p = _source_path(name)
    if not p:
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:  # corrupted/partial write: skip this table instead of breaking every query
        logger.error("kb_db: cannot read %s: %s", name, exc)
        return None


def _read_text(name: str) -> str:
    p = _source_path(name)
    if not p:
        return ""
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _name_map(franchises: Dict[str, Any]) -> Dict[str, int]:
    """Every spelling of a manager/user name -> roster_id."""
    out: Dict[str, int] = {}
    try:
        for rid, p in league_ctx.current().people.entries.items():
            out[str(p.get("username") or "").lower()] = int(rid)
            out[str(p.get("name") or "").lower()] = int(rid)
    except Exception as exc:  # the people registry is an enrichment: the franchise history below still names everyone
        logger.warning("kb_db: people registry unavailable (%s)", exc)
    for k, f in (franchises or {}).items():
        rid = int(f.get("roster_id") or k)
        out[str(f.get("canonical_manager", "")).lower()] = rid
        for h in f.get("managers_history", []):
            out[str(h.get("manager_name", "")).lower()] = rid
            out[str(h.get("sleeper_user", "")).lower()] = rid
        for s in (f.get("seasons") or {}).values():
            out[str(s.get("sleeper_display_name", "")).lower()] = rid
    out.pop("", None)
    return out


# --------------------------------------------------------------------------- season phase
OFFSEASON = 0   # index week of everything that happens before the NFL kickoff (the Sleeper files it under "week 1")


def season_kickoff(season: Any) -> Optional[date]:
    """First NFL game day of `season`: league.yaml `rules.season_start: {"2026": "2026-09-09"}` wins, else the Thursday
    after Labor Day (right for 2022-2025; set the override when the league year opens on another day)."""
    try:
        yr = int(season)
    except (TypeError, ValueError):
        return None
    over = (league_ctx.current().rule("season_start") or {}).get(str(yr)) or (league_ctx.current().rule("season_start") or {}).get(yr)
    if over:
        try:
            return date.fromisoformat(str(over))
        except ValueError:
            logger.warning("rules.season_start[%s]=%r is not YYYY-MM-DD; using the default", yr, over)
    labor_day = date(yr, 9, 1)
    labor_day += timedelta(days=(7 - labor_day.weekday()) % 7)
    return labor_day + timedelta(days=3)


def index_week(season: Any, week: Any, day: Optional[str]) -> Optional[int]:
    """The week a move belongs to in the index. Sleeper files pre-season trades/waivers under week 1, which made
    "week 1" mean "everything since the draft"; before the season's kickoff they are week 0 (offseason) instead."""
    try:
        w = int(week)
    except (TypeError, ValueError):
        return week
    if w > 1 or not day:
        return w
    kick = season_kickoff(season)
    try:
        return OFFSEASON if kick and date.fromisoformat(str(day)[:10]) < kick else w
    except ValueError:
        return w


def week_label(week: Any) -> str:
    return "offseason" if week == OFFSEASON and week is not None else f"S{week}"


# --------------------------------------------------------------------------- trades file (source of truth)
def trades_from_file() -> List[Dict[str, Any]]:
    data = _load("trades_history.json") or {}
    return data.get("trades", []) if isinstance(data, dict) else []


def _write_trades_file(trades: List[Dict[str, Any]]) -> None:
    """One trade per line: small, diff-friendly, atomic."""
    tf = trades_file()
    tmp = tf + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write('{"version": 1, "trades": [\n')
        f.write(",\n".join(json.dumps(t, ensure_ascii=False, separators=(",", ":")) for t in trades))
        f.write("\n]}\n")
    os.replace(tmp, tf)


def append_trade(trade_info: Dict[str, Any]) -> bool:
    """Record a trade parsed by TransactionMonitor._parse_trade. Idempotent on transaction_id."""
    tx_id = str(trade_info.get("transaction_id") or "")
    os.makedirs(kb_dir(), exist_ok=True)
    with open(trades_file() + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        trades = trades_from_file()
        if tx_id and any(t.get("tx_id") == tx_id for t in trades):
            return False
        d, m, y = (trade_info.get("date_str") or "01/01/1970").split("/")
        sides = []
        for s in trade_info.get("sides", []):
            assets: List[Dict[str, Any]] = []
            for det in s.get("player_details_received", []):
                label = det.get("label") or ""
                m_ = re.match(r"^(.*?) \(([A-Z/?]+) - ([A-Z]{2,3})\)$", label)
                assets.append({"kind": "player", "player_id": det.get("player_id"),
                               "name": (m_.group(1) if m_ else label).strip(),
                               "pos": m_.group(2) if m_ else "?", "team": m_.group(3) if m_ else "FA"})
            for dp in s.get("picks_received", []):
                assets.append({"kind": "pick", "round": int(dp.get("round") or 1), "year": str(dp.get("season")),
                               "via": dp.get("original_owner"), "status": "future"})
            if s.get("faab_received"):
                assets.append({"kind": "faab", "amount": int(s["faab_received"])})
            sides.append({"manager": s.get("manager_name"), "team": s.get("team_name"), "assets": assets})
        trades.append({"tx_id": tx_id or None, "date": f"{y}-{m}-{d}", "season": str(trade_info.get("season")),
                       "week": int(trade_info.get("week") or 1), "sides": sides})
        _write_trades_file(trades)
    return True


# --------------------------------------------------------------------------- build
def _pos_label(name: str, pos: Optional[str], team: Optional[str]) -> str:
    return f"{name} ({pos or '?'} - {team or 'FA'})"


def _impact(assets: List[Dict[str, Any]]) -> float:
    picks = [a for a in assets if a["kind"] == "pick"]
    players = [a for a in assets if a["kind"] == "player"]
    stars = sum(1 for p in players if any(s in p["name"].lower() for s in STAR_PLAYERS))
    return round(3.0 * sum(p["round"] == 1 for p in picks) + 1.5 * sum(p["round"] == 2 for p in picks)
                 + 0.5 * sum(p["round"] > 2 for p in picks) + 3.0 * stars + 0.5 * len(players), 1)


def _load_franchises(con, franchises) -> None:
    for k, f in (franchises or {}).items():
        rid = int(f.get("roster_id") or k)
        con.execute("INSERT INTO franchises VALUES (?,?,?,?)",
                    (rid, f.get("canonical_manager"), f.get("real_name"), f.get("current_team")))
        for y, s in (f.get("seasons") or {}).items():
            con.execute("INSERT INTO franchise_seasons VALUES (?,?,?,?,?,?,?)",
                        (rid, y, s.get("team_name"), s.get("wins"), s.get("losses"), s.get("ties", 0),
                         s.get("points_for")))
    for name, rid in _name_map(franchises).items():
        con.execute("INSERT OR REPLACE INTO manager_names VALUES (?,?)", (name, rid))


def _load_trades(con, names: Dict[str, int]) -> None:
    for seq, t in enumerate(trades_from_file(), 1):
        all_assets, side_rows = [], []
        for s in t.get("sides", []):
            rid = names.get(str(s.get("manager", "")).lower())
            side_rows.append((rid, s))
            all_assets += s.get("assets", [])
        impact = _impact(all_assets)
        con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (seq, t.get("tx_id"), t.get("date"), t.get("season"), index_week(t.get("season"), t.get("week"), t.get("date")),
                     sum(a["kind"] == "player" for a in all_assets), sum(a["kind"] == "pick" for a in all_assets),
                     sum(a.get("amount", 0) for a in all_assets if a["kind"] == "faab"), impact, int(impact >= 5.0)))
        for rid, s in side_rows:
            con.execute("INSERT OR REPLACE INTO trade_sides VALUES (?,?,?,?)", (seq, rid, s.get("manager"), s.get("team")))
            for a in s.get("assets", []):
                k = a["kind"]
                if k == "player":
                    con.execute("INSERT INTO trade_assets (trade_seq,roster_id,kind,label,player_id,player_name,pos,nfl_team)"
                                " VALUES (?,?,?,?,?,?,?,?)",
                                (seq, rid, k, _pos_label(a["name"], a.get("pos"), a.get("team")), a.get("player_id"),
                                 a["name"], a.get("pos"), a.get("team")))
                elif k == "pick":
                    label = f"Pick R{a['round']} {a['year']}" + (f" via {a['via']}" if a.get("via") else "")
                    con.execute("INSERT INTO trade_assets (trade_seq,roster_id,kind,label,pick_round,pick_year,pick_via,"
                                "pick_overall,pick_status,used_no,used_player,used_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (seq, rid, k, label, a["round"], a["year"], a.get("via"), a.get("overall"),
                                 a.get("status"), a.get("used_no"), a.get("used_player"), a.get("used_by")))
                else:
                    con.execute("INSERT INTO trade_assets (trade_seq,roster_id,kind,label,faab) VALUES (?,?,?,?,?)",
                                (seq, rid, "faab", f"${a['amount']} FAAB", a["amount"]))


def _load_games(con, matchups, names) -> None:
    """Two rows per matchup. ``is_playoff`` = any post-season week (both brackets); ``bracket``/``stage`` come from the
    official Sleeper brackets: regular | winners (Wild Card..Final, 3º, 5º) | losers (Toilet Bowl)."""
    import h2h_builder
    stages = h2h_builder.StageIndex(_load("brackets_history.json") or {})
    for m in matchups or []:
        pa, pb = float(m.get("points_a") or 0), float(m.get("points_b") or 0)
        ra, rb = m.get("roster_a"), m.get("roster_b")
        if not ra or not rb:
            continue
        played = pa > 0 or pb > 0
        res_a = ("W" if pa > pb else "L" if pa < pb else "T") if played else None
        res_b = {"W": "L", "L": "W", "T": "T", None: None}[res_a]
        po = bool(m.get("is_playoff"))
        st = stages.classify(m.get("season"), int(m.get("week")), po, ra, rb)
        base = (str(m.get("season")), int(m.get("week")), int(po), h2h_builder.phase_name(st["bracket"]))
        for rid, opp, mg, tm, omg, otm, pf, pa_, res in (
                (ra, rb, m.get("manager_a"), m.get("team_a"), m.get("manager_b"), m.get("team_b"), pa, pb, res_a),
                (rb, ra, m.get("manager_b"), m.get("team_b"), m.get("manager_a"), m.get("team_a"), pb, pa, res_b)):
            con.execute("INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*base, rid, opp, mg, tm, omg, otm, pf, pa_, res, st["bracket"], st["stage"]))


def _load_draft(con, drafts, slots, seasons, names) -> None:
    aliases = dict(league_ctx.current().config.get("manager_aliases") or {})
    for year, d in (drafts or {}).items():
        smap: Dict[int, Any] = {}
        for k, v in ((slots or {}).get(year) or {}).get("slot_to_roster_id", {}).items():
            try:                               # real drafts carry blank slots (auction/startup): skip them
                smap[int(k)] = v
            except (TypeError, ValueError):
                continue
        tnames = {}
        for st in ((seasons or {}).get(year) or {}).get("standings", []):
            tnames[st.get("roster_id")] = (st.get("owner_name"), st.get("team_name"))
        for p in d.get("picks", []):
            meta = p.get("metadata") or {}
            name = f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip() or f"Player {p.get('player_id')}"
            rid = p.get("roster_id")
            slot = p.get("draft_slot")
            orig = smap.get(slot) if slot else None
            mgr, team = tnames.get(rid, (None, None))
            mgr = aliases.get(mgr, mgr)   # league.yaml `manager_aliases`: a handle the manager has since renamed
            con.execute("INSERT OR REPLACE INTO draft_picks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (year, p.get("pick_no"), p.get("round"), slot, rid, orig, mgr, team,
                         int(bool(orig and rid and orig != rid)), p.get("player_id"), name, meta.get("position"),
                         meta.get("team"), d.get("draft_name")))


def _load_waivers(con, data, names) -> None:
    for t in (data or {}).get("transactions", []):
        adds, drops = t.get("adds") or [], t.get("drops") or []
        a = adds[0] if adds else {}
        dr = drops[0] if drops else {}
        con.execute("INSERT OR REPLACE INTO waiver_moves VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(t.get("transaction_id")), str(t.get("season")), index_week(t.get("season"), t.get("week"), t.get("date")), t.get("date"),
                     t.get("created_ts"), t.get("type"), t.get("status"), t.get("roster_id"), t.get("manager"),
                     t.get("team_name"), t.get("bid") or 0, t.get("quality_tier"), t.get("tier_badge"),
                     t.get("quality_reason"), a.get("player_id"), a.get("name"), a.get("position"), a.get("team"),
                     a.get("label"), dr.get("name"), dr.get("label")))


def _load_players(con, reg) -> None:
    for pid, p in ((reg or {}).get("players") or {}).items():
        o = p.get("origin") or {}
        c = p.get("current_status_2026") or {}
        con.execute("INSERT OR REPLACE INTO players VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(pid), p.get("name"), p.get("position"), p.get("team_nfl"), p.get("status_nfl"),
                     o.get("type"), o.get("season"), o.get("round"), o.get("pick_no"), o.get("original_roster_id"),
                     c.get("roster_id"), c.get("role")))


def _load_profiles(con, text: str) -> None:
    """MANAGERS_PSYCHOLOGICAL_PROFILES.md: one `### N. Name` section per manager, matched to a roster through the
    league's people registry (names, nicknames and team names)."""
    registry = league_ctx.current().people
    for sec in text.split("\n### ")[1:]:
        title = sec.split("\n", 1)[0].strip()
        body = "### " + sec
        if "\n---\n" in body:
            body = body.split("\n---\n", 1)[0]
        rid = registry.resolve(re.sub(r"^\d+\.\s*", "", title))
        if rid:
            con.execute("INSERT OR REPLACE INTO profiles VALUES (?,?,?)", (rid, title, body.strip()))


def _load_player_weeks(con, data) -> None:
    import player_scores
    con.executemany("INSERT OR REPLACE INTO player_weeks VALUES (?,?,?,?,?,?,?,?,?,?)", player_scores.rows(data or {}))


def rebuild(path: Optional[str] = None) -> str:
    """Build the DB from the sources into a temp file and swap it in atomically."""
    path = path or db_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    for p in (tmp, tmp + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    fp = _fingerprint()
    franchises = _load("franchises_history.json") or {}
    names = _name_map(franchises)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        _load_franchises(con, franchises)
        _load_trades(con, names)
        _load_games(con, _load("matchups_history.json"), names)
        _load_draft(con, _load("draft_history.json"), _load("exact_draft_slots.json"), _load("seasons_history.json"), names)
        _load_waivers(con, _load("waivers_market_classified.json"), names)
        _load_players(con, _load("players_registry.json"))
        _load_player_weeks(con, _load("player_points.json"))
        _load_profiles(con, _read_text("MANAGERS_PSYCHOLOGICAL_PROFILES.md"))
        con.execute("INSERT INTO meta VALUES ('fingerprint', ?)", (fp,))
        con.commit()
    finally:
        con.close()
    os.replace(tmp, path)
    logger.info("kb_db: rebuilt %s", path)
    return path


def table_counts() -> Dict[str, int]:
    con = connect()
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("franchises", "trades", "trade_assets", "games", "draft_picks", "waiver_moves", "players", "profiles")}
    finally:
        con.close()


def export_trade_history_md(out_path: Optional[str] = None) -> str:
    """Human-readable dump of every trade (generated; the JSON is the source of truth)."""
    import kb_queries
    out_path = out_path or os.path.join(kb_dir(), "TRADE_HISTORY.md")
    con = connect()
    try:
        lines = ["# Histórico de trocas (gerado de trades_history.json — não editar)", ""]
        for t in con.execute("SELECT * FROM trades ORDER BY seq DESC"):
            lines += [f"### Troca #{t['seq']} em {t['date']} ({t['season']} - {'Offseason' if t['week'] == OFFSEASON else 'Semana ' + str(t['week'])})", ""]
            lines += kb_queries.trade_lines(con, t["seq"], detail=True) + ["", "---", ""]
    finally:
        con.close()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "rebuild":
        rebuild()
    if cmd == "export":
        print(export_trade_history_md())
    else:
        print(json.dumps(table_counts(), indent=1))
