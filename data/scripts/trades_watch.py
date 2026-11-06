"""Cron (no_agent, every 15 min): new official trades.

The engine writes the verdict with the LLM (OpenRouter, deterministic fallback), sends the announcement and
only records the trade after Evolution confirms the delivery — a failed send is retried on the next run."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("trades")
from transaction_monitor import TransactionMonitor  # noqa: E402

new = TransactionMonitor().check_and_process_trades(notify_whatsapp=True)
if new:
    log.info("trades announced: %s", ", ".join(t["transaction_id"] for t in new))
