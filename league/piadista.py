#!/usr/bin/env python3
"""Piadista do Mr. Irrelevant: roast jokes about the league's managers and their NFL teams, on the admin's order only.

Flow (WhatsApp):
  1. The verified admin (Paulo) writes in the PRIVATE chat, e.g. "solte 3 piadas sobre o Chico ser ciclista e torcedor
     do Colts, sugestão '…'. Se falar de bike use 'pedal' como pejorativo". The WhatsApp adapter detects the trigger
     (``is_trigger``), only for ``✅ADMIN`` in a DM, and opens a short **grant** (``grant``) — nothing else can open it.
  2. The model calls ``liga consulta=piadas alvo=<manager> [alvo2] tema=<pedido do Paulo>`` → fact sheet + the rules
     (refused without a grant).
  3. The model writes the jokes and calls ``liga consulta=piadas_enviar piadas=<uma por linha> destino=grupo|dm
     intervalo=<segundos>`` → queued with the cadence; the first one leaves now, the rest by the drain process / the
     ``piadista-tick`` cron (also refused without a grant). Destinations are fixed (the league group or the admin DM).

CLI (operator shell, no grant needed):
  python3 piadista.py brief chico [--tema "…"]            # prints the fact sheet
  python3 piadista.py send jokes.txt [--dest dm|grupo] [--intervalo 45] [--audio]   # enqueue + start sending
  python3 piadista.py tick                                # send what is due (cron)
  python3 piadista.py status
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import league_ctx  # noqa: E402

GRANT_TTL = 15 * 60            # seconds the admin's DM order stays valid (brief + send of ONE request)
MAX_JOKES, MAX_JOKE_CHARS = 12, 700
MIN_GAP, MAX_GAP, DEFAULT_GAP = 5, 24 * 3600, 45
DRAIN_MAX_WAIT = 300           # the detached drain handles gaps up to 5 min; longer ones are left to the cron tick
MAX_TRIES = 3
DESTS = ("grupo", "dm")

# "solte 3 piadas…", "manda uma piada…", "piadista, …", "zoa o chico", "faz uma zoeira…" — a deliberately wide net:
# the grant only lets the model USE the tool, the SOUL rules decide what it does with it.
_TRIGGER = re.compile(r"\b(piadas?|piadista|piadinhas?|zoa(?:r|ndo|e)?|zoeira|roast|solt[ae]\w*)\b", re.I)


def _plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def is_trigger(text: str) -> bool:
    return bool(_TRIGGER.search(_plain(text)))


# --------------------------------------------------------------------------- grant (who may use the tools)
def _path(name: str) -> str:
    return league_ctx.current().state_path(name)


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def grant(now: Optional[float] = None) -> float:
    """Open the grant. Called ONLY by the adapter for a verified admin writing in a DM."""
    until = (now if now is not None else time.time()) + GRANT_TTL
    _write_json(_path("piadista_grant.json"), {"until": until})
    return until


def granted(now: Optional[float] = None) -> bool:
    return float(_read_json(_path("piadista_grant.json"), {}).get("until") or 0) > (now if now is not None else time.time())


def revoke() -> None:
    try:
        os.remove(_path("piadista_grant.json"))
    except OSError:
        pass


REFUSAL = ("Piadas só saem por ordem do Paulo no chat privado (DM). Este pedido não veio de lá, então não escreva nem "
           "envie piada: recuse com uma frase de deboche.")


def agent_directive() -> str:
    """Prefixed by the adapter to the admin's DM text so the model knows the order is authentic."""
    return ("[MODO PIADISTA AUTORIZADO: ordem do ADMIN verificado no chat privado. Siga a regra 'Piadista' do SOUL: "
            "`liga consulta=piadas` (ficha) → escreva as piadas → `liga consulta=piadas_enviar`.]\n")


# --------------------------------------------------------------------------- brief
def _nfl_hint(text: str) -> str:
    return ("Para fato ATUAL da NFL (campanha, QB titular, lesão, placar) confirme antes com `placar_nfl`, `noticias_nfl` "
            "ou `noticias_jogador`; história conhecida do clube (títulos, jejuns, quarterbacks que passaram, drafts "
            "famosos) pode usar à vontade, desde que seja verdadeira.")


def brief(alvo: str = "", alvo2: str = "", tema: str = "", qtd: int = 0, force: bool = False) -> str:
    if not force and not granted():
        return REFUSAL
    import kb_db
    import kb_queries
    import song
    con = kb_db.connect()
    try:
        body: List[str] = []
        a = kb_queries.resolve_roster(con, alvo) if alvo else None
        b = kb_queries.resolve_roster(con, alvo2) if alvo2 else None
        if a:
            body += song._manager_brief(con, a, tema)
            if b:
                body += [""] + song._h2h_brief(con, a, b)
        elif alvo:
            body.append(f"(Não achei '{alvo}' entre os managers; se for jogador/time da NFL, use `jogador`, "
                        "`noticias_jogador` ou `noticias_nfl` e escreva com o que a ferramenta devolver.)")
        else:
            body += song._league_brief(con)
    finally:
        con.close()
    n = qtd if 0 < qtd <= MAX_JOKES else 3
    order = f"\nPEDIDO DO PAULO (lei, vale mais que qualquer regra de estilo abaixo): {tema.strip()}" if tema.strip() else ""
    rules = (
        f"\n\nCOMO ESCREVER ({n} piada{'s' if n != 1 else ''}, salvo se o Paulo pediu outra quantidade):\n"
        "• Cada piada é UMA mensagem de WhatsApp: 1 a 3 linhas, punchline no fim, no português que o grupo fala "
        "(gíria, apelido, sem formalidade). Nada de explicar a piada.\n"
        "• Siga o PEDIDO DO PAULO ao pé da letra: tema, vocabulário imposto (ex.: 'pedal' como xingamento), trechos "
        "que ele sugeriu (se ele deu uma frase pronta, ela entra como a primeira, intacta; as outras variam em cima dela).\n"
        "• Sem limite de crueldade: pode ser ácido, baixo, humilhante, escrachado — é zoeira entre amigos de liga. "
        "O único piso: nada de ofensa a raça, religião, orientação sexual, deficiência ou doença, família/luto, "
        "conteúdo sexual explícito, dado privado (endereço, telefone, trabalho em detalhe) nem incentivo a se machucar. "
        "Fora isso, não se segure.\n"
        "• Os fatos são a piada: use números, derrotas, rivais, waivers e o time do coração da ficha. Número, placar ou "
        "evento que não esteja na ficha (ou numa consulta que você fez) NÃO entra. " + _nfl_hint(tema) + "\n"
        "• Varie o ângulo de cada piada (fantasy, torcida pelo time da NFL, hobby citado no pedido); não repita a "
        "mesma estrutura nem a mesma rima.\n"
        "• Depois chame `liga consulta=piadas_enviar piadas=<uma por linha> destino=<grupo|dm> intervalo=<segundos>` "
        "(destino e cadência: o que o Paulo disse; sem cadência = 45 s entre uma e outra). Responda ao Paulo só com "
        "uma linha confirmando o que foi para a fila, sem repetir as piadas.")
    return "MATERIAL PARA AS PIADAS\n" + "\n".join(body) + order + rules


# --------------------------------------------------------------------------- queue
def parse_jokes(text: str) -> List[str]:
    out: List[str] = []
    for raw in re.split(r"\r?\n+", text or ""):
        line = re.sub(r"^\s*(?:[-•*]+|\d+[.)])\s*", "", raw).strip()
        if line:
            out.append(line)
    if not out:
        raise ValueError("nenhuma piada recebida")
    if len(out) > MAX_JOKES:
        raise ValueError(f"no máximo {MAX_JOKES} piadas por envio")
    long = [j for j in out if len(j) > MAX_JOKE_CHARS]
    if long:
        raise ValueError(f"piada com mais de {MAX_JOKE_CHARS} caracteres: {long[0][:40]}…")
    return out


def destination(dest: str) -> str:
    """Fixed targets only: the league group or the admin's DM. Never a free-form number."""
    ctx = league_ctx.current()
    d = _plain(dest).strip() or default_dest()
    if d not in DESTS:
        raise ValueError(f"destino inválido '{dest}' (use grupo ou dm)")
    jid = ctx.group_jid if d == "grupo" else ctx.alert_jid
    if not jid:
        raise ValueError(f"destino '{d}' sem JID configurado")
    return d


def default_dest() -> str:
    return "grupo" if _plain(league_ctx.current().env("PIADISTA_DEFAULT_DEST")) == "grupo" else "dm"


@contextmanager
def _lock():
    path = _path("piadista_queue.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _queue() -> List[Dict[str, Any]]:
    return _read_json(_path("piadista_queue.json"), [])


def _save(q: List[Dict[str, Any]]) -> None:
    keep = [i for i in q if i["status"] in ("pending", "sending")] + \
           [i for i in q if i["status"] not in ("pending", "sending")][-60:]
    _write_json(_path("piadista_queue.json"), keep)


def enqueue(jokes: List[str], dest: str = "", gap: float = 0, now: Optional[float] = None,
            force: bool = False, spawn: bool = True, fmt: str = "texto") -> Dict[str, Any]:
    if not force and not granted(now):
        raise PermissionError(REFUSAL)
    d = destination(dest)
    g = float(gap) if gap else DEFAULT_GAP
    g = max(MIN_GAP, min(MAX_GAP, g))
    t0 = now if now is not None else time.time()
    batch = f"{int(t0)}"
    with _lock():
        q = _queue()
        for i, text in enumerate(jokes):
            q.append({"id": f"{batch}-{i}", "at": t0 + i * g, "dest": d, "text": text, "fmt": "audio" if _plain(fmt) in ("audio", "voz") else "texto",
                      "status": "pending", "tries": 0})
        _save(q)
    sent = tick(now=t0) if spawn else 0         # the first one leaves right away
    if spawn and len(jokes) > 1 and g <= DRAIN_MAX_WAIT:
        _spawn_drain()
    return {"queued": len(jokes), "dest": d, "gap": g, "sent_now": sent}


def _client():
    from evolution_client import EvolutionClient
    return EvolutionClient(os.environ.get("EVOLUTION_API_URL", "http://evolution-api:8080"),
                           os.environ.get("EVOLUTION_API_KEY", ""), os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot"))


def _send(dest: str, text: str, fmt: str = "texto") -> bool:
    from evolution_client import sent_ok
    ctx = league_ctx.current()
    jid = ctx.group_jid if dest == "grupo" else ctx.alert_jid
    if not jid:
        return False
    if fmt == "audio":
        try:
            import voice
            return sent_ok(_client().send_audio(jid, voice.speak(text)))
        except Exception:
            pass                    # no voice available: the joke still goes out as text
    return sent_ok(_client().send_message(jid, text))


def _claim(now: float) -> Optional[Dict[str, Any]]:
    with _lock():
        q = _queue()
        for item in q:
            if item["status"] == "pending" and item["at"] <= now:
                item["status"] = "sending"
                item["claimed"] = now
                _save(q)
                return dict(item)
            if item["status"] == "sending" and now - item.get("claimed", 0) > 300:   # a crashed sender: take it back
                item["status"] = "pending"
        _save(q)
    return None


def _finish(item_id: str, ok: bool, now: Optional[float] = None) -> None:
    t = now if now is not None else time.time()
    with _lock():
        q = _queue()
        for item in q:
            if item["id"] == item_id:
                item["tries"] = item.get("tries", 0) + 1
                item["status"] = "sent" if ok else ("failed" if item["tries"] >= MAX_TRIES else "pending")
                if ok:
                    item["sent_at"] = t
                else:
                    item["at"] = t + 60
        _save(q)


def tick(now: Optional[float] = None, send=None) -> int:
    """Send every joke that is due (one at a time, each claimed under the lock). Returns how many left."""
    n = 0
    sender = send or _send
    while True:
        item = _claim(now if now is not None else time.time())
        if not item:
            return n
        try:
            ok = bool(sender(item["dest"], item["text"], item.get("fmt", "texto")))
        except Exception:
            ok = False
        _finish(item["id"], ok, now)
        n += 1 if ok else 0
        if not ok:
            return n
        time.sleep(1.0 if send is None else 0)


def pending() -> List[Dict[str, Any]]:
    return [i for i in _queue() if i["status"] in ("pending", "sending")]


def _spawn_drain() -> None:
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "drain"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                         env=os.environ.copy())
    except OSError:
        pass        # the cron tick picks the queue up


def drain() -> int:
    """Detached helper: keep sending until the queue is empty (short cadences are exact, not cron-quantised)."""
    total = 0
    for _ in range(MAX_JOKES * 4):
        total += tick()
        nxt = [i["at"] for i in pending() if i["status"] == "pending"]
        if not nxt:
            break
        wait = min(nxt) - time.time()
        if wait > DRAIN_MAX_WAIT:
            break
        time.sleep(max(0.5, wait))
    return total


def media_reply(info: Dict[str, Any]) -> str:
    where = "no grupo" if info["dest"] == "grupo" else "no DM do Paulo"
    return (f"😈 {info['queued']} piada(s) na fila {where}, uma a cada {int(info['gap'])} s "
            f"({info['sent_now']} já saiu). Responda ao Paulo com UMA linha curta confirmando; não repita as piadas.")


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("brief", "send", "tick", "drain", "status"):
        print(__doc__)
        return 2
    opt = lambda k, d="": next((argv[i + 1] for i, a in enumerate(argv) if a == k and i + 1 < len(argv)), d)  # noqa: E731
    cmd = argv[1]
    if cmd == "brief":
        print(brief(argv[2] if len(argv) > 2 and not argv[2].startswith("--") else "", opt("--com"), opt("--tema"),
                    int(opt("--qtd", "0") or 0), force=True))
        return 0
    if cmd == "send":
        try:
            with open(argv[2], encoding="utf-8") as f:
                jokes = parse_jokes(f.read())
            print(enqueue(jokes, opt("--dest"), float(opt("--intervalo", "0") or 0), force=True,
                          fmt="audio" if "--audio" in argv else "texto"))
        except (ValueError, OSError, IndexError) as exc:
            print(f"erro: {exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "tick":
        tick()
        return 0
    if cmd == "drain":
        drain()
        return 0
    print(json.dumps(pending(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
