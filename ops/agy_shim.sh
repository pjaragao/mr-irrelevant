#!/usr/bin/env bash
# Start/stop the Antigravity (agy) OpenAI-compatible shim used by Mr Irrelevant's test group.
#   ops/agy_shim.sh start|stop|status|logs   (tmux, manual)   |   ops/agy_shim.sh run   (foreground; used by systemd)
# Production: systemd user unit `agy-shim.service` (ops/agy-shim.service) keeps it up across reboots and crashes.
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION=agyshim
LOG="$PWD/data/logs/agy_shim.log"
load_env() { set -a; . <(grep -E '^(AGY_SHIM_[A-Z_]+)=' data/.env); set +a; }
case "${1:-status}" in
  run)
    load_env
    exec python3 ops/agy_shim.py >> "$LOG" 2>&1 ;;
  start)
    tmux has-session -t "$SESSION" 2>/dev/null && { echo "already running"; exit 0; }
    load_env
    tmux new-session -d -s "$SESSION" "python3 ops/agy_shim.py 2>&1 | tee -a '$LOG'"
    sleep 1; curl -fsS "http://${AGY_SHIM_HOST:-172.26.0.1}:${AGY_SHIM_PORT:-8787}/health" && echo " started" ;;
  stop)   tmux kill-session -t "$SESSION" 2>/dev/null && echo stopped || echo "not running" ;;
  status) tmux has-session -t "$SESSION" 2>/dev/null && echo running || echo stopped ;;
  logs)   tail -n 40 "$LOG" ;;
  *) echo "usage: $0 start|stop|status|logs|run"; exit 2 ;;
esac
