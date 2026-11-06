"""Songs: fact sheet from the history DB, lyrics parsing/number reading, tool wiring, audio assembly.

Run:  python3 -m unittest tests.test_song -v   (audio assembly needs ffmpeg and is skipped without it)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

_TMP = tempfile.mkdtemp(prefix="song_")
os.environ["LEAGUE_DB"] = os.path.join(_TMP, "league.db")
os.environ["LIGA_SONGS_DIR"] = os.path.join(_TMP, "songs")

import kb_db  # noqa: E402
import liga_core  # noqa: E402
import song  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class SpeechTextTest(unittest.TestCase):
    def test_numbers_are_spelled_in_portuguese(self):
        cases = {0: "zero", 7: "sete", 15: "quinze", 21: "vinte e um", 43: "quarenta e três", 100: "cem", 101: "cento e um",
                 117: "cento e dezessete", 1000: "mil", 2022: "dois mil e vinte e dois", 2026: "dois mil e vinte e seis",
                 1100: "mil e cem", 1250: "mil duzentos e cinquenta", 28771: "vinte e oito mil setecentos e setenta e um"}
        for n, w in cases.items():
            self.assertEqual(song.num2pt(n), w, n)

    def test_speak_text_cleans_markdown_weeks_decimals_and_percent(self):
        self.assertEqual(song.speak_text("*Na S14*, 71,4 a 188,9 em 2022: 39%"),
                         "Na semana catorze, setenta e um vírgula quatro a cento e oitenta e oito vírgula nove em dois mil e vinte e dois: trinta e nove por cento")
        self.assertNotIn("🏈", song.speak_text("vai 🏈 time"))

    def test_lyrics_parsing_rules(self):
        ok = "[Refrão]\numa linha\n\nSegunda linha\n(Verso 1)\nterceira\nquarta\nVerso 2:\nquinta"
        self.assertEqual(song.parse_lyrics(ok), ["uma linha", "Segunda linha", "terceira", "quarta", "quinta"])
        for bad in ("", "a\nb\nc", "x\n" * 41, "y" * (song.MAX_LINE_CHARS + 1) + "\na\nb\nc", "linha longa\n" * 300):
            with self.assertRaises(ValueError):
                song.parse_lyrics(bad)

    def test_beat_loop_has_the_expected_length_and_is_not_silent(self):
        loop = song.beat_loop(110, bars=2)
        self.assertAlmostEqual(len(loop) / song.SR, 2 * 4 * 60 / 110, delta=0.01)
        self.assertGreater(max(abs(v) for v in loop), 10000)


class BriefTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = kb_db.connect()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_manager_brief_matches_the_database(self):
        out = song.brief("Ohira", tema="derrotas")
        w, l = self.con.execute("SELECT SUM(result='W'), SUM(result='L') FROM games WHERE manager='eduohira'").fetchone()
        self.assertIn(f"{w} vitórias e {l} derrotas", out)
        top = self.con.execute("""SELECT opp_manager, SUM(result='L') l FROM games WHERE manager='eduohira'
                                  GROUP BY 1 ORDER BY l DESC LIMIT 1""").fetchone()
        self.assertIn(f"Quem mais venceu eduohira: {top[0]} {top[1]}x", out)
        for needle in ("Marcas:", "Por temporada", "Trocas:", "Waivers:", "COMO ESCREVER", "Foco do pedido: derrotas"):
            self.assertIn(needle, out)
        self.assertNotIn("ID Sleeper", out)

    def test_rivalry_player_and_league_briefs(self):
        h = song.brief("Ohira", "Lula")
        self.assertIn("Confronto direto eduohira x luizfelipesouza", h)
        self.assertIn("MATERIAL PARA A MÚSICA", song.brief(""))
        self.assertIn("Jogador: Josh Allen", song.brief("Josh Allen"))

    def test_brief_stays_small_enough_for_the_tool(self):
        self.assertLess(len(song.brief("Ohira", "Lula", "derrotas")), liga_core.MAX_CHARS)


class ToolTest(unittest.TestCase):
    def test_tool_routes_and_validates(self):
        self.assertIn("MATERIAL PARA A MÚSICA", liga_core.run_query("musica", {"alvo": "Ohira"}))
        self.assertIn("Não consegui cantar", liga_core.run_query("cantar", {}))
        self.assertIn("Não consegui cantar", liga_core.run_query("cantar", {"letra": "uma\nduas"}))
        self.assertIn("Não consegui cantar", liga_core.run_query("cantar", {"letra": "a\nb\nc\nd", "estilo": "opera"}))

    def test_plugin_and_help_list_the_new_queries(self):
        self.assertIn("musica", liga_core.CMDS)
        self.assertIn("cantar", liga_core.CMDS)
        self.assertEqual(set(liga_core.HELP), set(liga_core.CMDS))
        with open(os.path.join(ROOT, "data", "SOUL.md"), encoding="utf-8") as f:
            soul = f.read()
        self.assertIn("Música e rap", soul)
        self.assertIn("Sem pedido explícito, nunca faça música", soul)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class AudioAssemblyTest(unittest.TestCase):
    def test_sing_builds_a_voice_note_with_the_beat(self):
        def fake_tts(texts, workdir):  # stands in for Edge TTS: a short tone per verse
            out = []
            for i, t in enumerate(texts):
                p = os.path.join(workdir, f"l{i}.mp3")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={200 + 30 * i}:duration=3",
                                p], check=True)
                out.append(p)
            return out
        with mock.patch.object(song, "_synth_lines", fake_tts):
            res = song.sing("[Refrão]\nprimeiro verso 43\nsegundo verso\nterceiro verso\nquarto verso")
        self.assertTrue(res["path"].endswith(".ogg") and os.path.isfile(res["path"]))
        self.assertEqual(res["lines"], 4)
        bar = 4 * 60 / song.STYLES["rap"]
        self.assertAlmostEqual(res["seconds"], round((2 + 8 + 2) * bar), delta=1)
        head = open(res["path"], "rb").read(4)
        self.assertEqual(head, b"OggS")
        reply = song.media_reply(res)
        self.assertRegex(reply, r"(?m)^MEDIA:/.+\.ogg$")


if __name__ == "__main__":
    unittest.main()
