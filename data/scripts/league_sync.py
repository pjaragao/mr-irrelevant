"""Cron (no_agent, every 30 min): silent league maintenance. Never posts to the group.

* refreshes the live player DB (team + official injury status) from Sleeper /players/nfl
  (every 6h; every 1h on game days Thu/Sun/Mon);
* pre-kickoff injury snapshot (lesões da rodada flags 🆕 in-game injuries);
* waiver/FA ingestion during the processing windows (Thu/Sun 03–10h BRT), current + previous leg;
* Tuesday: consolidates the finished week once every game is final; Wed/Thu: applies stat corrections."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("sync")
import league_ctx  # noqa: E402
import players_db  # noqa: E402
import round_injuries  # noqa: E402
from transaction_monitor import TransactionMonitor  # noqa: E402

now = datetime.now(ZoneInfo(league_ctx.current().timezone))
wd = now.strftime("%a")

try:
    n = players_db.refresh(max_age_h=1.0 if wd in ("Thu", "Sun", "Mon") else 6.0)
    if n:
        log.info("players_db refreshed: %d players", n)
except Exception as exc:
    log.error("players_db refresh failed: %s", exc)

try:
    n = round_injuries.snapshot_pre_kickoff()
    if n:
        log.info("injury snapshots saved: %d", n)
except Exception as exc:
    log.error("snapshot failed: %s", exc)

mon = TransactionMonitor()
if wd in ("Thu", "Sun") and 3 <= now.hour <= 10:
    try:
        res = mon.check_and_process_waivers()
        log.info("waivers new=%d", len(res or []))
    except Exception as exc:
        log.error("waivers failed: %s", exc)
if wd == "Tue":
    try:
        log.info("weekly=%s", mon.check_and_sync_weekly_results())
    except Exception as exc:
        log.error("weekly sync failed: %s", exc)
if wd in ("Tue", "Wed"):   # player points per week (feeds `estatisticas entidade=pontuacao`); finished weeks are never refetched
    try:
        import player_scores
        player_scores.refresh()
    except Exception as exc:
        log.error("player_scores refresh failed: %s", exc)
if wd in ("Wed", "Thu") and now.minute < 30 and now.hour in (9, 21):
    try:
        fixed = mon.reconcile_week()
        if fixed:
            log.info("stat corrections applied: %d", fixed)
    except Exception as exc:
        log.error("reconcile failed: %s", exc)
