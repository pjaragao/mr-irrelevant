"""H2H: the matrix must be a pure function of the raw games + the official Sleeper brackets.

Regression behind it: the bot told Mario × Lula "playoffs 1x0, toilet bowl 1x2" (real: 2x2 in the title bracket)
because phases came from a hand-typed table. Run:  python3 -m unittest tests.test_h2h -v   (offline)
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAGUE = os.path.join(ROOT, "league")
KB = os.path.join(LEAGUE, "knowledge_base")
sys.path.insert(0, LEAGUE)

import h2h_builder as hb  # noqa: E402
from knowledge_service import KnowledgeBaseService  # noqa: E402


def _kb(name):
    with open(os.path.join(KB, name), encoding="utf-8") as f:
        return json.load(f)


MATCHUPS = _kb("matchups_history.json")
FRANCHISES = {int(k): v for k, v in _kb("franchises_history.json").items()}
BRACKETS = _kb("brackets_history.json")
PLAYOFFS = _kb("playoffs_history.json")
SEASONS = sorted(s for s, d in BRACKETS.items() if s != "_meta" and d["games"])


def _paren(text):  # "Bayer Kaxinawás (Ayahuasquers)" -> "ayahuasquers"
    return text.rsplit("(", 1)[1].split(")")[0].lower()


class BracketsAgreeWithGamesTest(unittest.TestCase):
    def test_every_bracket_game_is_a_real_game_won_by_the_bracket_winner(self):
        games = {(g["season"], g["week"], frozenset((g["roster_a"], g["roster_b"]))): g for g in MATCHUPS}
        for season in SEASONS:
            for b in BRACKETS[season]["games"]:
                g = games.get((season, b["week"], frozenset((b["roster_a"], b["roster_b"]))))
                self.assertIsNotNone(g, (season, b))
                hi = g["roster_a"] if g["points_a"] > g["points_b"] else g["roster_b"]
                self.assertEqual(hi, b["winner"], (season, b))

    def test_every_postseason_game_is_in_exactly_one_bracket(self):
        stages = hb.StageIndex(BRACKETS)
        for season in SEASONS:
            post = [g for g in MATCHUPS if g["season"] == season and g["is_playoff"]]
            self.assertEqual(len(post), len(BRACKETS[season]["games"]), season)  # 7 winners + 7 losers
            for g in post:
                self.assertIsNotNone(stages.lookup(season, g["week"], g["roster_a"], g["roster_b"]), (season, g))

    def test_final_and_toilet_final_match_the_official_standings(self):
        by_roster = {rid: f["canonical_manager"].lower() for rid, f in FRANCHISES.items()}
        for season in SEASONS:
            games = BRACKETS[season]["games"]
            final = next(g for g in games if g["bracket"] == "winners" and g["place"] == 1)
            toilet = next(g for g in games if g["bracket"] == "losers" and g["place"] == 1)
            self.assertEqual(by_roster[final["winner"]], _paren(PLAYOFFS[season]["playoffs_title"]["champion"]), season)
            self.assertEqual(by_roster[final["loser"]], _paren(PLAYOFFS[season]["playoffs_title"]["runner_up"]), season)
            self.assertEqual(by_roster[toilet["winner"]],
                             _paren(PLAYOFFS[season]["toilet_bowl"]["toilet_champion"].split(" [")[0]), season)


class MatrixTest(unittest.TestCase):
    def test_stored_matrix_is_exactly_the_rebuild(self):
        rebuilt = json.loads(json.dumps(hb.build_matrix(MATCHUPS, FRANCHISES, BRACKETS)))
        self.assertEqual(_kb("h2h_matrix.json"), rebuilt,
                         "h2h_matrix.json is stale: run `python3 league/h2h_builder.py --rebuild --write`")

    def test_recortes_sum_to_total_and_every_game_is_in_the_log(self):
        for key, p in _kb("h2h_matrix.json").items():
            self.assertEqual(p["games_reg"] + p["games_playoffs"] + p["games_toilet"] + p["games_unclassified"],
                             p["games_total"], key)
            self.assertEqual(p["games_unclassified"], 0, key)
            self.assertEqual(len(p["games_log"]), p["games_total"], key)
            self.assertNotIn("games", p, key)  # 2026 games used to live in a separate key the readers ignored
            for side in ("1", "2"):
                self.assertEqual(p[f"wins_{side}"], p[f"wins_{side}_reg"] + p[f"wins_{side}_po"] + p[f"wins_{side}_tb"], key)
            self.assertEqual(p["wins_1"] + p["wins_2"] + p["ties"], p["games_total"], key)

    def test_matrix_covers_every_game_of_the_history(self):
        total = sum(p["games_total"] for p in _kb("h2h_matrix.json").values())
        self.assertEqual(total, len(MATCHUPS))

    def test_current_season_games_are_in_the_game_log(self):
        n26 = sum(1 for g in MATCHUPS if g["season"] == "2026")
        got = sum(1 for p in _kb("h2h_matrix.json").values() for g in p["games_log"] if g["season"] == "2026")
        self.assertEqual(got, n26)

    def test_blowout_and_closest_follow_the_log(self):
        for key, p in _kb("h2h_matrix.json").items():
            decided = [g for g in p["games_log"] if g["winner"] != "Empate"]
            self.assertEqual(p["biggest_blowout"]["margin"], max(g["margin"] for g in decided), key)
            self.assertEqual(p["closest_match"]["margin"], min(g["margin"] for g in p["games_log"]), key)


class MarioVsLulaRegressionTest(unittest.TestCase):
    """What Lula and Mario settled in the group: they met every year; 2x2 in the post-season."""

    @classmethod
    def setUpClass(cls):
        cls.kb = KnowledgeBaseService()

    def test_summary_from_mario_side(self):
        r = self.kb.get_h2h_segregated_summary("Mario", "Lula")
        self.assertEqual((r["wins_a"], r["wins_b"]), (6, 3))
        self.assertEqual(r["reg_str"], "4 x 1")
        self.assertEqual(r["po_str"], "2 x 2")
        self.assertEqual(r["tb_str"], "0 x 0")
        self.assertEqual([(g["season"], g["stage"], g["side"]) for g in r["playoff_games"]],
                         [("2022", "WC", "a"), ("2023", "WC", "b"), ("2024", "WC", "b"), ("2025", "Semi", "a")])

    def test_summary_from_lula_side_is_the_mirror(self):
        r = self.kb.get_h2h_segregated_summary("Lula", "Mario")
        self.assertEqual((r["wins_a"], r["wins_b"]), (3, 6))
        self.assertEqual((r["reg_str"], r["po_str"]), ("1 x 4", "2 x 2"))
        self.assertEqual([g["side"] for g in r["playoff_games"]], ["b", "a", "a", "b"])

    def test_full_text_does_not_invent_a_playoff_edge(self):
        text = self.kb.get_h2h("Mario", "Lula")
        self.assertIn("2 x 2 (4 jogos)", text)
        self.assertNotIn("superioridade", text)
        self.assertNotIn("Toilet Bowl (consolação", text)  # they never met in the consolation bracket

    def test_toilet_bowl_is_not_counted_as_playoffs(self):
        # Paulo × Chevarria: both toilet bowl meetings (2022 W16 / 2025 W17...) are not "playoffs"
        r = self.kb.get_h2h_segregated_summary("Paulo", "Chevarria")
        self.assertEqual(r["po_str"], "0 x 0")
        self.assertNotEqual(r["tb_str"], "0 x 0")
        self.assertEqual(r["playoff_games"], [])

    def test_unknown_postseason_game_is_never_guessed_as_toilet_bowl(self):
        tag, _ = self.kb.classify_game("2031", 15, True, "Mario", "Lula")
        self.assertEqual(tag, hb.UNKNOWN)
        self.assertEqual(self.kb.classify_game("2031", 3, False, "Mario", "Lula")[0], hb.REGULAR)


class BuilderUnitTest(unittest.TestCase):
    FR = {1: {"canonical_manager": "ana", "real_name": "Ana", "current_team": "Aces", "managers_history": [],
              "teams_history": []},
          2: {"canonical_manager": "bia", "real_name": "Bia", "current_team": "Bees", "managers_history": [],
              "teams_history": []}}

    @staticmethod
    def _game(week, a, b, po=False, season="2030"):
        return {"season": season, "week": week, "is_playoff": po, "phase": "Playoffs" if po else "Temporada Regular",
                "roster_a": 1, "roster_b": 2, "manager_a": "ana", "manager_b": "bia", "team_a": "Aces", "team_b": "Bees",
                "points_a": a, "points_b": b}

    def test_stage_of(self):
        self.assertEqual(hb.stage_of("winners", 1, None)[0], hb.WILD_CARD)
        self.assertEqual(hb.stage_of("winners", 2, None)[0], hb.SEMI)
        self.assertEqual(hb.stage_of("winners", 2, 5)[0], hb.FIFTH)
        self.assertEqual(hb.stage_of("winners", 3, 3)[0], hb.THIRD)
        self.assertEqual(hb.stage_of("winners", 3, 1)[0], hb.FINAL)
        for rnd, p in ((1, None), (2, None), (2, 5), (3, 3), (3, 1)):
            self.assertEqual(hb.stage_of("losers", rnd, p)[0], hb.TOILET)

    def test_recortes_and_marks(self):
        brackets = {"2030": {"playoff_week_start": 15, "games": [
            {"bracket": "winners", "round": 1, "week": 15, "roster_a": 1, "roster_b": 2, "winner": 2,
             "stage": hb.WILD_CARD, "label": "Playoffs — Wild Card"},
            {"bracket": "losers", "round": 3, "week": 17, "roster_a": 1, "roster_b": 2, "winner": 1,
             "stage": hb.TOILET, "label": "Toilet Bowl"}]}}
        games = [self._game(1, 100.0, 90.0), self._game(2, 80.0, 120.0), self._game(15, 110.0, 130.5, po=True),
                 self._game(17, 140.0, 100.0, po=True)]
        p = hb.build_matrix(games, self.FR, brackets)["1_2"]
        self.assertEqual((p["games_reg"], p["games_playoffs"], p["games_toilet"], p["games_total"]), (2, 1, 1, 4))
        self.assertEqual((p["wins_1_reg"], p["wins_2_reg"]), (1, 1))
        self.assertEqual((p["wins_1_po"], p["wins_2_po"], p["wins_1_tb"], p["wins_2_tb"]), (0, 1, 1, 0))
        self.assertEqual(p["biggest_blowout"]["margin"], 40.0)
        self.assertEqual(p["closest_match"]["margin"], 10.0)
        self.assertEqual([g["phase"] for g in p["games_log"]],
                         ["Temporada Regular", "Temporada Regular", "Playoffs", "Toilet Bowl"])
        # a post-season game the brackets do not know is reported as unclassified, never as toilet bowl
        p2 = hb.build_matrix(games, self.FR, {})["1_2"]
        self.assertEqual((p2["games_playoffs"], p2["games_toilet"], p2["games_unclassified"]), (0, 0, 2))

    def test_fetch_season_keeps_only_decided_games(self):
        league = {"status": "in_season", "settings": {"playoff_week_start": 15, "playoff_teams": 6}}
        winners = [{"r": 1, "m": 1, "t1": 3, "t2": 6, "w": 3, "l": 6},
                   {"r": 2, "m": 3, "t1": 1, "t2": None, "w": None, "l": None},  # placeholder: next round
                   {"r": 3, "m": 6, "p": 1, "t1": None, "t2": None, "w": None, "l": None}]
        losers = [{"r": 1, "m": 1, "t1": 8, "t2": 9, "w": None, "l": None}]  # projected pairing, not played

        def get(path):
            return winners if path.endswith("winners_bracket") else losers if path.endswith("losers_bracket") else league
        out = hb.fetch_season("2030", "123", get)
        self.assertEqual(len(out["games"]), 1)
        g = out["games"][0]
        self.assertEqual((g["bracket"], g["week"], g["stage"], g["winner"]), ("winners", 15, hb.WILD_CARD, 3))

    def test_sync_regenerates_the_matrix_from_games(self):
        """check_and_sync/reconcile no longer patch h2h_matrix: saving history rebuilds it (no games/games_log split)."""
        import transaction_monitor as tm
        tmp = tempfile.mkdtemp()
        for name in ("franchises_history.json", "brackets_history.json"):
            with open(os.path.join(KB, name), encoding="utf-8") as src, open(os.path.join(tmp, name), "w", encoding="utf-8") as dst:
                dst.write(src.read())
        history = [g for g in MATCHUPS if g["season"] != "2026"]
        history.append(dict(self._game(1, 150.0, 120.0, season="2026"), roster_a=1, roster_b=9, manager_a="marinh012",
                            manager_b="luizfelipesouza", team_a="MOC’s Outcasts", team_b="Squid Games"))
        monitor = object.__new__(tm.TransactionMonitor)
        with mock.patch.object(tm, "KB_DIR", tmp):
            monitor._save_history(history, FRANCHISES)
        with open(os.path.join(tmp, "h2h_matrix.json"), encoding="utf-8") as f:
            pair = json.load(f)["1_9"]
        self.assertEqual(pair["games_total"], 9)
        self.assertEqual(pair["games_log"][-1]["season"], "2026")
        self.assertEqual((pair["wins_1"], pair["wins_2"]), (6, 3))
        self.assertNotIn("games", pair)


if __name__ == "__main__":
    unittest.main()
