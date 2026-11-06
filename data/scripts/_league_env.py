"""Shared bootstrap for Mr Irrelevant cron scripts.

* puts LEAGUE_DIR on sys.path;
* the league a job serves is decided by HERMES_HOME (Hermes sets it to the job's profile home, where league.yaml
  lives), so the same script runs unchanged for every league: `league_ctx.current()`;
* logs to stderr AND to /opt/data/logs/league-cron.log (Hermes cron output only keeps stdout);
* the engine sends WhatsApp messages itself (EvolutionClient) and only marks a slot/trade as done when
  Evolution confirms the delivery. Scripts print nothing on stdout, so Hermes delivers nothing.
"""
import logging
import os
import sys

LEAGUE_DIR = os.environ.get("LEAGUE_DIR", "/opt/league")
if LEAGUE_DIR not in sys.path:
    sys.path.insert(0, LEAGUE_DIR)

_HOME = os.environ.get("HERMES_HOME", "/opt/data")
_LOG_DIR = os.path.join(_HOME, "logs")


def _load_home_env() -> None:
    """Hermes scrubs secrets (EVOLUTION_API_KEY, ...) from cron script envs: refill the missing ones from
    HERMES_HOME/.env before the engine reads them at import (without it every send gets a 401)."""
    try:
        with open(os.path.join(_HOME, ".env"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key and not os.environ.get(key):
            os.environ[key] = val


_load_home_env()


def setup_logging(name: str) -> logging.Logger:
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(_LOG_DIR, "league-cron.log"), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format=f"%(asctime)s [{name}] %(levelname)s %(name)s: %(message)s")
    return logging.getLogger(name)
