"""Cron (no_agent, every 5 min): live score DM for the admin's own matchup (see league/live_score.py).
No model call. Silent unless a starter of either team is playing (or a final is due). Off with LIVE_SCORE=0;
LIVE_SCORE_DRY_RUN=1 prints the messages and keeps the state untouched."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("live-score")
if os.environ.get("LIVE_SCORE", "1").lower() in ("0", "false", "no", "off"):
    sys.exit(0)

import league_time  # noqa: E402
from live_score import LiveScore  # noqa: E402

DRY = os.environ.get("LIVE_SCORE_DRY_RUN") == "1"
season, week = league_time.season_week()
msgs = LiveScore(dry=DRY).tick(season, week)
for m in msgs:
    if DRY:
        print("--- DRY RUN ---\n" + m)
    else:
        log.info("sent live score (%d chars)", len(m))
