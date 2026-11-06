"""Plantão do waiver: only claims from OTHER teams make a player disputed (own duplicate claims are ignored)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import waiver_report  # noqa: E402


def tx(status, team, pid, bid, drops=()):
    return {"status": status, "type": "waiver", "teams": [team], "adds": {pid: 1}, "drops": {d: 1 for d in drops},
            "settings": {"waiver_bid": bid}}


class FakeSleeper:
    def __init__(self, txs):
        self.txs = txs

    def get_recent_transactions(self, hours=30, types=(), only_complete=False):
        return self.txs


class DisputasTest(unittest.TestCase):
    def render(self, txs):
        return waiver_report.build(FakeSleeper(txs), lambda pid, with_pos=True: f"P{pid}", 200)

    def test_own_duplicate_claims_are_not_disputes(self):
        out = self.render([tx("complete", "Panda", "hig", 5), tx("failed", "Panda", "hig", 5),
                           tx("complete", "Panda", "bar", 0), tx("failed", "Panda", "bar", 0),
                           tx("failed", "Panda", "bar", 0)])
        self.assertNotIn("Disputas", out)
        self.assertNotIn("venceu", out)
        self.assertIn("0 lances perdidos", out)

    def test_single_valued_bid_against_zero_rival_is_still_a_dispute(self):
        out = self.render([tx("complete", "MOC", "lau", 3), tx("failed", "Miners", "lau", 0),
                           tx("failed", "MOC", "lau", 3)])
        self.assertIn("Disputas", out)
        self.assertIn("venceu 1 lance", out)
        self.assertIn("Miners ($0)", out)
        self.assertNotIn("MOC ($3)", out)
        self.assertIn("1 lances perdidos", out)


if __name__ == "__main__":
    unittest.main()
