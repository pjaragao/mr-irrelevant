#!/usr/bin/env python3
"""Mock Sleeper / Evolution API / OpenAI-compatible LLM for the multi-league end-to-end test (stdlib only).
Runs INSIDE the throwaway container (see e2e.py); never talks to the real services.

    mocks.py sleeper [port]     two leagues: league #1 (12-slot dynasty) and "Liga Falsa" (redraft with K/DEF)
    mocks.py evolution [port]   records every outbound WhatsApp call to $E2E_LOGS/evolution.jsonl
    mocks.py llm [port]         chat.completions: calls the `liga` tool once, then answers with its result
"""
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.environ.get("E2E_REPO", "/repo")
sys.path.insert(0, os.path.join(REPO, "tests"))
LOGS = os.environ.get("E2E_LOGS", "/logs")
_LOCK = threading.Lock()


def log(name: str, rec: dict) -> None:
    rec = {"ts": round(time.time(), 3), **rec}
    with _LOCK:
        with open(os.path.join(LOGS, name), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def _json(self, code: int, obj) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}


# --------------------------------------------------------------------------- Sleeper
def make_sleepers():
    import league_fixtures as fx

    class LeagueOne(fx.FakeSleeper):
        CUR, PREV = "1312694161610113024", "1312694161610113023"

        def _league(self, lid):
            lg = super()._league(lid)          # a dynasty league with a mid-November playoff, unlike "Liga Falsa"
            lg["settings"].update({"type": 2, "playoff_week_start": 15, "playoff_teams": 4})
            return lg

    one = LeagueOne(slots=fx.DYNASTY_SLOTS, name="High Stakes Fantasy")
    two = fx.FakeSleeper(slots=fx.REDRAFT_SLOTS, name="Liga Falsa")
    return one, two


def sleeper_server(port: int):
    one, two = make_sleepers()

    def pick(path: str):
        m = re.match(r"(?:league/|draft/d?)(\d+)", path)
        lid = m.group(1) if m else ""
        return one if lid in (one.CUR, one.PREV) else two

    class H(Quiet):
        def do_GET(self):
            path = self.path.split("?")[0]
            path = path.split("/v1/", 1)[-1] if "/v1/" in path else path.lstrip("/")
            log("sleeper.jsonl", {"path": path})
            if path.startswith("players/nfl/trending"):
                return self._json(200, [])
            if path.startswith("players"):
                return self._json(200, {})
            try:
                out = pick(path).get(path)
            except Exception as exc:  # noqa: BLE001
                log("sleeper.jsonl", {"error": repr(exc), "path": path})
                out = None
            self._json(200, out)
    return ThreadingHTTPServer(("127.0.0.1", port), H)


# --------------------------------------------------------------------------- Evolution
def evolution_server(port: int):
    counter = {"n": 0}

    class H(Quiet):
        def do_POST(self):
            body = self._body()
            counter["n"] += 1
            small = {k: (v if k != "media" else f"<{len(v)} b64 chars>") for k, v in body.items()}
            log("evolution.jsonl", {"path": self.path, "apikey": self.headers.get("apikey", ""), "body": small})
            self._json(201, {"key": {"id": f"MOCK-{counter['n']}", "remoteJid": body.get("number")}, "status": "PENDING"})

        def do_GET(self):
            if "/group/fetchAllGroups" in self.path:
                return self._json(200, [{"id": "120363000000000001@g.us", "subject": "High Stakes"},
                                        {"id": "120363000000000002@g.us", "subject": "Liga Falsa"},
                                        {"id": "120363000000000003@g.us", "subject": "Terceira Liga"}])
            self._json(200, {"instance": {"state": "open"}})
    return ThreadingHTTPServer(("127.0.0.1", port), H)


# --------------------------------------------------------------------------- LLM
def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def decide(messages: list) -> dict:
    """Last message a tool result -> answer with it; else pick a `liga` query from the user's words."""
    last = messages[-1] if messages else {}
    if last.get("role") == "tool":
        return {"text": "RESP " + re.sub(r"\s+", " ", _text(last.get("content")))[:6000]}
    user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            user = _text(m.get("content")).split("\n\n[CONTEXTO")[0].lower()   # the member's words, not the injected context
            break
    # the sender label may carry the team name ("[Marcio | Time Ana]"): match on the words after it
    user = user.split("] ", 1)[-1]
    for key, args in (("grafico", {"consulta": "grafico"}), ("tabela", {"consulta": "tabela"}),
                      ("notas", {"consulta": "notas"}), ("ajuda", {"consulta": "ajuda"}),
                      ("jogador", {"consulta": "jogador", "alvo": "Pat One"}),
                      ("meu time", {"consulta": "time", "alvo": "Ana"})):
        if key in user:
            return {"tool": ("liga", args)}
    return {"text": "RESP direto: sem consulta"}


def llm_server(port: int):
    class H(Quiet):
        def do_GET(self):
            if "models" in self.path:
                return self._json(200, {"object": "list", "data": [{"id": "mock-model", "object": "model",
                                                                      "context_length": 128000}]})
            self._json(200, {"status": "ok"})

        def do_POST(self):
            body = self._body()
            msgs = body.get("messages") or []
            system = next((_text(m.get("content")) for m in msgs if m.get("role") == "system"), "")
            tools = [(t.get("function") or {}).get("name") for t in (body.get("tools") or [])]
            plan = decide(msgs)
            log("llm.jsonl", {"stream": bool(body.get("stream")), "model": body.get("model"), "roles": [m.get("role") for m in msgs],
                              "tools": tools, "system": system, "n_system": len(system),
                              "last_user": next((_text(m.get("content")) for m in reversed(msgs) if m.get("role") == "user"), ""),
                              "plan": plan})
            cid = f"chatcmpl-{int(time.time() * 1000)}"
            if "tool" in plan and plan["tool"][0] in tools:
                name, args = plan["tool"]
                msg = {"role": "assistant", "content": None,
                       "tool_calls": [{"id": "call_1", "type": "function",
                                       "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            else:
                msg = {"role": "assistant", "content": plan.get("text") or "RESP sem ferramenta"}
                finish = "stop"
            usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                delta = dict(msg)
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [{"index": 0, **tc} for tc in delta["tool_calls"]]
                chunks = [{"id": cid, "object": "chat.completion.chunk", "model": "mock-model",
                           "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {"id": cid, "object": "chat.completion.chunk", "model": "mock-model",
                           "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}]
                for c in chunks:
                    self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                return
            self._json(200, {"id": cid, "object": "chat.completion", "model": "mock-model", "usage": usage,
                             "choices": [{"index": 0, "message": msg, "finish_reason": finish}]})
    return ThreadingHTTPServer(("127.0.0.1", port), H)


if __name__ == "__main__":
    os.makedirs(LOGS, exist_ok=True)
    kind = sys.argv[1]
    defaults = {"sleeper": 18080, "evolution": 18081, "llm": 18082}
    port = int(sys.argv[2]) if len(sys.argv) > 2 else defaults[kind]
    srv = {"sleeper": sleeper_server, "evolution": evolution_server, "llm": llm_server}[kind](port)
    print(f"mock {kind} on 127.0.0.1:{port}", flush=True)
    srv.serve_forever()
