"""Cron (no_agent, daily 05:00): start a fresh conversation in each WhatsApp group.

Hermes 0.21 never resets a routed session by time (`session_reset` in config.yaml is inert). Ending the
session row in state.db makes the gateway's routing self-heal open a brand-new session on the next message
(no notice is posted). Sessions active in the last `IDLE_MIN` minutes are left alone. Silent (empty stdout).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("session_reset")
IDLE_MIN = int(os.environ.get("SESSION_RESET_IDLE_MIN", "120"))

try:
    from hermes_state import SessionDB
except Exception as exc:  # pragma: no cover - only inside the Hermes image
    log.error("hermes_state unavailable: %s", exc)
    sys.exit(0)

import sqlite3  # noqa: E402

db = SessionDB()

path = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "state.db")
ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
rows = ro.execute(
    "SELECT s.id, COALESCE((SELECT MAX(timestamp) FROM messages m WHERE m.session_id = s.id), s.started_at) "
    "FROM sessions s WHERE s.source = 'evolution' AND s.chat_type = 'group' AND s.ended_at IS NULL"
).fetchall()
ro.close()

now = time.time()
for sid, last in rows:
    idle = (now - float(last or now)) / 60.0
    if idle < IDLE_MIN:
        log.info("keep %s (active %.0f min ago)", sid, idle)
        continue
    db.end_session(sid, "daily_reset")
    log.info("ended %s (idle %.0f min) — next message opens a fresh session", sid, idle)
