"""SQLite knowledge index: it must agree with the raw sources and answer counting questions exactly.

Run:  python3 -m unittest tests.test_kb_db -v   (offline; builds league.db into a temp dir)
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEAGUE = os.path.join(ROOT, "league")
sys.path.insert(0, LEAGUE)

import kb_db  # noqa: E402
import kb_queries  # noqa: E402
import kb_stats  # noqa: E402
import liga_core  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="kbdb_")
os.environ["LEAGUE_DB"] = os.path.join(_TMP, "league.db")


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _json(*parts):
    with open(os.path.join(LEAGUE, *parts), encoding="utf-8") as f:
        return json.load(f)


class IndexMatchesSourcesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = kb_db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_trade_totals_per_season_match_sleeper_raw(self):
        raw = _json("all_sleeper_trades_raw.json")
        got = dict(self.con.execute("SELECT season, COUNT(*) FROM trades GROUP BY season").fetchall())
        for year, txs in raw.items():
            if year == "2026":
                self.assertGreaterEqual(got[year], len(txs))  # trades-watch may have added newer ones
            else:
                self.assertEqual(got[year], len(txs), year)

    def test_every_trade_side_has_a_roster_and_every_player_a_name(self):
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM trade_sides WHERE roster_id IS NULL").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM trade_assets WHERE player_name LIKE 'Player #%'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM trade_sides GROUP BY trade_seq HAVING COUNT(*)<2").fetchall(), [])

    def test_games_are_two_rows_per_matchup_and_match_season_records(self):
        n = len(_json("knowledge_base", "matchups_history.json"))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM games").fetchone()[0], 2 * n)
        rows = self.con.execute("""SELECT f.roster_id, f.season, f.wins, f.losses,
              (SELECT SUM(result='W') FROM games g WHERE g.roster_id=f.roster_id AND g.season=f.season AND g.is_playoff=0),
              (SELECT SUM(result='L') FROM games g WHERE g.roster_id=f.roster_id AND g.season=f.season AND g.is_playoff=0)
              FROM franchise_seasons f WHERE f.season < '2026'""").fetchall()  # 2026 in franchises_history is a stale snapshot
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual((r[2], r[3]), (r[4], r[5]), tuple(r))

    def test_postseason_games_carry_their_official_bracket(self):
        brackets = _json("knowledge_base", "brackets_history.json")
        n = sum(len(d["games"]) for s, d in brackets.items() if s != "_meta")
        by = dict(self.con.execute("SELECT bracket, COUNT(*) FROM games GROUP BY bracket").fetchall())
        self.assertEqual(by.get("winners", 0) + by.get("losers", 0), 2 * n)
        self.assertEqual(by.get("winners"), by.get("losers"))  # 7 + 7 games per season
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM games WHERE is_playoff=1 AND bracket IS NULL").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM games WHERE is_playoff=0 AND bracket!='regular'").fetchone()[0], 0)

    def test_playoffs_filter_excludes_the_toilet_bowl(self):
        mario = self.con.execute("SELECT roster_id FROM franchises WHERE manager='marinh012'").fetchone()[0]
        n = lambda b: self.con.execute("SELECT COUNT(*) FROM games WHERE roster_id=? AND bracket=?", (mario, b)).fetchone()[0]
        po = kb_stats.fetch(self.con, "jogos", metrica="contagem", fase="playoffs", alvo="Mario")["total"]
        tb = kb_stats.fetch(self.con, "jogos", metrica="contagem", fase="toilet", alvo="Mario")["total"]
        self.assertEqual((po, tb or 0), (n("winners"), n("losers")))

    def test_waiver_profiles_match_the_classified_json(self):
        for m, p in _json("knowledge_base", "waivers_market_classified.json")["manager_profiles"].items():
            r = self.con.execute("""SELECT SUM(type='waiver' AND status='complete'), SUM(type='waiver' AND status='failed'),
                SUM(type='free_agent' AND status='complete'), SUM(CASE WHEN status='complete' THEN bid ELSE 0 END)
                FROM waiver_moves WHERE manager=?""", (m,)).fetchone()
            self.assertEqual(tuple(r), (p["waivers_won"], p["waivers_failed"], p["free_agents"], p["total_faab_spent"]), m)

    def test_draft_and_profiles_loaded(self):
        counts = dict(self.con.execute("SELECT season, COUNT(*) FROM draft_picks GROUP BY season").fetchall())
        self.assertEqual(counts["2022"], 420)
        self.assertEqual(counts["2025"], 60)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 12)

    def test_rebuild_is_skipped_when_sources_are_unchanged(self):
        before = os.stat(kb_db.db_path()).st_mtime_ns
        kb_db.ensure_fresh()
        self.assertEqual(os.stat(kb_db.db_path()).st_mtime_ns, before)


class StatsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = kb_db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_trades_per_week_pivot_adds_up(self):
        out = kb_stats.query(self.con, "trocas", "semana")
        total = self.con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertIn("2022 | 2023 | 2024 | 2025 | 2026", out)
        total_line = next(l for l in out.splitlines() if l.startswith("TOTAL:"))
        self.assertTrue(total_line.endswith(f"| {total}"), total_line)
        # week rows sum to the grand total
        row_totals = [int(l.rsplit("|", 1)[1]) for l in out.splitlines() if l.startswith(("S", "Offseason"))]
        self.assertEqual(sum(row_totals), total)

    def test_trades_by_season_and_manager(self):
        out = kb_stats.query(self.con, "trocas", "temporada")
        for y, n in self.con.execute("SELECT season, COUNT(*) FROM trades GROUP BY season"):
            self.assertIn(f"• {y}: *{n}*", out)
        mgr = kb_stats.query(self.con, "trocas", "manager")
        self.assertIn("Trocas distintas", mgr)

    def test_offseason_is_not_week_one(self):
        """Sleeper files pre-kickoff trades under week 1; the index calls them offseason (week 0)."""
        from datetime import date
        self.assertEqual(kb_db.season_kickoff("2025"), date(2025, 9, 4))   # Thursday after Labor Day
        self.assertEqual(kb_db.index_week("2026", 1, "2026-06-16"), kb_db.OFFSEASON)
        self.assertEqual(kb_db.index_week("2026", 1, "2026-09-12"), 1)      # after the kickoff: a real week 1
        self.assertEqual(kb_db.index_week("2026", 2, "2026-09-16"), 2)
        n = self.con.execute("SELECT COUNT(*) FROM trades WHERE season='2026' AND date<'2026-09-09'").fetchone()[0]
        self.assertGreater(n, 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM trades WHERE season='2026' AND week=0").fetchone()[0], n)
        pivot = kb_stats.query(self.con, "trocas", "semana")
        self.assertIn("Offseason:", pivot)
        on = kb_stats.query(self.con, "trocas", ano="2026", fase="temporada")
        off = kb_stats.query(self.con, "trocas", ano="2026", fase="offseason")
        total = self.con.execute("SELECT COUNT(*) FROM trades WHERE season='2026'").fetchone()[0]
        self.assertIn(f"*Total: {total - n}*", on)
        self.assertIn(f"*Total: {n}*", off)
        self.assertIn("offseason", kb_queries.trades_report(self.con, ano="2026", limit=0))

    def test_week_filter_is_a_historical_week_not_rewritten(self):
        out = kb_stats.query(self.con, "trocas", ano="2025", semana=14)
        n = self.con.execute("SELECT COUNT(*) FROM trades WHERE season='2025' AND week=14").fetchone()[0]
        self.assertIn(f"Total: {n}" if "Total" in out else f"*Total: {n}*", out)

    def test_games_win_pct_and_manager_filter(self):
        out = kb_stats.query(self.con, "jogos", "manager", "aproveitamento", fase="regular")
        self.assertIn("%", out)
        one = kb_stats.query(self.con, "jogos", "temporada", "vitorias", alvo="Ari")
        self.assertIn("ARIBRUNO", one)

    def test_two_dimensions_and_second_metric(self):
        out = kb_stats.query(self.con, "trocas", "manager", serie="temporada")
        self.assertIn("manager \\ temporada: 2022 | 2023", out)
        r = kb_stats.fetch(self.con, "jogos", "manager", "media", metrica2="media_contra")
        self.assertEqual(set(r["values2"]), {g for g, _ in r["rows"]})
        self.assertIn("serie", kb_stats.query(self.con, "trocas", "", serie="temporada"))
        self.assertIn("Opções", kb_stats.query(self.con, "trocas", "manager", metrica2="vitorias"))

    def test_invalid_inputs_explain_options(self):
        self.assertIn("entidade", kb_stats.query(self.con, ""))
        self.assertIn("Opções", kb_stats.query(self.con, "trocas", "tier"))
        self.assertIn("Opções", kb_stats.query(self.con, "trocas", "", "vitorias"))
        self.assertIn("não encontrado", kb_stats.query(self.con, "jogos", "temporada", alvo="xyz"))


class ReadersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = kb_db.connect()
        cls.total = cls.con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_year_filter_header_says_n_of_total(self):
        n = self.con.execute("SELECT COUNT(*) FROM trades WHERE season='2025'").fetchone()[0]
        out = kb_queries.trades_report(self.con, ano="2025", limit=5)
        self.assertIn(f"{n} de {self.total} trocas", out)
        self.assertIn("faltam", out)  # says what was left out
        self.assertEqual(kb_queries.trades_report(self.con, "2025", limit=5), out)  # alvo=2025 ≡ ano=2025

    def test_week_and_manager_filters(self):
        out = kb_queries.trades_report(self.con, ano="2026", semana=2, limit=0)
        n = self.con.execute("SELECT COUNT(*) FROM trades WHERE season='2026' AND week=2").fetchone()[0]
        self.assertIn(f"{n} de {self.total}", out)
        self.assertNotIn("faltam", out)
        by_mgr = kb_queries.trades_report(self.con, "rafa", limit=3)
        self.assertIn("rafastw", by_mgr)

    def test_counterpart_filter_lists_only_trades_between_the_two(self):
        a = kb_queries.resolve_roster(self.con, "Bayer")
        b = kb_queries.resolve_roster(self.con, "Jonathan")
        both = ("SELECT COUNT(*) FROM trades t WHERE EXISTS (SELECT 1 FROM trade_sides s WHERE s.trade_seq=t.seq "
                "AND s.roster_id=?) AND EXISTS (SELECT 1 FROM trade_sides s WHERE s.trade_seq=t.seq AND s.roster_id=?)")
        n = self.con.execute(both, (a, b)).fetchone()[0]
        mine = self.con.execute("SELECT COUNT(*) FROM trades t WHERE EXISTS (SELECT 1 FROM trade_sides s "
                                "WHERE s.trade_seq=t.seq AND s.roster_id=?)", (a,)).fetchone()[0]
        self.assertGreater(n, 0)
        self.assertLess(n, mine)  # the counterpart really narrows "all trades of Bayer"
        out = kb_queries.trades_report(self.con, "Bayer", limit=0, contra="Jonathan")
        self.assertIn(f"{n} de {self.total} trocas", out)
        self.assertIn("ENTRE", out.splitlines()[0])
        self.assertIn(f"{n} de {self.total} trocas", kb_queries.trades_report(self.con, "Jonathan", limit=0, contra="Bayer"))
        self.assertIn(f"{n} de {self.total} trocas", kb_queries.trades_report(self.con, "Bayer", limit=0, contra="Jonatan"))

    def test_counterpart_composes_with_other_filters_and_rejects_unknown_names(self):
        out = kb_queries.trades_report(self.con, "Bayer", ano="2025", limit=0, contra="Jonathan").lower()
        self.assertIn("temporada 2025", out)  # also true when that year has no trade between them (the empty notice)
        self.assertIn("entre", out)
        self.assertTrue(kb_queries.trades_report(self.con, "Bayer", contra="Fulano de Tal").startswith("❌ Treinador"))

    def test_tool_passes_alvo2_as_the_counterpart(self):
        out = liga_core.run_query("trocas", {"alvo": "Bayer", "alvo2": "Jonathan", "limite": 50})
        self.assertIn("ENTRE", out.splitlines()[0])
        alone = liga_core.run_query("trocas", {"alvo": "Bayer", "limite": 50})
        self.assertNotEqual(out, alone)  # distinct cache keys: alvo2 must not be served the one-manager answer

    def test_blockbusters_sorted_by_impact(self):
        out = kb_queries.trades_report(self.con, "blockbusters", limit=5)
        ids = [int(l.split("#")[1].split(" ")[0]) for l in out.splitlines() if l.startswith("• *#")]
        impacts = [self.con.execute("SELECT impact FROM trades WHERE seq=?", (i,)).fetchone()[0] for i in ids]
        self.assertEqual(impacts, sorted(impacts, reverse=True))

    def test_compact_line_fits_the_budget(self):
        out = kb_queries.trades_report(self.con, limit=12)
        self.assertLess(len(out), liga_core.MAX_CHARS)

    def test_other_readers_answer(self):
        self.assertIn("RAIO-X DO DRAFT 2025", kb_queries.draft_picks_report(self.con, "2025"))
        self.assertIn("ESCOLHAS DE DRAFT", kb_queries.manager_draft_report(self.con, "Ari"))
        self.assertIn("TROCAS DE PICKS", kb_queries.trade_picks_report(self.con, "Ari"))
        self.assertIn("MAIORES GOLEADAS", kb_queries.records_report(self.con))
        self.assertIn("Volume", kb_queries.waivers_report(self.con, ""))
        self.assertIn("PERFIL DE WAIVER", kb_queries.waivers_report(self.con, "panda"))
        self.assertIn("STEALS", kb_queries.waivers_report(self.con, "steals", 3, ano="2025"))
        self.assertTrue(kb_queries.profile_report(self.con, "Lula").startswith("### 4."))

    def test_waivers_overview_uses_live_numbers_not_hardcoded(self):
        n = self.con.execute("SELECT COUNT(*) FROM waiver_moves").fetchone()[0]
        self.assertIn(f"{n:,} movimentações", kb_queries.waivers_report(self.con, ""))


class LigaToolTest(unittest.TestCase):
    def test_trocas_ano_semana_are_honoured_and_not_rewritten(self):
        out = liga_core.run_query("trocas", {"ano": "2025", "semana": 14, "limite": 50})
        self.assertIn("TEMPORADA 2025", out)
        self.assertIn("SEMANA 14", out)

    def test_estatisticas_through_the_tool(self):
        out = liga_core.run_query("estatisticas", {"entidade": "trocas", "agrupar_por": "semana"})
        self.assertIn("TOTAL:", out)
        self.assertIn("entidade", liga_core.run_query("estatisticas", {}))

    def test_help_covers_every_query_and_plugin_enum_matches(self):
        self.assertEqual(set(liga_core.HELP), set(liga_core.CMDS))
        import importlib.util
        spec = importlib.util.spec_from_file_location("sleeper_league", os.path.join(ROOT, "data/plugins/sleeper-league/__init__.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(set(mod._QUERIES), set(liga_core.CMDS))
        for k in ("entidade", "agrupar_por", "metrica", "metrica2", "serie", "forma", "dados", "fase", "detalhe"):
            self.assertIn(k, mod.SCHEMA["parameters"]["properties"])
        self.assertLess(len(mod.SCHEMA["description"]), 1500)

    def test_truncation_cuts_on_a_line_boundary_and_says_so(self):
        text = "\n".join(f"linha {i} " + "x" * 40 for i in range(400))
        out = liga_core._truncate(text, 1000)
        body, note = out.rsplit("\n", 1)
        self.assertTrue(body.splitlines()[-1].endswith("x" * 40))  # last kept line is complete
        self.assertIn("faltam", note)
        self.assertEqual(liga_core._truncate("curto"), "curto")


class AppendTradeTest(unittest.TestCase):
    def test_append_is_idempotent_and_reindexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = os.path.join(tmp, "knowledge_base")
            shutil.copytree(os.path.join(LEAGUE, "knowledge_base"), kb,
                            ignore=shutil.ignore_patterns("chat_history_whatsapp.txt", "league.db*"))
            info = {"transaction_id": "999", "date_str": "07/10/2026", "season": "2026", "week": 5, "sides": [
                {"manager_name": "rafastw", "team_name": "PsicoPanda", "player_details_received": [
                    {"player_id": "4984", "label": "Josh Allen (QB - BUF)"}], "picks_received": [], "faab_received": 0},
                {"manager_name": "eduohira", "team_name": "Tomorrowland", "player_details_received": [],
                 "picks_received": [{"season": "2028", "round": 1, "original_owner": "rafastw", "label": "x"}],
                 "faab_received": 2}]}
            with mock.patch.object(kb_db, "KB_DIR", kb), mock.patch.object(kb_db, "TRADES_FILE", os.path.join(kb, "trades_history.json")), \
                    mock.patch.dict(os.environ, {"LEAGUE_DB": os.path.join(tmp, "x.db")}):
                before = len(kb_db.trades_from_file())
                self.assertTrue(kb_db.append_trade(info))
                self.assertFalse(kb_db.append_trade(info))
                self.assertEqual(len(kb_db.trades_from_file()), before + 1)
                con = kb_db.connect()
                try:
                    last = con.execute("SELECT * FROM trades ORDER BY seq DESC LIMIT 1").fetchone()
                    self.assertEqual((last["tx_id"], last["date"], last["week"]), ("999", "2026-10-07", 5))
                    self.assertEqual(con.execute("SELECT COUNT(*) FROM trades").fetchone()[0], before + 1)
                    kinds = {r[0] for r in con.execute("SELECT kind FROM trade_assets WHERE trade_seq=?", (last["seq"],))}
                    self.assertEqual(kinds, {"player", "pick", "faab"})
                finally:
                    con.close()


if __name__ == "__main__":
    unittest.main()
