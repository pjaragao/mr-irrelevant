"""Reply threads (league/reply_chain.py) and multi-list position rankings (player_scores.ranking_lists)."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import kb_db  # noqa: E402
import player_scores  # noqa: E402
import reply_chain  # noqa: E402


class ReplyChainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = reply_chain.ChainStore(os.path.join(self.tmp, "c.db"))

    def test_thread_walks_quotes_oldest_first_and_joins_chunks(self):
        g = "g@g.us"
        self.st.record(g, "m1", "Mario", "top 12 QB só managers")
        self.st.record(g, "b1", "Mr. Irrelevant", "parte 1", is_bot=True, reply_to="m1", turn="t1", seq=1)
        self.st.record(g, "b2", "Mr. Irrelevant", "parte 2", is_bot=True, reply_to="m1", turn="t1", seq=2)
        self.st.record(g, "m2", "Mario", "agora RB", reply_to="b2")
        chain = self.st.chain(g, "m2")
        self.assertEqual([m["id"] for m in chain], ["m1", "b2", "m2"])
        self.assertEqual(chain[1]["text"], "parte 1\nparte 2")
        block = reply_chain.format_context(chain)
        self.assertLess(block.index("top 12 QB"), block.index("agora RB"))
        self.assertIn("Mr. Irrelevant: parte 1 parte 2", block)

    def test_was_bot_and_cycles(self):
        g = "g@g.us"
        self.st.record(g, "b1", "Bot", "oi", is_bot=True)
        self.st.record(g, "a", "X", "a", reply_to="b")
        self.st.record(g, "b", "Y", "b", reply_to="a")          # malformed loop must terminate
        self.assertTrue(self.st.was_bot(g, "b1"))
        self.assertFalse(self.st.was_bot(g, "a"))
        self.assertFalse(self.st.was_bot("other", "b1"))        # chats never mix
        self.assertEqual(len(self.st.chain(g, "a")), 2)


class RankingListsTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(kb_db.SCHEMA)
        self.con.execute("INSERT INTO franchises VALUES (1,'m1','Ana','Team A')")
        pts = {"r1": ("RB", 50), "r2": ("RB", 40), "r3": ("RB", 30), "w1": ("WR", 45), "w2": ("WR", 20),
               "t1": ("TE", 25), "q1": ("QB", 99)}
        for pid, (pos, p) in pts.items():
            self.con.execute("INSERT INTO player_weeks VALUES ('2026',1,1,?,?,?,'KC',?,1,0)", (pid, pid.upper(), pos, p))
        self.con.execute("INSERT INTO player_weeks VALUES ('2026',1,1,'bye','BYE','RB','KC',0,0,1)")

    def test_flex_excludes_earlier_lists_and_other_positions(self):
        out = player_scores.ranking_lists(self.con, "RB:1,WR:1,TE:1,FLEX:3")
        flex = out.split("*TOP 3 FLEX")[1]
        names = [l.split(". ")[1].split(" (")[0] for l in flex.splitlines() if l[:1].isdigit()]
        self.assertEqual(names, ["R2", "R3", "W2"])    # what is left after RB:1, WR:1, TE:1
        self.assertNotIn("R1", names)          # taken by RB list
        self.assertNotIn("Q1", names)          # QB is not flex
        self.assertNotIn("BYE", names)         # bye-only player has no game

    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            player_scores.parse_lists("RB")
        with self.assertRaises(ValueError):
            player_scores.parse_lists("XX:3")


if __name__ == "__main__":
    unittest.main()


class ContextInfoShapeTest(unittest.TestCase):
    """Evolution v2 puts contextInfo at data.contextInfo (beside `message`): the adapter must find the quote there."""

    @staticmethod
    def _fn():
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "data", "plugins", "evolution-whatsapp", "adapter.py"), encoding="utf-8").read()
        ns = {}
        exec("from typing import Any, Dict, Optional\n" + src[src.index("def _context_info"):src.index("def _league_module")], ns)
        return ns["_context_info"]

    def test_top_level_context_info(self):
        data = {"message": {"conversation": "oi", "messageContextInfo": {"threadId": []}},
                "contextInfo": {"stanzaId": "S1", "participant": "1@lid", "quotedMessage": {"conversation": "q"}}}
        ctx = self._fn()(data["message"], data)
        self.assertEqual(ctx["stanzaId"], "S1")
        self.assertIn("quotedMessage", ctx)

    def test_nested_shape_still_works(self):
        msg = {"extendedTextMessage": {"text": "x", "contextInfo": {"stanzaId": "S2", "mentionedJid": ["a"]}}}
        self.assertEqual(self._fn()(msg, {})["stanzaId"], "S2")
