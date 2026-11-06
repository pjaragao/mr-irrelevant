#!/usr/bin/env bash
# Return the WhatsApp group to the legacy sleeper-bot bridge (undo ops/cutover.sh).
set -euo pipefail
R=/home/paulojr/mr-irrelevant

echo "1/3 Evolution webhook → http://sleeper-bot:8650/webhook …"
docker exec -i mr-irrelevant /opt/hermes/.venv/bin/python - <<'PY'
import json, os, urllib.request
base, key, inst = os.environ["EVOLUTION_API_URL"], os.environ["EVOLUTION_API_KEY"], os.environ["EVOLUTION_INSTANCE"]
body = {"webhook": {"enabled": True, "url": "http://sleeper-bot:8650/webhook", "headers": {},
                    "byEvents": False, "base64": False, "events": ["MESSAGES_UPSERT"]}}
req = urllib.request.Request(f"{base}/webhook/set/{inst}", data=json.dumps(body).encode(),
                             headers={"apikey": key, "Content-Type": "application/json"}, method="POST")
print("  set:", urllib.request.urlopen(req, timeout=15).status)
PY

A=/home/paulojr/sleeper-bot/_archive_2026-09-28/hermes-default-skill-sleeper-fantasy
if [ -d "$A" ] && [ ! -d /home/paulojr/hermes-agent/data/skills/sleeper-fantasy ]; then
  cp -r "$A" /home/paulojr/hermes-agent/data/skills/sleeper-fantasy && echo "  restored sleeper-fantasy skill for the legacy bridge"
fi
echo "2/3 recreating legacy sleeper-bot from its compose file (image sleeper-bot:latest)…"
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v /home/paulojr/sleeper-bot:/home/paulojr/sleeper-bot \
  -w /home/paulojr/sleeper-bot docker:cli docker compose -p sleeper-bot up -d >/dev/null

echo "3/3 stopping mr-irrelevant (avoids duplicate scheduled posts)…"
docker stop -t 10 mr-irrelevant >/dev/null
echo "rolled back."
