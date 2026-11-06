"""WhatsApp via Evolution API — Hermes platform plugin.

Inbound : Evolution webhook (``MESSAGES_UPSERT``) → aiohttp on ``EVOLUTION_WEBHOOK_PORT``
          (default 8660), path ``/webhook/evolution`` (``/webhook/evolution/<event>`` also
          accepted for ``webhookByEvents``). Auth: ``X-Webhook-Token`` == ``EVOLUTION_WEBHOOK_TOKEN``.
Outbound: ``POST {EVOLUTION_API_URL}/message/sendText/{EVOLUTION_INSTANCE}`` (text) and ``.../sendMedia/...``
          (images: a ``MEDIA:/path.png`` line in a reply becomes a WhatsApp photo; used by the `grafico` query)
          and ``.../sendWhatsAppAudio/...`` (voice notes from the TTS tool, only on explicit request).

Gating is done HERE (the gateway runs with EVOLUTION_ALLOW_ALL_USERS=true):
  * groups: only ``EVOLUTION_ALLOWED_GROUPS`` plus the group of every configured league (league.yaml); only when the
    bot is @mentioned, named (``EVOLUTION_MENTION_REGEX`` or the league's own persona regex) or the message quotes
    a bot message;
  * DMs: only ``EVOLUTION_DM_ALLOWED`` plus each league's ``LEAGUE_DM_ALLOWED``.

One WhatsApp number, many leagues: the league of a message is the one whose league.yaml lists its group
(``league_ctx.for_group``); admins, people, persona and notes are that league's, so leagues stay isolated.
Sender names come from the WhatsApp pushName but are sanitized; admins (``EVOLUTION_ADMINS``)
are tagged from their verified number, never from the display name. The sender's league team is resolved
(phone via ``LEAGUE_PEOPLE_PHONES``, else display name) and appended as ``[Nome | Time]`` so "meu confronto"
works. Non-admin members are rate limited (``EVOLUTION_RATE_LIMIT`` calls per 10 min, default 8).

A cron/agent message containing the line ``<<<MSG>>>`` is split into separate WhatsApp messages.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import time
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, gateway_trust_env
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms._shared import get_scoped_secret as _secret, send_error

try:
    import aiohttp
    from aiohttp import web
    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore
    web = None  # type: ignore

logger = logging.getLogger(__name__)

PLATFORM_NAME = "evolution"
MSG_SPLIT = "<<<MSG>>>"
MAX_LEN = 3800
_MAX_BODY = 2 * 1024 * 1024
_SEEN_TTL = 600.0
_RATE_WINDOW = 600.0
_TRUTHY = {"1", "true", "yes", "on"}

_GREETINGS = {"", "oi", "ola", "olá", "fala", "eae", "eai", "opa", "bom dia", "boa tarde", "boa noite",
              "help", "ajuda", "?", "??", "salve", "e ai", "e aí"}
_GREETING_REPLIES = (
    "Fala, *{n}*! Chamou o *{p}* por quê? Manda a pergunta: placar, confronto, lesão, trade, H2H, draft…",
    "Diga aí, *{n}*! Se me acordou só pra marcar meu nome, é bom ter uma pergunta decente sobre a liga.",
    "Ora, ora… *{n}* marcou o *{p}* sem dizer nada. Bateu o desespero com o time?",
)


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "").split("@", 1)[0] if ch.isdigit())


def _csv(name: str) -> set[str]:
    raw = _secret(name, "") or ""
    return {p.strip() for p in str(raw).split(",") if p.strip()}


def _id_matches(candidate_digits: str, allowed: set[str]) -> bool:
    """Phone numbers arrive with/without the Brazilian 9th digit; LIDs are exact."""
    if not candidate_digits:
        return False
    for entry in allowed:
        e = _digits(entry)
        if not e:
            continue
        if e == candidate_digits:
            return True
        # 55 48 9XXXX-XXXX vs 55 48 XXXX-XXXX: compare the last 8 digits + DDD.
        if len(e) >= 10 and len(candidate_digits) >= 10 and e[-8:] == candidate_digits[-8:] \
                and e[:4] == candidate_digits[:4]:
            return True
    return False


def whatsapp_format(text: str) -> str:
    """Markdown → WhatsApp: **b**→*b*, headers→*h*, '* ' bullets→'• ', '*T:*'→'*T*:'."""
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"^#{1,6}\s*(.+?)\s*#*$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"\*([^*:\n]+):\*", r"*\1*:", text)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", text)
    out = []
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith(("* ", "- ")) and not stripped.startswith("- -"):
            indent = line[: len(line) - len(stripped)]
            line = f"{indent}• {stripped[2:]}"
        if line.count("*") % 2 and line.rstrip().endswith("*"):
            line = line.rstrip()[:-1]
        out.append(line)
    return "\n".join(out).replace("**", "*").strip()


def _extract_text(message: Dict[str, Any]) -> str:
    if not isinstance(message, dict):
        return ""
    if message.get("conversation"):
        return str(message["conversation"])
    for key in ("extendedTextMessage",):
        if isinstance(message.get(key), dict) and message[key].get("text"):
            return str(message[key]["text"])
    for key in ("imageMessage", "videoMessage", "documentMessage"):
        if isinstance(message.get(key), dict) and message[key].get("caption"):
            return str(message[key]["caption"])
    return ""


def _context_info(message: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Quote / mention info of a message. Evolution v2 delivers it at `data.contextInfo` (beside `message`) for plain
    texts and replies; older/other shapes nest it inside the message node. Look in all of them and merge."""
    found: Dict[str, Any] = {}
    nodes = []
    if isinstance(message, dict):
        for key in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage", "conversation"):
            node = message.get(key)
            if isinstance(node, dict):
                nodes.append(node.get("contextInfo"))
        nodes += [message.get("contextInfo"), message.get("messageContextInfo")]
    if isinstance(data, dict):
        nodes.append(data.get("contextInfo"))
    for node in nodes:
        if isinstance(node, dict):
            for k, v in node.items():
                if v not in (None, "", [], {}) and k not in found:
                    found[k] = v
    return found


def _league_module(name: str):
    league = os.environ.get("LEAGUE_DIR", "/opt/league")
    if league not in sys.path:
        sys.path.insert(0, league)
    try:
        return __import__(name)
    except Exception as exc:  # pragma: no cover
        logger.warning("[evolution] league module %s unavailable: %s", name, exc)
        return None


def _load_admin_notes():
    """Import league/admin_notes.py (explicit "anota aí: …" command for the verified admin)."""
    league = os.environ.get("LEAGUE_DIR", "/opt/league")
    if not os.path.isdir(league):
        return None
    if league not in sys.path:
        sys.path.insert(0, league)
    try:
        import admin_notes  # noqa: WPS433
        return admin_notes
    except Exception as exc:  # pragma: no cover
        logger.warning("[evolution] admin notes unavailable: %s", exc)
        return None


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE and bool(_secret("EVOLUTION_API_URL", "") and _secret("EVOLUTION_API_KEY", "")
                                      and _secret("EVOLUTION_INSTANCE", ""))


def validate_config(config) -> bool:
    return check_requirements()


def is_connected(config) -> bool:
    return check_requirements()


def _env_enablement() -> Optional[dict]:
    if not check_requirements():
        return None
    home = (_secret("EVOLUTION_HOME_CHANNEL", "") or "").strip()
    seed: Dict[str, Any] = {"instance": _secret("EVOLUTION_INSTANCE", "")}
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "WhatsApp group"}
    return seed


async def _post_json(session, url: str, api_key: str, payload: dict, timeout: float = 30.0):
    async with session.post(url, json=payload, headers={"apikey": api_key},
                            timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        try:
            body = await resp.json(content_type=None)
        except Exception:
            body = {"raw": (await resp.text())[:300]}
        return resp.status, body


_AUDIO_EXT = {".mp3", ".ogg", ".opus", ".m4a", ".wav", ".aac", ".webm"}


def _audio_payload(chat_id: str, path: str) -> dict:
    """Evolution v2 /message/sendWhatsAppAudio body: a push-to-talk voice note (base64, Ogg/Opus when ffmpeg is there;
    otherwise the original file and Evolution transcodes it, ``encoding: true``)."""
    import base64
    import subprocess
    import tempfile
    src, tmp = path, None
    if os.path.splitext(path)[1].lower() not in (".ogg", ".opus"):
        fd, tmp = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vn", "-c:a", "libopus", "-b:a", "32k",
                            "-ac", "1", "-ar", "48000", tmp], check=True, timeout=45)
            src = tmp
        except (OSError, subprocess.SubprocessError):
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
            tmp = None  # no ffmpeg / failed: send the original, Evolution converts
    try:
        with open(src, "rb") as f:
            audio = base64.b64encode(f.read()).decode("ascii")
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return {"number": chat_id, "audio": audio, "delay": 0, "encoding": True}


_IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
_CAPTION_MAX = 1000  # WhatsApp caps captions at 1024 chars


def _media_payload(chat_id: str, path: str, caption: str = "", force_document: bool = False) -> dict:
    """Evolution v2 /message/sendMedia body (base64). Images go as photos, anything else as a document."""
    import base64
    import mimetypes
    ext = os.path.splitext(path)[1].lower()
    is_image = ext in _IMAGE_EXT and not force_document
    with open(path, "rb") as f:
        media = base64.b64encode(f.read()).decode("ascii")
    return {"number": chat_id, "mediatype": "image" if is_image else "document",
            "mimetype": _IMAGE_EXT.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream",
            "caption": (caption or "")[:_CAPTION_MAX], "media": media, "fileName": os.path.basename(path), "delay": 0}


def _split_messages(content: str) -> list[str]:
    parts = [p.strip() for p in (content or "").split(MSG_SPLIT)]
    return [p for p in parts if p]


class EvolutionAdapter(BasePlatformAdapter):
    """Evolution API WhatsApp <-> Hermes gateway."""

    MAX_MESSAGE_LENGTH = MAX_LEN

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self._base_url = (_secret("EVOLUTION_API_URL", "") or "").rstrip("/")
        self._api_key = _secret("EVOLUTION_API_KEY", "") or ""
        self._instance = _secret("EVOLUTION_INSTANCE", "") or ""
        self._token = _secret("EVOLUTION_WEBHOOK_TOKEN", "") or ""
        self._port = int(_secret("EVOLUTION_WEBHOOK_PORT", "8660") or 8660)
        self._host = _secret("EVOLUTION_WEBHOOK_HOST", "0.0.0.0") or "0.0.0.0"
        self._groups = _csv("EVOLUTION_ALLOWED_GROUPS")
        self._dm_allowed = _csv("EVOLUTION_DM_ALLOWED")
        self._admins = _csv("EVOLUTION_ADMINS")
        self._bot_ids = {_digits(x) for x in _csv("EVOLUTION_BOT_IDS")} - {""}
        rx = _secret("EVOLUTION_MENTION_REGEX", "") or ""
        self._mention_rx = re.compile(rx, re.IGNORECASE) if rx else None
        self._seen: Dict[str, float] = {}
        self._unlisted_logged: set = set()
        self._notes = _load_admin_notes()
        self._people = _league_module("people")
        self._lctx = _league_module("league_ctx")
        self._rate: Dict[str, list] = {}
        self._rate_limit = int(_secret("EVOLUTION_RATE_LIMIT", "8") or 8)
        self._runner = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._chain = None            # reply_chain.ChainStore: the thread behind a reply (league/reply_chain.py)
        try:
            mod = _league_module("reply_chain")
            self._chain = mod.ChainStore() if mod else None
        except Exception as exc:  # pragma: no cover
            logger.warning("[evolution] reply chain unavailable: %s", exc)

    # ------------------------------------------------------------------ lifecycle
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not check_requirements():
            self._set_fatal_error("evolution_config", "[evolution] EVOLUTION_API_URL/KEY/INSTANCE missing",
                                  retryable=False)
            return False
        if not self._token:
            logger.warning("[evolution] EVOLUTION_WEBHOOK_TOKEN unset — webhook is UNAUTHENTICATED")
        app = web.Application(client_max_size=_MAX_BODY)
        app.router.add_post("/webhook/evolution", self._handle_webhook)
        app.router.add_post("/webhook/evolution/{event}", self._handle_webhook)
        app.router.add_get("/health", lambda _r: web.json_response({"status": "ok", "platform": PLATFORM_NAME}))
        from gateway.platforms.shared_ingress import bind_listener
        self._runner = await bind_listener(self, app, self._host, self._port, "/webhook/evolution")
        self._http = aiohttp.ClientSession(trust_env=gateway_trust_env())
        self._running = True
        logger.info("[evolution] webhook on %s:%d/webhook/evolution (instance=%s, groups=%d, dm=%d)",
                    self._host, self._port, self._instance, len(self._groups), len(self._dm_allowed))
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._running = False

    # ------------------------------------------------------------------ outbound
    def format_message(self, content: str) -> str:
        return whatsapp_format(content)

    def _remember(self, chat_id: str, msg_id: str, text: str, *, reply_to: Optional[str] = None,
                  turn: Optional[str] = None, seq: int = 0) -> None:
        """Log what the bot said so a later reply on top of it can bring the whole thread back."""
        if self._chain and msg_id:
            try:
                persona = self._lctx.current().persona_name if self._lctx else "Mr. Irrelevant"
            except Exception:
                persona = "Mr. Irrelevant"
            try:
                self._chain.record(chat_id, msg_id, persona, text, is_bot=True, reply_to=reply_to, turn=turn, seq=seq)
            except Exception as exc:  # pragma: no cover
                logger.warning("[evolution] reply chain write failed: %s", exc)

    async def _send_one(self, chat_id: str, text: str, *, reply_to: Optional[str] = None,
                        turn: Optional[str] = None, seq: int = 0) -> SendResult:
        url = f"{self._base_url}/message/sendText/{self._instance}"
        payload = {"number": chat_id, "text": text, "delay": 0, "linkPreview": False}
        session = self._http or aiohttp.ClientSession(trust_env=gateway_trust_env())
        try:
            status, body = await _post_json(session, url, self._api_key, payload)
        except Exception as exc:
            logger.error("[evolution] send error: %s", exc)
            return SendResult(success=False, error=str(exc))
        finally:
            if session is not self._http:
                await session.close()
        if status >= 400:
            logger.error("[evolution] send failed %s: %s", status, str(body)[:300])
            return SendResult(success=False, error=f"Evolution {status}: {str(body)[:200]}")
        msg_id = ((body or {}).get("key") or {}).get("id", "") if isinstance(body, dict) else ""
        self._remember(chat_id, msg_id, text, reply_to=reply_to, turn=turn, seq=seq)
        return SendResult(success=True, message_id=msg_id)

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        result = SendResult(success=True)
        messages = _split_messages(content) or [content or ""]
        turn, seq = f"t{time.time_ns()}", 0           # the chunks of one answer are one message for the thread
        for i, part in enumerate(messages):
            for chunk in self.truncate_message(self.format_message(part), MAX_LEN):
                if not chunk.strip():
                    continue
                seq += 1
                result = await self._send_one(chat_id, chunk, reply_to=reply_to, turn=turn, seq=seq)
                if not result.success:
                    return result
            if i < len(messages) - 1:
                await asyncio.sleep(2.0)
        return result

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        """Upload a local image (e.g. a league chart PNG) as a WhatsApp photo with a caption."""
        try:
            payload = await asyncio.get_running_loop().run_in_executor(
                None, _media_payload, chat_id, image_path, self.format_message(caption or ""))
        except OSError as exc:
            logger.error("[evolution] cannot read %s: %s", image_path, exc)
            return SendResult(success=False, error=f"cannot read image: {exc}")
        session = self._http or aiohttp.ClientSession(trust_env=gateway_trust_env())
        try:
            status, body = await _post_json(session, f"{self._base_url}/message/sendMedia/{self._instance}",
                                            self._api_key, payload, timeout=90.0)
        except Exception as exc:
            logger.error("[evolution] sendMedia error: %s", exc)
            return SendResult(success=False, error=str(exc))
        finally:
            if session is not self._http:
                await session.close()
        if status >= 400:
            logger.error("[evolution] sendMedia failed %s: %s", status, str(body)[:300])
            return SendResult(success=False, error=f"Evolution {status}: {str(body)[:200]}")
        msg_id = ((body or {}).get("key") or {}).get("id", "") if isinstance(body, dict) else ""
        self._remember(chat_id, msg_id, (caption or "").strip() or "[gráfico/imagem enviado]", reply_to=reply_to)
        return SendResult(success=True, message_id=msg_id)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        """Upload a TTS file as a WhatsApp voice note (the bot only does this when someone asks for audio)."""
        try:
            payload = await asyncio.get_running_loop().run_in_executor(None, _audio_payload, chat_id, audio_path)
        except OSError as exc:
            logger.error("[evolution] cannot read %s: %s", audio_path, exc)
            return SendResult(success=False, error=f"cannot read audio: {exc}")
        session = self._http or aiohttp.ClientSession(trust_env=gateway_trust_env())
        try:
            status, body = await _post_json(session, f"{self._base_url}/message/sendWhatsAppAudio/{self._instance}",
                                            self._api_key, payload, timeout=90.0)
        except Exception as exc:
            logger.error("[evolution] sendWhatsAppAudio error: %s", exc)
            return SendResult(success=False, error=str(exc))
        finally:
            if session is not self._http:
                await session.close()
        if status >= 400:
            logger.error("[evolution] sendWhatsAppAudio failed %s: %s", status, str(body)[:300])
            return SendResult(success=False, error=f"Evolution {status}: {str(body)[:200]}")
        msg_id = ((body or {}).get("key") or {}).get("id", "") if isinstance(body, dict) else ""
        self._remember(chat_id, msg_id, (caption or "").strip() or "[áudio enviado]", reply_to=reply_to)
        if caption and caption.strip():  # voice notes carry no caption: send it as text right after
            await self._send_one(chat_id, self.format_message(caption))
        return SendResult(success=True, message_id=msg_id)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if not self._http:
            return
        url = f"{self._base_url}/chat/sendPresence/{self._instance}"
        try:
            await _post_json(self._http, url, self._api_key,
                             {"number": chat_id, "presence": "composing", "delay": 4000}, timeout=8)
        except Exception:
            pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "group" if chat_id.endswith("@g.us") else "dm", "chat_id": chat_id}

    # ------------------------------------------------------------------ inbound
    async def _route(self, ev: MessageEvent, text: str, is_admin: bool, author: str, chat_id: str,
                     league=None) -> None:
        await _route_impl(self, ev, text, is_admin, author, chat_id, league)

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.monotonic()
        if len(self._seen) > 2000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < _SEEN_TTL}
        if not msg_id:
            return False
        if msg_id in self._seen and now - self._seen[msg_id] < _SEEN_TTL:
            return True
        self._seen[msg_id] = now
        return False

    # ------------------------------------------------------------------ league resolution (logic: league_ctx)
    def _league_for(self, jid: str):
        """LeagueContext served in a group (unknown group -> the default league)."""
        if not self._lctx:
            return None
        try:
            return self._lctx.league_for_group(jid)
        except Exception as exc:  # pragma: no cover - never lose a message to a config problem
            logger.warning("[evolution] league resolution failed: %s", exc)
            return None

    def _league_for_sender(self, sender_ids):
        if not self._lctx:
            return None
        try:
            return self._lctx.league_for_sender(sorted(sender_ids))
        except Exception as exc:  # pragma: no cover
            logger.warning("[evolution] DM league resolution failed: %s", exc)
            return None

    def _route_mismatch(self, league, chat_id: str) -> Optional[str]:
        """Isolation guard. A non-default league must be served by ITS profile: if the gateway would answer this chat
        with another one (routes not synced / gateway not restarted / multiplex off), the reply would carry the wrong
        persona and the wrong league's data. Returns a description of the problem, or None when all is well (or when
        it cannot be verified: the pinned Hermes internals changed)."""
        if league is None or getattr(league, "legacy_layout", True):
            return None
        want = os.path.basename(str(getattr(league, "home", "")).rstrip("/"))
        try:
            from gateway.config import load_gateway_config
            from gateway.profile_routing import match_profile_route
            cfg_path = os.path.join(str(os.environ.get("HERMES_HOME") or "/opt/data"), "config.yaml")
            mtime = os.path.getmtime(cfg_path) if os.path.exists(cfg_path) else 0.0
            if getattr(self, "_routes_mtime", None) != mtime:
                gw = load_gateway_config()
                self._routes_cache = (bool(gw.multiplex_profiles), list(gw.profile_routes or []))
                self._routes_mtime = mtime
            multiplex, routes = self._routes_cache
        except Exception as exc:  # pragma: no cover - Hermes internals moved: do not take every league down
            logger.warning("[evolution] cannot verify the profile route of %s: %s", chat_id, exc)
            return None
        if not multiplex:
            return f"gateway.multiplex_profiles is off, so league {league.slug!r} would be answered by the default profile"
        route = match_profile_route(routes, PLATFORM_NAME, chat_id=chat_id)
        got = route.profile if route is not None else "default"
        if got != want:
            return (f"league {league.slug!r} is served by profile {want!r} but the gateway would use {got!r} for "
                    f"chat {chat_id!r} (run `ops/liga.py sync` and restart the container)")
        return None

    def _all_group_jids(self) -> set:
        out = set(self._groups)
        try:
            out |= self._lctx.all_group_jids() if self._lctx else set()
        except Exception:  # pragma: no cover
            pass
        return out

    def _all_dm_allowed(self) -> set:
        out = set(self._dm_allowed)
        try:
            out |= self._lctx.all_dm_allowed() if self._lctx else set()
        except Exception:  # pragma: no cover
            pass
        return out

    def _mention_rx_for(self, league):
        """The league's persona regex (league.yaml persona.mention_regex), else the instance-wide one."""
        rx = ""
        if league is not None:
            try:
                rx = str((league.persona or {}).get("mention_regex") or "")
            except Exception:  # pragma: no cover
                rx = ""
        if rx and rx != getattr(self._mention_rx, "pattern", None):
            try:
                return re.compile(rx, re.IGNORECASE)
            except re.error:
                logger.warning("[evolution] invalid mention_regex for league %s", getattr(league, "slug", "?"))
        return self._mention_rx

    def _bot_addressed(self, text: str, ctx: Dict[str, Any], rx=None) -> bool:
        rx = rx if rx is not None else self._mention_rx
        mentioned = {_digits(j) for j in (ctx.get("mentionedJid") or [])}
        if self._bot_ids & mentioned:
            return True
        quoted_author = _digits(ctx.get("participant"))
        if quoted_author and quoted_author in self._bot_ids and ctx.get("quotedMessage"):
            return True
        if any(f"@{b}" in text for b in self._bot_ids):
            return True
        return bool(rx and rx.search(text))

    def _clean_text(self, text: str, rx=None) -> str:
        rx = rx if rx is not None else self._mention_rx
        cleaned = text
        for b in self._bot_ids:
            cleaned = re.sub(rf"@{re.escape(b)}\b[,:\-]*\s*", "", cleaned)
        if rx:
            # Strip a leading summon ("Purdy, ...", "@Mr Irrelevant ...") but keep names used mid-sentence.
            cleaned = re.sub(rf"^\s*@?(?:{rx.pattern})[\s,:!\-]*", "", cleaned, flags=re.IGNORECASE)
        return cleaned.replace("!bot", "").replace("/bot", "").strip()

    def _rate_limited(self, user_id: str) -> Optional[bool]:
        """None = allowed; True = blocked (first time in this window → warn); False = blocked, stay quiet."""
        now = time.monotonic()
        hits = [t for t in self._rate.get(user_id, []) if now - t < _RATE_WINDOW]
        if len(hits) >= self._rate_limit:
            warned = self._rate.get(f"{user_id}:warned")
            self._rate[user_id] = hits
            if warned and now - warned[0] < _RATE_WINDOW:
                return False
            self._rate[f"{user_id}:warned"] = [now]
            return True
        hits.append(now)
        self._rate[user_id] = hits
        return None

    @staticmethod
    def _safe_name(push_name: str) -> str:
        name = re.sub(r"[\[\]\(\)<>{}|\n\r\t]", " ", str(push_name or "")).strip()
        name = re.sub(r"(?i)\badmin\w*|\bverificad\w*|\bcomiss\w*", "", name)
        return re.sub(r"\s+", " ", name).strip()[:40] or "Membro"

    async def _forward_webhook(self, url: str, payload: Dict[str, Any]) -> None:
        try:
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["X-Webhook-Token"] = self._token
            session = self._http or aiohttp.ClientSession(trust_env=gateway_trust_env())
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0)) as resp:
                pass
        except Exception as exc:
            logger.debug("[evolution] forward to %s failed: %s", url, exc)

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        if self._token and request.headers.get("X-Webhook-Token", "") != self._token:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        fwd_raw = _secret("EVOLUTION_FORWARD_WEBHOOKS", "")
        if fwd_raw:
            fwd_urls = [u.strip() for u in fwd_raw.split(",") if u.strip()]
            for u in fwd_urls:
                asyncio.create_task(self._forward_webhook(u, payload))
        try:
            self._dispatch(payload)
        except Exception as exc:  # never let a bad payload 500 → Evolution retries storm
            logger.error("[evolution] payload handling error: %s", exc, exc_info=True)
        return web.json_response({"status": "ok"})

    def _dispatch(self, payload: Dict[str, Any]) -> None:
        event = str(payload.get("event") or "").lower().replace("_", ".")
        if event != "messages.upsert":
            return
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        key = data.get("key") or {}
        if key.get("fromMe"):
            return
        remote_jid = str(key.get("remoteJid") or "")
        if not remote_jid or remote_jid.endswith(("@broadcast", "@newsletter")) or remote_jid == "status@broadcast":
            return
        msg_id = str(key.get("id") or "")
        if self._is_duplicate(msg_id):
            return
        message = data.get("message") or {}
        text = _extract_text(message).strip()
        if not text:
            return
        ctx = _context_info(message, data)
        is_group = remote_jid.endswith("@g.us")
        sender_raw = (key.get("participantAlt") or key.get("participantPn") or data.get("participantPn")
                      or key.get("participant") or data.get("participant")) if is_group else \
                     (key.get("remoteJidAlt") or remote_jid)
        sender_ids = {_digits(x) for x in (key.get("participant"), key.get("participantAlt"),
                                           key.get("participantPn"), data.get("participant"),
                                           key.get("remoteJidAlt"), None if is_group else remote_jid,
                                           key.get("sender"), data.get("sender")) if x} - {""}
        push = self._safe_name(data.get("pushName") or "")

        if is_group:
            known = self._all_group_jids()
            if known and remote_jid not in known:
                if remote_jid not in self._unlisted_logged:      # once per group: how an operator finds a new group's JID
                    self._unlisted_logged.add(remote_jid)
                    logger.info("[evolution] message from a group no league uses: %s (ops/liga.py new … --group %s)",
                                remote_jid, remote_jid)
                return
            league = self._league_for(remote_jid)
            rx = self._mention_rx_for(league)
            stanza = str(ctx.get("stanzaId") or "") or None
            if self._chain:        # every group message joins the log: a thread may start on a message nobody addressed
                try:
                    self._chain.record(remote_jid, msg_id, push or "alguém", text, reply_to=stanza)
                except Exception as exc:  # pragma: no cover
                    logger.warning("[evolution] reply chain write failed: %s", exc)
            replied_to_bot = bool(self._chain and self._chain.was_bot(remote_jid, stanza))
            if not (replied_to_bot or self._bot_addressed(text, ctx, rx)):
                return
            chat_id = remote_jid
        else:
            if not any(_id_matches(s, self._all_dm_allowed()) for s in sender_ids):
                logger.info("[evolution] DM blocked from unlisted sender")
                return
            league = self._league_for_sender(sender_ids)
            rx = self._mention_rx_for(league)
            alt = str(key.get("remoteJidAlt") or "")
            chat_id = alt.split("@", 1)[0] if alt.endswith("@s.whatsapp.net") else remote_jid
        problem = self._route_mismatch(league, chat_id)
        if problem:
            logger.error("[evolution] message dropped to keep leagues isolated: %s", problem)
            return
        # operator admins (instance-wide) plus the admins of THIS league
        admins = set(self._admins) | (set(league.admins) if league is not None else set())
        is_admin = any(_id_matches(s, admins) for s in sender_ids)

        clean = self._clean_text(text, rx)
        first = push.split()[0] if push else "camarada"
        persona = league.persona_name if league is not None else "Mr. Irrelevant"
        if clean.lower().strip(" !?.,") in _GREETINGS:
            # Zero-token canned reply for bare pings.
            task = asyncio.create_task(self.send(chat_id, random.choice(_GREETING_REPLIES).format(n=first, p=persona)))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            return

        user_id_rl = _digits(sender_raw) or next(iter(sender_ids), "") or remote_jid
        if not is_admin:
            limited = self._rate_limited(user_id_rl)
            if limited is not None:
                if limited:
                    task = asyncio.create_task(self.send(chat_id, f"⏳ Calma, *{first}*: limite de perguntas por 10 minutos atingido. Respira e tenta daqui a pouco."))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
                logger.info("[evolution] rate limited %s", push)
                return

        user_name = f"{push} ✅ADMIN" if is_admin else push
        team = ""
        try:
            if league is not None:       # this league's phone book and names, never another league's
                team = league.people.label(league.people.resolve_sender(sorted(sender_ids), push))
            elif self._people:
                team = self._people.label(self._people.resolve_sender(sorted(sender_ids), push))
        except Exception as exc:
            logger.warning("[evolution] team resolution failed: %s", exc)
        if team:
            user_name = f"{user_name} | {team}"
        quoted = ctx.get("quotedMessage") or {}
        reply_text = _extract_text(quoted)[:500] if quoted else None
        agent_text = clean
        if self._chain and ctx.get("stanzaId"):
            try:
                import reply_chain
                thread = self._chain.chain(chat_id, str(ctx["stanzaId"]))
                if not thread and reply_text:    # parent sent before the log existed: keep at least WhatsApp's own quote
                    q_bot = _digits(ctx.get("participant")) in self._bot_ids
                    thread = reply_chain.quoted_fallback(
                        persona if q_bot else "alguém", reply_text, q_bot)
                block = reply_chain.format_context(thread, persona)
                if block:
                    agent_text, reply_text = block + clean, None      # the thread replaces the single quoted line
                    logger.info("[evolution] reply thread: %d message(s) of context", len(thread))
            except Exception as exc:  # pragma: no cover
                logger.warning("[evolution] reply thread failed: %s", exc)
        user_id = _digits(sender_raw) or next(iter(sender_ids), "") or remote_jid
        source = self.build_source(
            chat_id=chat_id, chat_name=remote_jid if is_group else push,
            chat_type="group" if is_group else "dm", user_id=user_id, user_name=user_name,
            message_id=msg_id)
        ev = MessageEvent(text=agent_text, message_type=MessageType.TEXT, source=source,
                          raw_message=None, message_id=msg_id,
                          reply_to_text=reply_text or None,
                          reply_to_author_id=_digits(ctx.get("participant")) or None)
        logger.info("[evolution] %s from %s%s: %s", "group" if is_group else "dm", push,
                    " (admin)" if is_admin else "", clean[:80])
        task = asyncio.create_task(self._route(ev, clean, is_admin, push, chat_id, league))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)


async def _route_impl(adapter: "EvolutionAdapter", ev: MessageEvent, text: str, is_admin: bool,
                      author: str, chat_id: str, league=None) -> None:
    """Admin note / watch switch commands (deterministic) → confirm; everything else goes to the agent."""
    if is_admin and "@g.us" not in (chat_id or ""):
        # "parar placar" / "continuar escalação": the personal DM alerts (league/watch_prefs.py), no model call
        prefs = _league_module("watch_prefs")
        cmd = prefs.parse_command(text) if prefs else None
        if cmd:
            try:
                if league is not None and adapter._lctx:
                    with adapter._lctx.use(league):
                        reply = prefs.apply_command(cmd)
                else:
                    reply = prefs.apply_command(cmd)
                logger.info("[evolution] route=watch-switch %s %s", cmd["action"], cmd["kinds"])
                await adapter.send(chat_id, reply)
                return
            except Exception as exc:
                logger.warning("[evolution] watch switch failed: %s", exc)
    if is_admin and adapter._notes:
        body = adapter._notes.parse_command(text)
        if body:
            try:
                if league is not None and adapter._lctx:
                    with adapter._lctx.use(league):     # the note lands in THIS league's knowledge base
                        adapter._notes.save_note(body, author)
                else:
                    adapter._notes.save_note(body, author)
                logger.info("[evolution] route=note (admin) %d chars", len(body))
                await adapter.send(chat_id, "📝 Anotado, chefe. Vira fato oficial da liga a partir de agora.")
                return
            except Exception as exc:
                logger.warning("[evolution] saving admin note failed: %s", exc)
    await adapter.handle_message(ev)


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """Out-of-process delivery (cron running outside the gateway)."""
    if not AIOHTTP_AVAILABLE:
        return send_error("aiohttp not installed")
    base = (_secret("EVOLUTION_API_URL", "") or "").rstrip("/")
    key = _secret("EVOLUTION_API_KEY", "") or ""
    inst = _secret("EVOLUTION_INSTANCE", "") or ""
    if not (base and key and inst):
        return send_error("Evolution not configured")
    last_id = ""
    try:
        async with aiohttp.ClientSession() as session:
            for part in (_split_messages(message) or [message]) if (message or "").strip() else []:
                for chunk in BasePlatformAdapter.truncate_message(whatsapp_format(part), MAX_LEN):
                    status, body = await _post_json(session, f"{base}/message/sendText/{inst}", key,
                                                    {"number": chat_id, "text": chunk, "delay": 0,
                                                     "linkPreview": False})
                    if status >= 400:
                        return send_error(f"Evolution {status}: {str(body)[:200]}")
                    last_id = ((body or {}).get("key") or {}).get("id", "") if isinstance(body, dict) else ""
                await asyncio.sleep(1.5)
            for i, item in enumerate(media_files or []):
                path = item[0] if isinstance(item, (tuple, list)) else item
                if os.path.splitext(str(path))[1].lower() in _AUDIO_EXT and not force_document:
                    endpoint, payload = "sendWhatsAppAudio", _audio_payload(chat_id, str(path))
                else:
                    endpoint, payload = "sendMedia", _media_payload(chat_id, str(path), "", force_document=force_document)
                status, body = await _post_json(session, f"{base}/message/{endpoint}/{inst}", key, payload, timeout=90.0)
                if status >= 400:
                    return send_error(f"Evolution sendMedia {status}: {str(body)[:200]}")
                last_id = ((body or {}).get("key") or {}).get("id", "") if isinstance(body, dict) else last_id
    except Exception as exc:
        return send_error(f"Evolution send failed: {exc}")
    return {"success": True, "platform": PLATFORM_NAME, "chat_id": chat_id, "message_id": last_id}


def register(ctx) -> None:
    ctx.register_platform(
        name=PLATFORM_NAME,
        label="WhatsApp (Evolution API)",
        adapter_factory=EvolutionAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["EVOLUTION_API_URL", "EVOLUTION_API_KEY", "EVOLUTION_INSTANCE"],
        install_hint="aiohttp (bundled with the gateway)",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="EVOLUTION_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="EVOLUTION_ALLOWED_USERS",
        allow_all_env="EVOLUTION_ALLOW_ALL_USERS",
        max_message_length=MAX_LEN,
        emoji="🏈",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "Você está num grupo de WhatsApp. Formatação WhatsApp: *negrito* com UM asterisco, _itálico_, "
            "listas com '• '. Nada de tabelas, cabeçalhos # ou **duplo asterisco**. Respostas curtas e diretas."),
    )
