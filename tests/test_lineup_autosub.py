"""Lineup advice: Questionable starters go to the widest slot, AutoSubs are Sleeper pairs (one sub → one starter).

Sleeper AutoSub: each bench player is paired with ONE starter and must fit that starter's SLOT; the pair locks when
either kicks off. So a Questionable WR in FLEX can be covered by any RB/WR/TE (and swapped by hand until his game),
while in a WR slot only a WR can. Week 7, 2026: Pickens (Q, Monday night) belonged in FLEX.

Run:  python3 -m unittest tests.test_lineup_autosub -v   (offline)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import league_profile  # noqa: E402
import lineup_advisor as la  # noqa: E402

THU, SUN, MON = "2026-10-22T21:15:00-03:00", "2026-10-25T14:00:00-03:00", "2026-10-26T21:15:00-03:00"


def p(pid, pos, slot=None, q=False, ko=SUN, proj=10.0, out=False, started=False):
    return {"pid": pid, "name": f"P{pid}", "pos": pos, "slot": slot, "q": q, "out": out, "bye": False,
            "started": started, "kickoff": ko, "proj": proj, "status": "Questionable" if q else "", "game": ko[:10]}


def eligible(slot):
    return league_profile.SLOT_ELIGIBILITY.get(slot, frozenset({slot}))


class Realign(unittest.TestCase):
    def test_questionable_wr_goes_to_flex(self):
        plan = [p("wr_q", "WR", "WR", q=True, ko=MON), p("wr_ok", "WR", "FLEX")]
        moves = la.realign(plan, eligible)
        self.assertEqual([(m["name"], m["from"], m["to"], m["with"]) for m in moves], [("Pwr_q", "WR", "FLEX", "Pwr_ok")])
        self.assertEqual({x["pid"]: x["slot"] for x in plan}, {"wr_q": "FLEX", "wr_ok": "WR"})

    def test_already_in_flex_or_no_fit(self):
        self.assertEqual(la.realign([p("a", "WR", "FLEX", q=True), p("b", "WR", "WR")], eligible), [])
        # the flex occupant (RB) cannot go to the WR slot
        self.assertEqual(la.realign([p("a", "WR", "WR", q=True), p("b", "RB", "FLEX")], eligible), [])
        # started games never move
        self.assertEqual(la.realign([p("a", "WR", "WR", q=True), p("b", "WR", "FLEX", started=True)], eligible), [])

    def test_two_questionables_later_game_gets_flex(self):
        plan = [p("early", "WR", "FLEX", q=True, ko=THU), p("late", "WR", "WR", q=True, ko=MON)]
        la.realign(plan, eligible)
        self.assertEqual({x["pid"]: x["slot"] for x in plan}, {"late": "FLEX", "early": "WR"})
        self.assertEqual(la.realign(plan, eligible), [])          # stable: no ping-pong

    def test_prefers_healthy_partner(self):
        plan = [p("q", "WR", "WR", q=True, ko=MON), p("f1", "WR", "FLEX", q=True, ko=THU), p("f2", "WR", "FLEX")]
        moves = la.realign(plan, eligible)
        self.assertEqual(moves[0]["with"], "Pf2")


class LateGamesInFlex(unittest.TestCase):
    def test_later_game_goes_to_flex(self):
        plan = [p("late", "WR", "WR", ko=MON), p("early", "WR", "FLEX", ko=SUN)]
        moves = la.realign(plan, eligible)
        self.assertEqual({x["pid"]: x["slot"] for x in plan}, {"late": "FLEX", "early": "WR"})
        self.assertIn("joga depois", moves[0]["why"])

    def test_same_kickoff_no_move(self):
        self.assertEqual(la.realign([p("a", "WR", "WR"), p("b", "WR", "FLEX")], eligible), [])

    def test_questionable_beats_later_healthy(self):
        plan = [p("q", "WR", "WR", q=True, ko=SUN), p("late", "WR", "FLEX", ko=MON)]
        self.assertEqual([m["name"] for m in la.realign(plan, eligible)], ["Pq"])   # Q outranks a later healthy
        plan = [p("q", "WR", "FLEX", q=True, ko=SUN), p("late", "WR", "WR", ko=MON)]
        self.assertEqual(la.realign(plan, eligible), [])

    def test_superflex_takes_latest(self):
        # SUPER_FLEX is wider than FLEX: the latest of the flex-eligible players ends there
        plan = [p("rb", "RB", "FLEX", ko=MON), p("wr", "WR", "SUPER_FLEX", ko=SUN), p("wr2", "WR", "WR", ko=THU)]
        la.realign(plan, eligible)
        slots = {x["pid"]: x["slot"] for x in plan}
        self.assertEqual(slots["rb"], "SUPER_FLEX")
        self.assertEqual(slots["wr2"], "WR")                        # Thursday game stays in the narrow slot

    def test_terminates_on_full_lineup(self):
        kos = [THU, SUN, MON, SUN, THU, MON, SUN]
        plan = [p(str(i), "WR", s, ko=kos[i]) for i, s in enumerate(["WR", "WR", "WR", "FLEX", "FLEX", "FLEX",
                                                                      "SUPER_FLEX"])]
        la.realign(plan, eligible)
        self.assertEqual(la.realign(plan, eligible), [])
        wide = sorted(_ts for _ts in (x["kickoff"] for x in plan if x["slot"] != "WR"))
        narrow = max(x["kickoff"] for x in plan if x["slot"] == "WR")
        self.assertLessEqual(narrow, wide[0])


class Pairs(unittest.TestCase):
    def test_one_sub_one_starter_slot_eligible(self):
        plan = [p("te_q", "TE", "TE", q=True), p("wr_q", "WR", "FLEX", q=True, ko=MON), p("rb", "RB", "RB")]
        pool = [p("te_b", "TE", proj=9.5), p("rb_b", "RB", proj=7.0), p("wr_b", "WR", proj=5.0)]
        subs = la.pair_autosubs(plan, pool, eligible, 3)
        pairs = {a["covers_pid"]: a["pid"] for a in subs}
        self.assertEqual(pairs["te_q"], "te_b")                    # TE-only slot keeps the only TE
        self.assertEqual(pairs["wr_q"], "rb_b")                    # FLEX takes the best of the rest
        self.assertEqual(len({a["pid"] for a in subs}), len(subs))
        for a in subs:
            self.assertEqual(len(a["covers"]), 1)

    def test_deadline_is_earlier_kickoff(self):
        subs = la.pair_autosubs([p("s", "WR", "FLEX", q=True, ko=MON)], [p("b", "TE", ko=THU)], eligible, 1)
        self.assertEqual(subs[0]["deadline"], THU)

    def test_same_time_rule(self):
        plan, pool = [p("s", "WR", "FLEX", q=True, ko=SUN)], [p("b", "WR", ko=THU, proj=20)]
        self.assertEqual(la.pair_autosubs(plan, pool, eligible, 1, same_time=True), [])
        self.assertEqual(len(la.pair_autosubs(plan, pool, eligible, 1, same_time=False)), 1)


class ModelPairs(unittest.TestCase):
    def facts(self):
        plan = [p("wr_q", "WR", "WR", q=True)]
        return {"starters": plan, "plan": plan, "bench": [p("te_b", "TE"), p("wr_b", "WR")]}

    def test_invalid_pairs_dropped(self):
        raw = [{"jogador": "Pte_b", "cobre": "Pwr_q"},             # TE cannot fill a WR slot
               {"jogador": "Pwr_b", "cobre": "Pwr_q e mais"},
               {"jogador": "Pwr_b", "cobre": "Pwr_q"}]             # sub and starter already used
        out = la._valid_pairs(raw, self.facts(), 3)
        self.assertEqual([(a["jogador"], a["cobre"], a["slot"]) for a in out], [("Pwr_b", "Pwr_q", "WR")])


if __name__ == "__main__":
    unittest.main()
