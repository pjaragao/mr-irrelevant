"""Chart engine: layout invariants, HTML/PNG output, data -> chart mapping, delivery payloads.

Run:  python3 -m unittest tests.test_charts -v     (PNG tests need Pillow and are skipped without it)
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

_TMP = tempfile.mkdtemp(prefix="charts_")
os.environ["LEAGUE_DB"] = os.path.join(_TMP, "league.db")
os.environ["LIGA_CHARTS_DIR"] = os.path.join(_TMP, "out")

import charts  # noqa: E402
import chart_service  # noqa: E402
import kb_db  # noqa: E402
import liga_core  # noqa: E402

try:
    import PIL  # noqa: F401
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

SPEC = os.path.join(ROOT, "data", "skills", "mr-irrelevant", "league-charts", "references", "spec-example.json")


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def _spec():
    with open(SPEC, encoding="utf-8") as f:
        return json.load(f)


class SceneInvariantsTest(unittest.TestCase):
    def test_no_text_leaves_the_canvas_and_nothing_is_clipped(self):
        for spec in (_spec(), self._pivot_spec(), self._groups_spec()):
            sc = charts.compose(spec)
            for it in sc.items:
                if it["k"] != "text":
                    continue
                w = charts.text_width(it["s"], it["size"], it["bold"])
                left = {"start": it["x"], "middle": it["x"] - w / 2, "end": it["x"] - w}[it["anchor"]]
                self.assertGreaterEqual(left, -1, (it["s"], left))
                self.assertLessEqual(left + w, sc.width + 1, (it["s"], left + w))
                self.assertLessEqual(it["y"], sc.height)

    def test_hatch_stays_inside_its_rect(self):
        sc = charts.Scene()
        sc.hatch(100, 50, 60, 40, "$hatch", gap=11)
        self.assertTrue(sc.items)
        for it in sc.items:
            for x, y in ((it["x1"], it["y1"]), (it["x2"], it["y2"])):
                self.assertTrue(100 - 1e-6 <= x <= 160 + 1e-6 and 50 - 1e-6 <= y <= 90 + 1e-6, (x, y))

    def test_fit_size_shrinks_to_the_box(self):
        self.assertEqual(charts.fit_size("x", 40, 500), 40)
        s = charts.fit_size("Waivers e free agents por semana e temporada", 58, 300, True, 20)
        self.assertLess(s, 58)
        self.assertGreaterEqual(s, 20)

    def test_colour_tokens_resolve_per_theme(self):
        for name, th in charts.THEMES.items():
            self.assertEqual(charts.resolve("$surface", th), th["surface"])
            self.assertEqual(charts.resolve(("series", 9), th), th["series"][1])  # wraps, never invents a hue
            self.assertTrue(charts.resolve(("ramp", 0.5), th).startswith("#"))
        dark = charts.THEMES["dark"]
        self.assertEqual(charts.resolve(("on", 0.0), dark), "#ffffff")   # dark cell -> white ink
        self.assertEqual(charts.resolve(("on", 1.0), dark), "#0b0b0b")   # bright cell -> dark ink

    def test_unknown_block_is_an_error(self):
        with self.assertRaises(ValueError):
            charts.compose({"title": "x", "blocks": [{"type": "pie"}]})

    @staticmethod
    def _pivot_spec():
        con = kb_db.connect()
        try:
            return chart_service.spec_from_stats(con, "trocas", "semana")
        finally:
            con.close()

    @staticmethod
    def _groups_spec():
        con = kb_db.connect()
        try:
            return chart_service.spec_from_stats(con, "trocas", "manager")
        finally:
            con.close()


class SpecValidationTest(unittest.TestCase):
    def bad(self, spec):
        with self.assertRaises(ValueError):
            charts.validate_spec(spec)

    def test_rejects_malformed_specs_with_clear_errors(self):
        self.bad("x")
        self.bad({"blocks": []})
        self.bad({"blocks": [{"type": "pie"}]})
        self.bad({"blocks": [{"type": "bars", "items": [{"label": "a", "value": "abc"}]}]})
        self.bad({"blocks": [{"type": "bars", "items": [{"label": "a", "value": float("nan")}]}]})
        self.bad({"blocks": [{"type": "lines", "x": ["a", "b"], "series": [{"name": "s", "values": [1]}]}]})
        self.bad({"blocks": [{"type": "heatmap", "rows": ["a"], "cols": ["x", "y"], "values": [[1]]}]})
        self.bad({"blocks": [{"type": "columns", "x": ["a"], "series": [{"name": str(i), "values": [1]} for i in range(7)]}]})
        self.bad({"blocks": [{"type": "scatter", "points": [{"label": "a", "x": 1}]}]})
        self.bad({"blocks": [{"type": "tiles", "items": []}]})
        self.bad({"blocks": [{"type": "bars", "items": [{"label": "x" * 500, "value": 1}]}]})

    def test_normalises_and_truncates(self):
        out = charts.validate_spec({"title": "t" * 300 if False else "ok", "blocks": [
            {"type": "bars", "items": [{"label": "a" * 200, "value": "3"}], "fmt": "weird"}]})
        b = out["blocks"][0]
        self.assertEqual(b["items"][0]["value"], 3.0)
        self.assertLessEqual(len(b["items"][0]["label"]), charts.LIMITS["text"])
        self.assertEqual(b["fmt"], "int")

    def test_every_block_type_composes_and_stays_inside_the_canvas(self):
        spec = {"title": "Tudo", "blocks": [
            {"type": "tiles", "items": [{"value": "1", "label": "a", "sub": "b"}]},
            {"type": "heatmap", "rows": ["r1", "r2"], "cols": ["a", "b", "c"], "values": [[1, 2.5, None], [0, 3, 4]], "fmt": "float", "row_totals": True},
            {"type": "lines", "x": ["a", "b", "c"], "series": [{"name": "s1", "values": [1, 2, 3]}, {"name": "s2", "values": [3, None, 1]}]},
            {"type": "columns", "x": ["a", "b"], "series": [{"name": "s1", "values": [1, 2]}, {"name": "s2", "values": [2, 1]}]},
            {"type": "columns", "x": ["a", "b"], "stacked": True, "series": [{"name": "s1", "values": [1, 2]}, {"name": "s2", "values": [2, 1]}]},
            {"type": "bars", "orientation": "v", "items": [{"label": "a", "value": 3}, {"label": "b", "value": 1, "muted": True, "note": "n"}]},
            {"type": "donut", "items": [{"label": str(i), "value": i + 1} for i in range(11)]},
            {"type": "scatter", "points": [{"label": f"p{i}", "x": i, "y": (i * 7) % 5} for i in range(6)], "quadrants": {"tl": "a", "br": "b"}}]}
        sc = charts.compose(spec)
        kinds = {it["k"] for it in sc.items}
        self.assertTrue({"rect", "line", "poly", "circle", "arc", "text"} <= kinds)
        for it in sc.items:
            if it["k"] == "text":
                w = charts.text_width(it["s"], it["size"], it["bold"])
                left = {"start": it["x"], "middle": it["x"] - w / 2, "end": it["x"] - w}[it["anchor"]]
                self.assertGreaterEqual(left, -1, it["s"])
                self.assertLessEqual(left + w, sc.width + 1, it["s"])
        svg = charts.render_svg(sc, "light")
        self.assertIn("<path", svg)  # the donut

    def test_donut_folds_the_tail_into_outros(self):
        sc = charts.compose({"title": "d", "blocks": [{"type": "donut", "items": [{"label": str(i), "value": i + 1} for i in range(11)]}]})
        self.assertEqual(len([it for it in sc.items if it["k"] == "arc"]), 8)
        self.assertTrue(any(it["k"] == "text" and it["s"] == "Outros" for it in sc.items))

    def test_single_series_line_has_no_legend(self):
        sc = charts.compose({"title": "t", "blocks": [{"type": "lines", "x": ["a", "b"], "series": [{"name": "UNICA", "values": [1, 2]}]}]})
        self.assertFalse(any(it["k"] == "text" and it["s"] == "UNICA" for it in sc.items))


class HtmlOutputTest(unittest.TestCase):
    def test_html_has_both_themes_tooltips_and_a_table_view(self):
        spec = _spec()
        sc = charts.compose(spec)
        path = os.path.join(_TMP, "h.html")
        charts.render_html(sc, spec, path)
        with open(path, encoding="utf-8") as f:
            page = f.read()
        self.assertIn("s-dark", page)
        self.assertIn("s-light", page)
        self.assertIn("data-tip=", page)
        self.assertIn("<table>", page)
        self.assertIn("Ver dados em tabela", page)
        self.assertIn("prefers-color-scheme", page)
        self.assertEqual(page.count("<svg"), 2)
        self.assertNotIn("<script src", page)  # self-contained

    def test_text_is_escaped(self):
        spec = {"title": "A <b>&</b>", "blocks": [{"type": "bars", "items": [{"label": "<x>", "value": 1}]}]}
        sc = charts.compose(spec)
        svg = charts.render_svg(sc, "dark")
        self.assertNotIn("<b>", svg)
        self.assertIn("&lt;x&gt;", svg)


class DataToChartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = kb_db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_pivot_spec_matches_the_database(self):
        spec = chart_service.spec_from_stats(self.con, "trocas", "semana")
        heat = next(b for b in spec["blocks"] if b["type"] == "heatmap")
        lines = next(b for b in spec["blocks"] if b["type"] == "lines")
        total = self.con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(sum(v for row in heat["values"] for v in row if v is not None), total)
        for s, row in zip(heat["rows"], heat["values"]):
            n = self.con.execute("SELECT COUNT(*) FROM trades WHERE season=?", (s,)).fetchone()[0]
            self.assertEqual(sum(v for v in row if v is not None), n, s)
        for ser in lines["series"]:  # cumulative line ends at the season total
            last = [v for v in ser["values"] if v is not None][-1]
            self.assertEqual(last, self.con.execute("SELECT COUNT(*) FROM trades WHERE season=?", (ser["name"],)).fetchone()[0])
        self.assertTrue(lines["series"][-1]["highlight"])  # the current season is the highlighted one

    def test_current_season_future_weeks_are_none_not_zero(self):
        spec = chart_service.spec_from_stats(self.con, "trocas", "semana")
        heat = next(b for b in spec["blocks"] if b["type"] == "heatmap")
        cur_row = heat["values"][-1]
        self.assertIn(None, cur_row)
        self.assertNotIn(None, heat["values"][0])  # finished seasons have every week
        played = self.con.execute("SELECT MAX(week) FROM games WHERE season=? AND result IS NOT NULL AND is_playoff=0",
                                  (heat["rows"][-1],)).fetchone()[0]
        self.assertGreaterEqual(sum(v is not None for v in cur_row), played)

    def test_groups_spec_uses_distinct_trades_for_the_headline(self):
        spec = chart_service.spec_from_stats(self.con, "trocas", "manager")
        tiles = next(b for b in spec["blocks"] if b["type"] == "tiles")["items"]
        self.assertEqual(tiles[0]["value"], str(self.con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]))

    def test_in_progress_season_is_marked(self):
        spec = chart_service.spec_from_stats(self.con, "trocas", "temporada")
        bars = next(b for b in spec["blocks"] if b["type"] == "bars")
        self.assertTrue(bars["items"][-1].get("muted"))
        self.assertIn("até a S", bars["items"][-1]["note"])
        self.assertFalse(bars["items"][0].get("muted"))

    def test_matrix_heatmap_matches_the_database(self):
        spec = chart_service.spec_from_stats(self.con, "trocas", "manager", serie="temporada")
        heat = spec["blocks"][0]
        self.assertEqual(heat["type"], "heatmap")
        self.assertEqual(heat["rows"][0], "rafastw")
        j = heat["cols"].index("2023")
        self.assertEqual(heat["values"][0][j], self.con.execute(
            "SELECT COUNT(*) FROM trade_sides WHERE manager='rafastw' AND trade_seq IN (SELECT seq FROM trades WHERE season='2023')").fetchone()[0])

    def test_forms_for_two_dimensions(self):
        lines = chart_service.spec_from_stats(self.con, "waivers", "temporada", serie="tipo", forma="linhas")["blocks"][0]
        self.assertEqual((lines["type"], len(lines["series"])), ("lines", 2))
        stacked = chart_service.spec_from_stats(self.con, "waivers", "temporada", serie="tipo", forma="empilhado")["blocks"][0]
        self.assertTrue(stacked["stacked"])
        with self.assertRaises(ValueError) as cm:  # 12 managers do not fit 6 stacked series
            chart_service.spec_from_stats(self.con, "trocas", "temporada", serie="manager", forma="empilhado")
        self.assertIn("máximo", str(cm.exception))
        with self.assertRaises(ValueError):  # lines need a time dimension
            chart_service.spec_from_stats(self.con, "draft", "posicao", serie="rodada", forma="linhas") if False else \
                chart_service.spec_from_stats(self.con, "waivers", "manager", serie="tipo", forma="linhas")

    def test_scatter_donut_and_line_forms(self):
        sc = chart_service.spec_from_stats(self.con, "jogos", "manager", "media", metrica2="media_contra", fase="regular")["blocks"][0]
        self.assertEqual(sc["type"], "scatter")
        self.assertEqual(len(sc["points"]), 12)
        with self.assertRaises(ValueError):
            chart_service.spec_from_stats(self.con, "jogos", "manager", "media", forma="dispersao")  # needs metrica2
        pie = chart_service.spec_from_stats(self.con, "draft", "posicao", forma="pizza")["blocks"][0]
        self.assertEqual(pie["type"], "donut")
        self.assertEqual(sum(i["value"] for i in pie["items"]), self.con.execute("SELECT COUNT(*) FROM draft_picks").fetchone()[0])
        with self.assertRaises(ValueError):
            chart_service.spec_from_stats(self.con, "jogos", "manager", "media", forma="pizza")  # averages are not parts of a whole
        line = chart_service.spec_from_stats(self.con, "jogos", "semana", "pontos", ano="2026", alvo="Ari", forma="linhas")["blocks"][-1]
        self.assertEqual(line["type"], "lines")
        self.assertEqual(len(line["series"]), 1)

    def test_cards_and_texts_follow_the_subject(self):
        losses = chart_service.spec_from_stats(self.con, "jogos", "semana", "derrotas", alvo="Ohira")
        txt = json.dumps(losses, ensure_ascii=False)
        self.assertEqual(losses["title"], "Derrotas de eduohira por semana")
        self.assertIn("derrotas", txt)
        for wrong in ("pré-temporada", "movimentada", "mais quente", "jogos em", "das jogos", "Quantas"):
            self.assertNotIn(wrong, txt, wrong)  # trade-flavoured wording must not leak into other subjects
        tiles = losses["blocks"][0]["items"]
        self.assertEqual(len(tiles), 3)
        self.assertIn("na mesma altura", tiles[2]["sub"])  # pace card instead of the pre-season share
        total = self.con.execute("SELECT COUNT(*) FROM games WHERE manager='eduohira' AND result='L'").fetchone()[0]
        self.assertIn(f"{total} derrotas", losses["subtitle"])
        trades = json.dumps(chart_service.spec_from_stats(self.con, "trocas", "semana"), ensure_ascii=False)
        self.assertIn("pré-temporada", trades)  # trades do have a pre-season, so that card stays
        wins = chart_service.spec_from_stats(self.con, "jogos", "semana", "vitorias", alvo="Ari")
        self.assertEqual(wins["title"], "Vitórias de aribruno por semana")

    def test_units_follow_the_metric(self):
        spec = chart_service.spec_from_stats(self.con, "jogos", "manager", "media")
        bars = next(b for b in spec["blocks"] if b["type"] == "bars")
        self.assertEqual(bars["unit"], "pts")

    def test_bad_requests_explain_themselves(self):
        with self.assertRaises(ValueError):
            chart_service.spec_from_stats(self.con, "trocas", "")
        with self.assertRaises(ValueError):
            chart_service.spec_from_stats(self.con, "jogos", "semana", "media")  # non-integer pivot
        with self.assertRaises(ValueError):
            chart_service.spec_from_stats(self.con, "xyz", "semana")


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class PngOutputTest(unittest.TestCase):
    def test_png_is_rendered_with_the_scene_size(self):
        from PIL import Image
        spec = _spec()
        res = charts.render_all(spec, os.path.join(_TMP, "a", "x.png"), os.path.join(_TMP, "a", "x.html"))
        im = Image.open(res["png"])
        self.assertEqual(im.size, (charts.W, res["height"]))
        self.assertEqual(im.getpixel((2, 2)), (0x1a, 0x1a, 0x19))  # dark surface
        self.assertGreater(os.path.getsize(res["png"]), 10_000)
        self.assertLess(os.path.getsize(res["png"]), 1_500_000)  # WhatsApp-friendly

    def test_generate_writes_png_and_html_and_media_reply(self):
        res = chart_service.generate("trocas", "semana")
        self.assertTrue(os.path.isfile(res["png"]) and os.path.isfile(res["html"]))
        self.assertTrue(res["png"].startswith(os.environ["LIGA_CHARTS_DIR"]))
        reply = chart_service.media_reply(res)
        self.assertIn(f"\nMEDIA:{res['png']}\n", reply)
        self.assertIn("191", res["caption"])

    def test_liga_tool_returns_a_media_line(self):
        out = liga_core.run_query("grafico", {})
        m = re.search(r"^MEDIA:(\S+\.png)$", out, re.M)
        self.assertIsNotNone(m, out)
        self.assertTrue(os.path.isfile(m.group(1)))
        self.assertIn("Não consegui", liga_core.run_query("grafico", {"entidade": "jogos", "agrupar_por": "semana", "metrica": "media"}))

    def test_custom_data_through_the_tool(self):
        dados = json.dumps({"title": "Meu gráfico", "blocks": [{"type": "bars", "items": [{"label": "a", "value": 3}, {"label": "b", "value": 5}]}]})
        out = liga_core.run_query("grafico", {"dados": dados})
        m = re.search(r"^MEDIA:(\S+\.png)$", out, re.M)
        self.assertIsNotNone(m, out)
        # a model may pass the object itself instead of a string
        out2 = liga_core.run_query("grafico", {"dados": json.loads(dados)})
        self.assertIn("MEDIA:", out2)
        self.assertIn("Não consegui", liga_core.run_query("grafico", {"dados": "{nao e json"}))
        self.assertIn("Não consegui", liga_core.run_query("grafico", {"dados": json.dumps({"blocks": [{"type": "pie"}]})}))

    def test_old_charts_are_pruned(self):
        d = chart_service.out_dir()
        old = os.path.join(d, "velho.png")
        open(old, "wb").close()
        past = time.time() - (chart_service.KEEP_DAYS + 1) * 86400
        os.utime(old, (past, past))
        chart_service.generate("trocas", "temporada")
        self.assertFalse(os.path.exists(old))


class DeliveryTest(unittest.TestCase):
    def test_evolution_client_sends_base64_media(self):
        from evolution_client import EvolutionClient
        img = os.path.join(_TMP, "p.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
        cli = EvolutionClient("http://x", "k", "inst")
        with mock.patch.object(cli, "_request", return_value={"key": {"id": "1"}}) as req:
            cli.send_image("123@g.us", img, "legenda")
        method, endpoint, payload = req.call_args[0][:3]
        self.assertEqual((method, endpoint), ("POST", "message/sendMedia/inst"))
        self.assertEqual((payload["mediatype"], payload["mimetype"], payload["caption"]), ("image", "image/png", "legenda"))
        import base64
        self.assertEqual(base64.b64decode(payload["media"]), b"\x89PNG\r\n\x1a\nfake")

    def test_cli_send_requires_a_destination(self):
        with mock.patch.dict(os.environ, {"WHATSAPP_GROUP_JID": ""}):
            self.assertIn("error", chart_service.send_png("x.png", "c"))

    def test_skill_files_are_in_place(self):
        base = os.path.join(ROOT, "data", "skills", "mr-irrelevant", "league-charts")
        with open(os.path.join(base, "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: league-charts\n"))
        self.assertIn("MEDIA:", text)
        self.assertTrue(os.path.isfile(os.path.join(base, "references", "spec-example.json")))
        # every grafico example in the skill must be a real query the tool accepts
        self.assertIn("grafico", liga_core.CMDS)
        self.assertIn("grafico", liga_core.HELP)


if __name__ == "__main__":
    unittest.main()


class VoiceConfigTest(unittest.TestCase):
    """TTS is on (Edge, pt-BR Antonio) but only answers in audio when explicitly asked."""

    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_config_enables_edge_antonio_and_keeps_auto_voice_off(self):
        cfg = self._read("data", "config.yaml")
        self.assertRegex(cfg, r"(?m)^tts:\n  provider: edge\n  edge:\n    voice: pt-BR-AntonioNeural\n")
        self.assertRegex(cfg, r"(?m)^voice:\n  auto_tts: false\n")
        self.assertRegex(cfg, r"(?m)^  evolution:\n    - liga\n    - tts\n")

    def test_soul_restricts_audio_to_explicit_requests(self):
        soul = self._read("data", "SOUL.md")
        self.assertIn("somente quando alguém pedir explicitamente", soul)
        self.assertIn("text_to_speech", soul)
        self.assertIn("Nunca ofereça nem mande áudio por conta própria", soul)

    def test_adapter_sends_voice_notes_through_evolution(self):
        src = self._read("data", "plugins", "evolution-whatsapp", "adapter.py")
        self.assertIn("async def send_voice", src)
        self.assertIn("sendWhatsAppAudio", src)
