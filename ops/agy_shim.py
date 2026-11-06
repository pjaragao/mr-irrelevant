#!/usr/bin/env python3
"""OpenAI-compatible shim in front of the Antigravity CLI (`agy`), so Hermes can use it as a chat provider.

Why a shim: Hermes talks to providers over /v1/chat/completions (with function calling); `agy` is an agent CLI
(`agy -p <prompt> --output-format json`). Each request becomes ONE stateless `agy` run in an empty scratch dir:

  * the Hermes conversation (system prompt, history, tool results) is rendered into a single prompt;
  * Hermes' tools are described in the prompt with a tiny protocol — to call one, the model answers with a single
    line  TOOL_CALL {"name": "...", "arguments": {...}}  which the shim turns into an OpenAI `tool_calls` reply;
    otherwise the answer is the final text. Hermes executes the tool itself and calls again with the result;
  * agy's own tools (files/terminal/web) are forbidden in the prompt, the run happens in an empty temp dir, and
    --dangerously-skip-permissions is never passed, so a tool request that slips through is denied, not executed.

The model is `gemini-flash-latest` by default: `agy` has no such alias, so the shim looks the catalogue up (`agy models`)
and runs the newest plain Gemini Flash at AGY_SHIM_EFFORT (default low), e.g. gemini-3.8-flash-low. The lookup is cached
(AGY_SHIM_MODEL_TTL) and repeated when agy rejects the cached id, so a model being withdrawn needs no edit here. Set
AGY_SHIM_MODEL to a concrete id from `agy models` to pin one instead.

Hermes gives a turn a fixed budget of model calls (config.yaml `agent.max_turns`). A lookup that keeps asking for tools
would end with no answer at all, so after AGY_SHIM_MAX_TOOL_ROUNDS tool rounds in one turn the shim stops offering the
tools and asks for the final answer with what was already fetched. Keep it at most max_turns - 2 (a spare call is lost
whenever the model answers empty after a tool result and Hermes nudges it).

Listens on the docker bridge of the bot (default 172.26.0.1:8787), bearer token required (AGY_SHIM_TOKEN).
Run:  AGY_SHIM_TOKEN=... python3 ops/agy_shim.py   (see ops/agy_shim.sh for start/stop/status)
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

HOST = os.environ.get("AGY_SHIM_HOST", "172.26.0.1")
PORT = int(os.environ.get("AGY_SHIM_PORT", "8787"))
TOKEN = os.environ.get("AGY_SHIM_TOKEN", "")
AGY = os.environ.get("AGY_BIN") or shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
LATEST_ALIAS = "gemini-flash-latest"  # Google's own name for "newest Gemini Flash"; resolve_model() maps it to an agy id
MODEL = os.environ.get("AGY_SHIM_MODEL", LATEST_ALIAS)
EFFORT = os.environ.get("AGY_SHIM_EFFORT", "low")  # only used to resolve the alias; a pinned id carries its own
MODEL_TTL = int(os.environ.get("AGY_SHIM_MODEL_TTL", "3600"))
MAX_TOOL_ROUNDS = int(os.environ.get("AGY_SHIM_MAX_TOOL_ROUNDS", "6"))
TIMEOUT = int(os.environ.get("AGY_SHIM_TIMEOUT", "90"))    # chamada normal leva 6-40 s; travar além disso é falha transitória: falhar rápido
MAX_PROMPT_BYTES = int(os.environ.get("AGY_SHIM_MAX_PROMPT_BYTES", "65000"))  # cabe folgado em -p (< ARG_LIMIT), evitando conversa.md + múltiplos read_file
MAX_PARALLEL = int(os.environ.get("AGY_SHIM_PARALLEL", "2"))
MAX_BODY = 4 * 1024 * 1024

log = logging.getLogger("agy_shim")
_slots = threading.BoundedSemaphore(MAX_PARALLEL)

PROTOCOL = """Você é o motor de linguagem de um bot. Siga estas regras acima de qualquer outra:
1. NÃO use nenhuma ferramenta sua (arquivos, terminal, navegador, busca, edição). Você só escreve texto.
2. Há ferramentas do bot descritas abaixo. Para chamar UMA delas, responda com UMA ÚNICA linha, sem mais nada:
TOOL_CALL {"name": "<ferramenta>", "arguments": {...}}
   (argumentos conforme o schema; omita os opcionais que não precisa). Você receberá o resultado e continua.
3. Quando já tiver o que precisa, ou a ferramenta não for necessária, escreva a resposta final ao usuário, em texto normal,
   no estilo definido pelo sistema. Nunca escreva TOOL_CALL numa resposta final.
4. Não explique este protocolo nem mencione ferramentas internas ao usuário."""
WRAP_UP = ("(LIMITE DE CONSULTAS ATINGIDO: não há mais ferramentas nesta resposta e TOOL_CALL não funciona. Responda agora, "
           "em texto, com o que os resultados acima já mostram; se algo não deu para consultar, diga o que ficou de fora.)")
WRAP_UP_FALLBACK = "Não consegui fechar essa consulta dentro do limite de chamadas. Peça de novo de forma mais específica."
# Prompts Hermes itself injects as a `user` message mid-turn (empty answer after a tool result); they are not the user
HERMES_NUDGES = ("You just executed tool calls but returned an empty response",)


# --------------------------------------------------------------------------- prompt
def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in (None, "text"))
    return str(content)


def tool_rounds(messages: List[Dict[str, Any]]) -> int:
    """Tool-calling rounds already made in the current turn: assistant `tool_calls` messages after the last real user
    message (Hermes' own empty-answer nudge is a `user` message too, but it does not start a new turn)."""
    n = 0
    for m in reversed(messages):
        if m.get("role") == "user" and not _text(m.get("content")).startswith(HERMES_NUDGES):
            break
        if m.get("role") == "assistant" and m.get("tool_calls"):
            n += 1
    return n


def _format_msg(m: Dict[str, Any]) -> List[str]:
    role = m.get("role")
    if role == "system":
        return [f"[SISTEMA]\n{_text(m.get('content'))}"]
    if role == "user":
        return [f"[USUÁRIO]\n{_text(m.get('content'))}"]
    if role == "assistant":
        calls = m.get("tool_calls") or []
        if calls:
            return [f"[ASSISTENTE]\nTOOL_CALL " + json.dumps(
                {"name": c.get("function", {}).get("name"),
                 "arguments": _loads(c.get("function", {}).get("arguments"))}, ensure_ascii=False) for c in calls]
        return [f"[ASSISTENTE]\n{_text(m.get('content'))}"]
    if role == "tool":
        return [f"[RESULTADO DA FERRAMENTA {m.get('name') or ''}]\n{_text(m.get('content'))}"]
    return []


def render_prompt(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]], wrap_up: bool = False,
                  max_bytes: int = MAX_PROMPT_BYTES) -> str:
    out = [PROTOCOL, ""]
    if wrap_up:
        out.append(WRAP_UP)
    elif tools:
        out.append("FERRAMENTAS DO BOT:")
        for t in tools:
            f = t.get("function", t)
            out.append(f"- {f.get('name')}: {(f.get('description') or '').strip()}\n  schema: "
                       f"{json.dumps(f.get('parameters') or {}, ensure_ascii=False, separators=(',', ':'))}")
    else:
        out.append("(Nenhuma ferramenta disponível: responda direto.)")
    out.append("\n=== CONVERSA ===")
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    conv_msgs = [m for m in messages if m.get("role") != "system"]
    for m in sys_msgs:
        out.extend(_format_msg(m))
    base_bytes = len("\n\n".join(out + ["[ASSISTENTE]"]).encode("utf-8"))
    budget = max(12000, max_bytes - base_bytes)
    rendered = ["\n\n".join(_format_msg(m)) for m in conv_msgs]
    sizes = [len(s.encode("utf-8")) + 2 for s in rendered]
    if sum(sizes) <= budget or len(conv_msgs) <= 6:
        out.extend(s for s in rendered if s)
    else:
        # Mantém o final da conversa dentro do orçamento para sempre caber em `-p` sem cair em conversa.md (3x read_file)
        used = 0
        keep_from = len(conv_msgs)
        for idx in range(len(conv_msgs) - 1, -1, -1):
            if used + sizes[idx] > budget and keep_from < len(conv_msgs) - 2:
                break
            used += sizes[idx]
            keep_from = idx
        while keep_from < len(conv_msgs) and conv_msgs[keep_from].get("role") == "tool":
            keep_from += 1
        if keep_from > 0:
            out.append(f"(... {keep_from} mensagens antigas da sessão omitidas para resposta rápida ...)")
        out.extend(s for s in rendered[keep_from:] if s)
    out.append("[ASSISTENTE]")
    return "\n\n".join(out)


def _loads(s: Any) -> Any:
    if isinstance(s, dict):
        return s
    try:
        return json.loads(s or "{}")
    except (TypeError, ValueError):
        return {}


# --------------------------------------------------------------------------- model
class QuotaExceeded(RuntimeError):
    """A cota individual do modelo acabou ("Resets in 105h16m"): bloqueia o modelo até a renovação."""


_blocked: Dict[str, float] = {}


def _block(model: str, msg: str) -> None:
    m = re.search(r"Resets in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", msg)
    secs = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 3600
    _blocked[model] = time.time() + min(max(secs, 600), 7 * 86400)
    log.error("modelo %s sem cota: bloqueado por %dh%02dm", model, secs // 3600, secs % 3600 // 60)


def _is_blocked(model: str) -> bool:
    return _blocked.get(model, 0) > time.time()


class ModelRejected(RuntimeError):
    """agy does not recognise the requested model id (withdrawn or renamed)."""


def pick_latest_flash(listing: str, effort: str) -> Optional[str]:
    """Newest plain Gemini Flash id at `effort` in `agy models` output (e.g. gemini-3.8-flash-low), else None.
    Versions compare numerically (3.10 > 3.8); Flash-Lite, Pro and other vendors never match."""
    pat = re.compile(rf"^gemini-(\d+(?:\.\d+)*)-flash-{re.escape(effort)}$")
    best: Optional[Tuple[Tuple[int, ...], str]] = None
    for line in listing.splitlines():
        parts = line.split()
        m = pat.match(parts[0]) if parts else None
        if m:
            ver = tuple(int(n) for n in m.group(1).split("."))
            if best is None or ver > best[0]:
                best = (ver, parts[0])
    return best[1] if best else None


def list_models() -> str:
    with tempfile.TemporaryDirectory(prefix="agy_shim_") as wd:  # same empty cwd as a chat run
        p = subprocess.run([AGY, "models"], cwd=wd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise RuntimeError(f"agy models rc={p.returncode} err={p.stderr.strip()[:200]!r}")
    return p.stdout


_resolved: Dict[str, Any] = {"model": None, "until": 0.0}
_resolved_by_effort: Dict[str, Dict[str, Any]] = {}      # per-request `reasoning_effort` overrides (e.g. "high")
_resolve_lock = threading.Lock()
EFFORTS = ("low", "medium", "high")


def resolve_model(force: bool = False, effort: Optional[str] = None) -> str:
    """Concrete agy model id for MODEL. A pinned id passes through; the `gemini-flash-latest` alias becomes the newest
    Gemini Flash at EFFORT, cached for MODEL_TTL. A failed lookup keeps the last good id (retried after 60 s)."""
    if MODEL != LATEST_ALIAS:
        return MODEL
    effort = effort if effort in EFFORTS and effort != EFFORT else None
    if effort:   # a caller asked for another thinking level for this one request: separate cache entry
        with _resolve_lock:
            ent = _resolved_by_effort.setdefault(effort, {"model": None, "until": 0.0})
            if not ent["model"] or force or time.time() >= ent["until"]:
                found = None
                try:
                    found = pick_latest_flash(list_models(), effort)
                except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                    log.warning("effort %s lookup failed (%s)", effort, exc)
                if found:
                    ent.update(model=found, until=time.time() + MODEL_TTL)
            if ent["model"]:
                return ent["model"]
        # no model at that effort: fall through to the default one
    with _resolve_lock:
        now = time.time()
        if _resolved["model"] and not force and now < _resolved["until"]:
            return _resolved["model"]
        try:
            found = pick_latest_flash(list_models(), EFFORT)
            if not found:
                raise RuntimeError(f"no gemini-*-flash-{EFFORT} in `agy models`")
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            if not _resolved["model"]:
                raise RuntimeError(f"cannot resolve {LATEST_ALIAS}: {exc}") from None
            log.warning("model lookup failed (%s); keeping %s", exc, _resolved["model"])
            _resolved["until"] = now + 60
        else:
            if found != _resolved["model"]:
                log.info("model %s -> %s", LATEST_ALIAS, found)
            _resolved.update(model=found, until=now + MODEL_TTL)
        return _resolved["model"]


_catalog: Dict[str, Any] = {"models": {}, "until": 0.0}


def catalog() -> Dict[str, str]:
    """{id: label} from `agy models`, cached for MODEL_TTL (a failed lookup keeps the last one, retried after 60 s)."""
    with _resolve_lock:
        if time.time() < _catalog["until"]:
            return _catalog["models"]
        try:
            found = {}
            for line in list_models().splitlines():
                parts = line.split("\t", 1) if "\t" in line else line.split(None, 1)
                if parts and re.match(r"^[a-z0-9][a-z0-9.\-]+$", parts[0]):
                    found[parts[0]] = (parts[1] if len(parts) > 1 else parts[0]).strip()
            if found:
                _catalog.update(models=found, until=time.time() + MODEL_TTL)
            else:
                _catalog["until"] = time.time() + 60
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            log.warning("catalog lookup failed (%s)", exc)
            _catalog["until"] = time.time() + 60
        return _catalog["models"]


# --------------------------------------------------------------------------- roteamento automático (model="auto")
_TAG_ARGON = re.compile(r"(?:^|[\s#])argon(?:[\s\-_:]*(baixo|low|m[eé]dio|medio|mid|medium|alto|high))?\b", re.I)
_TAG_HIGH = re.compile(r"(?:#high\b|\bmodo high\b|\bpense bem\b|\bpensa bem\b|\bracioc[ií]nio profundo\b|\ban[áa]lise profunda\b)", re.I)
_TAG_LOW = re.compile(r"(?:#low\b|\bmodo r[áa]pido\b|\bflash low\b)", re.I)
_COMPLEX = re.compile(r"\b(analis[ea]|an[áa]lise|compar[ae]|planej[ae]|planejamento|estrat[ée]gia|avali[ae]|pr[óo]s e contras|"
                      r"passo a passo|contrato|cl[áa]usula|or[çc]amento|c[áa]lcul[oa]|simul[ae]|cen[áa]rios?|otimiz[ae]|"
                      r"c[óo]digo|script|programa|arquitetura|resum[ae] (?:o |esse |este )?(?:documento|pdf|artigo)|"
                      r"pesquis[ae] a fundo|relat[óo]rio|diagn[óo]stico|jur[ií]dic|tribut[áa]ri|impost)", re.I)
_EFF = {"baixo": "low", "low": "low", "médio": "medium", "medio": "medium", "mid": "medium", "medium": "medium",
        "alto": "high", "high": "high"}


def last_user_text(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user" and not _text(m.get("content")).startswith(HERMES_NUDGES):
            txt = _text(m.get("content"))
            # O adaptador do Evolution inclui a cadeia de replies antes de "[Mensagem atual]"; avaliamos só a mensagem nova
            if "[Mensagem atual]" in txt:
                return txt.split("[Mensagem atual]", 1)[-1].strip()
            return txt
    return ""


def route_auto(messages: List[Dict[str, Any]]) -> Tuple[str, str, str]:
    """-> (família, esforço, motivo). Pedido explícito do usuário vence; senão, complexidade da última mensagem."""
    sys_txt = " ".join(_text(m.get("content")) for m in messages if m.get("role") == "system").lower()
    t = last_user_text(messages)
    tl = t.lower()
    # Chamadas auxiliares do Hermes (compactação de contexto, título, curadoria) precisam de modelo rápido
    if ("context compressor" in sys_txt or "conversation compressor" in sys_txt
            or "summarize the conversation" in tl or "context compaction" in tl
            or ("<conversation>" in tl and "summary" in tl) or len(t) > 8000):
        return "flash", "low", "aux/compaction: flash-low"
    m = _TAG_ARGON.search(t)
    if m:
        return "argon", _EFF.get((m.group(1) or "medium").lower(), "medium"), "pedido: argon"
    if _TAG_HIGH.search(t):
        return "flash", "high", "pedido: high"
    if _TAG_LOW.search(t):
        return "flash", "low", "pedido: low"
    score = 0
    n = len(t)
    score += 1 if n > 700 else 0
    score += 1 if n > 2000 else 0
    score += min(len(_COMPLEX.findall(t)), 3)
    score += 1 if len(re.findall(r"(?m)^\s*(?:\d+[.)]|[-•*])\s", t)) >= 4 else 0   # lista de várias tarefas
    score += 1 if tool_rounds(messages) >= 3 else 0                                    # turno já longo: mais fôlego
    sem_url = re.sub(r"https?://\S+", "", t).strip()
    if re.search(r"https?://", t) and len(sem_url.split()) <= 12:                      # link (quase) sozinho: análise cruzada
        score += 2
    tiers = [("flash", "low"), ("flash", "high"), ("argon", "low"), ("argon", "medium"), ("argon", "high")]
    fam, eff = tiers[min(score, 4)]
    return fam, eff, f"complexidade {score}"


def pick_auto(messages: List[Dict[str, Any]]) -> str:
    fam, eff, why = route_auto(messages)
    if fam == "argon":
        ordem = ["high", "medium", "low"]
        for e in ordem[ordem.index(eff):]:           # argon sem cota => tenta o nível abaixo, depois flash high
            mid = f"gemini-4-argon-{e}"
            try:
                if mid in catalog() and not _is_blocked(mid):
                    log.info("auto → %s (%s)", mid, why)
                    return mid
            except (RuntimeError, OSError, subprocess.SubprocessError):
                break
        fam, eff = "flash", "high"
    mid = resolve_model(effort=eff)
    if _is_blocked(mid):
        mid = resolve_model(effort="low")
    log.info("auto → %s (%s)", mid, why)
    return mid


def _agy_run(cmd, cwd=None, timeout=None, **_kw):
    """subprocess.run do agy, mas sem depender do processo sair: o agy às vezes termina o trabalho em ~10 s e fica
    pendurado no encerramento (futex) por minutos. Assim que o JSON de resultado está completo no stdout, devolve e mata o
    grupo de processos."""
    import signal
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    out, err = [], []

    def pump(stream, sink):
        try:
            for chunk in iter(lambda: stream.read(4096), ""):
                sink.append(chunk)
        except (ValueError, OSError):
            pass
    threads = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
    for t in threads:
        t.start()

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + (timeout or TIMEOUT)
    done = False
    try:
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            txt = "".join(out)
            if "{" in txt:
                try:
                    json.loads(txt[txt.index("{"):])
                    done = True
                    break
                except ValueError:
                    pass
            time.sleep(0.2)
        else:
            kill()
            raise subprocess.TimeoutExpired(cmd, timeout or TIMEOUT)
        if done and proc.poll() is None:
            time.sleep(1.0)                       # dá 1 s para sair sozinho; senão encerra
            if proc.poll() is None:
                log.warning("agy terminou o trabalho mas não saiu: encerrando o processo")
                kill()
        return subprocess.CompletedProcess(cmd, proc.returncode if proc.returncode is not None else 0, "".join(out), "".join(err))
    finally:
        for t in threads:
            t.join(2)
        for s in (proc.stdout, proc.stderr):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass


# --------------------------------------------------------------------------- agy
ARG_LIMIT = int(os.environ.get("AGY_SHIM_ARG_LIMIT", "100000"))   # bytes; o limite do Linux por argumento é 131072


def run_agy(prompt: str, model: str, files: Optional[Dict[str, bytes]] = None) -> Tuple[str, Dict[str, Any]]:
    """One stateless agy run. Returns (response text, raw JSON). Raises RuntimeError on failure (ModelRejected when
    agy does not know `model`). `files` (name -> bytes) are written to the empty scratch dir for agy to read."""
    with _slots:
        wd = tempfile.mkdtemp(prefix="agy_shim_")
        try:
            for name, data in (files or {}).items():
                with open(os.path.join(wd, name), "wb") as fh:
                    fh.write(data)
            t0 = time.time()
            if len(prompt.encode("utf-8")) > ARG_LIMIT:
                # um argumento de linha de comando passa de ~128 KB => "Argument list too long": o prompt vai num arquivo
                with open(os.path.join(wd, "conversa.md"), "w", encoding="utf-8") as fh:
                    fh.write(prompt)
                log.warning("prompt de %d KB: enviado como arquivo conversa.md", len(prompt) // 1024)
                prompt = ("O arquivo ./conversa.md contém o prompt COMPLETO (regras, ferramentas e a conversa). Leia-o por inteiro "
                          "com a sua ferramenta de leitura de arquivos e responda EXATAMENTE como ele instrui, como o próximo "
                          "[ASSISTENTE]. Nenhum comando de terminal.")
            p = _agy_run([AGY, "-p", prompt, "--model", model, "--output-format", "json", "--sandbox"],
                               cwd=wd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            raw = p.stdout.strip()
            try:
                data = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
            except ValueError:
                data = {}
            if data.get("status") != "SUCCESS":
                if "quota reached" in (p.stderr + raw).lower():
                    _block(model, p.stderr + raw)
                    raise QuotaExceeded(f"agy: cota do modelo {model} esgotada")
                cls = ModelRejected if "invalid model selection" in p.stderr + raw else RuntimeError
                # `error` comes after `response` in agy's JSON: a long partial answer used to push it past the cut
                detail = str(data.get("error") or p.stderr or raw)
                raise cls(f"agy status={data.get('status')!r} rc={p.returncode} err={detail[:300]!r}")
            log.info("agy %s in %.1fs usage=%s", model, time.time() - t0, data.get("usage"))
            return data.get("response") or "", data
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"agy timeout after {TIMEOUT}s") from None
        finally:
            shutil.rmtree(wd, ignore_errors=True)


_DATA_URL = re.compile(r"^data:([\w.+/-]+)(?:;[^,]*)?;base64,(.*)$", re.S)
ATTACH_RULE = ("EXCEÇÃO À REGRA 1: os arquivos marcados como ANEXO na conversa estão na pasta atual. Abra cada um diretamente com a sua "
               "ferramenta de leitura/visualização de arquivos (imagem ou PDF) e use o conteúdo na resposta. Nenhum comando de terminal, "
               "nenhum outro arquivo, nenhuma edição.")


# Uploaded files (POST /v1/files) for clients behind Hermes: its API only lets `data:image/...` through, so a PDF goes
# here first and the message carries just [[agy-file:<id>]], which extract_attachments() turns into a file again.
FILE_TTL = int(os.environ.get("AGY_SHIM_FILE_TTL", "900"))
_uploads: Dict[str, Dict[str, Any]] = {}
_uploads_lock = threading.Lock()
_FILE_REF = re.compile(r"\[\[agy-file:(f_[0-9a-f]{24})\]\]")


def store_upload(body: Dict[str, Any]) -> Dict[str, Any]:
    import base64
    data = base64.b64decode(str(body.get("data_b64") or ""), validate=False)
    if not data:
        raise ValueError("data_b64 vazio")
    mime = "application/pdf" if data[:5] == b"%PDF-" else str(body.get("mime") or "application/octet-stream").split(";")[0].strip().lower()
    fid = f"f_{uuid.uuid4().hex[:24]}"
    now = time.time()
    with _uploads_lock:
        for k in [k for k, v in _uploads.items() if v["until"] < now]:
            del _uploads[k]
        _uploads[fid] = {"data": data, "mime": mime, "filename": str(body.get("filename") or "")[:120], "until": now + FILE_TTL}
    log.info("upload %s %s %dB", fid, mime, len(data))
    return {"id": fid, "mime": mime, "bytes": len(data), "expires_in": FILE_TTL}


def _upload(fid: str) -> Optional[Dict[str, Any]]:
    with _uploads_lock:
        u = _uploads.get(fid)
        return u if u and u["until"] >= time.time() else None


def extract_attachments(messages: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, bytes]]:
    """Image/PDF parts (OpenAI `image_url` or `file` with a base64 data URL) and [[agy-file:<id>]] references -> files for
    the agy scratch dir; each becomes a text marker pointing at its file. Other parts are kept as they are."""
    import base64
    files: Dict[str, bytes] = {}
    out: List[Dict[str, Any]] = []

    def add(data: bytes, mime: str, fname: str) -> str:
        ext = _EXT.get(mime) or (fname.rsplit(".", 1)[-1].lower() if "." in fname else "bin")
        name = f"anexo_{len(files) + 1}.{re.sub(r'[^a-z0-9]', '', ext)[:5] or 'bin'}"
        files[name] = data
        return f"[ANEXO ./{name}{f' ({fname})' if fname else ''}: {mime} — abra este arquivo para ler]"

    def refs(text: str) -> str:
        def sub(mm: "re.Match[str]") -> str:
            u = _upload(mm.group(1))
            return add(u["data"], u["mime"], u["filename"]) if u else "[anexo expirado: peça para enviar de novo]"
        return _FILE_REF.sub(sub, text)

    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            out.append({**m, "content": refs(content)} if "[[agy-file:" in content else m)
            continue
        if not isinstance(content, list):
            out.append(m)
            continue
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in (None, "text") and "[[agy-file:" in str(p.get("text") or ""):
                parts.append({**p, "text": refs(str(p.get("text")))})
                continue
            url, fname = "", ""
            if isinstance(p, dict) and p.get("type") == "image_url":
                iu = p.get("image_url")
                url = iu.get("url", "") if isinstance(iu, dict) else str(iu or "")
            elif isinstance(p, dict) and p.get("type") in ("file", "input_file"):
                f = p.get("file") if isinstance(p.get("file"), dict) else p
                url, fname = str(f.get("file_data") or ""), str(f.get("filename") or "")
            match = _DATA_URL.match(url) if url else None
            if not match:
                parts.append(p)
                continue
            mime = match.group(1).lower()
            try:
                data = base64.b64decode(match.group(2), validate=False)
            except ValueError:
                parts.append({"type": "text", "text": "[anexo ilegível]"})
                continue
            # the Hermes API only lets `data:image/...` through, so a PDF arrives as image/x-pdf: trust the bytes
            if data[:5] == b"%PDF-":
                mime = "application/pdf"
            parts.append({"type": "text", "text": add(data, mime, fname)})
        out.append({**m, "content": parts})
    return out, files


_CALL = re.compile(r"^\s*TOOL_CALL\s*(\{.*\})\s*$", re.M | re.S)


def parse_reply(text: str, tools: Optional[List[Dict[str, Any]]]) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """-> (content, tool_calls). A TOOL_CALL line for a known tool wins; anything else is the final text."""
    names = {(t.get("function", t)).get("name") for t in tools or []}
    t = re.sub(r"^```[a-z]*\n|\n```\s*$", "", text.strip())
    m = _CALL.search(t)
    if m and names:
        try:
            obj = json.loads(m.group(1))
            if obj.get("name") in names:
                args = obj.get("arguments") or {}
                return "", [{"id": f"call_{uuid.uuid4().hex[:20]}", "type": "function",
                             "function": {"name": obj["name"], "arguments": json.dumps(args, ensure_ascii=False)}}]
        except ValueError:
            pass
    return re.sub(r"(?m)^\s*TOOL_CALL\b.*$", "", t).strip() or t, None


def chat(body: Dict[str, Any]) -> Dict[str, Any]:
    tools = body.get("tools") or None
    messages = body.get("messages") or []
    wrap_up = bool(tools) and tool_rounds(messages) >= MAX_TOOL_ROUNDS
    if wrap_up:
        log.warning("tool budget reached (%d rounds in this turn): asking for the final answer", tool_rounds(messages))
        tools = None
    messages, files = extract_attachments(messages)
    prompt = render_prompt(messages, tools, wrap_up=wrap_up)
    if files:
        prompt = prompt.replace(PROTOCOL, PROTOCOL + "\n" + ATTACH_RULE, 1)
        log.info("chat with %d attachment(s): %s", len(files), ", ".join(f"{n} {len(b)}B" for n, b in files.items()))
    effort = body.get("reasoning_effort")
    requested = str(body.get("model") or "")
    # a concrete id from `agy models` (e.g. claude-sonnet-5-5-high, sent by the CentralOps Hermes) runs as is; anything
    # else (the bots' gemini-flash-latest alias, unknown ids) keeps the default resolution
    auto = requested.lower() == "auto"   # roteamento por complexidade/pedido (assistente do Paulo): flash low/high, argon low/mid/high
    model = pick_auto(messages) if auto else (requested if requested in catalog() else resolve_model(effort=effort))
    try:
        text, raw = run_agy(prompt, model, files)
    except QuotaExceeded:
        if not auto:
            raise
        model = pick_auto(messages)          # o bloqueio recém-registrado faz o roteador escolher outro modelo
        log.warning("cota esgotada; refazendo com %s", model)
        text, raw = run_agy(prompt, model, files)
    except ModelRejected:  # the id resolved earlier was withdrawn or renamed meanwhile: look the catalogue up once more
        fresh = resolve_model(force=True, effort="high" if auto else effort)
        if fresh == model:
            raise
        log.warning("agy rejected %s; retrying with %s", model, fresh)
        model = fresh
        text, raw = run_agy(prompt, model, files)
    except RuntimeError as exc:
        # Se um modelo pesado (flash-high / argon) deu timeout na rota automática, cai na hora para flash-low com prompt enxuto
        low = resolve_model(effort="low")
        if not auto or model == low:
            raise
        log.warning("falha em %s (%s); refazendo em modo rápido com %s", model, exc, low)
        model = low
        short_prompt = render_prompt(messages, tools, wrap_up=wrap_up, max_bytes=32000)
        if files:
            short_prompt = short_prompt.replace(PROTOCOL, PROTOCOL + "\n" + ATTACH_RULE, 1)
        text, raw = run_agy(short_prompt, model, files)
    content, calls = parse_reply(text, tools)
    if wrap_up and re.match(r"\s*TOOL_CALL\b", content or ""):  # insisted on a tool: never show the protocol to the group
        content = WRAP_UP_FALLBACK
    u = raw.get("usage") or {}
    msg: Dict[str, Any] = {"role": "assistant", "content": content or None}
    if calls:
        msg["tool_calls"] = calls
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
            "model": model if auto else (body.get("model") or model),
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                      "total_tokens": u.get("total_tokens", 0)}}


# --------------------------------------------------------------------------- tasks (audio / imagem / web)
MAX_TASK_BODY = int(os.environ.get("AGY_SHIM_MAX_TASK_BODY", str(16 * 1024 * 1024)))
_EXT = {"audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav", "audio/webm": "webm",
        "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
_TASK_PROMPTS = {
    "transcribe": "Transcreva fielmente o áudio {f}. Idioma esperado: português do Brasil. Responda só com a transcrição (e o idioma se não for português).",
    "ocr": ("Leia o arquivo {f} diretamente com a sua ferramenta de visualização de arquivos (NÃO execute comandos de "
            "terminal, nem pdftotext/python). Se for nota fiscal/cupom/recibo/comprovante, devolva em linhas: FORNECEDOR, CNPJ/CPF, DATA, "
            "NÚMERO DO DOCUMENTO, VALOR TOTAL e ITENS. Caso contrário, descreva em 1-2 linhas e transcreva o texto visível. "
            "Responda só com isso, sem inventar o que não estiver legível."),
    "doc": ("Leia o arquivo {f} diretamente com a sua ferramenta de leitura/visualização de arquivos (NÃO execute comandos de "
            "terminal, nem pdftotext/python). Transcreva INTEGRALMENTE o conteúdo, de todas as páginas, sem resumir: preserve títulos, "
            "campos no formato 'RÓTULO: valor' (nomes, CPF/CNPJ/RG, datas, números de matrícula/registro, endereços, valores), "
            "cláusulas e tabelas (uma linha por linha da tabela, colunas separadas por ' | '). Comece com uma linha 'TIPO: <que "
            "documento é>'. Marque trechos ilegíveis como [ilegível]; nunca invente nem complete o que não estiver visível. "
            "Responda só com a transcrição."),
    "search": ("Pesquise na web e responda em português do Brasil, de forma executiva (tópicos curtos). PRIORIZE anúncios do "
               "Mercado Livre (mercadolivre.com.br): traga 3 a 5 opções com título EXATO do anúncio, preço atual, vendedor/reputação, "
               "prazo de entrega e o LINK DIRETO da página do anúncio (formato produto.mercadolivre.com.br/MLB-123... ou "
               "mercadolivre.com.br/.../p/MLB123... ou /up/MLBU123...), obtido do próprio Mercado Livre: abra https://lista.mercadolivre.com.br/<termo-de-busca-com-hifens> (e, se precisar, "
               "abra o anúncio), escolha anúncios reais e copie a URL canônica (produto.mercadolivre.com.br/MLB-...). Abra APENAS "
               "páginas do mercadolivre.com.br, nenhum outro site. NUNCA use link de busca/lista "
               "(lista.mercadolivre.com.br, /jm/search) e nunca invente link: se não achar o link direto de uma opção, escreva "
               "'link direto não localizado' nela. Só complemente com outras lojas se o Mercado Livre não tiver boa opção. "
               "Aponte a melhor relação custo-benefício e alertas (medidas, cor, frete). Use SOMENTE busca na web e leitura de URL; "
               "NÃO execute comandos de terminal (curl/wget/python). Pedido: {q}"),
    "web": ("Pesquise na web e responda em português do Brasil, de forma objetiva e em tópicos curtos (formato WhatsApp: "
            "*negrito* com um asterisco). Use fontes atuais e confiáveis, cite datas quando o assunto for recente e inclua "
            "os links das fontes principais (no máximo 5). Se não encontrar a informação, diga isso claramente; nunca invente. "
            "Use SOMENTE busca na web e leitura de URL; NÃO execute comandos de terminal (curl/wget/python). Pedido: {q}"),
}


_URL = re.compile(r"https?://[^\s)\]>]+")
_ML_LISTING = re.compile(r"lista\.mercadolivre\.com\.br|/jm/search|mercadolivre\.com\.br/(?:ofertas|categorias)|[?&]search")
_ML_PRODUCT = re.compile(r"(?:produto\.mercadolivre\.com\.br/MLB-?\d+|mercadolivre\.com\.br/[^\s)]*/(?:p|up)/MLBU?\d+|"
                         r"mercadolivre\.com\.br/[^\s)]*MLB-?\d{6,})")


def _link_stats(text: str):
    urls = _URL.findall(text)
    return ([u for u in urls if _ML_PRODUCT.search(u) and not _ML_LISTING.search(u)],
            [u for u in urls if _ML_LISTING.search(u)])


def _fix_search_links(text: str, prompt: str, model: str, wd: str) -> str:
    """Garante link direto de anúncio: uma segunda tentativa mais firme se só vieram links de busca; no fim, links de
    busca são trocados por um aviso honesto (nunca apresentados como se fossem o produto)."""
    good, listing = _link_stats(text)
    if listing or not good:
        retry = (prompt + "\n\nATENÇÃO: sua resposta anterior trouxe links de busca/lista ou nenhum link de anúncio. Refaça: abra "
                 "a página de resultados do mercadolivre.com.br, entre em cada anúncio escolhido e copie a URL direta dele "
                 "(contém MLB- seguido de números). Sem link de busca. Se não conseguir para alguma, diga 'link direto não localizado'.")
        try:
            with _slots:
                p2 = _agy_run([AGY, "-p", retry, "--model", model, "--output-format", "json", "--sandbox"],
                                    cwd=wd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            raw = p2.stdout.strip()
            d2 = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
            t2 = (d2.get("response") or "").strip()
            if d2.get("status") == "SUCCESS" and t2:
                g2, l2 = _link_stats(t2)
                if len(g2) >= len(good):
                    text, good, listing = t2, g2, l2
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            log.warning("search retry failed: %s", exc)
    for u in set(_link_stats(text)[1]):
        text = text.replace(u, "(link direto não localizado — busque o título no Mercado Livre)")
    if not good:
        text += ("\n\n⚠️ Não consegui links diretos dos anúncios. Os títulos acima servem para buscar no Mercado Livre; "
                 "prefira mandar o link no grupo para eu confirmar preço e prazo.")
    return text


_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
BRAIN = os.environ.get("AGY_BRAIN_DIR", os.path.expanduser("~/.gemini/antigravity-cli/brain"))
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def gen_image(body: Dict[str, Any]) -> Dict[str, Any]:
    """Gera (ou edita a partir de uma referência) uma imagem com a ferramenta generate_image do agy — memes inclusive.
    `prompt` (obrigatório), `data_b64`+`mime` (imagem de referência, opcional). Devolve a imagem em base64."""
    import base64
    prompt = str(body.get("prompt") or "").strip()[:1500]
    if len(prompt) < 3:
        raise ValueError("prompt vazio")
    wd = tempfile.mkdtemp(prefix="agy_img_")
    ref = ""
    try:
        if body.get("data_b64"):
            ext = _EXT_BY_MIME.get(str(body.get("mime") or "").split(";")[0].lower(), "jpg")
            ref = f"referencia.{ext}"
            with open(os.path.join(wd, ref), "wb") as fh:
                fh.write(base64.b64decode(body["data_b64"]))
        ask = ("Gere uma imagem usando sua ferramenta de geração de imagem (generate_image)"
               + (f", com ./{ref} como imagem de referência (parta dela e mantenha o essencial da cena)" if ref else "")
               + f". Pedido: {prompt}\nQualquer texto na imagem deve sair EXATAMENTE como pedido, em português, grande e legível. "
               "NÃO execute comandos de terminal. Ao final responda só com o nome do arquivo gerado.")
        model = resolve_model(effort="high")
        conv, last = "", None
        for attempt in range(2):
            with _slots:
                t0 = time.time()
                p = _agy_run([AGY, "-p", ask, "--model", model, "--output-format", "json", "--sandbox"],
                                   cwd=wd, capture_output=True, text=True, timeout=TIMEOUT + 90, stdin=subprocess.DEVNULL)
            raw = p.stdout.strip()
            try:
                data = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
            except ValueError:
                data = {}
            conv = data.get("conversation_id") or ""
            d = os.path.join(BRAIN, conv) if conv else ""
            imgs = []
            if d and os.path.isdir(d):
                for root, dirs, files in os.walk(d):
                    dirs[:] = [x for x in dirs if not x.startswith(".")]
                    imgs += [os.path.join(root, f) for f in files if f.lower().endswith(_IMG_EXT)]
            if imgs:
                last = max(imgs, key=os.path.getmtime)
                break
            ask += "\nIMPORTANTE: a tentativa anterior não gerou arquivo; chame generate_image agora, sem comandos de terminal."
        if not last:
            raise RuntimeError(f"agy não gerou imagem: {(data.get('response') or p.stderr or raw)[:200]!r}")
        ext = os.path.splitext(last)[1].lower().lstrip(".")
        with open(last, "rb") as fh:
            blob = fh.read()
        log.info("agy image %s in %.1fs (%d bytes)", model, time.time() - t0, len(blob))
        if conv:
            shutil.rmtree(os.path.join(BRAIN, conv), ignore_errors=True)   # não acumular artefatos
        return {"kind": "image", "model": model, "mime": "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}",
                "data_b64": base64.b64encode(blob).decode(), "text": (data.get("response") or "")[:200]}
    except subprocess.TimeoutExpired:
        raise RuntimeError("agy image timeout") from None
    finally:
        shutil.rmtree(wd, ignore_errors=True)


def run_task(body: Dict[str, Any]) -> Dict[str, Any]:
    """Tarefas multimodais/web sem tool-calling: o arquivo vai para um diretório temporário vazio e o agy o lê (somente
    leitura, --sandbox). Nunca passa --dangerously-skip-permissions."""
    import base64
    kind = str(body.get("kind") or "")
    if kind == "image":
        return gen_image(body)
    if kind not in _TASK_PROMPTS:
        raise ValueError("kind deve ser transcribe|ocr|doc|search|web|image")
    effort = body.get("reasoning_effort") or "low"
    model = resolve_model(effort=effort)
    fname = ""
    wd = tempfile.mkdtemp(prefix="agy_task_")
    try:
        if kind in ("transcribe", "ocr", "doc"):
            mime = str(body.get("mime") or "").split(";")[0].strip().lower()
            blob = base64.b64decode(body.get("data_b64") or "")
            if blob[:5] == b"%PDF-":   # o mime do WhatsApp nem sempre vem certo: vale o conteúdo
                mime = "application/pdf"
            ext = _EXT.get(mime) or ("ogg" if kind == "transcribe" else "jpg")
            fname = f"input.{ext}"
            with open(os.path.join(wd, fname), "wb") as fh:
                fh.write(blob)
            prompt = _TASK_PROMPTS[kind].format(f=f"./{fname}")
            if body.get("prompt"):
                prompt += "\nContexto: " + str(body["prompt"])[:500]
        else:
            q = str(body.get("query") or "").strip()[:800]
            if not q:
                raise ValueError("query vazia")
            prompt = _TASK_PROMPTS[kind].format(q=q)
        data, raw, p = {}, "", None
        for attempt in range(3):  # o agy às vezes tenta um comando (negado no headless): uma nova tentativa resolve
            with _slots:
                t0 = time.time()
                p = _agy_run([AGY, "-p", prompt, "--model", model, "--output-format", "json", "--sandbox"],
                                   cwd=wd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
            raw = p.stdout.strip()
            try:
                data = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
            except ValueError:
                data = {}
            if data.get("status") == "SUCCESS" and (data.get("response") or "").strip():
                break
            if kind in ("ocr", "transcribe", "doc") and attempt == 0:
                prompt += ("\n\nIMPORTANTE: na tentativa anterior você tentou rodar um comando (negado). Abra o arquivo "
                           "diretamente com a ferramenta de leitura/visualização; nenhum comando de terminal.")
            if kind == "web" and attempt == 0:
                prompt += ("\n\nIMPORTANTE: na tentativa anterior você tentou rodar um comando (negado). Use apenas a busca na "
                           "web e a leitura de URLs; nenhum comando de terminal.")
            if kind == "search" and attempt == 0:
                prompt += ("\n\nIMPORTANTE: na tentativa anterior você tentou rodar um comando (negado). Use apenas a busca na "
                           "web e a leitura de URLs do mercadolivre.com.br; nenhum comando de terminal.")
        if data.get("status") != "SUCCESS" or not (data.get("response") or "").strip():
            raise RuntimeError(f"agy task {kind} falhou: status={data.get('status')!r} err={(p.stderr or raw)[:200]!r}")
        log.info("agy task %s %s in %.1fs", kind, model, time.time() - t0)
        text = data["response"].strip()
        if kind == "search":
            text = _fix_search_links(text, prompt, model, wd)
        return {"kind": kind, "model": model, "text": text}
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"agy task timeout after {TIMEOUT}s") from None
    finally:
        shutil.rmtree(wd, ignore_errors=True)


# --------------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "agy-shim/1"

    def log_message(self, fmt, *a):  # route access logs through logging
        log.info("%s %s", self.address_string(), fmt % a)

    def _send(self, code: int, obj: Dict[str, Any]) -> None:
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self) -> bool:
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return bool(TOKEN) and hmac.compare_digest(got, TOKEN)

    def do_GET(self):
        p = self.path.rstrip("/")
        if p == "/health":
            return self._send(200, {"status": "ok"})
        if not self._authed():
            return self._send(401, {"error": {"message": "unauthorized"}})
        if p in ("/v1/models", "/models"):
            ids = ["auto", MODEL] + ([_resolved["model"]] if _resolved["model"] and _resolved["model"] != MODEL else [])
            base = [{"id": i, "object": "model", "owned_by": "agy", "context_length": 64000, "max_model_len": 64000} for i in dict.fromkeys(ids)]
            extra = [{"id": i, "object": "model", "owned_by": "agy", "name": label, "context_length": 64000, "max_model_len": 64000}
                     for i, label in catalog().items() if i not in ids]
            return self._send(200, {"object": "list", "data": base + extra})
        if p.startswith(("/v1/models/", "/models/")):
            mid = p.rsplit("/", 1)[-1]
            return self._send(200, {"id": mid, "object": "model", "owned_by": "agy", "context_length": 64000, "max_model_len": 64000})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": {"message": "unauthorized"}})
        if self.path.rstrip("/").endswith("/files"):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_TASK_BODY:
                return self._send(413, {"error": {"message": "bad body size"}})
            try:
                return self._send(200, store_upload(json.loads(self.rfile.read(n))))
            except ValueError as exc:
                return self._send(400, {"error": {"message": str(exc)}})
        if self.path.rstrip("/").endswith("/task"):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_TASK_BODY:
                return self._send(413, {"error": {"message": "bad body size"}})
            try:
                return self._send(200, run_task(json.loads(self.rfile.read(n))))
            except ValueError as exc:
                return self._send(400, {"error": {"message": str(exc)}})
            except RuntimeError as exc:
                log.error("agy task failed: %s", exc)
                return self._send(502, {"error": {"message": str(exc), "type": "upstream_error"}})
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": {"message": "not found"}})
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > MAX_BODY:
            return self._send(413, {"error": {"message": "bad body size"}})
        try:
            body = json.loads(self.rfile.read(n))
            res = chat(body)
        except RuntimeError as exc:
            log.error("agy failed: %s", exc)
            return self._send(502, {"error": {"message": str(exc), "type": "upstream_error"}})
        except Exception as exc:  # malformed request etc.
            log.exception("bad request")
            return self._send(400, {"error": {"message": str(exc)}})
        if body.get("stream"):  # Hermes streaming is off here, but answer a stream request correctly anyway
            msg = res["choices"][0]["message"]
            delta = {"role": "assistant", **({"content": msg["content"]} if msg.get("content") else {}),
                     **({"tool_calls": [{"index": i, **c} for i, c in enumerate(msg.get("tool_calls") or [])]} if msg.get("tool_calls") else {})}
            chunks = [{**{k: res[k] for k in ("id", "object", "created", "model")}, "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {**{k: res[k] for k in ("id", "created", "model")}, "object": "chat.completion.chunk",
                       "choices": [{"index": 0, "delta": {}, "finish_reason": res["choices"][0]["finish_reason"]}], "usage": res["usage"]}]
            data = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            b = data.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._send(200, res)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN or len(TOKEN) < 16:
        raise SystemExit("AGY_SHIM_TOKEN (>= 16 chars) is required")
    if not AGY or not os.path.exists(AGY):
        raise SystemExit("agy binary not found (set AGY_BIN)")
    try:
        running = resolve_model()  # also warms the cache so the first chat does not pay for the lookup
    except RuntimeError as exc:  # catalogue unreachable: serve anyway, every request retries the lookup
        log.warning("%s", exc)
        running = MODEL
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("agy shim on http://%s:%d (model %s -> %s, parallel %d)", HOST, PORT, MODEL, running, MAX_PARALLEL)
    srv.serve_forever()


if __name__ == "__main__":
    main()
