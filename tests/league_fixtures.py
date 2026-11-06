"""Synthetic Sleeper leagues + league homes for the offline tests (no network, nothing real)."""
import os
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

import league_ctx  # noqa: E402

# Shapes copied from real Sleeper payloads (GET /league/{id}, /users, /rosters).
DYNASTY_SLOTS = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "FLEX", "SUPER_FLEX"] + ["BN"] * 19
REDRAFT_SLOTS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6
IDP_SLOTS = ["QB", "RB", "WR", "WR", "TE", "REC_FLEX", "DL", "DL", "LB", "DB", "IDP_FLEX"] + ["BN"] * 5


def league_json(league_id: str, name: str, slots: List[str], teams: int, **settings: Any) -> Dict[str, Any]:
    base = {"num_teams": teams, "playoff_teams": 4, "playoff_week_start": 15, "playoff_round_type": 0,
            "waiver_type": 0, "waiver_budget": 0, "trade_deadline": 11, "taxi_slots": 0, "reserve_slots": 0,
            "draft_rounds": 4, "best_ball": 0, "type": 0, "divisions": 0}
    base.update(settings)
    return {"league_id": league_id, "name": name, "season": "2026", "status": "in_season", "sport": "nfl",
            "total_rosters": teams, "roster_positions": slots, "previous_league_id": "0", "draft_id": "d" + league_id,
            "settings": base, "scoring_settings": {"rec": 1.0, "pass_td": 4.0},
            "metadata": {"division_1": "Norte", "division_2": "Sul"}}


def users_rosters(teams: int, handles: Optional[List[str]] = None, league_id: str = "L"):
    handles = handles or [f"Manager{i}" for i in range(1, teams + 1)]
    users = [{"user_id": f"u{i}", "display_name": handles[i - 1], "metadata": {"team_name": f"Time {handles[i - 1]}"}}
             for i in range(1, teams + 1)]
    rosters = [{"roster_id": i, "owner_id": f"u{i}", "league_id": league_id, "players": [], "settings": {}}
               for i in range(1, teams + 1)]
    return users, rosters


def fetcher(league: Dict[str, Any], users: List[Dict[str, Any]], rosters: List[Dict[str, Any]], calls: Optional[list] = None):
    def fetch():
        if calls is not None:
            calls.append(1)
        return league, users, rosters
    return fetch


def make_home(base: str, slug: str, league_id: str, config: Optional[Dict[str, Any]] = None,
              env: Optional[Dict[str, str]] = None) -> str:
    """A profile-like league home with a league.yaml (+ optional private .env)."""
    import yaml
    home = os.path.join(base, slug)
    os.makedirs(home, exist_ok=True)
    cfg = {"slug": slug, "sleeper_league_id": league_id}
    cfg.update(config or {})
    with open(os.path.join(home, league_ctx.CONFIG_NAME), "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True)
    if env is not None:
        with open(os.path.join(home, ".env"), "w", encoding="utf-8") as f:
            f.write("\n".join(f"{k}={v}" for k, v in env.items()) + "\n")
    return home


class FakeSleeper:
    """A tiny Sleeper: 4 teams, 2 seasons (2025 complete, 2026 running at NFL week 4), one trade with a pick, a
    waiver claim, official brackets and a rookie draft. `get(path)` answers like api.sleeper.app/v1."""

    CUR, PREV = "9002", "9001"

    def __init__(self, slots=None, name="Liga Falsa", names=None, team_prefix=None):
        self.slots = slots or REDRAFT_SLOTS
        self.name = name
        self.names = names or {}                    # league id -> its own name (tests with two distinct leagues)
        self.team_prefix = team_prefix or {}        # league id -> prefix of its team names
        self.calls = []
        handles = ["Ana_Silva", "BrunoCosta", "CarlaM", "DiegoRocha"]
        self._handles = handles
        self.users = [{"user_id": f"u{i}", "display_name": h, "metadata": {"team_name": f"Time {h.split('_')[0]}"}}
                      for i, h in enumerate(handles, 1)]
        self.rosters = {
            self.PREV: [{"roster_id": i, "owner_id": f"u{i}", "players": [f"p{i}"], "settings": {
                "wins": [3, 2, 1, 0][i - 1], "losses": [0, 1, 2, 3][i - 1], "ties": 0, "fpts": 300 - 10 * i, "fpts_decimal": 5,
                "fpts_against": 200 + i, "fpts_against_decimal": 0, "division": 1 if i < 3 else 2}} for i in range(1, 5)],
            self.CUR: [{"roster_id": i, "owner_id": f"u{i}", "players": [f"p{i}", f"q{i}"], "settings": {
                "wins": [2, 1, 1, 0][i - 1], "losses": [0, 1, 1, 2][i - 1], "ties": 0, "fpts": 200 - 5 * i, "fpts_decimal": 0,
                "fpts_against": 150, "fpts_against_decimal": 0, "division": 1 if i < 3 else 2}} for i in range(1, 5)],
        }

    def _league(self, lid):
        prev = lid == self.PREV
        lg = league_json(lid, self.names.get(lid, self.name), self.slots, 4, playoff_week_start=3, playoff_teams=4,
                         divisions=2, draft_rounds=2, type=0)
        lg.update({"season": "2025" if prev else "2026", "status": "complete" if prev else "in_season",
                   "previous_league_id": "0" if prev else self.PREV, "draft_id": "d" + lid,
                   "total_rosters": 4})
        return lg

    def get(self, path):
        path = path.lstrip("/")
        self.calls.append(path)
        parts = path.split("/")
        if path == "state/nfl":
            return {"season": "2026", "week": 4, "season_type": "regular"}
        if parts[0] == "league" and len(parts) == 2:
            return self._league(parts[1]) if parts[1] in (self.CUR, self.PREV) else None
        if parts[0] == "league" and len(parts) >= 3:
            lid, what = parts[1], parts[2]
            prev = lid == self.PREV
            if what == "users":
                pre = self.team_prefix.get(lid)
                if not pre:
                    return self.users
                return [{**u, "metadata": {"team_name": f"{pre} {u['display_name'].split('_')[0]}"}} for u in self.users]
            if what == "rosters":
                return self.rosters[lid]
            if what == "matchups":
                w = int(parts[3])
                pts = {1: (100.0, 90.0, 80.0, 70.0), 2: (110.0, 85.0, 95.0, 60.0), 3: (120.0, 100.0, 90.0, 80.0),
                       4: (130.0, 105.0, 99.0, 88.0)}.get(w)
                if not pts or (not prev and w > 3):
                    return []
                if prev and w >= 3:   # playoffs: 1v4 & 2v3, then final 1v2 and third place 4v3
                    pairs = {3: [(1, 4), (2, 3)], 4: [(1, 2), (4, 3)]}.get(w, [])
                else:
                    pairs = [(1, 4), (2, 3)] if w % 2 else [(1, 3), (2, 4)]
                out = []
                for mid, (a, b) in enumerate(pairs, 1):
                    out += [{"roster_id": a, "matchup_id": mid, "points": pts[a - 1]},
                            {"roster_id": b, "matchup_id": mid, "points": pts[b - 1]}]
                return out
            if what == "transactions":
                w = int(parts[3])
                if w == 1 and prev:
                    return [{"type": "trade", "status": "complete", "transaction_id": "t1", "roster_ids": [1, 2],
                             "status_updated": 1750000000000, "adds": {"p1": 2, "q1": 1},
                             "draft_picks": [{"season": "2025", "round": 1, "roster_id": 1, "owner_id": 2,
                                              "previous_owner_id": 1}], "waiver_budget": [{"sender": 2, "receiver": 1, "amount": 7}]},
                            {"type": "waiver", "status": "complete", "transaction_id": "w1", "roster_ids": [3],
                             "status_updated": 1750100000000, "adds": {"q3": 3}, "drops": {"p3": 3},
                             "settings": {"waiver_bid": 4}}]
                return []
            if what == "winners_bracket":
                if not prev:
                    return []
                return [{"r": 1, "m": 1, "t1": 1, "t2": 4, "w": 1, "l": 4}, {"r": 1, "m": 2, "t1": 2, "t2": 3, "w": 2, "l": 3},
                        {"r": 2, "m": 3, "t1": 1, "t2": 2, "w": 1, "l": 2, "p": 1},
                        {"r": 2, "m": 4, "t1": 4, "t2": 3, "w": 4, "l": 3, "p": 3}]
            if what == "losers_bracket":
                return []
            if what == "drafts":
                return [{"draft_id": "d" + lid, "status": "complete" if prev else "pre_draft", "season": "2025" if prev else "2026",
                         "start_time": 1740000000000, "metadata": {"name": "Rookie 2025"}}] if prev else []
        if parts[0] == "draft":
            did = parts[1]
            if len(parts) == 2:
                return {"draft_id": did, "slot_to_roster_id": {"1": 4, "2": 3, "3": 2, "4": 1},
                        "draft_order": {"u4": 1, "u3": 2, "u2": 3, "u1": 4}, "metadata": {"name": "Rookie 2025"}}
            return [{"pick_no": n, "round": 1 if n <= 4 else 2, "draft_slot": ((n - 1) % 4) + 1,
                     "roster_id": [4, 3, 2, 1][(n - 1) % 4], "player_id": f"r{n}", "picked_by": f"u{[4, 3, 2, 1][(n - 1) % 4]}",
                     "metadata": {"first_name": "Rookie", "last_name": str(n), "position": "RB", "team": "KC"}}
                    for n in range(1, 9)]
        return None
