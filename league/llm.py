"""One-shot LLM calls made by the engine itself (trade verdicts, editorial one-liners, Data Lab comments).

Uses the SAME model chain as the chat, read from Hermes' config.yaml (single source of truth): `model` (the primary,
e.g. provider `agy` = Antigravity CLI via the host shim) followed by `fallback_providers`, tried in order until one
answers. Provider endpoints: a named entry under `providers:` (base_url + key_env), `openrouter`, or `gemini`.
If config.yaml cannot be read, DEFAULT_CHAIN (same models) is used. Always sends an explicit max_tokens. Returns ""
on failure, so callers keep their deterministic text.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("llm")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
AGY_URL = "http://172.26.0.1:8787/v1/chat/completions"

DEFAULT_CHAIN: List[Dict[str, str]] = [
    {"provider": "agy", "model": "gemini-flash-latest"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3-ultra-550b-a55b:free"},
    {"provider": "openrouter", "model": "google/gemma-4-31b-it:free"},
    {"provider": "openrouter", "model": "nvidia/nemotron-3.5-lightning:free"},
]
AGY_MIN_TIMEOUT = 45  # one agy run takes 5-20 s (more under load)


def _config() -> Dict[str, Any]:
    try:
        import yaml
        with open(os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def chain() -> List[Dict[str, str]]:
    cfg = _config()
    m = cfg.get("model") or {}
    out = []
    if m.get("provider") and m.get("default"):
        out.append({"provider": str(m["provider"]), "model": str(m["default"])})
    for fb in cfg.get("fallback_providers") or []:
        if isinstance(fb, dict) and fb.get("provider") and fb.get("model"):
            out.append({"provider": str(fb["provider"]), "model": str(fb["model"])})
    return out or list(DEFAULT_CHAIN)


def primary_model() -> str:
    """Model name of the first entry (used by callers as part of cache keys)."""
    return chain()[0]["model"]


def _endpoint(provider: str) -> Optional[Tuple[str, str]]:
    """-> (url, api key) or None when the provider is unknown / has no key."""
    named = (_config().get("providers") or {}).get(provider)
    if isinstance(named, dict) and named.get("base_url"):
        return named["base_url"].rstrip("/") + "/chat/completions", os.environ.get(named.get("key_env", ""), "")
    if provider == "agy":
        return AGY_URL, os.environ.get("AGY_SHIM_TOKEN", "")
    if provider == "openrouter":
        key = os.environ.get("OPENROUTER_API_KEY", "")
        return (OPENROUTER_URL, key) if key else None
    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        return (GEMINI_URL, key) if key else None
    return None


def _post(url: str, key: str, body: dict, timeout: int) -> str:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    return (data["choices"][0]["message"].get("content") or "").strip()


def chat(prompt: str, max_tokens: int = 600, timeout: int = 40, json_mode: bool = False,
         system: Optional[str] = None, effort: Optional[str] = None) -> str:
    """`effort` ("low"|"medium"|"high") is the thinking level; only the agy shim honours it, per request."""
    for entry in chain():
        provider, model = entry["provider"], entry["model"]
        ep = _endpoint(provider)
        if not ep:
            continue
        url, key = ep
        sys_txt = system
        if json_mode and provider == "agy":  # the shim has no response_format: ask for it in words
            sys_txt = ((system + "\n") if system else "") + "Responda APENAS com um objeto JSON válido, sem texto fora dele."
        msgs = ([{"role": "system", "content": sys_txt}] if sys_txt else []) + [{"role": "user", "content": prompt}]
        body: Dict[str, Any] = {"model": model, "messages": msgs, "max_tokens": max_tokens}
        if provider == "openrouter":
            body["reasoning"] = {"enabled": False, "exclude": True}
        if effort and provider == "agy":
            body["reasoning_effort"] = effort
        if json_mode and provider != "agy":
            body["response_format"] = {"type": "json_object"}
        try:
            text = _post(url, key, body, max(timeout, AGY_MIN_TIMEOUT) if provider == "agy" else timeout)
            if text:
                return text
        except Exception as exc:
            logger.warning("llm %s/%s failed: %s", provider, model, exc)
    return ""
