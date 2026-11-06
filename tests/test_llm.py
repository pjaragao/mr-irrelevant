"""Engine LLM client: it must follow the chat model chain from config.yaml (agy first, then the free fallbacks)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

import llm  # noqa: E402

CONFIG = """
model: {default: gemini-flash-latest, provider: agy, api_mode: chat_completions}
providers:
  agy: {base_url: "http://shim.test:8787/v1", key_env: AGY_SHIM_TOKEN}
fallback_providers:
  - {provider: openrouter, model: "nvidia/nemotron-3-ultra-550b-a55b:free"}
  - {provider: openrouter, model: "google/gemma-4-31b-it:free"}
  - {provider: openrouter, model: "nvidia/nemotron-3.5-lightning:free"}
"""


class LlmChainTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        with open(os.path.join(self.home.name, "config.yaml"), "w", encoding="utf-8") as f:
            f.write(CONFIG)
        self.env = mock.patch.dict(os.environ, {"HERMES_HOME": self.home.name, "AGY_SHIM_TOKEN": "tok",
                                                "OPENROUTER_API_KEY": "or-key"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.home.cleanup()

    def test_chain_is_read_from_config_in_order(self):
        self.assertEqual([(c["provider"], c["model"]) for c in llm.chain()], [
            ("agy", "gemini-flash-latest"), ("openrouter", "nvidia/nemotron-3-ultra-550b-a55b:free"),
            ("openrouter", "google/gemma-4-31b-it:free"), ("openrouter", "nvidia/nemotron-3.5-lightning:free")])
        self.assertEqual(llm.primary_model(), "gemini-flash-latest")

    def test_repo_config_matches_the_default_chain(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": os.path.join(ROOT, "data")}):
            self.assertEqual(llm.chain(), llm.DEFAULT_CHAIN)

    def test_unreadable_config_uses_the_default_chain(self):
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/nonexistent"}):
            self.assertEqual(llm.chain(), llm.DEFAULT_CHAIN)

    def test_first_answer_wins_and_agy_gets_the_shim_url_and_token(self):
        with mock.patch.object(llm, "_post", return_value="oi") as post:
            self.assertEqual(llm.chat("x", timeout=25), "oi")
        url, key, body, timeout = post.call_args[0]
        self.assertEqual((url, key, body["model"]), ("http://shim.test:8787/v1/chat/completions", "tok", "gemini-flash-latest"))
        self.assertGreaterEqual(timeout, llm.AGY_MIN_TIMEOUT)
        self.assertEqual(post.call_count, 1)

    def test_falls_through_the_chain_on_failure_and_on_empty_text(self):
        seen = []

        def fake(url, key, body, timeout):
            seen.append(body["model"])
            if len(seen) == 1:
                raise OSError("shim down")
            if len(seen) == 2:
                return ""
            return "resposta"
        with mock.patch.object(llm, "_post", side_effect=fake):
            self.assertEqual(llm.chat("x"), "resposta")
        self.assertEqual(seen, ["gemini-flash-latest", "nvidia/nemotron-3-ultra-550b-a55b:free", "google/gemma-4-31b-it:free"])

    def test_everything_failing_returns_empty_so_callers_keep_their_text(self):
        with mock.patch.object(llm, "_post", side_effect=OSError("down")):
            self.assertEqual(llm.chat("x"), "")

    def test_json_mode_and_reasoning_flags_per_provider(self):
        bodies = []

        def fake(url, key, body, timeout):
            bodies.append(body)
            if len(bodies) == 1:
                raise OSError("x")
            return "{}"
        with mock.patch.object(llm, "_post", side_effect=fake):
            llm.chat("x", json_mode=True)
        agy, orouter = bodies
        self.assertNotIn("response_format", agy)                       # the shim has no response_format
        self.assertIn("JSON", agy["messages"][0]["content"])           # ...so it is asked for in words
        self.assertEqual(orouter["response_format"], {"type": "json_object"})
        self.assertEqual(orouter["reasoning"], {"enabled": False, "exclude": True})

    def test_providers_without_a_key_are_skipped(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), mock.patch.object(llm, "_post", side_effect=OSError("x")) as post:
            llm.chat("x")
        self.assertEqual(post.call_count, 1)  # only agy; the three OpenRouter entries have no key


if __name__ == "__main__":
    unittest.main()
