"""One-off: resend today's missed editorial slots, 15 min apart."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env
log = _league_env.setup_logging("resend")
from datetime import datetime, timedelta
from scheduler_service import WeeklyScheduler, save_dispatched_slot
KEYS = ["opening_08h_waivers", "opening_09h_standings", "opening_matchup_1", "opening_matchup_2"]
s = WeeklyScheduler(); season, week = s._state()
fns = {sl[0]: sl[2] for sl in s.slot_table(datetime.now(s.tz).replace(hour=8, minute=5), week)}
for i, key in enumerate(KEYS):
    sid = f"{season}_W{week}_{key}"
    ok, err = s._send_parts(sid, fns[key]())
    if ok:
        save_dispatched_slot(sid)
    log.info("resend %s: %s %s", sid, "ok" if ok else "FALHOU", err)
    if i < len(KEYS) - 1:
        time.sleep(900)
