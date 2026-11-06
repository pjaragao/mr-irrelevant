"""Who gets the personal alerts (lineup advice, live score) and the on/off switch for each one.

* Recipients are the verified admins that also have a roster in `LEAGUE_PEOPLE_PHONES` ("meu time"): the numbers
  of that entry, DM only (same 55… filter the Saturday lineup heads-up uses). `ADMIN_ALERT_JID` is the fallback.
* `paused` flags live in the league's state dir (`watch_prefs.json`). The WhatsApp adapter flips them with a
  deterministic command ("parar placar" / "continuar placar"), no model call.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

import league_ctx

KINDS = ("placar", "escalacao")
LABEL = {"placar": "placar ao vivo", "escalacao": "análise de escalação"}
FILE = "watch_prefs.json"

_STOP = r"(?:par(?:ar|e|a)|paus(?:ar|e|a)|silenci(?:ar|e)|desativ(?:ar|e)|desliga(?:r)?|cancel(?:ar|e))"
_GO = r"(?:continu(?:ar|e|a)|retom(?:ar|e|a)|reativ(?:ar|e|a)|reativ|volt(?:ar|e|a)|ativ(?:ar|e|a)|liga(?:r)?)"
_WHAT = r"(?:o\s+|os\s+|as\s+|a\s+|de\s+|das\s+|dos\s+|do\s+)?(?:(?:meu|minha)\s+)?" \
        r"(?P<what>placar|parciais?|escala(?:c|ç)(?:a|ã)o|lineup|notifica(?:c|ç)(?:o|õ)es|alertas|avisos|tudo)"
CMD = re.compile(rf"^\s*(?:@?\S+[,:]?\s+)??(?:(?P<stop>{_STOP})|(?P<go>{_GO}))\s+{_WHAT}\b", re.I)


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def parse_command(text: str) -> Optional[Dict[str, Any]]:
    """{"action": "stop"|"go", "kinds": [...]} for "parar placar", "continuar escalação", "parar notificações"…"""
    m = CMD.match(_plain(text)) if text else None
    if not m or len((text or "").split()) > 8:       # a long sentence is a question for the agent, not a switch
        return None
    what = m.group("what")
    if what.startswith(("placar", "parcia")):
        kinds = ["placar"]
    elif what.startswith(("escala", "lineup")):
        kinds = ["escalacao"]
    else:
        kinds = list(KINDS)
    return {"action": "stop" if m.group("stop") else "go", "kinds": kinds}


def _path() -> str:
    return league_ctx.current().state_path(FILE)


def _load() -> Dict[str, Any]:
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def is_paused(kind: str) -> bool:
    return bool((_load().get(kind) or {}).get("paused"))


def set_paused(kind: str, paused: bool) -> None:
    data = _load()
    data[kind] = {"paused": bool(paused), "at": datetime.now().isoformat(timespec="seconds")}
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def apply_command(cmd: Dict[str, Any]) -> str:
    """Flip the switches and return the confirmation text."""
    stop = cmd["action"] == "stop"
    for k in cmd["kinds"]:
        set_paused(k, stop)
    names = " e ".join(LABEL[k] for k in cmd["kinds"])
    if stop:
        back = " / ".join(f'"continuar {k.replace("escalacao", "escalação")}"' for k in cmd["kinds"])
        return f"⏸️ Parei: {names}. Para voltar, mande {back}."
    return f"▶️ Voltei: {names}."


def instructions(kind: str) -> str:
    word = "escalação" if kind == "escalacao" else "placar"
    return f'Para parar estas mensagens, responda "parar {word}"; para voltar, "continuar {word}".'


def _digits(v: Any) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def targets(ctx=None) -> List[Dict[str, Any]]:
    """[{"rid": int, "dms": [numbers]}] — admins that own a roster. One entry per roster."""
    ctx = ctx or league_ctx.current()
    admins = {_digits(a) for a in ctx.admins if _digits(a)}
    by_rid: Dict[int, List[str]] = {}
    for phone, rid in ctx.people.phones().items():
        if phone in admins or any(phone.endswith(a) or a.endswith(phone) for a in admins if len(a) >= 8):
            by_rid.setdefault(int(rid), [])
            if len(phone) >= 12 and phone.startswith("55") and phone not in by_rid[int(rid)]:
                by_rid[int(rid)].append(phone)
    out = []
    for rid, dms in sorted(by_rid.items()):
        if not dms and ctx.alert_jid:
            dms = [ctx.alert_jid]
        if dms:
            out.append({"rid": rid, "dms": dms})
    return out


def dm(evo, numbers: List[str], text: str) -> bool:
    """Send to the first number Evolution confirms and stop: the numbers of one person (with/without the 9, LID)
    are the same WhatsApp account, so trying all of them would deliver the message twice."""
    from evolution_client import sent_ok
    for n in numbers:
        try:
            if sent_ok(evo.send_message(n, text)):
                return True
        except Exception:
            pass
    return False
