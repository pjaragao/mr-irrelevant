"""Season/week and NFL kickoff times (Brasília), derived from live sources instead of hardcoded hours.

* `season_week()`: current season/week from Sleeper `state/nfl`.
* `week_games(week)`: ESPN scoreboard for the week, with kickoff in BRT and game state.
* `sunday_schedule(week)`: first Sunday kickoff (international/London games included) and the main
  13:00 ET slate, with a US-DST-aware fallback when ESPN is unreachable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import league_ctx

BRT = ZoneInfo("America/Sao_Paulo")   # reference zone of the weekly NFL calendar (US date of each game)
ET = ZoneInfo("America/New_York")


def local_tz() -> ZoneInfo:
    """Time zone the current league's messages are written in (league.yaml `timezone`)."""
    return ZoneInfo(league_ctx.current().timezone)
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
_memo: Dict[str, Tuple[float, Any]] = {}
ESPN_TO_SLEEPER = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR"}  # team abbreviations differ between ESPN and Sleeper


def _cached(key: str, ttl: float, fn):
    hit = _memo.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _memo[key] = (time.monotonic(), val)
    return val


def season_week() -> Tuple[str, int]:
    from sleeper_client import SleeperClient
    st = SleeperClient().get_nfl_state()
    return str(st.get("season") or datetime.now(BRT).year), int(st.get("week") or 1)


def week_games(week: int, season: Optional[str] = None) -> List[Dict[str, Any]]:
    """[{name, short, home, away, kickoff (BRT datetime), state: pre|in|post, detail}] — [] on failure."""
    season = season or season_week()[0]

    def _fetch():
        url = f"{ESPN}?week={week}&season={season}&seasontype=2"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        data = json.loads(urllib.request.urlopen(req, timeout=12).read())
        out = []
        for ev in data.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            teams = {c.get("homeAway"): ESPN_TO_SLEEPER.get((c.get("team") or {}).get("abbreviation", ""),
                                                            (c.get("team") or {}).get("abbreviation", ""))
                     for c in comp.get("competitors", [])}
            st = (comp.get("status") or {}).get("type") or {}
            try:
                ko = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(BRT)
            except Exception:
                ko = None
            out.append({"name": ev.get("name", ""), "short": ev.get("shortName", ""),
                        "home": teams.get("home", ""), "away": teams.get("away", ""),
                        "kickoff": ko, "state": st.get("state", "pre"), "detail": st.get("shortDetail", "")})
        return out

    try:
        return _cached(f"games:{season}:{week}", 300, _fetch)
    except Exception:
        return []


def _us_dst(day: date) -> bool:
    """US daylight saving: 2nd Sunday of March → 1st Sunday of November."""
    mar = date(day.year, 3, 8)
    start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = date(day.year, 11, 1)
    end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return start <= day < end


def main_slate_fallback(day: date) -> datetime:
    """13:00 ET in BRT for that Sunday (Brazil has no DST): 14:00 under US DST, 15:00 otherwise."""
    return datetime(day.year, day.month, day.day, 14 if _us_dst(day) else 15, 0, tzinfo=BRT)


def next_sunday(ref: Optional[datetime] = None) -> date:
    ref = ref or datetime.now(BRT)
    return (ref + timedelta(days=(6 - ref.weekday()) % 7)).date()


def sunday_schedule(week: int, season: Optional[str] = None, day: Optional[date] = None) -> Dict[str, Any]:
    """Kickoff plan for the Sunday of `week`.

    Returns first_kickoff (earliest Sunday game, e.g. London 10:30), main_kickoff (13:00 ET slate),
    early_games (Sunday games before the main slate) and from_espn (False = DST fallback).
    """
    games = [g for g in week_games(week, season) if g["kickoff"] and g["kickoff"].weekday() == 6]
    day = day or (games[0]["kickoff"].date() if games else next_sunday())
    fallback = main_slate_fallback(day)
    if not games:
        return {"first_kickoff": fallback, "main_kickoff": fallback, "early_games": [], "from_espn": False}
    kicks = sorted({g["kickoff"] for g in games})
    # Main slate = 13:00 ET; anything earlier is an international/early game.
    main = next((k for k in kicks if k.astimezone(ET).hour >= 13), kicks[0])
    early = sorted((g for g in games if g["kickoff"] < main), key=lambda g: g["kickoff"])
    return {"first_kickoff": kicks[0], "main_kickoff": main, "early_games": early, "from_espn": True}


def hhmm(dt: datetime) -> str:
    return dt.astimezone(local_tz()).strftime("%H:%M")


def games_by_team(week: int, season: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Sleeper team abbreviation -> its game this week (teams missing = bye)."""
    out: Dict[str, Dict[str, Any]] = {}
    for g in week_games(week, season):
        out[g["home"]] = g
        out[g["away"]] = g
    return out


def sunday_waves(week: int, season: Optional[str] = None) -> Dict[str, Any]:
    """Sunday games grouped by slate, using the New York kickoff hour (valid every week, with or without DST):
    early < 13h ET (international) · wave1 13h–15h59 ET · wave2 16h–19h59 ET · snf ≥ 20h ET.
    Each wave: {"games": [...], "first": dt, "last": dt}. Missing wave = None. ESPN down → fallback kickoffs
    (13:00 / 16:05 / 20:20 ET on the next Sunday) with no games, so callers can still schedule."""
    games = [g for g in week_games(week, season) if g["kickoff"] and g["kickoff"].astimezone(ET).weekday() == 6]
    out: Dict[str, Any] = {"early": None, "wave1": None, "wave2": None, "snf": None, "from_espn": bool(games)}
    if not games:
        day = next_sunday()
        for name, (h, m) in (("wave1", (13, 0)), ("wave2", (16, 5)), ("snf", (20, 20))):
            ko = datetime(day.year, day.month, day.day, h, m, tzinfo=ET).astimezone(BRT)
            out[name] = {"games": [], "first": ko, "last": ko}
        return out
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for g in games:
        h = g["kickoff"].astimezone(ET).hour
        name = "early" if h < 13 else "wave1" if h < 16 else "wave2" if h < 20 else "snf"
        buckets.setdefault(name, []).append(g)
    for name, gs in buckets.items():
        gs.sort(key=lambda g: g["kickoff"])
        out[name] = {"games": gs, "first": gs[0]["kickoff"], "last": gs[-1]["kickoff"]}
    return out


def all_final(games: List[Dict[str, Any]]) -> bool:
    return bool(games) and all(g.get("state") == "post" for g in games)


def game_label(g: Optional[Dict[str, Any]]) -> str:
    """'jogando agora' / 'encerrado' / 'SNF 21:20' / 'MNF seg 21:15' / 'dom 17:25' for a player's game."""
    if not g:
        return "bye"
    if g["state"] == "in":
        return "jogando agora"
    if g["state"] == "post":
        return "encerrado"
    ko = g["kickoff"]
    et = ko.astimezone(ET)
    if et.weekday() == 6 and et.hour >= 20:
        return f"SNF {hhmm(ko)}"
    if et.weekday() == 0:
        return f"MNF seg {hhmm(ko)}"
    days = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    return f"{days[ko.astimezone(local_tz()).weekday()]} {hhmm(ko)}"
