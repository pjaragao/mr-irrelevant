"""A league created from nothing but its Sleeper id gets a working knowledge base, isolated from every other league.

Run:  python3 -m unittest discover -s tests -p "test_kb_bootstrap.py" -v      (offline: a fake Sleeper)
"""
import json
import os
import tempfile
import unittest

import league_fixtures as fx

import kb_bootstrap
import kb_db
import kb_queries
import league_ctx

PLAYERS = {"p1": {"name": "Pat One", "pos": "QB", "team": "KC"}, "q1": {"name": "Quinn One", "pos": "RB", "team": "SF"},
           "q3": {"name": "Quinn Three", "pos": "WR", "team": "DAL"}, "p3": {"name": "Pat Three", "pos": "TE", "team": "MIA"}}


class BootstrapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        league_ctx.reset_cache()
        cls.sleeper = fx.FakeSleeper()
        home = fx.make_home(cls.tmp.name, "liga-falsa", fx.FakeSleeper.CUR, {"group_jid": "1203@g.us"})
        lg = cls.sleeper.get(f"league/{fx.FakeSleeper.CUR}")
        cls.ctx = league_ctx.LeagueContext(      # the rules/people come from the same fake Sleeper: no network
            home=home, config={"slug": "liga-falsa", "sleeper_league_id": fx.FakeSleeper.CUR, "group_jid": "1203@g.us"},
            legacy_layout=False,
            fetch=lambda: (lg, cls.sleeper.get(f"league/{fx.FakeSleeper.CUR}/users"),
                           cls.sleeper.get(f"league/{fx.FakeSleeper.CUR}/rosters")))
        cls.report = kb_bootstrap.bootstrap(cls.ctx, get=cls.sleeper.get, players=PLAYERS, progress=lambda m: None)
        cls.kb = cls.ctx.kb_dir

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        league_ctx.reset_cache()

    def _json(self, name):
        with open(os.path.join(self.kb, name), encoding="utf-8") as f:
            return json.load(f)

    def test_follows_the_season_chain_and_writes_every_source(self):
        self.assertEqual(self.report["seasons"], ["2025", "2026"])
        for name in ("seasons_history.json", "franchises_history.json", "matchups_history.json", "brackets_history.json",
                     "playoffs_history.json", "h2h_matrix.json", "draft_history.json", "exact_draft_slots.json",
                     "trades_history.json", "waivers_market_classified.json", "synced_transactions.json"):
            self.assertTrue(os.path.isfile(os.path.join(self.kb, name)), name)
        self.assertEqual({k: v["league_id"] for k, v in self._json("seasons_history.json").items()},
                         {"2025": "9001", "2026": "9002"})

    def test_matchups_skip_unplayed_weeks_and_mark_playoffs(self):
        games = self._json("matchups_history.json")
        by_season = {}
        for g in games:
            by_season.setdefault(g["season"], set()).add(g["week"])
            self.assertLess(g["roster_a"], g["roster_b"])          # lower roster first, like the weekly sync
        self.assertEqual(by_season["2025"], {1, 2, 3, 4})
        self.assertEqual(by_season["2026"], {1, 2, 3})             # NFL week 4 is running: only finished weeks
        self.assertTrue(all(g["is_playoff"] == (g["week"] >= 3) for g in games if g["season"] == "2025"))

    def test_playoffs_title_comes_from_the_official_bracket(self):
        title = self._json("playoffs_history.json")["2025"]["playoffs_title"]
        self.assertIn("Ana_Silva", title["champion"])               # roster 1 won the final
        self.assertIn("BrunoCosta", title["runner_up"])
        self.assertIn("DiegoRocha", title["third_place"])

    def test_trades_carry_players_picks_faab_and_resolve_used_picks(self):
        trades = self._json("trades_history.json")["trades"]
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual((t["season"], t["week"], t["date"]), ("2025", 1, "2025-06-15"))
        kinds = sorted(a["kind"] for s in t["sides"] for a in s["assets"])
        self.assertEqual(kinds, ["faab", "pick", "player", "player"])
        pick = next(a for s in t["sides"] for a in s["assets"] if a["kind"] == "pick")
        self.assertEqual(pick["status"], "used")                    # the 2025 draft already happened
        self.assertIn("Rookie", pick["used_player"])

    def test_roster_dossier_and_player_career_exist_for_the_new_league(self):
        self.assertTrue(os.path.isfile(os.path.join(self.kb, "TEAMS_AND_ROSTERS_ANALYSIS.md")))
        with open(os.path.join(self.kb, "TEAMS_AND_ROSTERS_ANALYSIS.md"), encoding="utf-8") as f:
            md = f.read()
        self.assertIn("Liga Falsa", md)
        self.assertNotIn("SUPER_FLEX", md)                   # this league's slots, not league #1's
        with league_ctx.use(self.ctx):
            con = kb_db.connect()
            try:
                out = kb_queries.player_report(con, "Rookie 1", "Time Ana")
            finally:
                con.close()
        self.assertIn("DOSSIÊ NA LIGA: ROOKIE 1", out)
        self.assertIn("Draft 2025", out)
        self.assertIn("Time Ana", out)

    def test_history_is_not_re_announced(self):
        self.assertEqual(self._json("synced_transactions.json")["transactions"], ["t1", "w1"])

    def test_the_index_and_the_readers_work_on_the_new_league(self):
        with league_ctx.use(self.ctx):
            con = kb_db.connect()
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 1)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM games").fetchone()[0], 0)
                out = kb_queries.trades_report(con, "Ana")
                self.assertIn("1 de 1 trocas", out)
                self.assertIn("2025–hoje", out)                       # first season comes from the data
                self.assertIn("RAIO-X DO DRAFT 2025", kb_queries.draft_picks_report(con, "2025"))
                self.assertIn("LIGA FALSA", kb_queries.records_report(con))     # the league's own name, not another's
            finally:
                con.close()

    def test_rivalries_and_standings_cover_only_what_the_league_has(self):
        import knowledge_service
        with league_ctx.use(self.ctx):
            ks = knowledge_service.KnowledgeBaseService()
            rivals = ks.get_manager_rivalries("Ana")
            text = ks.get_standings_formatted("2025")
        self.assertEqual(len(rivals), 3)                                   # 4 teams -> 3 opponents, not 11
        self.assertNotIn("None", text)                                     # a 4-team bracket has no 5th/6th place
        self.assertIn("CAMPEÃO DA LIGA", text)
        self.assertNotIn("TOILET BOWL", text)                              # the fake league has no consolation games

    def test_db_lives_in_the_leagues_own_directory(self):
        with league_ctx.use(self.ctx):
            kb_db.connect().close()
            self.assertEqual(kb_db.db_path(), os.path.join(self.kb, "league.db"))
        self.assertTrue(os.path.isfile(os.path.join(self.kb, "league.db")))
        self.assertNotEqual(os.path.dirname(kb_db.db_path()), self.kb)   # outside `use`: the default league's


class TwoLeaguesAreIsolatedTest(unittest.TestCase):
    def test_a_missing_file_never_falls_back_to_another_leagues_data(self):
        import knowledge_service
        with tempfile.TemporaryDirectory() as tmp:
            home = fx.make_home(tmp, "vazia", "555")               # a league with no knowledge base yet
            league_ctx.reset_cache()
            ctx = league_ctx.for_home(home)
            with league_ctx.use(ctx):
                kb = knowledge_service.KnowledgeBaseService()
                self.assertEqual(kb.load_franchises(), {})
                self.assertIsNone(kb._load_json("matchups_history.json"))
                for path in kb._get_candidate_paths("matchups_history.json"):
                    self.assertTrue(path.startswith(home), path)   # nothing outside the league's own home
            league_ctx.reset_cache()


if __name__ == "__main__":
    unittest.main()
