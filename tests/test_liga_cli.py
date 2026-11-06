"""ops/liga.py: from a Sleeper league id to an isolated, routed bot (offline: a fake Sleeper, a temp data root).

Run:  python3 -m unittest discover -s tests -p "test_liga_cli.py" -v
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import yaml

import league_fixtures as fx

ROOT = fx.ROOT
sys.path.insert(0, os.path.join(ROOT, "ops"))

import league_ctx  # noqa: E402
import liga  # noqa: E402


def make_data_root(tmp: str) -> str:
    """A throwaway `data/`: the real default config + league #1's league.yaml, no secrets, no live data."""
    data = os.path.join(tmp, "data")
    os.makedirs(os.path.join(data, "plugins", "sleeper-league"))
    os.makedirs(os.path.join(data, "scripts"))
    os.makedirs(os.path.join(data, "skills", "mr-irrelevant", "league-charts"))
    for name in ("config.yaml", "league.yaml"):
        shutil.copy(os.path.join(ROOT, "data", name), os.path.join(data, name))
    with open(os.path.join(data, ".env"), "w", encoding="utf-8") as f:
        f.write("AGY_SHIM_TOKEN=test-token-123456\nOPENROUTER_API_KEY=or-key\nWHATSAPP_GROUP_JID=main@g.us\n"
                "EVOLUTION_API_KEY=must-not-be-copied\n")
    return data


class CreateLeagueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = make_data_root(self.tmp.name)
        self.sleeper = fx.FakeSleeper()
        league_ctx.reset_cache()
        self.addCleanup(league_ctx.reset_cache)

    def create(self, **kw):
        args = dict(get=self.sleeper.get, admins=["5548999990000"], admin_name="Ana", out=lambda m: None)
        args.update(kw)
        return liga.create_league(self.data, "liga-falsa", fx.FakeSleeper.CUR, "120363111@g.us", **args)

    def test_creates_an_isolated_profile_from_only_the_sleeper_id(self):
        ctx = self.create()
        pdir = os.path.join(self.data, "profiles", "liga-falsa")
        for name in ("league.yaml", "config.yaml", "SOUL.md", ".env"):
            self.assertTrue(os.path.isfile(os.path.join(pdir, name)), name)
        for d in liga.PROFILE_DIRS:
            self.assertTrue(os.path.isdir(os.path.join(pdir, d)), d)
        self.assertEqual(oct(os.stat(os.path.join(pdir, ".env")).st_mode & 0o777), "0o600")
        self.assertTrue(os.path.isfile(os.path.join(pdir, "scripts", "..", "league.yaml")))      # symlinks resolve
        self.assertTrue(os.path.islink(os.path.join(pdir, "plugins")))
        self.assertTrue(os.path.isdir(os.path.join(pdir, "skills", "mr-irrelevant")))
        self.assertEqual((ctx.slug, ctx.league_id, ctx.group_jid), ("liga-falsa", "9002", "120363111@g.us"))
        self.assertFalse(ctx.legacy_layout)
        self.assertTrue(ctx.kb_dir.startswith(pdir))
        self.assertTrue(os.path.isfile(os.path.join(ctx.kb_dir, "matchups_history.json")))   # bootstrap ran

    def test_dm_routes_cover_every_spelling_of_the_chat_id(self):
        self.assertEqual(liga.dm_chat_ids("5548999990002"),
                         ["5548999990002", "554899990002", "5548999990002@s.whatsapp.net", "554899990002@s.whatsapp.net"])
        self.assertEqual(liga.dm_chat_ids("554899990002")[:2], ["554899990002", "5548999990002"])   # 9th digit both ways
        self.assertEqual(liga.dm_chat_ids("14155550100"), ["14155550100", "14155550100@s.whatsapp.net"])
        self.assertEqual(liga.dm_chat_ids("123456789012345@lid"), ["123456789012345@lid"])           # LIDs are exact
        self.assertEqual(liga.dm_chat_ids(""), [])
        self.create()
        with open(os.path.join(self.data, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        chat_ids = {r["chat_id"] for r in cfg["gateway"]["profile_routes"] if r["profile"] == "liga-falsa"}
        self.assertTrue({"120363111@g.us", "5548999990000", "5548999990000@s.whatsapp.net"} <= chat_ids, chat_ids)
        names = [r["name"] for r in cfg["gateway"]["profile_routes"]]
        self.assertEqual(len(names), len(set(names)), names)                      # route names stay unique

    def test_groups_lists_the_instance_groups_and_marks_the_used_ones(self):
        import subprocess
        self.create()
        payload = json.dumps([{"id": "120363111@g.us", "subject": "Liga Falsa"}, {"id": "main@g.us", "subject": "Principal"},
                              {"id": "120363777@g.us", "subject": "Nova liga"}])
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, payload, "")
        lines = []
        groups = liga.cmd_groups(self.data, "meu-container", out=lines.append, run=fake_run)
        self.assertEqual(calls[0][:3], ["docker", "exec", "meu-container"])
        self.assertEqual(len(groups), 3)
        by_id = {ln.split()[0]: ln for ln in lines}
        self.assertIn("[em uso]", by_id["120363111@g.us"])                 # a league's group
        self.assertIn("[em uso]", by_id["main@g.us"])                      # league #1's group (data/.env)
        self.assertNotIn("[em uso]", by_id["120363777@g.us"])              # free: the one to pass to `new --group`
        with self.assertRaises(liga.LigaError):
            liga.cmd_groups(self.data, "x", run=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such container"))

    def test_dm_contact_resolves_to_the_league_that_lists_it(self):
        """A DM has no group: the league is the one whose DM list has the number — the very list the routes come from."""
        self.create()
        league_ctx.reset_cache()
        self.assertEqual(league_ctx.league_for_sender(["5548999990000"], self.data).slug, "liga-falsa")
        self.assertEqual(league_ctx.league_for_sender(["554899990000"], self.data).slug, "liga-falsa")   # no 9th digit
        self.assertEqual(league_ctx.league_for_sender(["5511900000000"], self.data).legacy_layout, True)  # unknown: default

    def test_a_number_the_default_league_lists_stays_with_the_default_profile(self):
        self.create()
        with open(os.path.join(self.data, ".env"), "a", encoding="utf-8") as f:
            f.write("EVOLUTION_DM_ALLOWED=5548999990000\n")             # league #1's admin is also this league's admin
        league_ctx.reset_cache()
        chat_ids = {r["chat_id"] for r in liga._route_entries(self.data) if not r["chat_id"].endswith("@g.us")}
        self.assertEqual(chat_ids, set())                                # nothing routed away from the default
        problems = liga.doctor(self.data, "liga-falsa")
        self.assertTrue(any("também está em" in p and "5548999990000" in p for p in problems), problems)

    def test_default_mention_regex_strips_the_whole_summon(self):
        import re
        rx = liga.default_mention_regex("Mr. Irrelevant", "Brock Purdy")
        self.assertEqual(re.sub(rf"^\s*@?(?:{rx})[\s,:!\-]*", "", "Mr. Irrelevant, anota aí: x", flags=re.I), "anota aí: x")
        self.assertEqual(re.sub(rf"^\s*@?(?:{rx})[\s,:!\-]*", "", "brock purdy quem ganhou?", flags=re.I), "quem ganhou?")
        for text in ("fala Irrelevant", "mr irrelevant!", "Purdy?"):
            self.assertTrue(re.search(rx, text, re.I), text)
        self.assertIsNone(re.search(rx, "bom dia pessoal", re.I))

    def test_secrets_are_scoped_to_the_profile(self):
        self.create()
        env = league_ctx.read_env_file(os.path.join(self.data, "profiles", "liga-falsa", ".env"))
        self.assertEqual(env["AGY_SHIM_TOKEN"], "test-token-123456")        # Hermes scopes secrets per profile
        self.assertNotIn("EVOLUTION_API_KEY", env)                          # the WhatsApp connection stays with the default
        self.assertEqual(env["LEAGUE_ADMINS"], "5548999990000")
        self.assertEqual(env["EVOLUTION_ALLOW_ALL_USERS"], "true")

    def test_persona_carries_this_leagues_rules_and_nobody_elses(self):
        self.create()
        with open(os.path.join(self.data, "profiles", "liga-falsa", "SOUL.md"), encoding="utf-8") as f:
            soul = f.read()
        self.assertIn("Liga Falsa", soul)
        self.assertIn("NÃO há vaga para: IDP", soul)                         # K/DEF exist here, IDP does not
        self.assertIn("1 K", soul)
        self.assertIn("Time Ana", soul)                                     # managers read from Sleeper
        self.assertIn("Ana_Silva", soul)
        for league_one in ("High Stakes", "Brunão", "Bayer", "Floripa", "Riedel", "SUPER_FLEX"):
            self.assertNotIn(league_one, soul)
        self.assertNotIn("%%", soul)

    def test_hermes_config_reuses_the_model_chain_without_the_gateway_wiring(self):
        self.create()
        with open(os.path.join(self.data, "profiles", "liga-falsa", "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        default = liga._load_yaml(os.path.join(self.data, "config.yaml"))
        self.assertEqual(cfg["model"], default["model"])
        self.assertEqual(cfg["fallback_providers"], default["fallback_providers"])
        self.assertEqual(cfg["plugins"]["enabled"], ["sleeper-league"])     # no second WhatsApp adapter
        self.assertNotIn("gateway", cfg)
        self.assertNotIn("platforms", cfg)

    def test_routes_are_generated_idempotently_and_removed_with_the_league(self):
        self.create()
        path = os.path.join(self.data, "config.yaml")
        with open(path, encoding="utf-8") as f:
            first = f.read()
        gw = yaml.safe_load(first)["gateway"]
        self.assertTrue(gw["multiplex_profiles"])
        routes = gw["profile_routes"]
        self.assertEqual(routes[0], {"name": "liga-falsa", "platform": "evolution", "chat_id": "120363111@g.us",
                                     "profile": "liga-falsa"})
        # the league's admin may also DM the bot: that DM belongs to the league's profile, whichever way the chat is named
        self.assertEqual([r["chat_id"] for r in routes[1:]], liga.dm_chat_ids("5548999990000"))
        self.assertEqual({r["profile"] for r in routes}, {"liga-falsa"})
        self.assertEqual(liga.sync_routes(self.data), 1)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), first)                               # running sync again changes nothing
        self.assertIn("model:", first)                                      # the rest of the file is untouched
        shutil.rmtree(os.path.join(self.data, "profiles", "liga-falsa"))
        league_ctx.reset_cache()
        liga.sync_routes(self.data)
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("gateway:", f.read())

    def test_refuses_to_reuse_a_group_slug_or_league(self):
        self.create()
        with self.assertRaises(liga.LigaError):
            self.create()                                                   # same slug, no --force
        with self.assertRaises(liga.LigaError):
            liga.create_league(self.data, "outra", "9001", "120363111@g.us", get=self.sleeper.get, out=lambda m: None)
        with self.assertRaises(liga.LigaError):                              # the main league's own group
            liga.create_league(self.data, "terceira", "9001", "main@g.us", get=self.sleeper.get, out=lambda m: None)
        with self.assertRaises(liga.LigaError):
            liga.create_league(self.data, "quarta", "123", "x@g.us", get=self.sleeper.get, out=lambda m: None)  # not a group
        with self.assertRaises(liga.LigaError):
            liga.create_league(self.data, "quinta", "777777", "120363222@g.us", get=self.sleeper.get, out=lambda m: None)

    def test_cron_jobs_are_the_leagues_not_the_installations(self):
        cmds = liga.cron_commands("liga-falsa")
        text = "\n".join(cmds)
        self.assertIn("-p liga-falsa cron create", text)
        self.assertIn("--name liga-falsa-editorial-slots", text)
        self.assertIn("--name liga-falsa-session-reset", text)
        for global_job in ("provider-watch", "eleicao-watch"):
            self.assertNotIn(global_job, text)

    def test_second_league_does_not_disturb_the_first(self):
        first_ctx = self.create()
        second_ctx = liga.create_league(self.data, "segunda", fx.FakeSleeper.PREV, "120363333@g.us",
                                        get=self.sleeper.get, out=lambda m: None, bootstrap=False)
        groups = {r["profile"]: r["chat_id"] for r in liga._route_entries(self.data) if r["chat_id"].endswith("@g.us")}
        self.assertEqual(groups, {"liga-falsa": "120363111@g.us", "segunda": "120363333@g.us"})
        main = league_ctx.for_home(self.data)
        self.assertTrue(main.legacy_layout)                                  # league #1 keeps its layout
        homes = {c.home for c in (first_ctx, second_ctx, main)}
        self.assertEqual(len(homes), 3)
        self.assertEqual(len({first_ctx.kb_dir, second_ctx.kb_dir, main.kb_dir}), 3)
        self.assertEqual(len({first_ctx.state_dir, second_ctx.state_dir, main.state_dir}), 3)
        self.assertEqual(first_ctx.admins, ["5548999990000"])                # private settings are per league
        self.assertEqual(second_ctx.admins, [])
        self.assertEqual(league_ctx.for_group("120363333@g.us", self.data).slug, "segunda")
        self.assertEqual(league_ctx.league_for_group("120363111@g.us", self.data).slug, "liga-falsa")
        self.assertEqual(league_ctx.league_for_group("outro-grupo@g.us", self.data).slug, main.slug)   # unknown -> default
        self.assertIn("120363111@g.us", league_ctx.all_group_jids(self.data))

    def test_refresh_updates_only_the_managed_sections(self):
        self.create()
        soul_path = os.path.join(self.data, "profiles", "liga-falsa", "SOUL.md")
        with open(soul_path, encoding="utf-8") as f:
            soul = f.read()
        edited = soul.replace("## Estilo", "## Estilo\n- Linha escrita à mão pelo operador.")
        with open(soul_path, "w", encoding="utf-8") as f:
            f.write(edited)
        self.sleeper.users[0]["metadata"]["team_name"] = "Time Renomeado"
        liga.refresh_league(self.data, "liga-falsa", get=self.sleeper.get, out=lambda m: None)
        with open(soul_path, encoding="utf-8") as f:
            after = f.read()
        self.assertIn("Linha escrita à mão pelo operador.", after)
        self.assertIn("Time Renomeado", after)

    def test_doctor_is_clean_after_new_and_flags_missing_routes(self):
        self.create()
        with mock.patch.object(league_ctx.LeagueContext, "_download",
                               side_effect=AssertionError("the new league must answer from the data it was created with")):
            self.assertEqual(liga.doctor(self.data, "liga-falsa"), [])
            path = os.path.join(self.data, "config.yaml")
            with open(path, encoding="utf-8") as f:
                txt = f.read()
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt.replace("chat_id: \"120363111@g.us\"", "chat_id: \"outro@g.us\""))
            problems = liga.doctor(self.data, "liga-falsa")
        self.assertTrue(any("sem rota" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
