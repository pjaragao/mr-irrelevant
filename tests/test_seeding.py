"""Playoff seeds under the house rule `seeding: division_winners` (seeds 1..N = division leaders, rest by record/PF)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import sleeper_client  # noqa: E402


def roster(rid, div, w, l, pf):
    return {"roster_id": rid, "owner_id": f"u{rid}", "settings": {"division": div, "wins": w, "losses": l, "fpts": pf}}


ROSTERS = [roster(1, 1, 5, 0, 900), roster(2, 1, 3, 2, 800),   # Riedelots-like: better PF than a leader of div 2
           roster(3, 2, 3, 2, 700), roster(4, 2, 3, 2, 600),
           roster(5, 3, 4, 1, 790), roster(6, 3, 3, 2, 750)]


class SeedingTest(unittest.TestCase):
    def standings(self, rule):
        c = sleeper_client.SleeperClient("1")
        users = {f"u{i}": {"team_name": f"T{i}", "display_name": f"o{i}"} for i in range(1, 7)}
        with mock.patch.object(c, "get_league", return_value={"settings": {"divisions": 3}, "metadata": {}}), \
             mock.patch.object(c, "get_users", return_value=users), \
             mock.patch.object(c, "get_rosters", return_value=ROSTERS), \
             mock.patch.object(sleeper_client.league_ctx, "current") as cur:
            cur.return_value.rule.return_value = rule
            return c.get_standings()

    def test_division_leaders_take_seeds_1_to_n(self):
        order = [t["roster_id"] for t in sorted(self.standings("division_winners"), key=lambda t: t["rank"])]
        self.assertEqual(order[:3], [1, 5, 3])            # leaders: 5-0, 4-1, then 3-2 with the better PF
        self.assertEqual(order[3:], [2, 6, 4])            # the rest by record then PF (2 beats 3-2 6 on PF)

    def test_without_rule_plain_record(self):
        order = [t["roster_id"] for t in sorted(self.standings(None), key=lambda t: t["rank"])]
        self.assertEqual(order, [1, 5, 2, 6, 3, 4])


if __name__ == "__main__":
    unittest.main()
