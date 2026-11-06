"""agy shim: `gemini-flash-latest` must resolve to the newest plain Gemini Flash in `agy models`, stay cached, recover
when agy withdraws the cached id, and leave pinned ids alone."""
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops"))

import agy_shim  # noqa: E402

# `agy models` as printed by agy 1.3.3 (the first line is progress noise)
LISTING = """Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-5-5-low\tClaude Opus 5.5 (Low)
claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""
NEWER = LISTING + "gemini-3.10-flash-low\tGemini 3.10 Flash (Low)\n"


class PickLatestFlashTest(unittest.TestCase):
    def test_picks_the_requested_effort_from_the_real_listing(self):
        self.assertEqual(agy_shim.pick_latest_flash(LISTING, "low"), "gemini-3.8-flash-low")
        self.assertEqual(agy_shim.pick_latest_flash(LISTING, "high"), "gemini-3.8-flash-high")

    def test_versions_compare_numerically(self):
        listing = "gemini-3.6-flash-low\tx\ngemini-3.10-flash-low\tx\ngemini-3.8-flash-low\tx\ngemini-4-flash-low\tx\n"
        self.assertEqual(agy_shim.pick_latest_flash(listing, "low"), "gemini-4-flash-low")
        self.assertEqual(agy_shim.pick_latest_flash(listing.replace("gemini-4-flash-low\tx\n", ""), "low"),
                         "gemini-3.10-flash-low")

    def test_only_plain_flash_counts(self):
        listing = ("gemini-9-flash-lite-low\tx\ngemini-9-pro-low\tx\ngemini-9-flash-image-low\tx\n"
                   "claude-flash-9-low\tx\ngemini-3.8-flash-low\tx\n")
        self.assertEqual(agy_shim.pick_latest_flash(listing, "low"), "gemini-3.8-flash-low")

    def test_none_when_nothing_matches(self):
        self.assertIsNone(agy_shim.pick_latest_flash(LISTING, "max"))
        self.assertIsNone(agy_shim.pick_latest_flash("", "low"))


class ShimCase(unittest.TestCase):
    def setUp(self):
        agy_shim._resolved.update(model=None, until=0.0)
        for p in (mock.patch.object(agy_shim, "MODEL", agy_shim.LATEST_ALIAS),
                  mock.patch.object(agy_shim, "EFFORT", "low")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(agy_shim._resolved.update, model=None, until=0.0)


class ResolveModelTest(ShimCase):
    def test_alias_resolves_and_is_cached(self):
        with mock.patch.object(agy_shim, "list_models", return_value=LISTING) as lm:
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.8-flash-low")
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.8-flash-low")
        self.assertEqual(lm.call_count, 1)

    def test_a_new_release_is_picked_up_after_the_ttl_or_when_forced(self):
        with mock.patch.object(agy_shim, "list_models", side_effect=[LISTING, NEWER, NEWER]) as lm:
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.8-flash-low")
            agy_shim._resolved["until"] = time.time() - 1  # TTL over
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.10-flash-low")
            self.assertEqual(agy_shim.resolve_model(force=True), "gemini-3.10-flash-low")
        self.assertEqual(lm.call_count, 3)

    def test_a_pinned_id_is_used_verbatim_without_a_lookup(self):
        with mock.patch.object(agy_shim, "MODEL", "gemini-3.8-flash-high"), \
                mock.patch.object(agy_shim, "list_models") as lm:
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.8-flash-high")
            self.assertEqual(agy_shim.resolve_model(force=True), "gemini-3.8-flash-high")
        lm.assert_not_called()

    def test_failed_lookup_keeps_the_last_good_id(self):
        with mock.patch.object(agy_shim, "list_models", side_effect=[LISTING, OSError("offline")]):
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.8-flash-low")
            agy_shim._resolved["until"] = time.time() - 1
            self.assertEqual(agy_shim.resolve_model(), "gemini-3.8-flash-low")
        self.assertGreater(agy_shim._resolved["until"], time.time())  # retry is deferred, not hammered per request

    def test_no_id_and_no_catalogue_is_an_error_the_caller_can_map_to_502(self):
        with mock.patch.object(agy_shim, "list_models", side_effect=subprocess.TimeoutExpired("agy", 30)):
            with self.assertRaises(RuntimeError):
                agy_shim.resolve_model()
        with mock.patch.object(agy_shim, "list_models", return_value="Fetching available models...\n"):
            with self.assertRaises(RuntimeError):
                agy_shim.resolve_model()


class RunAgyTest(unittest.TestCase):
    def test_unknown_model_error_from_the_cli_is_flagged_as_rejected(self):
        # what agy 1.3.3 printed for the withdrawn gemini-3.6-flash-low (stderr + JSON on stdout, rc=1)
        err = ('error: invalid model selection (--model "gemini-3.6-flash-low" --effort ""): model gemini-3.6-flash-low '
               'is not recognized as a known model or custom model in settings\nAvailable models:\n')
        out = '{"conversation_id":"","status":"ERROR","response":"","error":"invalid model selection"}'
        done = subprocess.CompletedProcess([], 1, stdout=out, stderr=err)
        with mock.patch.object(agy_shim, "_agy_run", return_value=done):
            with self.assertRaises(agy_shim.ModelRejected):
                agy_shim.run_agy("oi", "gemini-3.6-flash-low")

    def test_other_failures_are_plain_runtime_errors(self):
        done = subprocess.CompletedProcess([], 1, stdout='{"status":"ERROR","error":"quota"}', stderr="quota exceeded")
        with mock.patch.object(agy_shim, "_agy_run", return_value=done):
            with self.assertRaises(RuntimeError) as cm:
                agy_shim.run_agy("oi", "gemini-3.8-flash-low")
        self.assertNotIsInstance(cm.exception, agy_shim.ModelRejected)


class ChatRecoveryTest(ShimCase):
    BODY = {"model": "gemini-flash-latest", "messages": [{"role": "user", "content": "oi"}]}

    def test_withdrawn_cached_id_is_replaced_and_the_call_retried_once(self):
        agy_shim._resolved.update(model="gemini-3.6-flash-low", until=time.time() + 3600)  # stale: agy dropped it
        calls = []

        def fake_run(prompt, model, files=None):
            calls.append(model)
            if model == "gemini-3.6-flash-low":
                raise agy_shim.ModelRejected("agy status='ERROR' invalid model selection")
            return "tudo certo", {"usage": {}}
        with mock.patch.object(agy_shim, "list_models", return_value=LISTING), \
                mock.patch.object(agy_shim, "run_agy", side_effect=fake_run):
            res = agy_shim.chat(self.BODY)
        self.assertEqual(calls, ["gemini-3.6-flash-low", "gemini-3.8-flash-low"])
        self.assertEqual(res["choices"][0]["message"]["content"], "tudo certo")

    def test_rejection_of_the_id_the_catalogue_still_lists_is_not_retried(self):
        agy_shim._resolved.update(model="gemini-3.8-flash-low", until=time.time() + 3600)
        with mock.patch.object(agy_shim, "list_models", return_value=LISTING), \
                mock.patch.object(agy_shim, "run_agy", side_effect=agy_shim.ModelRejected("nope")) as run:
            with self.assertRaises(agy_shim.ModelRejected):
                agy_shim.chat(self.BODY)
        self.assertEqual(run.call_count, 1)

    def test_pinned_id_rejection_is_not_retried(self):
        with mock.patch.object(agy_shim, "MODEL", "gemini-3.6-flash-low"), \
                mock.patch.object(agy_shim, "run_agy", side_effect=agy_shim.ModelRejected("nope")) as run:
            with self.assertRaises(agy_shim.ModelRejected):
                agy_shim.chat(self.BODY)
        self.assertEqual(run.call_count, 1)


def _tool_call(name="liga"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": name, "arguments": "{}"}}]}


class ToolRoundsTest(unittest.TestCase):
    def test_counts_only_the_current_turn(self):
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "[Ana] antes"}, _tool_call(),
                {"role": "tool", "content": "x"}, {"role": "assistant", "content": "resposta antiga"},
                {"role": "user", "content": "[Ana] agora"}, _tool_call(), {"role": "tool", "content": "x"},
                _tool_call(), {"role": "tool", "content": "y"}]
        self.assertEqual(agy_shim.tool_rounds(msgs), 2)

    def test_hermes_empty_answer_nudge_does_not_restart_the_count(self):
        # what Hermes injected in the 13:08 incident: assistant "(empty)" + a synthetic `user` prompt, then more tools
        nudge = {"role": "user", "content": "You just executed tool calls but returned an empty response. Please "
                                            "process the tool results above and continue with the task."}
        msgs = [{"role": "user", "content": "[Ana] pedido"}, _tool_call(), {"role": "tool", "content": "x"},
                {"role": "assistant", "content": "(empty)"}, nudge, _tool_call(), {"role": "tool", "content": "y"}]
        self.assertEqual(agy_shim.tool_rounds(msgs), 2)

    def test_no_tools_used_yet(self):
        self.assertEqual(agy_shim.tool_rounds([{"role": "user", "content": "oi"}]), 0)


class WrapUpTest(ShimCase):
    TOOLS = [{"type": "function", "function": {"name": "liga", "description": "d", "parameters": {}}}]

    @staticmethod
    def _conv(rounds):
        msgs = [{"role": "user", "content": "[Ana] historico completo"}]
        for _ in range(rounds):
            msgs += [_tool_call(), {"role": "tool", "name": "liga", "content": "dados"}]
        return msgs

    def _chat(self, rounds, reply, limit=3):
        prompts = []

        def fake(prompt, model, files=None):
            prompts.append(prompt)
            return reply, {"usage": {}}
        with mock.patch.object(agy_shim, "MAX_TOOL_ROUNDS", limit), \
                mock.patch.object(agy_shim, "resolve_model", return_value="gemini-3.8-flash-low"), \
                mock.patch.object(agy_shim, "run_agy", side_effect=fake):
            res = agy_shim.chat({"messages": self._conv(rounds), "tools": self.TOOLS})
        return res, prompts[0]

    def test_tools_stay_available_until_the_budget_is_spent(self):
        res, prompt = self._chat(2, 'TOOL_CALL {"name": "liga", "arguments": {}}')
        self.assertIn("FERRAMENTAS DO BOT", prompt)
        self.assertNotIn("LIMITE DE CONSULTAS", prompt)
        self.assertEqual(res["choices"][0]["finish_reason"], "tool_calls")

    def test_tools_are_withheld_and_the_final_answer_requested_at_the_limit(self):
        res, prompt = self._chat(3, "Deu para levantar isto aqui.")
        self.assertNotIn("FERRAMENTAS DO BOT", prompt)
        self.assertIn("LIMITE DE CONSULTAS ATINGIDO", prompt)
        self.assertEqual(res["choices"][0]["finish_reason"], "stop")
        self.assertEqual(res["choices"][0]["message"]["content"], "Deu para levantar isto aqui.")

    def test_a_tool_call_at_the_limit_never_reaches_the_group(self):
        res, _ = self._chat(3, 'TOOL_CALL {"name": "liga", "arguments": {"consulta": "trocas"}}')
        msg = res["choices"][0]["message"]
        self.assertNotIn("tool_calls", msg)
        self.assertEqual(msg["content"], agy_shim.WRAP_UP_FALLBACK)


class ErrorDetailTest(unittest.TestCase):
    def test_error_field_is_reported_even_after_a_long_partial_response(self):
        # the 13:07 incident: status ERROR with a long `response`; `error` came after it and was cut off the log line
        out = '{"conversation_id":"c","status":"ERROR","response":"' + "x" * 400 + '","error":"RESOURCE_EXHAUSTED: quota"}'
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(agy_shim, "_agy_run", return_value=done):
            with self.assertRaises(RuntimeError) as cm:
                agy_shim.run_agy("oi", "gemini-3.8-flash-low")
        self.assertIn("RESOURCE_EXHAUSTED", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
