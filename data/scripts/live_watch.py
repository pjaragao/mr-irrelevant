"""Cron (no_agent, every 5 min): live alerts while NFL games of the current week are being played.

* 🚑 a league STARTER shows up in the ESPN in-game injury report (dated after kickoff, not on the
  pre-game list) — once per player/week;
* 🔄 a matchup changes leader — EVERY time (see league/lead_watch.py): new leader ahead by ≥ LIVE_LEAD_MIN pts,
  alerts of one matchup at least LIVE_LEAD_COOLDOWN_MIN apart (deferred, never dropped).
Silent when no game is live (lead changes keep being checked for LIVE_GRACE_MIN after the last game, since Sleeper
scores trail the final whistle). Daily cap: LIVE_MAX_PER_DAY (default 30). Off with LIVE_ALERTS=0.
Messages go straight to the group and are recorded only after Evolution confirms them."""
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("live")
if os.environ.get("LIVE_ALERTS", "1").lower() in ("0", "false", "no", "off"):
    sys.exit(0)

import lead_watch  # noqa: E402
import league_ctx  # noqa: E402
import round_injuries  # noqa: E402
from evolution_client import EvolutionClient, sent_ok  # noqa: E402
from sleeper_client import SleeperClient  # noqa: E402

CTX = league_ctx.current()           # the league of this job's profile (HERMES_HOME)
STATE = CTX.state_path("live_state.json")
GROUP = CTX.group_jid
MAX_PER_DAY = int(os.environ.get("LIVE_MAX_PER_DAY", "30"))
DRY = os.environ.get("LIVE_DRY_RUN") == "1"  # print instead of sending, keep state untouched
LEAD_MIN = float(os.environ.get("LIVE_LEAD_MIN", "3"))
LEAD_COOLDOWN_S = 60 * float(os.environ.get("LIVE_LEAD_COOLDOWN_MIN", "20"))
GRACE_S = 60 * float(os.environ.get("LIVE_GRACE_MIN", "30"))

sleeper = SleeperClient()
st = sleeper.get_nfl_state()
season, week = str(st.get("season")), int(st.get("week") or 1)
games = round_injuries._scoreboard(week, season)
live = [g for g in games if g["state"] == "in"]
if not (GROUP or DRY):
    sys.exit(0)

try:
    state = json.load(open(STATE, encoding="utf-8"))
except Exception:
    state = {}
if live:
    state["last_live_at"] = time.time()
elif time.time() - float(state.get("last_live_at") or 0) > GRACE_S:
    sys.exit(0)
today = datetime.now(ZoneInfo(CTX.timezone)).strftime("%Y-%m-%d")
if state.get("day") != today:
    state.update(day=today, sent_today=0)
state.setdefault("injuries", {})
state.setdefault("leaders", {})
state.setdefault("lead_alert_at", {})
evo = EvolutionClient()


def send(text: str) -> bool:
    if DRY:
        print("--- DRY RUN ---\n" + text)
        return True
    if state["sent_today"] >= MAX_PER_DAY:
        log.info("daily cap reached, skipping: %s", text[:60])
        return False
    ok = sent_ok(evo.send_message(GROUP, text))
    if ok:
        state["sent_today"] += 1
    return ok


# 1) in-game injuries of league starters
idx = round_injuries._league_index(sleeper, week)
for g in live:
    gi = round_injuries._game_injuries(g)
    for it in gi.get("items", []):
        owner = idx.get(round_injuries._norm(it["name"]))
        if not owner or not owner.get("starter") or it.get("new") is False:
            continue
        key = f"{season}-{week}-{round_injuries._norm(it['name'])}"
        if key in state["injuries"]:
            continue
        status = round_injuries.STATUS_PT.get(it["status"], it["status"] or "?")
        body = round_injuries.BODY_PT.get(it["body"], it["body"]) if it.get("body") else "lesão não especificada"
        text = (f"🚑 *Lesão em jogo* — {it['name']} ({it['pos']}, {it['team']}) saiu machucado em {gi['game']}\n"
                f"• {status}, {body}\n• Titular de *{owner['team_name']}* ({owner['manager']})")
        if send(text):
            state["injuries"][key] = time.time()
            log.info("injury alert: %s", it["name"])

# 2) lead changes (every one of them; finished matchups included — the last play can flip it)
for alert in lead_watch.lead_changes(sleeper.get_matchups(week), state, season, week, time.time(),
                                     LEAD_MIN, LEAD_COOLDOWN_S):
    if send(alert["text"]):
        lead_watch.commit(state, alert, time.time())
        log.info("lead change alert: %s (#%d)", alert["key"], alert["n"])

if DRY:
    sys.exit(0)
os.makedirs(os.path.dirname(STATE), exist_ok=True)
tmp = STATE + ".tmp"
json.dump(state, open(tmp, "w", encoding="utf-8"))
os.replace(tmp, STATE)
