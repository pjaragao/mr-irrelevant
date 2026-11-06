"""Lead changes ("viradas") in the week's matchups, for the live group alert (data/scripts/live_watch.py).

Every lead change is announced, as many times as it happens in a matchup:
* `leaders[key]` is the last ANNOUNCED leader (the first one seen is the baseline). It only changes when an alert
  goes out, so a flip first seen by less than `lead_min` points is announced as soon as the margin reaches it,
  instead of being swallowed;
* `lead_min` is the hysteresis against ping-pong: the new leader must be ahead by that much;
* the cooldown DEFERS an alert (it goes out once the cooldown ends, if the lead still holds), it never drops it;
* finished matchups are not filtered out: a flip on the last play is still news, and a closed score never moves.
"""
from typing import Any, Dict, List


def lead_changes(matchups: List[Dict[str, Any]], state: Dict[str, Any], season: str, week: int, now: float,
                 lead_min: float = 3.0, cooldown_s: float = 20 * 60) -> List[Dict[str, Any]]:
    """Alerts due now: [{key, leader, n, text}]. Call `commit` for each one actually delivered."""
    leaders = state.setdefault("leaders", {})
    last_at = state.setdefault("lead_alert_at", {})
    flips = state.setdefault("lead_flips", {})
    out = []
    for m in matchups or []:
        t1, t2 = m.get("team1"), m.get("team2")
        if not t1 or not t2:
            continue
        a, b = float(t1.get("points") or 0), float(t2.get("points") or 0)
        if a == b:
            continue
        key = f"{season}-{week}-{m['matchup_id']}"
        leader, other = (t1, t2) if a > b else (t2, t1)
        prev = leaders.get(key)
        if prev is None:
            leaders[key] = leader["roster_id"]
            continue
        if prev == leader["roster_id"] or abs(a - b) < lead_min:
            continue
        if key in last_at and now - last_at[key] < cooldown_s:
            continue
        n = int(flips.get(key, 0)) + 1
        head = "🔄 *Virada!*" if n == 1 else f"🔄 *Nova virada!* ({n}ª no confronto)"
        out.append({"key": key, "leader": leader["roster_id"], "n": n,
                    "text": f"{head} {leader['team_name']} passou {other['team_name']}: "
                            f"{max(a, b):.2f} x {min(a, b):.2f}"})
    return out


def commit(state: Dict[str, Any], alert: Dict[str, Any], now: float) -> None:
    state.setdefault("leaders", {})[alert["key"]] = alert["leader"]
    state.setdefault("lead_alert_at", {})[alert["key"]] = now
    state.setdefault("lead_flips", {})[alert["key"]] = alert["n"]
