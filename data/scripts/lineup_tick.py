"""Cron (no_agent, every 5 min): lineup advice DM for the admin's own team (see league/lineup_advisor.py).
Thursday/special days and Sunday: morning + 1 h before kickoff; Monday: same, only when something should change.
One model call per message (incremental analysis). Off with LINEUP_ADVICE=0; LINEUP_DRY_RUN=1 prints, sends nothing."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("lineup")
if os.environ.get("LINEUP_ADVICE", "1").lower() in ("0", "false", "no", "off"):
    sys.exit(0)

import league_time  # noqa: E402
from lineup_advisor import LineupAdvisor  # noqa: E402

DRY = os.environ.get("LINEUP_DRY_RUN") == "1"
season, week = league_time.season_week()
for text in LineupAdvisor(dry=DRY).tick(season, week):
    if DRY:
        print("--- DRY RUN ---\n" + text)
    else:
        log.info("sent lineup advice (%d chars)", len(text))
