#!/usr/bin/env bash
# Benchmark the agent path (questions that must NOT hit the fast path). Usage: bench.sh <label>
cd "$(dirname "$0")"
P=/opt/hermes/.venv/bin/python
$P fire.py "/new" >/dev/null 2>&1; sleep 3
for q in "como está meu confronto com o Leo? tenho chance?" \
         "o Josh Allen arrebentou nessa rodada né?" \
         "a produção do Jaxon Smith-Njigba é sustentável? me dá os números de alvos e rotas" \
         "quem ganhou a troca do Brock Bowers?"; do
  echo "=== [$1] $q"
  $P fire.py "$q"
  echo
done
