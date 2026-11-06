"""Starters come from the week's /matchups entry, not /rosters (which misses a lineup saved for the week ahead).

Week 7, 2026: the lineup was fixed on Monday (Allen → Shough, Doubs → Sutton), /rosters still listed Allen and Doubs,
and Thursday's lineup DMs told the owner to make swaps already made.

Run:  python3 -m unittest tests.test_week_starters -v   (offline)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import lineup_advisor  # noqa: E402
import sleeper_client  # noqa: E402

ALLEN, DOUBS, SHOUGH, SUTTON = "4984", "8121", "12545", "5045"
ROSTERS = [{"roster_id": 4, "starters": [ALLEN, "7588", DOUBS], "players": [ALLEN, "7588", DOUBS, SHOUGH, SUTTON]},
           {"roster_id": 5, "starters": ["1", "2", "3"], "players": ["1", "2", "3"]}]
MATCHUPS = [{"roster_id": 4, "matchup_id": 2, "starters": [SHOUGH, "7588", SUTTON]},
            {"roster_id": 5, "matchup_id": 2, "starters": []}]


class FakeClient(sleeper_client.SleeperClient):
    def __init__(self, state):
        self.league_id = "L"
        self.state = state
        self.calls = []

    def _get(self, endpoint):
        self.calls.append(endpoint)
        return {"state/nfl": self.state, "league/L/rosters": ROSTERS, "league/L/matchups/7": MATCHUPS}.get(endpoint)


class WeekStarters(unittest.TestCase):
    def test_overlay_uses_matchup_lineup(self):
        out = {r["roster_id"]: r for r in sleeper_client.with_week_starters(ROSTERS, MATCHUPS)}
        self.assertEqual(out[4]["starters"], [SHOUGH, "7588", SUTTON])
        self.assertEqual(out[4]["players"], ROSTERS[0]["players"])
        self.assertEqual(out[5]["starters"], ["1", "2", "3"])          # no lineup in the matchup: keep /rosters
        self.assertEqual(ROSTERS[0]["starters"], [ALLEN, "7588", DOUBS])  # cached object left untouched

    def test_get_rosters_in_season(self):
        c = FakeClient({"week": 7, "season_type": "regular"})
        r4 = next(r for r in c.get_rosters() if r["roster_id"] == 4)
        self.assertNotIn(ALLEN, r4["starters"])
        self.assertNotIn(DOUBS, r4["starters"])

    def test_get_rosters_offseason_or_no_state(self):
        for state in ({"week": 0, "season_type": "off"}, {"week": 7, "season_type": "pre"}, None):
            c = FakeClient(state)
            self.assertIs(c.get_rosters(), ROSTERS)
            self.assertFalse(any("/matchups/" in e for e in c.calls))

    def test_advisor_week_lineup(self):
        r = lineup_advisor._week_lineup(ROSTERS[0], MATCHUPS[0])
        self.assertEqual(r["starters"], [SHOUGH, "7588", SUTTON])
        self.assertIs(lineup_advisor._week_lineup(ROSTERS[1], MATCHUPS[1]), ROSTERS[1])
        self.assertIs(lineup_advisor._week_lineup(ROSTERS[0], None), ROSTERS[0])
        self.assertIsNone(lineup_advisor._week_lineup(None, MATCHUPS[0]))


if __name__ == "__main__":
    unittest.main()
