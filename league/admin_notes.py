"""Admin notes (item 4): facts the league admin tells the bot to remember.

Deterministic — no model call. Only the verified admin (checked by exact phone in the Evolution adapter)
can write, and only with an explicit command at the start of the message:
    "@bot anota aí: o Yan é o dono do Riedelots desde 2023"
Notes are injected into the LLM context by the sleeper-league plugin (pre_llm_call hook) and are also
available through `liga notas`.
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import league_ctx


def notes_file() -> str:
    """Notes belong to the league: what one admin tells their bot must never reach another league's."""
    return league_ctx.current().kb_path("ADMIN_NOTES.md")


# "anota aí: X", "anota aí que X", "anota que X", "anota aí na sua base que X", "salva: X"
NOTE_CMD = re.compile(r"^\s*(?:anota(?:r)?|anote|grava(?:r)?|registra(?:r)?|salva(?:r)?)(?:\s+a[ií])?"
                      r"(?:\s+na\s+(?:sua\s+)?base)?(?:\s+que\b|\s*[:,\-])\s*", re.I)


def parse_command(text: str) -> str | None:
    """Return the note body if `text` is an explicit note command ("anota aí: …"), else None."""
    m = NOTE_CMD.match(text or "")
    if not m:
        return None
    body = text[m.end():].strip()
    return body or None


def save_note(text: str, author: str = "") -> None:
    path = notes_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(f"# Notas do admin ({league_ctx.current().persona['admin_name']}) — fatos que o bot deve respeitar\n\n")
        f.write(f"- [{datetime.now().strftime('%d/%m/%Y')}] {text.strip()}\n")


def read_notes(limit_chars: int = 1500) -> str:
    try:
        with open(notes_file(), encoding="utf-8") as f:
            txt = f.read().strip()
    except FileNotFoundError:
        return ""
    return txt[-limit_chars:]
