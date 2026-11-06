"""Cron (no_agent, every 5 min): weekly editorial slots (see league/scheduler_service.py).
Messages are sent by the engine with delivery confirmation; stdout stays empty (Hermes delivers nothing)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("editorial")
from scheduler_service import WeeklyScheduler  # noqa: E402

sent = WeeklyScheduler().check_and_dispatch()
if sent:
    log.info("dispatched: %s", ", ".join(sent))
