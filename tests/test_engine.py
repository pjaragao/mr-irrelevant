"""Offline unit tests for the league engine (no network, nothing is sent).

Run:  python3 -m unittest discover -s tests -v      (from the repo root, Python 3.9+)
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

import admin_notes  # noqa: E402
import league_time  # noqa: E402
import liga_core  # noqa: E402
import people  # noqa: E402
import players_db  # noqa: E402
from evolution_client import sent_ok  # noqa: E402

BRT = ZoneInfo("America/Sao_Paulo")


class PeopleTest(unittest.TestCase):
    def test_nicknames_and_whatsapp_names(self):
        cases = {
            "Ari": 7, "Paulo José Aragão Júnior": 4, "Yan Riedel": 8, "Léo": 2, "Brunão": 11, "Lula": 9,
            "Mário": 1, "Chico": 6, "Jony": 12, "MOc": 1, "time do Brunão": 11, "Bayer Kaxinawás": 11, "4": 4,
            "Paulo_Aragao_Jr": 4, "riedelots": 8,
        }
        for query, rid in cases.items():
            self.assertEqual(people.resolve(query), rid, query)

    def test_no_substring_false_positive(self):
        # "ari" is inside "marinh012" — must never resolve to roster 1.
        self.assertEqual(people.resolve("ari"), 7)
        self.assertIsNone(people.resolve("Felipe"))  # ambiguous: Chevarria × Luiz Felipe
        self.assertIsNone(people.resolve("xyz"))

    def test_sender_by_phone_with_ninth_digit(self):
        with mock.patch.dict(os.environ, {"LEAGUE_PEOPLE_PHONES": "4:5548999990001"}):
            self.assertEqual(people.resolve_sender(["554899990001@s.whatsapp.net"]), 4)
            self.assertEqual(people.resolve_sender(["5511900000000"], "Yan Riedel"), 8)


class LigaArgsTest(unittest.TestCase):
    def test_zero_and_empty_mean_missing(self):
        args = liga_core.normalize_args({"alvo": "", "alvo2": " ", "semana": 0, "limite": 0, "ano": "2026"})
        self.assertEqual(args, {"ano": "2026"})
        self.assertEqual(liga_core.normalize_args({"semana": "3", "limite": "5"}), {"semana": 3, "limite": 5})

    def test_target_required(self):
        out = liga_core.run_query("analise_confronto", {"alvo": ""})
        self.assertIn("precisa de `alvo`", out)
        self.assertIn("alvo2", liga_core.run_query("h2h", {"alvo": "Paulo"}))


class KickoffTest(unittest.TestCase):
    def test_us_dst_fallback(self):
        self.assertEqual(league_time.main_slate_fallback(datetime(2026, 10, 4).date()).hour, 14)
        self.assertEqual(league_time.main_slate_fallback(datetime(2026, 11, 1).date()).hour, 15)  # week 8
        self.assertEqual(league_time.main_slate_fallback(datetime(2026, 12, 6).date()).hour, 15)

    def test_international_game_is_early(self):
        games = [
            {"short": "IND VS WSH", "kickoff": datetime(2026, 10, 4, 10, 30, tzinfo=BRT), "state": "pre"},
            {"short": "BUF @ NO", "kickoff": datetime(2026, 10, 4, 14, 0, tzinfo=BRT), "state": "pre"},
            {"short": "SF @ DEN", "kickoff": datetime(2026, 10, 4, 17, 25, tzinfo=BRT), "state": "pre"},
        ]
        with mock.patch.object(league_time, "week_games", return_value=games):
            s = league_time.sunday_schedule(4, "2026")
        self.assertEqual(league_time.hhmm(s["first_kickoff"]), "10:30")
        self.assertEqual(league_time.hhmm(s["main_kickoff"]), "14:00")
        self.assertEqual([g["short"] for g in s["early_games"]], ["IND VS WSH"])


class SchedulerDispatchTest(unittest.TestCase):
    def setUp(self):
        import scheduler_service
        self.ss = scheduler_service
        self.tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.object(scheduler_service, "DISPATCH_LOG_FILE", os.path.join(self.tmp, "d.json")),
            mock.patch.object(scheduler_service, "FAILURES_FILE", os.path.join(self.tmp, "f.json")),
            mock.patch.object(scheduler_service.time, "sleep", lambda s: None),
        ]
        for p in self.patches:
            p.start()
        self.s = scheduler_service.WeeklyScheduler(group_jid="test@g.us")
        self.s._state = lambda: ("2026", 4)
        self.s._wave_slots = lambda week: []  # offline: wave reports are covered by SundayWavesTest
        main = datetime(2026, 10, 4, 14, 0, tzinfo=BRT)
        self.s._sunday = lambda week: {"main_kickoff": main, "first_kickoff": main, "early_games": []}
        for name in ("generate_sunday_waivers_recap", "generate_sunday_matchups_update", "generate_sunday_injuries",
                     "generate_sunday_trashtalk", "generate_sunday_inactives_alert"):
            setattr(self.s, name, (lambda *a, n=name, **k: f"msg {n}"))
        self.sent = []

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _ok(self, text):
        self.sent.append(text)
        return {"key": {"id": "ABC"}}

    def test_inactives_fires_after_trashtalk(self):
        self.s._send = self._ok
        r1 = self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 5, tzinfo=BRT))
        self.assertEqual(r1, ["sun_11h_trashtalk"])
        r2 = self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 35, tzinfo=BRT))
        self.assertEqual(r2, ["sun_11h30_inactives"])
        self.assertEqual(self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 40, tzinfo=BRT)), [])

    def test_failed_send_is_not_marked(self):
        self.s._send = lambda text: {"error": 500}
        self.assertEqual(self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 35, tzinfo=BRT)), [])
        self.assertEqual(self.ss.load_dispatched_slots(), {})
        self.s._send = self._ok
        self.assertEqual(self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 36, tzinfo=BRT)),
                         ["sun_11h_trashtalk", "sun_11h30_inactives"])

    def test_window_closed(self):
        self.s._send = self._ok
        self.assertEqual(self.s.check_and_dispatch(datetime(2026, 10, 4, 13, 31, tzinfo=BRT)), [])


class SchedulerExtrasTest(SchedulerDispatchTest):
    def test_early_window_gets_its_own_inactives_slot(self):
        london = datetime(2026, 10, 4, 10, 30, tzinfo=BRT)
        main = datetime(2026, 10, 4, 14, 0, tzinfo=BRT)
        self.s._sunday = lambda week: {"main_kickoff": main, "first_kickoff": london,
                                       "early_games": [{"kickoff": london, "home": "WAS", "away": "IND", "short": "IND VS WSH"}]}
        slots = {x[0]: x[1] for x in self.s.slot_table(datetime(2026, 10, 4, 7, 0, tzinfo=BRT), 4)}
        self.assertEqual(slots["sun_inactives_1030"].strftime("%H:%M"), "09:00")
        self.assertEqual(slots["sun_11h30_inactives"].strftime("%H:%M"), "12:30")

    def test_saturday_slot(self):
        keys = [x[0] for x in self.s.slot_table(datetime(2026, 10, 3, 20, 5, tzinfo=BRT), 4)]
        self.assertEqual(keys, ["sat_20h_lineup_check"])

    def test_multipart_resumes_without_duplicates(self):
        self.s.generate_sunday_trashtalk = lambda: "parte 1\n<<<MSG>>>\nparte 2\n<<<MSG>>>\nparte 3"
        calls = []

        def flaky(text):
            calls.append(text)
            return {"error": 500} if text == "parte 2" and calls.count("parte 2") == 1 else {"key": {"id": "X"}}
        self.s._send = flaky
        self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 5, tzinfo=BRT))
        self.s.check_and_dispatch(datetime(2026, 10, 4, 12, 10, tzinfo=BRT))
        self.assertEqual(calls, ["parte 1", "parte 2", "parte 2", "parte 3"])

    def test_tuesday_injuries_leave_the_08h_block(self):
        slots = {x[0]: x[1].strftime("%H:%M") for x in self.s.slot_table(datetime(2026, 10, 13, 7, 0, tzinfo=BRT), 6)}
        self.assertNotIn("tue_08h_round_injuries", slots)
        self.assertEqual(slots.pop("tue_10h_round_injuries"), "10:00")
        self.assertEqual(set(slots.values()), {"08:00"})  # the five other messages stay together at 08h

    def test_tuesday_dispatches_the_08h_block_then_injuries_at_10h(self):
        names = ("recap", "standings", "power", "playoff_odds", "draft_order")
        for n in names:
            setattr(self.s, f"generate_tuesday_{n}", (lambda n=n: f"msg {n}"))
        self.s._send = self._ok
        with mock.patch("round_injuries.format_report", return_value="msg injuries") as fmt:
            early = self.s.check_and_dispatch(datetime(2026, 10, 13, 8, 30, tzinfo=BRT))
            self.assertEqual(early, [f"tue_08h_{n}" for n in names])
            self.assertEqual(self.sent[-1], "msg draft_order")
            self.assertEqual(self.s.check_and_dispatch(datetime(2026, 10, 13, 9, 30, tzinfo=BRT)), [])
            late = self.s.check_and_dispatch(datetime(2026, 10, 13, 10, 5, tzinfo=BRT))
        self.assertEqual(late, ["tue_10h_round_injuries"])
        fmt.assert_called_once_with(3)  # the round that just ended (state week 4 -> report week 3)


class WaiverRadarTest(unittest.TestCase):
    def test_only_players_that_can_start_in_this_league_are_suggested(self):
        from types import SimpleNamespace
        import scheduler_service
        players = {"1": {"name": "Hot WR", "pos": "WR", "team": "KC"}, "2": {"name": "Some Defense", "pos": "DEF", "team": "SEA"},
                   "3": {"name": "Kicker Guy", "pos": "K", "team": "DAL"}, "4": {"name": "Edge Guy", "pos": "DE", "team": "SF"},
                   "5": {"name": "Rostered RB", "pos": "RB", "team": "MIA"}, "6": {"name": "Hot TE", "pos": "TE", "team": "BAL"},
                   "7": {"name": "Jogador 7", "pos": "FLEX", "team": "NFL"}}
        s = scheduler_service.WeeklyScheduler(group_jid="test@g.us")
        asked = {}

        def trending(kind, limit=25):
            asked["limit"] = limit
            return [{"player_id": pid, "count": 1000} for pid in players]
        s.sleeper = SimpleNamespace(
            get_trending=trending, get_league=lambda: {"settings": {"waiver_budget": 200}},
            get_rosters=lambda: [{"roster_id": 1, "owner_id": "u", "players": ["5"], "settings": {}}],
            get_users=lambda: {"u": {"team_name": "Time A"}})
        s._player = lambda pid: players[pid]
        out = s.generate_wednesday_waiver_radar()
        self.assertIn("Hot WR", out)
        self.assertIn("Hot TE", out)
        for hidden in ("Some Defense", "Kicker Guy", "Edge Guy", "Rostered RB", "Jogador 7"):
            self.assertNotIn(hidden, out)
        self.assertEqual(asked["limit"], 100)  # K/DEF used to fill the default top 25: look deeper for real candidates


class DraftOrderSyncTest(unittest.TestCase):
    def test_sync_writes_the_kb_files_and_does_not_raise(self):
        # regression: the closing log line referenced `logger` and `root_path`, neither of which existed, so the
        # Tuesday draft-order slot died with NameError every week from 06/10 on (the files were written first)
        import draft_order_service
        board = {
            "board": {1: [{"pick": "1.01", "is_traded": False, "current_team": "A", "current_owner": "a",
                           "original_team": "A", "original_owner": "a", "original_max_pf": 1.5}], 2: []},
            "max_pf_standings": [{"draft_slot": 1, "team_name": "A", "owner_name": "a", "max_pf": 1.5, "real_pf": 1.0,
                                  "bench_pts": 0.5, "efficiency": 66.7, "record": "0-1"}],
            "manager_picks_count": {"a": {"team_name": "A", "r1": 1, "r2": 0, "total": 1}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            svc = draft_order_service.DraftOrderService(base_dir=tmp)
            with mock.patch.object(svc, "build_draft_board", return_value=board):
                md = svc.sync_draft_knowledge_base(season="2027")
            self.assertIn("Ordem Oficial do Draft 2027", md)
            kb = os.path.join(tmp, "knowledge_base")
            self.assertTrue(os.path.exists(os.path.join(kb, "DRAFT_ORDER_AND_MAX_PF.md")))
            with open(os.path.join(kb, "draft_order_projected.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["manager_picks_count"]["a"]["total"], 1)


class SundayWavesTest(unittest.TestCase):
    ET = ZoneInfo("America/New_York")

    def _g(self, y, mo, d, h, mi, state="pre"):
        return {"short": f"{h}:{mi}", "home": "A", "away": "B", "state": state,
                "kickoff": datetime(y, mo, d, h, mi, tzinfo=self.ET).astimezone(BRT)}

    def test_waves_by_new_york_hour_with_and_without_dst(self):
        for day, wave1_brt in (((2026, 10, 4), "14:00"), ((2026, 11, 1), "15:00")):
            games = [self._g(*day, 9, 30), self._g(*day, 13, 0), self._g(*day, 16, 5), self._g(*day, 16, 25),
                     self._g(*day, 20, 20)]
            with mock.patch.object(league_time, "week_games", return_value=games):
                w = league_time.sunday_waves(4, "2026")
            self.assertEqual(len(w["early"]["games"]), 1)
            self.assertEqual(league_time.hhmm(w["wave1"]["first"]), wave1_brt)
            self.assertEqual(len(w["wave2"]["games"]), 2)
            self.assertEqual(len(w["snf"]["games"]), 1)

    def test_wave_report_waits_for_final_games_or_deadline(self):
        import scheduler_service
        tmp = tempfile.mkdtemp()
        with mock.patch.object(scheduler_service, "DISPATCH_LOG_FILE", os.path.join(tmp, "d.json")), \
                mock.patch.object(scheduler_service, "FAILURES_FILE", os.path.join(tmp, "f.json")), \
                mock.patch.object(scheduler_service.time, "sleep", lambda s: None):
            s = scheduler_service.WeeklyScheduler(group_jid="test@g.us")
            s._state = lambda: ("2026", 4)
            s._sunday = lambda week: {"main_kickoff": datetime(2026, 10, 4, 14, 0, tzinfo=BRT),
                                      "first_kickoff": datetime(2026, 10, 4, 14, 0, tzinfo=BRT), "early_games": []}
            s.generate_sunday_wave_report = lambda stage: f"balanço {stage}"
            s._send = lambda text: {"key": {"id": "X"}}
            state = {"w1": "in"}

            def waves(week, season=None):
                w1 = [self._g(2026, 10, 4, 13, 0, state["w1"])]
                return {"early": None, "wave1": {"games": w1, "first": w1[0]["kickoff"], "last": w1[0]["kickoff"]},
                        "wave2": {"games": [self._g(2026, 10, 4, 16, 5)], "first": self._g(2026, 10, 4, 16, 5)["kickoff"],
                                  "last": self._g(2026, 10, 4, 16, 5)["kickoff"]}, "snf": None, "from_espn": True}
            with mock.patch.object(league_time, "sunday_waves", side_effect=waves):
                t = lambda h, m: datetime(2026, 10, 4, h, m, tzinfo=BRT)  # noqa: E731
                self.assertEqual(s.check_and_dispatch(t(16, 35)), [])           # wave 1 still running
                state["w1"] = "post"
                self.assertEqual(s.check_and_dispatch(t(16, 40)), ["sun_after_wave1"])
            tmp2 = tempfile.mkdtemp()
            with mock.patch.object(scheduler_service, "DISPATCH_LOG_FILE", os.path.join(tmp2, "d.json")), \
                    mock.patch.object(league_time, "sunday_waves", side_effect=waves):
                state["w1"] = "in"  # overtime: the deadline (5 min before wave 2) forces the message
                self.assertEqual(s.check_and_dispatch(datetime(2026, 10, 4, 17, 0, tzinfo=BRT)), ["sun_after_wave1"])

    def test_post_snf_slot_also_runs_monday_early(self):
        import scheduler_service
        s = scheduler_service.WeeklyScheduler(group_jid="test@g.us")
        snf = self._g(2026, 10, 4, 20, 20)
        fake = {"early": None, "wave1": None, "wave2": None, "from_espn": True,
                "snf": {"games": [snf], "first": snf["kickoff"], "last": snf["kickoff"]}}
        with mock.patch.object(league_time, "sunday_waves", return_value=fake):
            keys = [x[0] for x in s.slot_table(datetime(2026, 10, 5, 0, 30, tzinfo=BRT), 4)]
        self.assertIn("sun_after_snf", keys)


class LayoutTest(unittest.TestCase):
    def test_split_long_message(self):
        from msg_format import Msg, split_parts
        m = Msg("Teste", "sub", emoji="🏈")
        for i in range(5):
            m.section(f"S{i}", [f"• item {j}" for j in range(8)])
        parts = split_parts(m.footer("fim").render(max_lines=28))
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith("*🏈 TESTE* (1/"))
        self.assertTrue(parts[-1].endswith("_fim_"))
        self.assertTrue(all(len(p.splitlines()) <= 28 for p in parts))

    def test_pick_parsing(self):
        import trade_values as tv
        self.assertEqual(tv._pick_name("2027 1st early"), "2027 1st (Early)")
        self.assertEqual(tv._pick_name("pick 1ª 2028"), "2028 1st")
        self.assertEqual(tv._pick_name("primeira rodada de 2027"), "2027 1st")
        self.assertIsNone(tv._pick_name("Josh Allen"))


class SmallHelpersTest(unittest.TestCase):
    def test_sent_ok(self):
        self.assertTrue(sent_ok({"key": {"id": "3EB0"}}))
        self.assertFalse(sent_ok({"error": 400, "message": "x"}))
        self.assertFalse(sent_ok({}))

    def test_injury_designation(self):
        self.assertEqual(players_db.injury({"injury_status": "Out"}), "Out")
        self.assertEqual(players_db.injury({"status": "Injured Reserve"}), "IR")
        self.assertIsNone(players_db.injury({"status": "Active", "injury_status": None}))
        self.assertIsNone(players_db.injury({"status": "Inactive"}))

    def test_live_master_overrides_stale_status(self):
        tmp = tempfile.mkdtemp()
        for name, inj in (("league_players.json", "Out"), ("master_nfl_players.json", None)):
            with open(os.path.join(tmp, name), "w") as f:
                json.dump({"1": {"name": "X", "pos": "RB", "team": "NO", "status": "Active", "injury_status": inj}}, f)
        with mock.patch.object(players_db, "BASE_DIR", tmp), \
                mock.patch.object(players_db, "MASTER_FILE", os.path.join(tmp, "master_nfl_players.json")):
            players_db._cache.update(mtime=None, db=None)
            self.assertIsNone(players_db.load()["1"]["injury_status"])
        players_db._cache.update(mtime=None, db=None)

    def test_admin_note_command(self):
        self.assertEqual(admin_notes.parse_command("anota aí: X é Y"), "X é Y")
        self.assertEqual(admin_notes.parse_command("anota aí que o Brunão é o comissário"), "o Brunão é o comissário")
        self.assertIsNone(admin_notes.parse_command("anotaram tudo?"))


if __name__ == "__main__":
    unittest.main()
