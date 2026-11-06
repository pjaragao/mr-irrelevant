"""Cron (no_agent): Data Lab editorial slots — real NFL advanced stats for every team of the league.

usage: data_lab_slot.py raio_x | tendencias | matchup
Dedupe per season/week/kind in the league's state dir (data_lab_dispatched.json), so the Tuesday job can tick every
30 min and only posts once nflverse has published the finished week (Monday night included).
The message is sent directly (EvolutionClient) and only marked as done when Evolution confirms it.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402  (LEAGUE_DIR on sys.path + logging)
log = _league_env.setup_logging("data_lab")
import data_lab  # noqa: E402
import league_ctx  # noqa: E402

ctx = league_ctx.current()
kind = sys.argv[1] if len(sys.argv) > 1 else "raio_x"
season = data_lab._season()
done = data_lab.last_complete_week(season)
week = done + 1 if kind == "matchup" else done
state_file = ctx.state_path("data_lab_dispatched.json")
try:
    sent = json.load(open(state_file, encoding="utf-8"))
except Exception:
    sent = {}
key = f"{season}_W{week}_{kind}"
if key in sent:
    sys.exit(0)

if kind == "raio_x":
    # Only once the whole week (incl. MNF) is in the play-by-play.
    # On Tuesday Sleeper already shows the NEXT week; the round to dissect is the one before it
    # (unless games of the current week have already been played).
    from sleeper_client import SleeperClient
    import league_time
    cur = int(SleeperClient(ctx.league_id).get_nfl_state().get("week") or done)
    if cur > 1 and not any(g["state"] != "pre" for g in league_time.week_games(cur, season)):
        cur -= 1
    if os.environ.get("DATA_LAB_FORCE") != "1" and not data_lab.week_fully_loaded(season, cur):
        print(f"[data_lab] week {cur} not fully in nflverse yet", file=sys.stderr)
        sys.exit(0)
    week = cur
    key = f"{season}_W{week}_{kind}"
    if key in sent:
        sys.exit(0)
    text = data_lab.raio_x(week)
elif kind == "tendencias":
    text = data_lab.tendencias(week)
elif kind == "matchup":
    text = data_lab.matchup_lab(week)
else:
    sys.exit(f"unknown kind {kind}")

if text and text.strip():
    from evolution_client import EvolutionClient, sent_ok
    group = ctx.group_jid
    res = EvolutionClient().send_message(group, text) if group else {"error": "group_jid ausente (league.yaml)"}
    if sent_ok(res):
        sent[key] = True
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        json.dump(sent, open(state_file, "w", encoding="utf-8"), indent=1)
        log.info("sent %s", key)
    else:
        log.error("send failed for %s: %s", key, res)
