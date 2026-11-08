"""Piadista: trigger, grant gating, queue/cadence, fixed destinations.

Run:  python3 -m unittest tests.test_piadista -v
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

_TMP = tempfile.mkdtemp(prefix="piadista_")
os.environ["LEAGUE_DB"] = os.path.join(_TMP, "league.db")
os.environ["LEAGUE_HOME"] = _TMP
os.environ["ADMIN_ALERT_JID"] = "5500000000000"
os.environ["WHATSAPP_GROUP_JID"] = "120000000000@g.us"

import league_ctx  # noqa: E402
import liga_core  # noqa: E402
import piadista  # noqa: E402


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


class PiadistaTest(unittest.TestCase):
    def setUp(self):
        piadista.revoke()
        try:
            os.remove(piadista._path("piadista_queue.json"))
        except OSError:
            pass

    def test_trigger_words(self):
        for t in ("solte 3 piadas sobre o Chico", "Faz uma piada do Lula", "zoa o Ari", "Piadista, manda ver"):
            self.assertTrue(piadista.is_trigger(t), t)
        for t in ("quem ganhou o jogo?", "como está meu confronto", "lesões da rodada"):
            self.assertFalse(piadista.is_trigger(t), t)

    def test_tools_refuse_without_grant(self):
        self.assertFalse(piadista.granted())
        self.assertIn("só saem por ordem do Paulo", piadista.brief("chico"))
        with self.assertRaises(PermissionError):
            piadista.enqueue(["a"], "dm")
        out = liga_core.run_query("piadas_enviar", {"piadas": "a\nb", "destino": "grupo"})
        self.assertIn("só saem por ordem do Paulo", out)
        self.assertEqual(piadista.pending(), [])

    def test_grant_expires(self):
        piadista.grant(now=1000)
        self.assertTrue(piadista.granted(now=1000 + piadista.GRANT_TTL - 1))
        self.assertFalse(piadista.granted(now=1000 + piadista.GRANT_TTL + 1))

    def test_parse_jokes(self):
        self.assertEqual(piadista.parse_jokes("1) um\n- dois\n\n• três"), ["um", "dois", "três"])
        for bad in ("", "x\n" * 13, "y" * 701):
            with self.assertRaises(ValueError):
                piadista.parse_jokes(bad)

    def test_destinations_are_fixed(self):
        self.assertEqual(piadista.destination("grupo"), "grupo")
        self.assertEqual(piadista.destination("DM"), "dm")
        self.assertEqual(piadista.destination(""), "dm")
        with self.assertRaises(ValueError):
            piadista.destination("5511999999999")

    def test_cadence_sends_one_now_and_the_rest_when_due(self):
        sent = []
        piadista.grant(now=1000)
        info = piadista.enqueue(["a", "b", "c"], "dm", 60, now=1000, spawn=False)
        self.assertEqual((info["queued"], info["gap"]), (3, 60.0))
        self.assertEqual(piadista.tick(now=1000, send=lambda d, t, f="texto": sent.append((d, t)) or True), 1)
        self.assertEqual(piadista.tick(now=1059, send=lambda d, t, f="texto": sent.append((d, t)) or True), 0)
        self.assertEqual(piadista.tick(now=1060, send=lambda d, t, f="texto": sent.append((d, t)) or True), 1)
        self.assertEqual(piadista.tick(now=1200, send=lambda d, t, f="texto": sent.append((d, t)) or True), 1)
        self.assertEqual(sent, [("dm", "a"), ("dm", "b"), ("dm", "c")])
        self.assertEqual(piadista.pending(), [])

    def test_failed_send_retries_then_gives_up(self):
        piadista.grant(now=1000)
        piadista.enqueue(["a"], "dm", now=1000, spawn=False)
        for i in range(piadista.MAX_TRIES):
            piadista.tick(now=1000 + 100 * i, send=lambda d, t, f="texto": False)
        self.assertEqual(piadista.pending(), [])
        self.assertEqual(piadista._queue()[0]["status"], "failed")

    def test_brief_with_grant_carries_order_and_rules(self):
        piadista.grant()
        out = piadista.brief("", "", "use 'pedal' como pejorativo", 3)
        self.assertIn("pedal", out)
        self.assertIn("PEDIDO DO PAULO", out)
        self.assertIn("piadas_enviar", out)


if __name__ == "__main__":
    unittest.main()


class VoiceTest(unittest.TestCase):
    def setUp(self):
        piadista.revoke()

    def test_audio_format_is_kept_in_the_queue_and_passed_to_the_sender(self):
        seen = []
        piadista.grant(now=1000)
        piadista.enqueue(["a"], "dm", now=1000, spawn=False, fmt="audio")
        piadista.tick(now=1000, send=lambda d, t, f="texto": seen.append(f) or True)
        self.assertEqual(seen, ["audio"])

    def test_transcribe_without_token_or_audio_returns_empty(self):
        import voice
        os.environ.pop("AGY_SHIM_TOKEN", None)
        self.assertEqual(voice.transcribe(b"abc"), "")
        self.assertEqual(voice.transcribe(b""), "")
