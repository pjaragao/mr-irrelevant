"""run_slots.py YYYY-MM-DD HH:MM — one editorial tick for the league of $HERMES_HOME at a fixed local time."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.environ.get("LEAGUE_DIR", "/opt/league"))
import league_ctx  # noqa: E402
from scheduler_service import WeeklyScheduler  # noqa: E402

ctx = league_ctx.current()
d, t = sys.argv[1], sys.argv[2]
now = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(ctx.timezone))
sched = WeeklyScheduler()
sent = sched.check_and_dispatch(now=now)
print(f"league={ctx.slug} group={ctx.group_jid} now={now.isoformat()} dispatched={sent}")
