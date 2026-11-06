"""Player points per week (`estatisticas entidade=pontuacao`): ranking by position with owner, bye never a 0-point game.

Run:  python3 -m unittest tests.test_player_scores -v   (offline, in-memory SQLite)
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import kb_db  # noqa: E402
import kb_stats  # noqa: E402
import player_scores  # noqa: E402

DATA = {
    "players": {"1": ["Qb One", "QB", "CAR"], "2": ["Qb Two", "QB", "KC"], "3": ["Rb One", "RB", "KC"]},
    "seasons": {"2026": {"weeks": {
        "1": {"playing": ["CAR", "KC"], "rosters": {"1": {"1": [30.0, 1], "3": [10.0, 1]}, "2": {"2": [20.0, 1]}}},
        "2": {"playing": ["KC"], "rosters": {"1": {"1": [0.0, 0], "3": [12.0, 1]}, "2": {"2": [10.0, 1]}}},   # CAR on bye
    }}},
}


class PontuacaoTest(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(kb_db.SCHEMA)
        self.con.execute("INSERT INTO franchises VALUES (1,'mgr1','Ana','Team A')")
        self.con.execute("INSERT INTO franchises VALUES (2,'mgr2','Beto','Team B')")
        kb_db._load_player_weeks(self.con, DATA)

    def test_bye_flag_only_when_team_off_the_slate(self):
        rows = {(r[1], r[3]): r[9] for r in player_scores.rows(DATA)}
        self.assertEqual(rows[(2, "1")], 1)
        self.assertEqual(rows[(1, "1")], 0)

    def test_top_qb_with_owner(self):
        r = kb_stats.fetch(self.con, "pontuacao", "jogador", "pontos", posicao="QB")
        self.assertEqual([g for g, _ in r["rows"]], ["Qb One · Team A", "Qb Two · Team B"])
        self.assertNotIn("Rb One", " ".join(g for g, _ in r["rows"]))

    def test_average_skips_bye(self):
        r = kb_stats.fetch(self.con, "pontuacao", "jogador", "media", posicao="QB", alvo="qb one")
        self.assertEqual(r["rows"][0][1], 30.0)          # one real game, the bye is not averaged as 0

    def test_week_view_flags_bye(self):
        out = kb_stats.query(self.con, "pontuacao", "semana", "pontos", alvo="qb one")
        self.assertIn("bye", out)
        self.assertIn("S2/2026", out)


if __name__ == "__main__":
    unittest.main()
