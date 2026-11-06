"""Live NFL player database (name, team, position and official injury status).

`master_nfl_players.json` is refreshed from Sleeper `GET /players/nfl` (the endpoint Sleeper asks to call at
most a few times a day) by the league-sync cron. Every module that needs player info goes through `load()`,
which merges the historical files first and the live master last, so the live team/status always win.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request
from typing import Any, Dict, Optional

import league_ctx

logger = logging.getLogger("players_db")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MASTER_FILE = os.path.join(BASE_DIR, "master_nfl_players.json")
URL = "https://api.sleeper.app/v1/players/nfl"
UA = {"User-Agent": "MrIrrelevant/2.0 (league bot)"}

# Fields that come from the live master and must overwrite older values, even when None (healthy).
LIVE_KEYS = ("name", "pos", "team", "status", "injury_status", "injury_body_part", "injury_notes",
             "injury_start_date", "first_name", "last_name")

_cache: Dict[str, Any] = {"mtime": None, "db": None}        # the NFL-level master mtime
_league_cache: Dict[str, Dict[str, Any]] = {}              # league slug -> {"stamp": ..., "db": merged}


def _compact(p: Dict[str, Any]) -> Dict[str, Any]:
    name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    return {
        "name": name,
        "pos": p.get("position"),
        "team": p.get("team"),
        "status": p.get("status"),
        "injury_status": p.get("injury_status"),
        "injury_body_part": p.get("injury_body_part"),
        "injury_notes": p.get("injury_notes"),
        "injury_start_date": p.get("injury_start_date"),
        "first_name": p.get("first_name"),
        "last_name": p.get("last_name"),
    }


def age_hours() -> float:
    try:
        return (time.time() - os.path.getmtime(MASTER_FILE)) / 3600.0
    except OSError:
        return float("inf")


def is_live_format() -> bool:
    """True once the master has been rebuilt by `refresh()` (old snapshots had no injury fields)."""
    db = _read_master()
    sample = next(iter(db.values()), {}) if db else {}
    return "injury_status" in sample


def refresh(max_age_h: float = 6.0, force: bool = False) -> int:
    """Download /players/nfl if the local master is older than `max_age_h`. Returns players written (0 = skipped)."""
    if not force and age_hours() < max_age_h and is_live_format():
        return 0
    req = urllib.request.Request(URL, headers=UA)
    raw = json.loads(urllib.request.urlopen(req, timeout=60).read())
    out = {str(pid): _compact(p) for pid, p in raw.items() if isinstance(p, dict)}
    if len(out) < 1000:  # sanity: never replace the master with a truncated payload
        raise RuntimeError(f"/players/nfl returned only {len(out)} players")
    tmp = MASTER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, MASTER_FILE)
    _cache["mtime"] = None
    _league_cache.clear()
    logger.info("players_db: %d players refreshed from Sleeper", len(out))
    return len(out)


def _read_json(name: str, path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(path or os.path.join(BASE_DIR, name), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if isinstance(data, dict) and isinstance(data.get("players"), dict):
        data = data["players"]
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _read_master() -> Dict[str, Any]:
    return _read_json("master_nfl_players.json")


def load() -> Dict[str, Dict[str, Any]]:
    """Merged player DB: league_players (old) -> players_registry (league history) -> live master (wins).
    The master is NFL-level (shared); the two league files are the current league's, so each league keeps its own
    merged copy (keyed by slug and by the files' mtimes)."""
    ctx = league_ctx.current()
    if ctx.legacy_layout:       # league #1: the files where they always were (BASE_DIR stays patchable)
        lp = os.path.join(BASE_DIR, "league_players.json")
        reg = os.path.join(BASE_DIR, "knowledge_base", "players_registry.json")
    else:
        lp, reg = ctx.source_path("league_players.json"), ctx.source_path("players_registry.json")

    def _mt(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    stamp = (_mt(MASTER_FILE), _mt(lp), _mt(reg))
    hit = _league_cache.get(ctx.slug)
    if hit is not None and hit["stamp"] == stamp:
        return hit["db"]
    merged: Dict[str, Dict[str, Any]] = {}
    for path in (lp, reg):
        for pid, v in _read_json("", path).items():
            cur = merged.setdefault(pid, {})
            cur.update({k: val for k, val in v.items() if val is not None})
    master = _read_master()
    live = bool(master) and "injury_status" in next(iter(master.values()), {})
    for pid, v in master.items():
        cur = merged.setdefault(pid, {})
        if live:
            cur.update({k: v.get(k) for k in LIVE_KEYS if k in v})
        else:
            cur.update({k: val for k, val in v.items() if val is not None})
    _league_cache[ctx.slug] = {"stamp": stamp, "db": merged}
    _cache.update(mtime=stamp[0], db=merged)
    return merged


def get(pid: Any) -> Dict[str, Any]:
    return load().get(str(pid), {})


def injury(p: Dict[str, Any]) -> Optional[str]:
    """Official injury designation (None = healthy). `status` covers IR/PUP/Suspended rosters."""
    inj = p.get("injury_status")
    if inj:
        return inj
    st = p.get("status")
    if st in ("Injured Reserve", "IR"):
        return "IR"
    if st in ("Physically Unable to Perform", "PUP"):
        return "PUP"
    if st in ("Suspended", "Sus"):
        return "Sus"
    if st in ("Non Football Injury", "NFI"):
        return "NFI"
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    n = refresh(force="--force" in sys.argv)
    print(f"refreshed={n} age_h={age_hours():.2f} live={is_live_format()}")
