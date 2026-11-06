"""Cron (no_agent, hourly): is the chat model answering? Alerts the admin (ADMIN_ALERT_JID) when the primary
model starts failing (402 = credits, 401 = key, 429 = rate limit) and again when it recovers.

Probe = 1 tiny completion against config.yaml model.default (max_tokens 5), plus a scan of agent.log for
provider errors since the previous run. State in league/provider_state.json. Silent (empty stdout)."""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _league_env  # noqa: E402

log = _league_env.setup_logging("provider")
HOME = os.environ.get("HERMES_HOME", "/opt/data")
STATE = os.path.join(os.environ.get("LEAGUE_DIR", "/opt/league"), "provider_state.json")
URLS = {"gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "GEMINI_API_KEY"),
        "openrouter": ("https://openrouter.ai/api/v1/chat/completions", "OPENROUTER_API_KEY")}

import yaml  # noqa: E402  (bundled with Hermes)

_full = yaml.safe_load(open(os.path.join(HOME, "config.yaml"))) or {}
cfg = _full.get("model") or {}
provider, model = cfg.get("provider", "gemini"), cfg.get("default", "")
_named = (_full.get("providers") or {}).get(provider)  # e.g. `agy`: the Antigravity shim declared under `providers:`
if isinstance(_named, dict) and _named.get("base_url"):
    url, key_env = _named["base_url"].rstrip("/") + "/chat/completions", _named.get("key_env", "")
else:
    url, key_env = URLS.get(provider, URLS["openrouter"])
key = os.environ.get(key_env) or (os.environ.get("GOOGLE_API_KEY") if provider == "gemini" else "")

try:
    state = json.load(open(STATE, encoding="utf-8"))
except Exception:
    state = {"ok": True, "since": time.time(), "log_pos": 0}

problem = ""
body = {"model": model, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 5}
try:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=75).read()  # agy answers in ~5-15 s; free providers can queue
except urllib.error.HTTPError as e:
    problem = f"{provider}/{model} HTTP {e.code}: {e.read().decode(errors='ignore')[:160]}"
except Exception as e:
    problem = f"{provider}/{model}: {e}"

# provider errors logged by real chat turns since the last run
logf = os.path.join(HOME, "logs", "agent.log")
try:
    size = os.path.getsize(logf)
    pos = state.get("log_pos", 0) if state.get("log_pos", 0) <= size else 0
    with open(logf, encoding="utf-8", errors="ignore") as f:
        f.seek(pos)
        chunk = f.read()
    state["log_pos"] = size
    hits = re.findall(r"(Non-retryable client error: Error code: (\d{3})[^\n]{0,120})", chunk)
    if hits and not problem:
        problem = f"chat falhou {len(hits)}x desde a última checagem (HTTP {hits[-1][1]})"
except OSError:
    pass

admin = os.environ.get("ADMIN_ALERT_JID", "")
from evolution_client import EvolutionClient  # noqa: E402

if problem and state.get("ok", True):
    log.error("provider problem: %s", problem)
    hint = {"402": "sem crédito — recarregue", "401": "chave inválida", "429": "limite de requisições"}
    code = re.search(r"\b(40[129]|429)\b", problem)
    if admin:
        EvolutionClient().send_message(admin, f"⚠️ Mr. Irrelevant: o modelo do chat está falhando.\n{problem}\n"
                                              f"{hint.get(code.group(1), '') if code else ''}".strip())
    state.update(ok=False, since=time.time(), last=problem)
elif not problem and not state.get("ok", True):
    log.info("provider recovered")
    if admin:
        EvolutionClient().send_message(admin, f"✅ Mr. Irrelevant: o modelo do chat ({model}) voltou a responder.")
    state.update(ok=True, since=time.time(), last="")
json.dump(state, open(STATE, "w", encoding="utf-8"))
