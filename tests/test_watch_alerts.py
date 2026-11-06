"""Live score DM, lineup advisor schedule/analysis and the pause switches (offline, no network, no model)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "league"))

import league_profile  # noqa: E402
import live_score  # noqa: E402
import lineup_advisor  # noqa: E402
import watch_prefs  # noqa: E402

BRT = ZoneInfo("America/Sao_Paulo")


def ko(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=BRT)


class CommandTest(unittest.TestCase):
    def test_parse(self):
        p = watch_prefs.parse_command
        self.assertEqual(p("parar placar"), {"action": "stop", "kinds": ["placar"]})
        self.assertEqual(p("Continuar o placar!"), {"action": "go", "kinds": ["placar"]})
        self.assertEqual(p("pausar escalação"), {"action": "stop", "kinds": ["escalacao"]})
        self.assertEqual(p("parar notificações")["kinds"], ["placar", "escalacao"])
        self.assertIsNone(p("quem vai parar o Chase essa semana no placar do Mario?"))
        self.assertIsNone(p("qual o placar"))


class LiveScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        watch_prefs.is_paused = lambda kind: False
        self.ctx = SimpleNamespace(league_id="L", timezone="America/Sao_Paulo",
                                   profile=SimpleNamespace(scoring={"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0}),
                                   state_path=lambda *p: os.path.join(self.tmp, *p))
        self.players = {"A": {"name": "Ja'Marr Chase", "pos": "WR", "team": "CIN"},
                        "B": {"name": "Saquon Barkley", "pos": "RB", "team": "PHI"},
                        "C": {"name": "Bench", "pos": "WR", "team": "CIN"}}
        self.games = [{"home": "CIN", "away": "PIT", "state": "in", "kickoff": ko(2026, 10, 18, 14)},
                      {"home": "PHI", "away": "DAL", "state": "in", "kickoff": ko(2026, 10, 18, 14)}]
        self.data = {}
        self.sent = []

    def make(self):
        raw = [{"roster_id": 1, "matchup_id": 1, "points": self.data.get("p1", 0.0), "starters": ["A"],
                "players_points": {"A": self.data.get("A", 0.0)}},
               {"roster_id": 2, "matchup_id": 1, "points": self.data.get("p2", 0.0), "starters": ["B"],
                "players_points": {"B": self.data.get("B", 0.0)}}]
        stats = {"A": self.data.get("sA", {}), "B": self.data.get("sB", {})}
        get = lambda ep: raw if "matchups" in ep else stats
        users = {"u1": {"team_name": "Meu Time"}, "u2": {"team_name": "Rival"}}
        rosters = [{"roster_id": 1, "owner_id": "u1"}, {"roster_id": 2, "owner_id": "u2"}]
        return live_score.LiveScore(ctx=self.ctx, get=get, games_fn=lambda w, s: self.games, players=self.players,
                                    users=users, rosters=rosters, send=lambda t: self.sent.append(t) or True,
                                    state_path=os.path.join(self.tmp, "ls.json"),
                                    targets=[{"rid": 1, "dms": ["5511999999999"]}])

    def test_plays_are_aggregated_one_player_per_line_and_gap_respected(self):
        t0 = 1_000_000.0
        self.make().tick("2026", 6, now=t0)                       # baseline + first scoreboard
        self.sent.clear()
        self.data.update(A=3.4, p1=3.4, sA={"rec": 2, "rec_yd": 14})
        self.make().tick("2026", 6, now=t0 + 300)                 # 5 min later: gap not reached, not big
        self.assertEqual(self.sent, [])
        self.data.update(A=14.4, p1=14.4, sA={"rec": 3, "rec_yd": 24, "rec_td": 1}, B=3.2, p2=3.2,
                         sB={"rec": 1, "rec_yd": 22})
        self.make().tick("2026", 6, now=t0 + 700)                 # big play → goes out with everything pending
        self.assertEqual(len(self.sent), 1)
        text = self.sent[0]
        self.assertIn("Ja'Marr Chase (WR·CIN) *+14,4*", text)     # accumulated across both intervals
        self.assertIn("3 recepções, 24 jds recebidos, 1 TD recebido", text)
        self.assertIn("Saquon Barkley (RB·PHI) *+3,2*", text)
        self.assertIn("Meu Time", text)
        self.assertIn("parar placar", text)
        self.assertEqual(text.count("Ja'Marr Chase"), 1)

    def test_heartbeat_every_30_min_and_quiet_between(self):
        t0 = 2_000_000.0
        self.make().tick("2026", 6, now=t0)
        n = len(self.sent)
        self.make().tick("2026", 6, now=t0 + 600)
        self.assertEqual(len(self.sent), n)                       # nothing new, < 30 min
        self.make().tick("2026", 6, now=t0 + 31 * 60)
        self.assertEqual(len(self.sent), n + 1)
        self.assertIn("Sem pontuação nova", self.sent[-1])

    def test_final_once_when_everyone_is_done(self):
        t0 = 3_000_000.0
        self.make().tick("2026", 6, now=t0)
        for g in self.games:
            g["state"] = "post"
        self.data.update(p1=20.0, p2=10.0)
        self.make().tick("2026", 6, now=t0 + 400)
        self.assertIn("FIM DO CONFRONTO", self.sent[-1])
        self.assertIn("Vitória", self.sent[-1])
        n = len(self.sent)
        self.make().tick("2026", 6, now=t0 + 800)
        self.assertEqual(len(self.sent), n)

    def test_failed_send_keeps_pending(self):
        t0 = 4_000_000.0
        self.make().tick("2026", 6, now=t0)
        self.data.update(A=9.0, p1=9.0, sA={"rec_td": 1})
        ls = self.make()
        ls._send_fn = lambda t: False
        ls.tick("2026", 6, now=t0 + 300)
        self.assertEqual(len(ls.load_state()["targets"]["1"]["pending"]), 1)
        self.make().tick("2026", 6, now=t0 + 600)
        self.assertIn("TD recebido", self.sent[-1])

    def test_describe(self):
        self.assertEqual(live_score.describe({"rush_yd": 12, "rush_td": 1, "fum_lost": 1}),
                         "1 TD corrido, 12 jds corridos, 1 fumble perdido")


def _profile():
    return league_profile.LeagueProfile.from_sleeper({
        "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "BN", "BN", "BN"],
        "settings": {"num_teams": 12, "playoff_teams": 4, "playoff_week_start": 15}, "scoring_settings": {"rec": 1.0}})


class LineupTest(unittest.TestCase):
    def test_schedule_thursday_sunday_monday_and_thanksgiving(self):
        g = [{"kickoff": ko(2026, 10, 15, 21, 15)},                                   # TNF
             {"kickoff": ko(2026, 10, 18, 10, 30)}, {"kickoff": ko(2026, 10, 18, 14)},  # Sun: London + main
             {"kickoff": ko(2026, 10, 19, 21, 15)}]                                   # MNF
        s = lineup_advisor.plan_slots(g, BRT)
        keys = [x["key"] for x in s]
        self.assertEqual(keys, ["2026-10-15_morning", "2026-10-15_pre_2115", "2026-10-18_morning",
                                "2026-10-18_pre_1030", "2026-10-19_morning", "2026-10-19_pre_2115"])
        self.assertEqual(s[1]["at"], ko(2026, 10, 15, 20, 15))
        self.assertEqual([x["conditional"] for x in s], [False, False, False, False, True, True])
        tg = [{"kickoff": ko(2026, 11, 26, 13, 30)}, {"kickoff": ko(2026, 11, 26, 17, 30)},
              {"kickoff": ko(2026, 11, 26, 21, 20)}]
        k = [x["key"] for x in lineup_advisor.plan_slots(tg, BRT)]
        self.assertEqual(k, ["2026-11-26_morning", "2026-11-26_pre_1330", "2026-11-26_pre_1730", "2026-11-26_pre_2120"])

    def test_early_game_skips_morning_when_too_close(self):
        s = lineup_advisor.plan_slots([{"kickoff": ko(2026, 12, 25, 9, 0)}], BRT)
        self.assertEqual([x["kind"] for x in s], ["pre"])

    def test_due_window(self):
        s = lineup_advisor.plan_slots([{"kickoff": ko(2026, 10, 15, 21, 15)}], BRT)
        self.assertEqual(len(lineup_advisor.due(s, {}, ko(2026, 10, 15, 8, 10))), 1)
        self.assertEqual(lineup_advisor.due(s, {}, ko(2026, 10, 15, 8, 40)), [])
        self.assertEqual(lineup_advisor.due(s, {s[0]["key"]: "x"}, ko(2026, 10, 15, 8, 10)), [])

    def _facts(self, status=None):
        db = {"q": {"name": "Qb Uno", "pos": "QB", "team": "AAA"},
              "r1": {"name": "Rb Um", "pos": "RB", "team": "BBB"}, "r2": {"name": "Rb Dois", "pos": "RB", "team": "CCC"},
              "w1": {"name": "Wr Um", "pos": "WR", "team": "DDD"}, "w2": {"name": "Wr Dois", "pos": "WR", "team": "EEE"},
              "t": {"name": "Te Um", "pos": "TE", "team": "FFF"}, "f": {"name": "Flex Um", "pos": "WR", "team": "GGG"},
              "b1": {"name": "Bench Rb", "pos": "RB", "team": "HHH"}, "b2": {"name": "Bench Wr", "pos": "WR", "team": "III"},
              "b3": {"name": "Bench Te", "pos": "TE", "team": "JJJ"}}
        games = {t: {"state": "pre", "kickoff": ko(2026, 10, 18, 14), "home": t, "away": "ZZZ"}
                 for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]}
        roster = {"starters": ["q", "r1", "r2", "w1", "w2", "t", "f"], "players": list(db)}
        proj = {k: {"pts_ppr": v} for k, v in {"q": 20, "r1": 15, "r2": 12, "w1": 14, "w2": 11, "t": 9, "f": 10,
                                               "b1": 8, "b2": 6, "b3": 5}.items()}
        inj = lambda p: (status or {}).get(p.get("name"), "")
        return lineup_advisor.build_facts(_profile(), roster, db, games, proj, inj, 3), db

    def test_autosubs_and_swaps_deterministic(self):
        f, _ = self._facts({"Rb Dois": "Out"})
        self.assertEqual([(s["out"], s["in"]) for s in f["swaps"]], [("Rb Dois", "Bench Rb")])
        self.assertTrue(f["needs_action"])
        # the bench RB is already the swap: the autosubs are the rest of the bench, never the same player twice
        self.assertEqual({a["name"] for a in f["autosubs"]}, {"Bench Wr", "Bench Te"})

    def test_incremental_prompt_and_validation(self):
        f, _ = self._facts({"Wr Um": "Questionable"})
        prev = {"at": "2026-10-15T08:00:10", "summary": "Mantém tudo.", "swaps": [], "autosubs": ["Bench Rb"],
                "snapshot": {"starters": f and [s["pid"] for s in f["starters"]], "status": {}, "bench": [], "started": []}}
        ch = lineup_advisor.changes_since(prev["snapshot"], f)
        self.assertIn("Wr Um: status saudável → Questionable", ch)
        p = lineup_advisor.prompt(f, "quinta, 1h antes", prev, ch, "Rival", 3)
        self.assertIn("Mantém tudo.", p)
        self.assertIn("até 3 pares", p)
        raw = ('{"veredito":"mudar","resumo":"Wr Um é dúvida.","trocas":[{"sai":"Wr Um","entra":"Bench Wr","motivo":"Q"},'
               '{"sai":"Fantasma","entra":"Bench Wr","motivo":"x"}],"autosubs":[{"jogador":"Bench Rb","cobre":"RBs"},'
               '{"jogador":"Inventado","cobre":"x"}]}')
        d = lineup_advisor.decide(f, raw, 3)
        self.assertEqual([t["sai"] for t in d["trocas"]], ["Wr Um"])           # hallucinated names dropped
        self.assertEqual(len(d["autosubs"]), 3)                                  # topped up to max_subs
        text = lineup_advisor.render(d, f, 6, "quinta", "Rival", prev, ch)
        self.assertIn("AutoSub", text)
        self.assertIn("O QUE MUDOU".lower(), text.lower())
        self.assertIn("parar escalação", text)

    def test_llm_down_uses_deterministic(self):
        f, _ = self._facts({"Rb Dois": "Out"})
        d = lineup_advisor.decide(f, "", 3)
        self.assertEqual(d["veredito"], "mudar")
        self.assertFalse(d["ai"])

    def test_tick_incremental_history_and_monday_silence(self):
        tmp = tempfile.mkdtemp()
        watch_prefs.is_paused = lambda kind: False
        f, db = self._facts()
        games = [{"home": t, "away": "ZZZ", "state": "pre", "kickoff": ko(2026, 10, 19, 21, 15)}
                 for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]]
        sleeper = SimpleNamespace(
            _get=lambda ep: {k: {"pts_ppr": 10.0} for k in db}, get_league=lambda: {"settings": {"max_subs": 3}},
            get_rosters=lambda: [{"roster_id": 1, "starters": ["q", "r1", "r2", "w1", "w2", "t", "f"],
                                  "players": list(db)}],
            get_matchups=lambda w: [])
        sent, prompts = [], []
        adv = lineup_advisor.LineupAdvisor(
            ctx=SimpleNamespace(timezone="America/Sao_Paulo", profile=_profile(),
                                state_path=lambda *p: os.path.join(tmp, *p)),
            sleeper=sleeper, llm_fn=lambda t: prompts.append(t) or "", send=lambda t: sent.append(t) or True,
            state_path=os.path.join(tmp, "l.json"), targets=[{"rid": 1, "dms": ["5511"]}],
            games_fn=lambda w, s: games, db=db)
        import players_db
        orig = players_db.injury
        players_db.injury = lambda p: ""
        try:
            out = adv.tick("2026", 6, now=ko(2026, 10, 19, 8, 5))     # Monday morning, nothing to change → silent
            self.assertEqual(out, [])
            self.assertEqual(len(prompts), 1)
            out = adv.tick("2026", 6, now=ko(2026, 10, 19, 8, 10))    # same slot never repeats
            self.assertEqual(len(prompts), 1)
            adv.tick("2026", 6, now=ko(2026, 10, 19, 20, 20))         # 1h before: prompt now carries the history
            self.assertEqual(len(prompts), 2)
            self.assertIn("Time titular sem pendências", prompts[1])
        finally:
            players_db.injury = orig

    def test_first_is_full_then_incremental_and_unapplied_swap_is_not_forgotten(self):
        tmp = tempfile.mkdtemp()
        watch_prefs.is_paused = lambda kind: False
        f, db = self._facts()
        games = [{"home": t, "away": "ZZZ", "state": "pre", "kickoff": ko(2026, 10, 15, 21, 15)}
                 for t in ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]]
        sleeper = SimpleNamespace(
            _get=lambda ep: {k: {"pts_ppr": 10.0} for k in db} if "projections" in ep else [],
            get_league=lambda: {"settings": {"max_subs": 3}},
            get_rosters=lambda: [{"roster_id": 1, "starters": ["q", "r1", "r2", "w1", "w2", "t", "f"],
                                  "players": list(db)}], get_matchups=lambda w: [])
        sent, prompts = [], []
        answers = iter([
            '{"veredito":"mudar","resumo":"R","adversario":"A","raio_x":[{"pos":"QB","texto":"Qb Uno forte"}],'
            '"escalacao":[{"slot":"QB","jogador":"Qb Uno"}],"trocas":[{"sai":"Qb Uno","entra":"Bench Wr","motivo":"x"}],'
            '"autosubs":[]}',
            '{"veredito":"mantem","resumo":"nada","trocas":[],"anteriores":[],"autosubs":[]}'])
        adv = lineup_advisor.LineupAdvisor(
            ctx=SimpleNamespace(timezone="America/Sao_Paulo", profile=_profile(), league_id="L",
                                state_path=lambda *p: os.path.join(tmp, *p)),
            sleeper=sleeper, llm_fn=lambda t: prompts.append(t) or next(answers),
            send=lambda t: sent.append(t) or True, state_path=os.path.join(tmp, "l.json"),
            targets=[{"rid": 1, "dms": ["5511"]}], games_fn=lambda w, s: games, db=db)
        import players_db
        orig = players_db.injury
        players_db.injury = lambda p: ""
        try:
            adv.tick("2026", 6, now=ko(2026, 10, 15, 8, 5))
            self.assertEqual(len(sent), 1)                           # exactly one message
            self.assertIn("ESCALAÇÃO COMPLETA", sent[0])
            self.assertIn("Raio-X por posição", sent[0])
            self.assertIn("Escalação ideal", sent[0])
            self.assertIn("AutoSub", sent[0])
            self.assertNotIn("<<<MSG>>>", sent[0])
            adv.tick("2026", 6, now=ko(2026, 10, 15, 20, 20))
            self.assertIn("SUGESTÕES ANTERIORES AINDA NÃO APLICADAS: Qb Uno → Bench Wr", prompts[1])
            self.assertIn("*Qb Uno* → *Bench Wr*", sent[1])            # model stayed silent: suggestion is kept
            self.assertNotIn("COMPLETA", sent[1].upper().split("\n")[0])
        finally:
            players_db.injury = orig

    def test_dm_stops_at_first_confirmed_number(self):
        calls = []
        evo = SimpleNamespace(send_message=lambda n, t: calls.append(n) or {"key": {"id": "x"}})
        self.assertTrue(watch_prefs.dm(evo, ["5548a", "5548b"], "oi"))
        self.assertEqual(calls, ["5548a"])


if __name__ == "__main__":
    unittest.main()
