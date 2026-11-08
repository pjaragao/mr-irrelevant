"""Cron (no_agent, every minute): sends the queued piadista jokes that are due (long cadences; short ones are handled
by the detached drain started when the admin orders them). Silent. See league/piadista.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("piadista")

import league_ctx  # noqa: E402
import piadista  # noqa: E402

try:
    with league_ctx.use(league_ctx.current()):
        if piadista.pending():
            n = piadista.tick()
            log.info("piadista tick: %d sent, %d pending", n, len(piadista.pending()))
except Exception as exc:  # never let a joke break the cron runner
    log.warning("piadista tick failed: %s", exc)
