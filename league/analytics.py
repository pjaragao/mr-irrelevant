"""Season analytics from live Sleeper data: all-play record, bench points, power ranking, playoff odds.

* all-play: each week every team "plays" all the others — record = how often its score beat the field.
* bench points: optimal (Max PF / best ball) minus real points (Sleeper `ppts` − `fpts`).
* power ranking: 50% all-play %, 30% points per game (scaled), 20% last-3-weeks form.
* playoff odds: Monte Carlo of the remaining regular-season schedule; each team's weekly score ~ Normal
  (its season mean shrunk toward the league mean, league-wide spread). Playoff = top `playoff_teams`
  by wins then points for (same order as the official standings in sleeper_client.get_standings).
"""

from __future__ import annotations

import os
import random
import statistics
from typing import Any, Dict, List, Optional, Tuple

import league_ctx
from sleeper_client import SleeperClient


def _client() -> SleeperClient:
    return SleeperClient()          # the current league's




def _week_scores(s: SleeperClient, week: int) -> Tuple[Dict[int, float], List[Tuple[int, int]]]:
    """({roster: points}, [(roster_a, roster_b)]) for one week."""
    raw = s._get(f"league/{s.league_id}/matchups/{week}") or []
    pts, by_mid = {}, {}
    for m in raw:
        rid = int(m["roster_id"])
        pts[rid] = float(m.get("points") or 0.0)
        if m.get("matchup_id") is not None:
            by_mid.setdefault(m["matchup_id"], []).append(rid)
    pairs = [tuple(v) for v in by_mid.values() if len(v) == 2]
    return pts, pairs


def completed_weeks(s: Optional[SleeperClient] = None) -> int:
    s = s or _client()
    st = s.get_nfl_state()
    league = s.get_league()
    last = int((league.get("settings") or {}).get("last_scored_leg") or 0)
    return last or max(0, int(st.get("week") or 1) - 1)


def team_names(s: SleeperClient) -> Dict[int, str]:
    users = s.get_users()
    return {int(r["roster_id"]): users.get(r.get("owner_id"), {}).get("team_name") or f"Time {r['roster_id']}"
            for r in s.get_rosters()}


def all_play(s: Optional[SleeperClient] = None, weeks: Optional[List[int]] = None) -> Dict[int, Dict[str, Any]]:
    s = s or _client()
    weeks = weeks or list(range(1, completed_weeks(s) + 1))
    out: Dict[int, Dict[str, Any]] = {}
    for w in weeks:
        pts, _ = _week_scores(s, w)
        for rid, p in pts.items():
            others = [v for r, v in pts.items() if r != rid]
            rec = out.setdefault(rid, {"w": 0, "l": 0, "t": 0, "scores": []})
            rec["w"] += sum(1 for v in others if p > v)
            rec["l"] += sum(1 for v in others if p < v)
            rec["t"] += sum(1 for v in others if p == v)
            rec["scores"].append(p)
    for rec in out.values():
        games = rec["w"] + rec["l"] + rec["t"]
        rec["pct"] = (rec["w"] + 0.5 * rec["t"]) / games if games else 0.0
    return out


def power_ranking(s: Optional[SleeperClient] = None) -> List[Dict[str, Any]]:
    s = s or _client()
    ap = all_play(s)
    if not ap:
        return []
    names = team_names(s)
    standings = {t["roster_id"]: t for t in s.get_standings()}
    rosters = {int(r["roster_id"]): r for r in s.get_rosters()}
    ppg = {rid: statistics.mean(r["scores"]) for rid, r in ap.items()}
    form = {rid: statistics.mean(r["scores"][-3:]) for rid, r in ap.items()}
    lo, hi = min(ppg.values()), max(ppg.values())
    flo, fhi = min(form.values()), max(form.values())
    rows = []
    for rid, rec in ap.items():
        st = (rosters.get(rid) or {}).get("settings") or {}
        ppts = float(f"{st.get('ppts', 0)}.{st.get('ppts_decimal', 0):02d}")
        fpts = float(f"{st.get('fpts', 0)}.{st.get('fpts_decimal', 0):02d}")
        score = (0.5 * rec["pct"]
                 + 0.3 * ((ppg[rid] - lo) / (hi - lo) if hi > lo else 0.5)
                 + 0.2 * ((form[rid] - flo) / (fhi - flo) if fhi > flo else 0.5))
        real = standings.get(rid, {})
        actual_pct = (real.get("wins", 0) + 0.5 * real.get("ties", 0)) / max(1, real.get("wins", 0) + real.get("losses", 0) + real.get("ties", 0))
        rows.append({
            "roster_id": rid, "team": names.get(rid, str(rid)), "score": round(score * 100, 1),
            "all_play": f"{rec['w']}-{rec['l']}" + (f"-{rec['t']}" if rec["t"] else ""), "all_play_pct": rec["pct"],
            "record": f"{real.get('wins', 0)}-{real.get('losses', 0)}", "ppg": round(ppg[rid], 1),
            "form": round(form[rid], 1), "bench_pts": max(0.0, round(ppts - fpts, 2)),
            "luck": round(actual_pct - rec["pct"], 3),  # >0 = won more than the scores deserved
        })
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def playoff_odds(s: Optional[SleeperClient] = None, sims: int = 5000, seed: int = 7) -> Dict[str, Any]:
    s = s or _client()
    league = s.get_league()
    settings = league.get("settings") or {}
    playoff_start = int(settings.get("playoff_week_start") or 15)
    n_playoff = int(settings.get("playoff_teams") or 6)
    done = completed_weeks(s)
    ap = all_play(s, list(range(1, done + 1)))
    if not ap:
        return {"rows": [], "weeks_left": 0}
    all_scores = [p for r in ap.values() for p in r["scores"]]
    league_mean = statistics.mean(all_scores)
    league_sd = statistics.pstdev(all_scores) or 25.0
    k = 3.0  # shrinkage: 3 "virtual" league-average games
    mu = {rid: (sum(r["scores"]) + k * league_mean) / (len(r["scores"]) + k) for rid, r in ap.items()}
    standings = {t["roster_id"]: t for t in s.get_standings()}
    schedule = [_week_scores(s, w)[1] for w in range(done + 1, playoff_start)]
    div = {rid: standings.get(rid, {}).get("division_id") for rid in mu}
    try:
        import league_ctx
        division_winners = league_ctx.current().rule("seeding", None) == "division_winners" and len(set(div.values())) > 1
    except Exception:
        division_winners = False
    rng = random.Random(seed)
    made = {rid: 0 for rid in mu}
    top2 = {rid: 0 for rid in mu}
    for _ in range(sims):
        wins = {rid: standings.get(rid, {}).get("wins", 0) for rid in mu}
        pf = {rid: standings.get(rid, {}).get("pts_for", 0.0) for rid in mu}
        for pairs in schedule:
            for a, b in pairs:
                pa, pb = rng.gauss(mu[a], league_sd), rng.gauss(mu[b], league_sd)
                pf[a] += pa
                pf[b] += pb
                wins[a if pa > pb else b] += 1
        order = sorted(mu, key=lambda r: (wins[r], pf[r]), reverse=True)
        if division_winners:       # house rule: division leaders take seeds 1..N, the rest follow by record then PF
            leaders = {max((r for r in mu if div.get(r) == d), key=lambda r: (wins[r], pf[r])) for d in set(div.values())}
            order = [r for r in order if r in leaders] + [r for r in order if r not in leaders]
        for i, rid in enumerate(order[:n_playoff]):
            made[rid] += 1
            if i < 2:
                top2[rid] += 1
    names = team_names(s)
    rows = [{"roster_id": rid, "team": names.get(rid, str(rid)),
             "record": f"{standings.get(rid, {}).get('wins', 0)}-{standings.get(rid, {}).get('losses', 0)}",
             "playoff_pct": round(100.0 * made[rid] / sims, 1), "top2_pct": round(100.0 * top2[rid] / sims, 1),
             "proj_ppg": round(mu[rid], 1)} for rid in mu]
    rows.sort(key=lambda r: (-r["playoff_pct"], -r["top2_pct"]))
    return {"rows": rows, "weeks_left": len(schedule), "sims": sims, "playoff_teams": n_playoff}
