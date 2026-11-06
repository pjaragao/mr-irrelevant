"""Live "virada" alerts: every lead change of a matchup is announced, not just the first one.

The old loop stored the new leader even when it did not alert (margin < LIVE_LEAD_MIN or inside the 90-min
cooldown), so a flip first seen by a small margin was lost for good — in practice one alert per matchup.

Run:  python3 -m unittest tests.test_lead_watch -v   (offline)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import lead_watch  # noqa: E402

MIN = 60


def m(a, b, is_open=True):
    return [{"matchup_id": 1, "is_open": is_open,
             "team1": {"roster_id": 1, "team_name": "Alfa", "points": a},
             "team2": {"roster_id": 2, "team_name": "Beta", "points": b}}]


class LeadWatch(unittest.TestCase):
    def run_ticks(self, scores, cooldown=20 * MIN, step=5 * MIN):
        """Feed one score per 5-min tick; returns the texts sent."""
        st, sent, now = {}, [], 0.0
        for a, b in scores:
            for alert in lead_watch.lead_changes(m(a, b), st, "2026", 7, now, 3.0, cooldown):
                sent.append(alert["text"])
                lead_watch.commit(st, alert, now)
            now += step
        return sent

    def test_small_flip_then_growing_is_announced(self):
        # Beta passes by 1 (no alert yet), then by 5: the old code never alerted here
        sent = self.run_ticks([(10, 5), (10, 11), (10, 15)])
        self.assertEqual(len(sent), 1)
        self.assertIn("Beta passou Alfa: 15.00 x 10.00", sent[0])

    def test_every_flip_of_the_match(self):
        sent = self.run_ticks([(10, 0), (10, 20), (12, 20), (12, 20), (30, 20), (30, 20), (30, 40)],
                              cooldown=0)
        self.assertEqual(len(sent), 3)
        self.assertTrue(sent[0].startswith("🔄 *Virada!* Beta"))
        self.assertIn("Nova virada!* (2ª no confronto) Alfa", sent[1])
        self.assertIn("(3ª no confronto) Beta", sent[2])

    def test_cooldown_defers_not_drops(self):
        # second flip 5 min after the first one: held back, then sent once the cooldown is over
        sent = self.run_ticks([(10, 0), (10, 20), (30, 20), (30, 20), (30, 20), (30, 20), (30, 20)],
                              cooldown=20 * MIN)
        self.assertEqual(len(sent), 2)
        self.assertIn("Alfa passou Beta", sent[1])

    def test_flip_back_inside_cooldown_is_no_news(self):
        sent = self.run_ticks([(10, 0), (10, 20), (30, 20), (30, 40), (30, 40), (30, 40), (30, 40)],
                              cooldown=20 * MIN)
        self.assertEqual(len(sent), 1)

    def test_ping_pong_below_margin_is_silent(self):
        self.assertEqual(self.run_ticks([(10, 5), (10, 11), (12, 11), (12, 13), (14, 13)], cooldown=0), [])

    def test_failed_delivery_retries(self):
        st = {}
        lead_watch.lead_changes(m(10, 0), st, "2026", 7, 0)
        self.assertEqual(len(lead_watch.lead_changes(m(10, 20), st, "2026", 7, 100)), 1)   # not committed
        self.assertEqual(len(lead_watch.lead_changes(m(10, 20), st, "2026", 7, 400)), 1)

    def test_closed_matchup_last_play_flip(self):
        st = {}
        lead_watch.lead_changes(m(100, 95), st, "2026", 7, 0)
        self.assertEqual(len(lead_watch.lead_changes(m(100, 104, is_open=False), st, "2026", 7, 300)), 1)


if __name__ == "__main__":
    unittest.main()
