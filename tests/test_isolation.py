"""Two leagues in ONE process (what the multiplexed gateway does): nothing one writes, caches or reads may reach
the other. Every place of the engine that keeps state is exercised under two different league contexts.

Run:  python3 -m unittest discover -s tests -p "test_isolation.py" -v
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import league_fixtures as fx
from league_fixtures import league_json, users_rosters

import admin_notes
import flavor
import kb_db
import league_ctx
import liga_core
import players_db
import round_injuries
import scheduler_service
import song
import chart_service
from league_profile import LeagueProfile


def make_league(base, slug, league_id, handles, slots=None, config=None, **settings):
    cfg = {"slug": slug, "sleeper_league_id": league_id, "persona": {"name": f"Bot {slug}"}, "group_jid": f"{slug}@g.us"}
    cfg.update(config or {})
    home = fx.make_home(base, slug, league_id, {k: v for k, v in cfg.items() if k not in ("slug", "sleeper_league_id")})
    users, rosters = users_rosters(len(handles), handles, league_id)
    prof = LeagueProfile.from_sleeper(league_json(league_id, f"Liga {slug}", slots or fx.REDRAFT_SLOTS, len(handles),
                                                  **settings))
    return league_ctx.LeagueContext(home=home, config=cfg, legacy_layout=False, profile=prof,
                                    directory=league_ctx.build_directory(users, rosters))


class IsolationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        league_ctx.reset_cache()
        self.a = make_league(self.tmp.name, "alfa", "1001", ["Ana", "Beto", "Caio", "Dani"])
        self.b = make_league(self.tmp.name, "beta", "2002", ["Eva", "Fábio", "Gil", "Hugo"], fx.DYNASTY_SLOTS)

    def test_admin_notes_are_per_league(self):
        with league_ctx.use(self.a):
            admin_notes.save_note("o comissário da alfa é a Ana")
        with league_ctx.use(self.b):
            self.assertEqual(admin_notes.read_notes(), "")
            admin_notes.save_note("a beta joga com kicker")
        with league_ctx.use(self.a):
            self.assertIn("alfa", admin_notes.read_notes())
            self.assertNotIn("kicker", admin_notes.read_notes())

    def test_dispatch_state_never_crosses(self):
        with mock.patch.object(scheduler_service, "DISPATCH_LOG_FILE", None):
            with league_ctx.use(self.a):
                scheduler_service.save_dispatched_slot("2026_W1_tue_08h_recap")
                self.assertIn("2026_W1_tue_08h_recap", scheduler_service.load_dispatched_slots())
            with league_ctx.use(self.b):
                self.assertEqual(scheduler_service.load_dispatched_slots(), {})   # b would otherwise skip its recap

    def test_scheduler_uses_each_leagues_own_slots_group_and_persona(self):
        with league_ctx.use(self.a):
            sa = scheduler_service.WeeklyScheduler()
        with league_ctx.use(self.b):
            sb = scheduler_service.WeeklyScheduler()
        self.assertEqual((sa.league_id, sa.group_jid, sb.league_id, sb.group_jid),
                         ("1001", "alfa@g.us", "2002", "beta@g.us"))
        self.assertEqual(sa.profile.matchups_per_week, 2)         # 4 teams
        self.assertFalse(sb.profile.can_start("K"))               # beta is a superflex league without K
        self.assertTrue(sa.profile.can_start("K"))

    def test_tool_cache_is_keyed_by_league(self):
        calls = []

        def fake_cli(argv):
            calls.append(league_ctx.current().slug)
            return f"tabela da {league_ctx.current().slug}"
        liga_core._cache.clear()
        with mock.patch.object(liga_core, "_run_cli", side_effect=fake_cli):
            with league_ctx.use(self.a):
                out_a = liga_core.run_query("tabela", {})
            with league_ctx.use(self.b):
                out_b = liga_core.run_query("tabela", {})
            with league_ctx.use(self.a):
                again = liga_core.run_query("tabela", {})        # served from a's own cache
        self.assertIn("alfa", out_a)
        self.assertIn("beta", out_b)
        self.assertEqual(out_a, again)
        self.assertEqual(calls, ["alfa", "beta"])

    def test_player_db_merges_each_leagues_own_files(self):
        for ctx, name in ((self.a, "Jogador da Alfa"), (self.b, "Jogador da Beta")):
            os.makedirs(ctx.kb_dir, exist_ok=True)
            with open(os.path.join(ctx.kb_dir, "league_players.json"), "w", encoding="utf-8") as f:
                json.dump({"p1": {"name": name, "pos": "RB"}}, f)
        with league_ctx.use(self.a):
            self.assertEqual(players_db.get("p1")["name"], "Jogador da Alfa")
        with league_ctx.use(self.b):
            self.assertEqual(players_db.get("p1")["name"], "Jogador da Beta")
        with league_ctx.use(self.a):
            self.assertEqual(players_db.get("p1")["name"], "Jogador da Alfa")

    def test_index_and_trade_log_are_per_league(self):
        info = {"transaction_id": "T1", "date_str": "07/10/2026", "season": "2026", "week": 5, "sides": [
            {"manager_name": "Ana", "team_name": "Time Ana", "player_details_received": [], "picks_received": [],
             "faab_received": 3}]}
        with league_ctx.use(self.a):
            self.assertTrue(kb_db.append_trade(info))
            self.assertEqual(len(kb_db.trades_from_file()), 1)
            path_a = kb_db.db_path()
        with league_ctx.use(self.b):
            self.assertEqual(kb_db.trades_from_file(), [])
            self.assertNotEqual(kb_db.db_path(), path_a)
            self.assertTrue(kb_db.append_trade(info))               # same tx id is a different league's own trade

    def test_round_injury_cache_belongs_to_the_league(self):
        with league_ctx.use(self.a):
            fa = round_injuries._cache_file()
        with league_ctx.use(self.b):
            fb = round_injuries._cache_file()
        self.assertNotEqual(fa, fb)
        self.assertTrue(fa.startswith(self.a.home) and fb.startswith(self.b.home))

    def test_flavor_cache_and_prompt_are_per_league(self):
        with league_ctx.use(self.a):
            flavor._save({"k": "linha da alfa"})
            self.assertIn("Bot alfa", flavor.persona_prompt())
            self.assertIn("Time Ana", flavor._tone_example())          # the example uses THIS league's teams
        with league_ctx.use(self.b):
            self.assertEqual(flavor._load(), {})
            self.assertIn("Bot beta", flavor.persona_prompt())
            self.assertNotIn("Time Ana", flavor._tone_example())

    def test_media_output_dirs_are_per_league(self):
        with mock.patch.dict(os.environ, {"LIGA_CHARTS_DIR": "", "LIGA_SONGS_DIR": ""}):
            with league_ctx.use(self.a):
                ca, sa = chart_service.out_dir(), song.out_dir()
            with league_ctx.use(self.b):
                cb, sb = chart_service.out_dir(), song.out_dir()
        self.assertTrue(ca.startswith(self.a.home) and sa.startswith(self.a.home))
        self.assertTrue(cb.startswith(self.b.home) and sb.startswith(self.b.home))

    def test_standings_footer_and_division_icons_belong_to_the_league(self):
        from sleeper_client import SleeperClient
        big = make_league(self.tmp.name, "grande", "3003", [f"M{i}" for i in range(1, 13)], playoff_teams=6,
                          playoff_week_start=15, config={"division_icons": {"1": "🔥"}})
        kb = os.path.join(big.kb_dir, "brackets_history.json")
        os.makedirs(big.kb_dir, exist_ok=True)
        with open(kb, "w", encoding="utf-8") as f:
            json.dump({"2025": {"games": [{"bracket": "losers", "week": 15}]}}, f)
        with league_ctx.use(big):
            text = "\n".join(SleeperClient._postseason_structure_lines())
        self.assertIn("SEMANAS 15 A 17", text)
        self.assertIn("Top 6", text)
        self.assertIn("7º ao 12º", text)                                   # consolation: it has losers-bracket history
        with league_ctx.use(self.a):                                       # 4 teams, no consolation history
            small = "\n".join(SleeperClient._postseason_structure_lines())
        self.assertIn("Top 4", small)
        self.assertNotIn("Toilet", small)
        self.assertNotIn("Top 6", small)
        self.assertFalse(self.a.has_consolation())
        self.assertTrue(big.has_consolation())
        ruled = make_league(self.tmp.name, "regra", "4004", ["A", "B", "C", "D"], config={"rules": {"toilet_bowl": True}})
        self.assertTrue(ruled.has_consolation())                           # the house rule decides when it is set

    def test_not_found_hints_use_the_leagues_own_names(self):
        import kb_queries
        with league_ctx.use(self.a):
            con = kb_db.connect()
            try:
                msg = kb_queries.profile_report(con, "zzzz")
            finally:
                con.close()
        self.assertIn("Ana", msg)
        self.assertNotIn("MOC", msg)
        self.assertNotIn("Panda", msg)
        self.assertIn("Eva", self.b.people.hint())
        self.assertNotIn("Ana", self.b.people.hint())

    def test_house_rules_gate_features_per_league(self):
        import draft_order_service
        a = league_ctx.LeagueContext(home=self.a.home, config={**self.a.config, "rules": {"draft_order": "max_pf"}},
                                     legacy_layout=False, profile=self.a.profile)
        with league_ctx.use(a):
            self.assertTrue(draft_order_service.DraftOrderService().uses_max_pf())
        with league_ctx.use(self.b):
            self.assertFalse(draft_order_service.DraftOrderService().uses_max_pf())


if __name__ == "__main__":
    unittest.main()
