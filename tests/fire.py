#!/usr/bin/env python3
"""Fire simulated Evolution webhooks at mr-irrelevant and report latency / api calls / tokens / reply."""
import json, sqlite3, sys, time, urllib.request, re

HOME = "/home/paulojr/mr-irrelevant/data"
ENV = dict(l.rstrip("\n").split("=", 1) for l in open(f"{HOME}/.env") if "=" in l and not l.startswith("#"))
TOKEN = ENV["EVOLUTION_WEBHOOK_TOKEN"]
# Test group = last allowed group; admin phone/LID and bot LID come from .env (no personal numbers in git).
GROUP = ENV.get("FIRE_GROUP") or ENV["EVOLUTION_ALLOWED_GROUPS"].split(",")[-1].strip()
_dm = [x.strip() for x in ENV.get("EVOLUTION_DM_ALLOWED", "").split(",") if x.strip()]
_phone = next((x for x in _dm if x.startswith("55") and len(x) == 13), _dm[0] if _dm else "0")
_lid = next((x for x in _dm if not x.startswith("55")), _phone)
BOT_LID = next((x.strip() for x in ENV.get("EVOLUTION_BOT_IDS", "").split(",") if not x.strip().startswith("55")), "")
PAULO = (f"{_lid}@lid", f"{_phone}@s.whatsapp.net", "Paulo José Aragão Júnior")
OTHER = ("99999999999999@lid", "5548999990000@s.whatsapp.net", "Yan Riedel")


def post(text, who=PAULO, mention=True, mid=None):
    mid = mid or f"TEST-{int(time.time()*1000)}"
    ctx = {"mentionedJid": [f"{BOT_LID}@lid"]} if mention else {}
    body = {"event": "messages.upsert", "instance": "sleeper-bot", "data": {
        "key": {"remoteJid": GROUP, "fromMe": False, "id": mid, "participant": who[0], "participantAlt": who[1]},
        "pushName": who[2], "message": {"extendedTextMessage": {"text": (f"@{BOT_LID} " if mention else "") + text,
                                                                "contextInfo": ctx}}}}
    req = urllib.request.Request("http://mr-irrelevant:8660/webhook/evolution", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Webhook-Token": TOKEN})
    urllib.request.urlopen(req, timeout=10).read()
    return mid


def wait_reply(since_lines, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        lines = open(f"{HOME}/logs/gateway.log", encoding="utf-8", errors="replace").read().splitlines()[since_lines:]
        for l in lines:
            m = re.search(r"response ready: .*time=([\d.]+)s api_calls=(\d+) response=(\d+)", l)
            if m:
                return float(m.group(1)), int(m.group(2)), int(m.group(3))
            if "route=note" in l:
                return ("NOTE", "note", 0)
        time.sleep(1.5)
    return None


def last_session_stats():
    c = sqlite3.connect(f"{HOME}/state.db"); c.row_factory = sqlite3.Row
    s = c.execute("select s.id,s.input_tokens,s.output_tokens,s.cache_read_tokens,s.model from sessions s where s.source='evolution' order by (select max(id) from messages m where m.session_id=s.id) desc limit 1").fetchone()
    msgs = c.execute("select role,content,tool_calls from messages where session_id=? order by id", (s["id"],)).fetchall()
    last = next((m["content"] for m in reversed(msgs) if m["role"] == "assistant" and m["content"]), "")
    tools = [json.loads(m["tool_calls"])[0]["function"] for m in msgs if m["tool_calls"]]
    return dict(s), last, tools


if __name__ == "__main__":
    who = OTHER if "--other" in sys.argv else PAULO
    mention = "--nomention" not in sys.argv
    text = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    n = len(open(f"{HOME}/logs/gateway.log", encoding="utf-8", errors="replace").read().splitlines())
    before = last_session_stats()[0]
    post(text, who=who, mention=mention)
    r = wait_reply(n, timeout=15 if not mention else 120)
    if not r:
        print("NO AGENT REPLY (ignored / canned / timeout)")
        tail = open(f"{HOME}/logs/gateway.log", encoding="utf-8", errors="replace").read().splitlines()[n:]
        print("\n".join(x[:200] for x in tail if "evolution" in x.lower())[-800:])
        sys.exit(0)
    if r[0] == "NOTE":
        print("ADMIN NOTE saved (deterministic, 0 LLM tokens)")
        sys.exit(0)
    st, last, tools = last_session_stats()
    din = (st["input_tokens"] or 0) - ((before["input_tokens"] or 0) if before["id"] == st["id"] else 0)
    dout = (st["output_tokens"] or 0) - ((before["output_tokens"] or 0) if before["id"] == st["id"] else 0)
    print(f"time={r[0]}s api_calls={r[1]} in_tok=+{din} out_tok=+{dout} model={st['model']}")
    print("tools:", [f"{t['name']}({t['arguments'][:80]})" for t in tools][-4:])
    print("---\n" + last)
