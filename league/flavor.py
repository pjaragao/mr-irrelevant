"""LLM one-liners for the scheduled messages: the engine computes the facts, the model only adds tone.

Uses llm.chat (the chat model chain from config.yaml), short max_tokens, 25s timeout and a JSON cache (same facts → same line, so retries never re-call). Any failure returns the deterministic
fallback, so a message never depends on the LLM being up.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict

import league_ctx

logger = logging.getLogger("flavor")


def _cache_file() -> str:
    return league_ctx.current().state_path("flavor_cache.json")   # the tone is the league's persona: per league


def persona_prompt() -> str:
    """Who is speaking and about what league: persona name and league format come from the league context."""
    ctx = league_ctx.current()
    voice = ctx.persona.get("voice")
    if voice:
        return str(voice)
    return (f"Você é o {ctx.persona_name}, comentarista ácido e sarcástico de uma liga {ctx.profile.league_type} de "
            f"fantasy football entre amigos no WhatsApp. Escreva em PT-BR.")


def _tone_example() -> str:
    """One tone example built from this league's own teams (never another league's names)."""
    teams = [e.get("team") for _, e in sorted(league_ctx.current().people.entries.items()) if e.get("team")]
    a, b = (teams + ["O Time A", "o Time B"])[:2]
    return f"{a} venceu jogando de chinelo, e {b} conseguiu perder mesmo assim."


def _load() -> Dict[str, str]:
    try:
        with open(_cache_file(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(cache: Dict[str, str]) -> None:
    try:
        if len(cache) > 500:
            cache = dict(list(cache.items())[-500:])
        path = _cache_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def _numbers_ok(text: str, payload: str) -> bool:
    """Every number the model wrote must exist in the facts (blocks invented scores/records)."""
    import re
    known = {n.replace(",", ".") for n in re.findall(r"\d+(?:[.,]\d+)?", payload)}
    for n in re.findall(r"\d+(?:[.,]\d+)?", text):
        if n.replace(",", ".") not in known:
            return False
    return True


def line(facts: Dict[str, Any], task: str, fallback: str, max_words: int = 35) -> str:
    """One acid sentence about `facts` (dict of real numbers/names). Returns `fallback` on any failure."""
    if os.environ.get("FLAVOR_DISABLED", "").lower() in ("1", "true", "yes"):
        return fallback
    try:
        import llm
        model = llm.primary_model()
    except Exception:
        model = "default"
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    ck = hashlib.sha1(f"{league_ctx.current().slug}|{model}|{task}|{payload}".encode()).hexdigest()
    cache = _load()
    if ck in cache:
        return cache[ck]
    prompt = (f"{persona_prompt()}\nTarefa: {task}\nFatos (use só estes; não invente número, nome ou evento):\n{payload}\n\n"
              f"Exemplo do tom esperado (outro assunto): \"{_tone_example()}\"\n"
              f"Responda com UMA frase coerente, de no máximo {max_words} palavras, citando pelo menos um nome dos fatos. "
              "Sem aspas, sem markdown, sem emoji.")
    try:
        import llm
        text = llm.chat(prompt, max_tokens=200, timeout=25)
        text = text.strip().strip('"').strip()
        text = text.replace("**", "").replace("*", "").splitlines()[0].strip() if text else ""
        low = text.lower()
        meta = low.startswith(("here", "okay", "ok,", "let me", "thinking", "the user", "sure", "aqui está", "claro"))
        if not text or meta or "thinking" in low or len(text.split()) > max_words * 2 or not _numbers_ok(text, payload):
            return fallback
        cache[ck] = text
        _save(cache)
        return text
    except Exception as exc:
        logger.warning("flavor LLM failed: %s", exc)
        return fallback
