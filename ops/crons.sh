#!/usr/bin/env bash
# (Re)create the Mr Irrelevant cron jobs inside the container. data/cron/ is not versioned, so this file is
# the source of truth for the schedule. Every job is no-agent and delivers "local": the scripts send WhatsApp
# messages themselves (EvolutionClient) and only mark a slot as done after Evolution confirms the delivery.
# Usage: ops/crons.sh            (prints the commands)   |   ops/crons.sh --apply   (runs them)
set -euo pipefail
H="docker exec -u hermes mr-irrelevant /opt/hermes/.venv/bin/hermes cron"
JOBS=(
  "editorial-slots|*/5 * * * *|editorial_tick.py"
  "trades-watch|*/15 * * * *|trades_watch.py"
  "league-sync|*/30 * * * *|league_sync.py"
  "data-lab-raio-x|*/30 11-21 * * 2|data_lab_raio_x.py"
  "data-lab-tendencias|0 12 * * 3|data_lab_tendencias.py"
  "data-lab-matchup|0 10 * * 5|data_lab_matchup.py"
  "session-reset|0 5 * * *|session_reset.py"
  "live-watch|*/5 * * * *|live_watch.py"
  "live-score|*/5 * * * *|live_score_tick.py"       # placar/jogadas do meu confronto por DM (sem IA)
  "lineup-advice|*/5 * * * *|lineup_tick.py"        # análise de escalação incremental por DM (qui/dom/seg + dias especiais)
  "piadista-tick|* * * * *|piadista_tick.py"       # piadas do piadista na cadência pedida pelo admin (silencioso sem fila)
  "provider-watch|17 * * * *|provider_watch.py"
  "eleicao-watch|* * 4-6,25-27 10 *|eleicao_watch.py"   # Eleições 2026 (DM do admin); vira no-op ao fim da apuração
)
for j in "${JOBS[@]}"; do
  IFS='|' read -r name sched script <<<"$j"
  cmd="$H create \"$sched\" --name $name --script $script --no-agent --deliver local --failure-deliver local"
  if [ "${1:-}" = "--apply" ]; then eval "$cmd"; else echo "$cmd"; fi
done
