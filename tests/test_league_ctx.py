"""Multi-league core: rules come from Sleeper, data/secrets/people are per league, and leagues never see each other.

Run:  python3 -m unittest discover -s tests -p "test_league_ctx.py" -v      (offline)
"""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import league_fixtures as fx
from league_fixtures import league_json, users_rosters, fetcher

import league_ctx
import league_profile
import people
from league_profile import LeagueProfile


class LeagueProfileTest(unittest.TestCase):
    def test_dynasty_superflex_league(self):
        p = LeagueProfile.from_sleeper(league_json("A", "Dynasty", fx.DYNASTY_SLOTS, 12, type=2, waiver_budget=200,
                                                   waiver_type=2, taxi_slots=5, reserve_slots=5, divisions=3))
        self.assertEqual(p.startable, frozenset({"QB", "RB", "WR", "TE"}))
        self.assertEqual(p.flex_positions, frozenset({"QB", "RB", "WR", "TE"}))
        self.assertEqual(p.n_starters, 11)
        self.assertEqual(p.bench_slots, 19)
        self.assertTrue(p.superflex and p.dynasty and p.faab)
        self.assertEqual((p.has_kicker, p.has_defense, p.has_idp), (False, False, False))
        self.assertFalse(p.can_start("K"))
        self.assertFalse(p.can_start("DEF"))
        self.assertFalse(p.can_start("LB"))
        self.assertEqual(p.n_qb_slots, 2)
        self.assertEqual(p.matchups_per_week, 6)
        self.assertEqual(p.regular_season_weeks, 14)

    def test_redraft_with_kicker_and_defense(self):
        p = LeagueProfile.from_sleeper(league_json("B", "Redraft", fx.REDRAFT_SLOTS, 10, reserve_slots=2))
        self.assertTrue(p.can_start("K") and p.can_start("DEF"))
        self.assertEqual(p.flex_positions, frozenset({"RB", "WR", "TE"}))   # plain FLEX: no QB
        self.assertFalse(p.superflex)
        self.assertEqual(p.n_qb_slots, 1)
        self.assertFalse(p.dynasty or p.faab)
        self.assertEqual(p.matchups_per_week, 5)

    def test_idp_best_ball_league_and_position_aliases(self):
        p = LeagueProfile.from_sleeper(league_json("C", "IDP", fx.IDP_SLOTS, 14, best_ball=1))
        self.assertTrue(p.has_idp and p.best_ball)
        self.assertTrue(p.can_start("DE") and p.can_start("CB") and p.can_start("OLB"))   # aliases fold into DL/DB/LB
        self.assertEqual(p.slot_positions("REC_FLEX"), frozenset({"WR", "TE"}))
        self.assertEqual(p.slot_positions("IDP_FLEX"), frozenset({"DL", "LB", "DB"}))
        self.assertIn("Best ball", " ".join(p.describe_pt()))

    def test_describe_pt_names_what_is_missing(self):
        text = " ".join(LeagueProfile.from_sleeper(league_json("A", "D", fx.DYNASTY_SLOTS, 12)).describe_pt())
        self.assertIn("NÃO há vaga para: kicker (K), defesa (DEF), IDP", text)
        self.assertIn("3 FLEX (RB/WR/TE)", text)
        text_k = " ".join(LeagueProfile.from_sleeper(league_json("B", "R", fx.REDRAFT_SLOTS, 10)).describe_pt())
        self.assertIn("NÃO há vaga para: IDP", text_k)         # K and DEF do have a slot here: only IDP is missing
        self.assertNotIn("kicker", text_k)

    def test_json_round_trip_and_first_season_has_no_previous(self):
        p = LeagueProfile.from_sleeper(league_json("A", "Dynasty", fx.DYNASTY_SLOTS, 12, type=2))
        self.assertEqual(p.previous_league_id, "")            # Sleeper sends "0" for a league's first season
        again = LeagueProfile.from_dict(json.loads(json.dumps(p.to_dict())))
        self.assertEqual(again, p)


    def test_playoff_rounds_and_last_week_follow_the_leagues_bracket(self):
        def prof(teams, start, round_type=0):
            return LeagueProfile.from_sleeper(league_json("P", "L", fx.REDRAFT_SLOTS, 12, playoff_teams=teams,
                                                          playoff_week_start=start, playoff_round_type=round_type))
        self.assertEqual((prof(6, 15).playoff_rounds, prof(6, 15).playoff_week_end), (3, 17))   # league #1: weeks 15-17
        self.assertEqual((prof(4, 3).playoff_rounds, prof(4, 3).playoff_week_end), (2, 4))
        self.assertEqual(prof(8, 14).playoff_week_end, 16)
        self.assertEqual(prof(4, 14, round_type=1).playoff_week_end, 17)       # two weeks per round
        self.assertEqual(prof(0, 0).playoff_week_end, 0)                       # no playoff schedule: nothing invented


class BracketLabelsTest(unittest.TestCase):
    def test_consolation_spots_follow_the_leagues_size(self):
        import h2h_builder as hb
        self.assertEqual(hb.stage_of("losers", 2, 1)[1], "Toilet Bowl — 7º/8º lugar — final do Toilet Bowl")  # league #1
        self.assertEqual(hb.stage_of("losers", 2, 5)[1], "Toilet Bowl — 11º/12º lugar — disputa da lanterna")
        self.assertEqual(hb.stage_of("losers", 2, 1, first_spot=5, last_spot=8)[1],
                         "Toilet Bowl — 5º/6º lugar — final do Toilet Bowl")
        self.assertEqual(hb.stage_of("losers", 2, 3, first_spot=5, last_spot=8)[1],
                         "Toilet Bowl — 7º/8º lugar — disputa da lanterna")
        self.assertEqual(hb.stage_of("winners", 3, 7)[0], hb.FIFTH)             # an 8-team bracket's 7th-place game
        self.assertIn("7º", hb.stage_of("winners", 3, 7)[1])


class ContextPathsAndSecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        league_ctx.reset_cache()

    def test_new_league_data_lives_in_its_own_home(self):
        home = fx.make_home(self.tmp.name, "liga-x", "999")
        ctx = league_ctx.for_home(home)
        self.assertFalse(ctx.legacy_layout)
        self.assertEqual(ctx.league_id, "999")
        self.assertEqual(ctx.kb_dir, os.path.join(home, "league", "knowledge_base"))
        self.assertEqual(ctx.state_path("dispatched_slots.json"), os.path.join(home, "league", "state", "dispatched_slots.json"))
        self.assertEqual(ctx.shared_dir, league_ctx.ENGINE_DIR)       # NFL-level caches stay shared
        self.assertTrue(ctx.media_dir("graficos").startswith(home))

    def test_legacy_league_keeps_its_paths(self):
        home = fx.make_home(self.tmp.name, "hsf", "1312694161610113024", {"legacy_layout": True})
        ctx = league_ctx.for_home(home)
        self.assertEqual(ctx.kb_dir, os.path.join(league_ctx.ENGINE_DIR, "knowledge_base"))
        self.assertEqual(ctx.state_path("dispatched_slots.json"), os.path.join(league_ctx.ENGINE_DIR, "dispatched_slots.json"))

    def test_unconfigured_home_is_the_single_league_setup(self):
        bare = os.path.join(self.tmp.name, "bare")
        os.makedirs(bare)
        with mock.patch.dict(os.environ, {"SLEEPER_LEAGUE_ID": "424242"}):
            ctx = league_ctx.for_home(bare)
        self.assertTrue(ctx.legacy_layout)
        self.assertEqual(ctx.league_id, "424242")

    def test_private_settings_never_leak_between_leagues(self):
        a = fx.make_home(self.tmp.name, "a", "1", env={"LEAGUE_ADMINS": "5511111", "ADMIN_ALERT_JID": "5511111@s.whatsapp.net"})
        b = fx.make_home(self.tmp.name, "b", "2", env={"LEAGUE_ADMINS": "5522222"})
        with mock.patch.dict(os.environ, {"LEAGUE_ADMINS": "5599999", "ADMIN_ALERT_JID": "5599999@s.whatsapp.net",
                                          "EVOLUTION_ADMINS": "5599999"}):
            ca, cb = league_ctx.for_home(a), league_ctx.for_home(b)
            self.assertEqual(ca.admins, ["5511111"])
            self.assertEqual(cb.admins, ["5522222"])
            self.assertEqual(cb.alert_jid, "")        # B has none: it must NOT inherit A's nor the process env's
            self.assertEqual(cb.dm_allowed, [])

    def test_league_one_reads_the_container_environment(self):
        home = fx.make_home(self.tmp.name, "hsf", "1", {"legacy_layout": True})
        with mock.patch.dict(os.environ, {"EVOLUTION_ADMINS": "5548000", "WHATSAPP_GROUP_JID": "g1@g.us"}):
            ctx = league_ctx.for_home(home)
            self.assertEqual(ctx.admins, ["5548000"])
            self.assertEqual(ctx.group_jid, "g1@g.us")

    def test_schedule_overrides_and_house_rules(self):
        home = fx.make_home(self.tmp.name, "x", "3", {"schedule": {"tue_08h_power": "09:30"}, "disable_slots": ["wed_08h_waiver_radar"],
                                                      "rules": {"draft_order": "max_pf"}})
        ctx = league_ctx.for_home(home)
        self.assertEqual(ctx.slot_time("tue_08h_power"), "09:30")
        self.assertIsNone(ctx.slot_time("tue_08h_recap"))
        self.assertFalse(ctx.slot_enabled("wed_08h_waiver_radar"))
        self.assertTrue(ctx.slot_enabled("tue_08h_recap"))
        self.assertEqual(ctx.rule("draft_order"), "max_pf")
        self.assertIsNone(ctx.rule("nope"))


class ContextRulesCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        league_ctx.reset_cache()
        self.users, self.rosters = users_rosters(10, ["JonathanMachado17", "ana_sil", "RafaSteinwandter"] +
                                                 [f"Mgr{i}" for i in range(4, 11)])

    def _ctx(self, calls, league=None):
        home = fx.make_home(self.tmp.name, "x", "777")
        lg = league or league_json("777", "Liga X", fx.REDRAFT_SLOTS, 10)
        c = league_ctx.LeagueContext(home=home, config={"slug": "x", "sleeper_league_id": "777"},
                                     legacy_layout=False, fetch=fetcher(lg, self.users, self.rosters, calls))
        return c

    def test_profile_is_fetched_once_and_cached_on_disk(self):
        calls = []
        c = self._ctx(calls)
        self.assertEqual(c.profile.name, "Liga X")
        self.assertTrue(c.profile.can_start("K"))
        self.assertEqual(len(calls), 1)
        c2 = self._ctx(calls)                      # a fresh process: served from the 6 h disk cache
        self.assertEqual(c2.profile.name, "Liga X")
        self.assertEqual(len(calls), 1)
        self.assertTrue(os.path.isfile(c.state_path(league_ctx.REMOTE_FILE)))

    def test_cache_written_for_another_league_id_is_ignored(self):
        calls = []
        c = self._ctx(calls)
        _ = c.profile
        with open(c.state_path(league_ctx.REMOTE_FILE), encoding="utf-8") as f:
            blob = json.load(f)
        blob["profile"]["league_id"] = "OTHER"
        with open(c.state_path(league_ctx.REMOTE_FILE), "w", encoding="utf-8") as f:
            json.dump(blob, f)
        c2 = self._ctx(calls)
        _ = c2.profile
        self.assertEqual(len(calls), 2)

    def test_sleeper_down_falls_back_to_cache_then_to_a_flagged_default(self):
        calls = []
        c = self._ctx(calls)
        _ = c.profile
        os.utime(c.state_path(league_ctx.REMOTE_FILE), None)
        with open(c.state_path(league_ctx.REMOTE_FILE), encoding="utf-8") as f:
            blob = json.load(f)
        blob["fetched_at"] = 0           # stale on disk
        with open(c.state_path(league_ctx.REMOTE_FILE), "w", encoding="utf-8") as f:
            json.dump(blob, f)

        def down():
            raise OSError("sleeper down")
        c2 = league_ctx.LeagueContext(home=c.home, config=c.config, legacy_layout=False, fetch=down)
        self.assertEqual(c2.profile.name, "Liga X")             # stale cache beats nothing
        fresh = os.path.join(self.tmp.name, "empty")
        c3 = league_ctx.LeagueContext(home=fresh, config={"slug": "e", "sleeper_league_id": "5"}, legacy_layout=False, fetch=down)
        self.assertTrue(c3.profile.estimated)                   # nothing known: flagged default, no exception

    def test_directory_and_people_from_sleeper(self):
        c = self._ctx([])
        self.assertEqual(c.directory[0]["display_name"], "JonathanMachado17")
        self.assertEqual(c.people.resolve("jonathan"), 1)
        self.assertEqual(c.people.resolve("Time JonathanMachado17"), 1)
        self.assertEqual(c.people.resolve("ana"), 2)
        self.assertEqual(c.people.resolve("3"), 3)              # roster number
        self.assertIsNone(c.people.resolve("ninguém"))
        self.assertIn("Time ana_sil", c.people.label(2))


class PeopleIsolationTest(unittest.TestCase):
    def setUp(self):
        league_ctx.reset_cache()

    def _ctx(self, handles, config=None, home="", env=None):
        users, rosters = users_rosters(len(handles), handles)
        c = league_ctx.LeagueContext(home=home or None, config=config or {"slug": "t", "sleeper_league_id": "1"},
                                     legacy_layout=False,
                                     profile=LeagueProfile.from_sleeper(league_json("1", "T", fx.REDRAFT_SLOTS, len(handles))),
                                     directory=league_ctx.build_directory(users, rosters))
        return c

    def test_curated_entries_win_and_the_rest_comes_from_sleeper(self):
        c = self._ctx(["alpha", "bravo", "charlie"],
                      {"slug": "t", "sleeper_league_id": "1",
                       "people": {2: {"name": "Beto", "username": "bravo", "team": "Bravos", "nicks": ["betão"]}}})
        self.assertEqual(c.people.resolve("betão"), 2)
        self.assertEqual(c.people.label(2), "Bravos (Beto)")
        self.assertEqual(c.people.resolve("alpha"), 1)          # not curated: from Sleeper
        self.assertEqual(c.people.resolve("charlie"), 3)

    def test_two_leagues_resolve_the_same_word_differently(self):
        a = self._ctx(["ZeCarlos", "Ana"])
        b = self._ctx(["Ana", "ZeCarlos"])
        self.assertEqual(a.people.resolve("ana"), 2)
        self.assertEqual(b.people.resolve("ana"), 1)
        with league_ctx.use(a):
            self.assertEqual(people.resolve("ana"), 2)
        with league_ctx.use(b):
            self.assertEqual(people.resolve("ana"), 1)

    def test_use_is_per_thread_and_restored(self):
        a, b = self._ctx(["Ana", "Beto"]), self._ctx(["Beto", "Ana"])
        seen = {}

        def run(name, ctx):
            with league_ctx.use(ctx):
                barrier.wait()                  # both threads are inside their own `use` at the same time
                seen[name] = people.resolve("ana")
        barrier = threading.Barrier(2)
        ts = [threading.Thread(target=run, args=("a", a)), threading.Thread(target=run, args=("b", b))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(seen, {"a": 1, "b": 2})
        before = league_ctx.current()
        with league_ctx.use(a):
            pass
        self.assertIs(league_ctx.current(), before)

    def test_phones_come_from_the_leagues_own_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = fx.make_home(tmp, "p", "1", env={"LEAGUE_PEOPLE_PHONES": "2:5548999990000"})
            other = fx.make_home(tmp, "q", "2", env={})
            with mock.patch.dict(os.environ, {"LEAGUE_PEOPLE_PHONES": "1:5548999990000"}):
                league_ctx.reset_cache()
                users, rosters = users_rosters(2, ["x", "y"])
                kw = dict(legacy_layout=False, directory=league_ctx.build_directory(users, rosters))
                cp = league_ctx.LeagueContext(home=home, config={"slug": "p"}, **kw)
                cq = league_ctx.LeagueContext(home=other, config={"slug": "q"}, **kw)
                self.assertEqual(cp.people.resolve_sender(["554899999-0000@s.whatsapp.net"]), 2)
                self.assertIsNone(cq.people.resolve_sender(["5548999990000"]))   # no leak from the env or from `p`

    def test_auto_nicks(self):
        self.assertEqual(people.auto_nicks("JonathanMachado17", "Arnhem Miners"), ["jonathan", "machado", "jonathan machado", "arnhem"])
        self.assertEqual(people.auto_nicks("marinh012", "MOC’s Outcasts"), ["marinh", "mocs"])
        self.assertNotIn("the", people.auto_nicks("TheKing", "The Kings"))


if __name__ == "__main__":
    unittest.main()
