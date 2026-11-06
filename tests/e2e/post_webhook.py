#!/usr/bin/env python3
"""post_webhook.py <jid> <sender_digits> <push_name> <text> [msg_id] — a MESSAGES_UPSERT like Evolution sends."""
import json
import os
import sys
import urllib.request
import uuid

jid, sender, push, text = sys.argv[1:5]
mid = sys.argv[5] if len(sys.argv) > 5 else "E2E" + uuid.uuid4().hex[:16]
body = {"event": "messages.upsert", "instance": "e2e", "data": {
    "key": {"remoteJid": jid, "fromMe": False, "id": mid, **({"participant": f"{sender}@s.whatsapp.net"} if jid.endswith("@g.us") else {})},
    "pushName": push, "message": {"conversation": text}, "messageType": "conversation"}}
req = urllib.request.Request("http://127.0.0.1:8660/webhook/evolution", data=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json",
                                      "X-Webhook-Token": os.environ.get("EVOLUTION_WEBHOOK_TOKEN", "e2e-hook")})
print(urllib.request.urlopen(req, timeout=10).read().decode())
