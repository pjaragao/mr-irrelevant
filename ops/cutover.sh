#!/usr/bin/env bash
# Switch the WhatsApp group from the legacy sleeper-bot bridge to the isolated mr-irrelevant container.
# Reversible with ops/rollback.sh. Run from any shell that has the docker CLI + socket.
set -euo pipefail
R=/home/paulojr/mr-irrelevant
OLD=/home/paulojr/sleeper-bot
MAIN_GROUP="120363023364784524@g.us"
TEST_GROUP="120363432710019792@g.us"

echo "1/5 stopping legacy sleeper-bot (container kept for rollback)…"
docker stop -t 5 sleeper-bot >/dev/null || true

echo "2/5 syncing final league state (json + knowledge_base + dispatched slots)…"
cp -p "$OLD"/*.json "$R/league/"
cp -rp "$OLD/knowledge_base/." "$R/league/knowledge_base/"
cp -p "$OLD"/*.md "$R/league/" 2>/dev/null || true
rm -f "$R/league/SKILL.md"

echo "3/5 pointing mr-irrelevant at the main group…"
ENV="$R/data/.env"
sed -i "s|^EVOLUTION_ALLOWED_GROUPS=.*|EVOLUTION_ALLOWED_GROUPS=${MAIN_GROUP},${TEST_GROUP}|" "$ENV"
sed -i "s|^EVOLUTION_HOME_CHANNEL=.*|EVOLUTION_HOME_CHANNEL=${MAIN_GROUP}|" "$ENV"
sed -i "s|^WHATSAPP_GROUP_JID=.*|WHATSAPP_GROUP_JID=${MAIN_GROUP}|" "$ENV"
# env_file is read at container creation → recreate (not just restart)
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$R:$R" -w "$R" docker:cli \
  docker compose -p mr-irrelevant up -d --force-recreate >/dev/null
for i in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' mr-irrelevant 2>/dev/null)" = healthy ] && break; sleep 3
done
docker inspect -f 'mr-irrelevant: {{.State.Health.Status}}' mr-irrelevant

echo "4/5 switching Evolution webhook → http://mr-irrelevant:8660/webhook/evolution …"
docker exec -i mr-irrelevant /opt/hermes/.venv/bin/python - <<'PY'
import json, os, urllib.request
base, key, inst = os.environ["EVOLUTION_API_URL"], os.environ["EVOLUTION_API_KEY"], os.environ["EVOLUTION_INSTANCE"]
body = {"webhook": {"enabled": True, "url": "http://mr-irrelevant:8660/webhook/evolution",
                    "headers": {"X-Webhook-Token": os.environ["EVOLUTION_WEBHOOK_TOKEN"]},
                    "byEvents": False, "base64": False, "events": ["MESSAGES_UPSERT"]}}
req = urllib.request.Request(f"{base}/webhook/set/{inst}", data=json.dumps(body).encode(),
                             headers={"apikey": key, "Content-Type": "application/json"}, method="POST")
print("  set:", urllib.request.urlopen(req, timeout=15).status)
req = urllib.request.Request(f"{base}/webhook/find/{inst}", headers={"apikey": key})
w = json.loads(urllib.request.urlopen(req, timeout=15).read())
print("  now:", w.get("url"), "enabled=", w.get("enabled"), "events=", w.get("events"))
PY

echo "5/5 done. Mr Irrelevant (isolated) is live. Rollback: $R/ops/rollback.sh"
