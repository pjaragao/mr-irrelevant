#!/usr/bin/env python3
"""End-to-end check of the multi-league gateway in a THROWAWAY container (same pinned Hermes image as production).

Nothing here touches production: the container has no network at all (`--network none`), its HERMES_HOME and league
dir are scratch copies, and WhatsApp (Evolution), the LLM and Sleeper are mocks started inside it (`mocks.py`).

    python3 tests/e2e/e2e.py up          build the scratch environment, create a 2nd league with ops/liga.py, start it
    python3 tests/e2e/scenarios.py       drive both groups through the real gateway and assert isolation
    python3 tests/e2e/e2e.py down        remove the container (the scratch dir stays for inspection)
    python3 tests/e2e/e2e.py run         up + scenarios + down (exit code = scenarios')

Run it after upgrading the pinned Hermes image or touching the adapter/plugin/CLI: it is the only test that exercises
Hermes' own routing (`gateway.profile_routes`), per-turn profile scoping and per-profile cron. Needs docker; ~2 minutes.
Scratch dir: $E2E_RUN_DIR (default /tmp/mr-irrelevant-e2e).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
RUN = os.environ.get("E2E_RUN_DIR") or os.path.join(tempfile.gettempdir(), "mr-irrelevant-e2e")
LOGS = os.path.join(RUN, "logs")
NAME = "e2e-multiliga"                      # never the production container's name
GROUP_ONE, GROUP_TWO = "120363000000000001@g.us", "120363000000000002@g.us"
LEAGUE_ONE_ID = "1312694161610113024"
# engine state that belongs to the running production bot: not copied, the scratch league starts clean
RUNTIME_STATE = {"dispatched_slots.json", "dispatch_failures.json", "live_state.json", "flavor_cache.json",
                 "news_cache.json", "provider_state.json", "eleicao_state.json", "data_lab_dispatched.json",
                 "synced_transactions.json", "synced_weeks.json", "league.db", "league.db-wal", "league.db-shm",
                 "round_injuries_cache.json", "injury_snapshots.json", "jev_cache.json"}

ENV = """AGY_SHIM_TOKEN=e2e-shim-token
EVOLUTION_API_URL=http://127.0.0.1:18081
EVOLUTION_API_KEY=e2e-evolution-key
EVOLUTION_INSTANCE=e2e
EVOLUTION_WEBHOOK_TOKEN=e2e-hook
EVOLUTION_WEBHOOK_PORT=8660
EVOLUTION_ALLOW_ALL_USERS=true
EVOLUTION_MENTION_REGEX=irrelevant|purdy
EVOLUTION_ADMINS=5548900000099
EVOLUTION_BOT_IDS=5548900000000
EVOLUTION_RATE_LIMIT=500
WHATSAPP_GROUP_JID={g1}
EVOLUTION_HOME_CHANNEL={g1}
ADMIN_ALERT_JID=5548900000099@s.whatsapp.net
"""

PROBE = '''"""e2e: what league does a cron job of this profile serve?"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: F401
import league_ctx
ctx = league_ctx.current()
rec = {"HERMES_HOME": os.environ.get("HERMES_HOME"), "slug": ctx.slug, "league_id": ctx.league_id,
       "group": ctx.group_jid, "kb_dir": ctx.kb_dir, "state_dir": ctx.state_dir, "persona": ctx.persona_name,
       "format": ctx.profile.format_label(), "evolution_url": os.environ.get("EVOLUTION_API_URL"),
       "has_model_key": bool(os.environ.get("AGY_SHIM_TOKEN"))}
with open("/logs/cron_probe.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(rec) + "\\n")
print("probe ok")
'''


def sh(*cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)


def pinned_image() -> str:
    with open(os.path.join(REPO, "docker-compose.yml"), encoding="utf-8") as f:
        m = re.search(r"^\s*image:\s*(\S+)", f.read(), re.M)
    if not m:
        raise SystemExit("cannot find the pinned image in docker-compose.yml")
    return m.group(1)


def prepare() -> None:
    shutil.rmtree(RUN, ignore_errors=True)
    data = os.path.join(RUN, "data")
    os.makedirs(data)
    os.makedirs(LOGS)
    os.chmod(LOGS, 0o777)
    for name in ("config.yaml", "SOUL.md", "league.yaml"):
        shutil.copy(os.path.join(REPO, "data", name), os.path.join(data, name))
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    for sub in ("plugins", "scripts"):
        shutil.copytree(os.path.join(REPO, "data", sub), os.path.join(data, sub), ignore=ign)
    os.makedirs(os.path.join(data, "skills"))
    shutil.copytree(os.path.join(REPO, "data", "skills", "mr-irrelevant"),
                    os.path.join(data, "skills", "mr-irrelevant"), ignore=ign)
    shutil.copytree(os.path.join(REPO, "league"), os.path.join(RUN, "league"),
                    ignore=lambda _d, names: [n for n in names if n in RUNTIME_STATE or n == "__pycache__"
                                              or n.endswith(".pyc")])
    cfg_path = os.path.join(data, "config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = f.read()
    cfg = re.sub(r"base_url:\s*http\S+", "base_url: http://127.0.0.1:18082/v1", cfg, count=1)   # the mock LLM
    cfg = re.sub(r"(?ms)^fallback_providers:.*?(?=^\S)", "", cfg)          # nothing may leave the sandbox
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(cfg)
    with open(os.path.join(data, ".env"), "w", encoding="utf-8") as f:
        f.write(ENV.format(g1=GROUP_ONE))
    os.chmod(os.path.join(data, ".env"), 0o600)
    with open(os.path.join(data, "scripts", "e2e_probe.py"), "w", encoding="utf-8") as f:
        f.write(PROBE)


def create_second_league() -> None:
    """The league is created exactly as an operator would: `ops/liga.py new` with a Sleeper id (here, a mock)."""
    sys.path.insert(0, HERE)
    os.environ["E2E_REPO"], os.environ["E2E_LOGS"] = REPO, os.path.join(RUN, "host-logs")
    os.makedirs(os.environ["E2E_LOGS"], exist_ok=True)
    import mocks
    srv = mocks.sleeper_server(18080)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env = dict(os.environ, SLEEPER_API_BASE="http://127.0.0.1:18080/v1")
    r = subprocess.run([sys.executable, os.path.join(REPO, "ops", "liga.py"), "--data", os.path.join(RUN, "data"),
                        "--container", NAME, "new", "liga-falsa", "--league-id", "9002", "--group", GROUP_TWO,
                        "--admin", "5548900000002", "--admin-name", "Ana", "--phones", "1:5548900000011;2:5548900000012"],
                       env=env, text=True, capture_output=True)
    srv.shutdown()
    if r.returncode:
        print(r.stdout[-3000:], r.stderr[-1500:])
        raise SystemExit(f"liga.py new failed ({r.returncode})")
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "league created")


def up() -> None:
    sh("docker", "rm", "-f", NAME, check=False)
    prepare()
    create_second_league()
    args = ["docker", "run", "-d", "--name", NAME, "--network", "none", "--security-opt", "no-new-privileges:true",
            "-e", "HERMES_UID=1000", "-e", "HERMES_GID=1000", "-e", "TZ=America/Sao_Paulo",
            "-e", "LEAGUE_DIR=/opt/league", "-e", f"SLEEPER_LEAGUE_ID={LEAGUE_ONE_ID}",
            "-e", "SLEEPER_API_BASE=http://127.0.0.1:18080/v1", "-e", "E2E_REPO=/repo", "-e", "E2E_LOGS=/logs",
            "--env-file", os.path.join(RUN, "data", ".env"),
            "-v", f"{RUN}/data:/opt/data", "-v", f"{RUN}/league:/opt/league", "-v", f"{REPO}:/repo:ro",
            "-v", f"{HERE}:/mocks:ro", "-v", f"{LOGS}:/logs", pinned_image(), "gateway", "run"]
    sh(*args)
    for kind in ("sleeper", "evolution", "llm"):
        sh("docker", "exec", "-d", NAME, "/opt/hermes/.venv/bin/python", "/mocks/mocks.py", kind)
    t0 = time.time()
    while time.time() - t0 < 150:
        r = sh("docker", "exec", NAME, "curl", "-fsS", "http://127.0.0.1:8660/health", check=False)
        if r.returncode == 0:
            print(f"gateway healthy after {time.time() - t0:.0f}s")
            return
        time.sleep(2)
    print(sh("docker", "logs", "--tail", "80", NAME, check=False).stdout)
    raise SystemExit("gateway did not become healthy")


def down() -> None:
    sh("docker", "rm", "-f", NAME, check=False)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "up":
        up()
    elif cmd == "down":
        down()
    elif cmd == "run":
        up()
        try:
            code = subprocess.run([sys.executable, os.path.join(HERE, "scenarios.py")]).returncode
        finally:
            down()
        sys.exit(code)
    else:
        raise SystemExit(__doc__)
