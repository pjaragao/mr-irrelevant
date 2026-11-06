"""Reply chains: when someone replies to a message, the agent sees the whole thread, not just the quoted line.

Mario asks → the bot answers → Mario replies on top of that answer → … Each hop is a WhatsApp "quote"
(`contextInfo.stanzaId`). The adapter records every group message it sees and every message it sends here; on a
reply it walks the quotes back (oldest first) and puts that thread in front of the new message. A reply to one of the
bot's own messages also *addresses* the bot, even if the quoted author id arrives in a form we do not know.

Storage: one small SQLite per HERMES_HOME (messages are kept a few days, per chat; leagues never share a chat id).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

KEEP_SECONDS = 3 * 86400
MAX_HOPS = 8              # messages of the thread handed to the model
MAX_CHARS = 700           # per message (a bot answer split in several chunks is joined first)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS msgs (chat_id TEXT, msg_id TEXT, author TEXT, is_bot INTEGER, text TEXT,
    reply_to TEXT, turn TEXT, seq INTEGER, ts REAL, PRIMARY KEY (chat_id, msg_id));
CREATE INDEX IF NOT EXISTS ix_turn ON msgs (chat_id, turn);
"""


def default_path() -> str:
    home = os.environ.get("HERMES_HOME") or "/opt/data"
    return os.path.join(home, "cache", "reply_chain.db")


class ChainStore:
    def __init__(self, path: Optional[str] = None):
        self.path = path or default_path()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._last_prune = 0.0
        with self._conn() as con:
            con.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------------ write
    def record(self, chat_id: str, msg_id: str, author: str, text: str, *, is_bot: bool = False,
               reply_to: Optional[str] = None, turn: Optional[str] = None, seq: int = 0) -> None:
        """Idempotent. `turn` groups the chunks of one bot answer (they read as a single message)."""
        if not chat_id or not msg_id or not (text or "").strip():
            return
        now = time.time()
        with self._lock, self._conn() as con:
            con.execute("INSERT OR REPLACE INTO msgs VALUES (?,?,?,?,?,?,?,?,?)",
                        (chat_id, msg_id, author or "", int(is_bot), text.strip(), reply_to or None,
                         turn or None, seq, now))
            if now - self._last_prune > 3600:
                self._last_prune = now
                con.execute("DELETE FROM msgs WHERE ts < ?", (now - KEEP_SECONDS,))

    # ------------------------------------------------------------------ read
    def was_bot(self, chat_id: str, msg_id: Optional[str]) -> bool:
        if not msg_id:
            return False
        with self._conn() as con:
            row = con.execute("SELECT is_bot FROM msgs WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
        return bool(row and row["is_bot"])

    def _message(self, con, chat_id: str, msg_id: str) -> Optional[Dict]:
        row = con.execute("SELECT * FROM msgs WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)).fetchone()
        if not row:
            return None
        text = row["text"]
        if row["turn"]:                                   # every chunk of the same answer, in order
            parts = [r["text"] for r in con.execute(
                "SELECT text FROM msgs WHERE chat_id=? AND turn=? ORDER BY seq, ts", (chat_id, row["turn"]))]
            text = "\n".join(parts) or text
        return {"id": row["msg_id"], "author": row["author"], "is_bot": bool(row["is_bot"]), "text": text,
                "reply_to": row["reply_to"]}

    def chain(self, chat_id: str, quoted_id: Optional[str], limit: int = MAX_HOPS) -> List[Dict]:
        """Thread behind a quoted message, oldest first (the quoted message is the last element)."""
        out: List[Dict] = []
        seen = set()
        cur = quoted_id
        with self._conn() as con:
            while cur and cur not in seen and len(out) < limit:
                seen.add(cur)
                m = self._message(con, chat_id, cur)
                if not m:
                    break
                out.append(m)
                cur = m["reply_to"]
        return out[::-1]


def format_context(chain: List[Dict], persona: str = "Mr. Irrelevant") -> str:
    """Block put before the new message. Empty chain -> ''."""
    if not chain:
        return ""
    lines = []
    for m in chain:
        who = persona if m.get("is_bot") else (m.get("author") or "alguém")
        body = " ".join(str(m.get("text") or "").split())
        if len(body) > MAX_CHARS:
            body = body[: MAX_CHARS - 1] + "…"
        lines.append(f"- {who}: {body}")
    return ("[Conversa em andamento: a mensagem abaixo é um REPLY nesta cadeia (do mais antigo ao mais novo). "
            "Responda dentro do contexto dela, sem pedir de novo o que já foi dito]\n" + "\n".join(lines)
            + "\n[Mensagem atual]\n")


def quoted_fallback(author: str, text: str, is_bot: bool) -> List[Dict]:
    """The quote WhatsApp itself delivered, for a parent message we never saw (sent before the log existed)."""
    return [{"author": author, "is_bot": is_bot, "text": text}] if (text or "").strip() else []
