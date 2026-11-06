#!/usr/bin/env python3
"""Evolution API WhatsApp Client for Sleeper Bot.

Handles sending messages, checking instance state, and configuring webhooks.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

DEFAULT_EVOLUTION_URL = os.environ.get("EVOLUTION_API_URL", "http://localhost:8084")
DEFAULT_API_KEY = os.environ.get("EVOLUTION_API_KEY", "")
DEFAULT_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot")


def sent_ok(res: Any) -> bool:
    """True only when Evolution accepted the message (HTTP 2xx with a message key)."""
    return isinstance(res, dict) and "error" not in res and bool((res.get("key") or {}).get("id"))


class EvolutionClient:
    def __init__(
        self,
        base_url: str = DEFAULT_EVOLUTION_URL,
        api_key: str = DEFAULT_API_KEY,
        instance: str = DEFAULT_INSTANCE,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.instance = instance

    def _request(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "EvolutionClient/1.0",
        }
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_body = response.read().decode("utf-8")
                return json.loads(res_body) if res_body else {}
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8") if e.fp else str(e)
            print(f"[ERROR] Evolution API HTTP {e.code}: {err}", file=sys.stderr)
            return {"error": e.code, "message": err}
        except Exception as e:
            print(f"[ERROR] Evolution API request failed: {e}", file=sys.stderr)
            return {"error": str(e)}

    def get_connection_state(self) -> Dict[str, Any]:
        """Check if WhatsApp is connected."""
        return self._request("GET", f"instance/connectionState/{self.instance}", timeout=10)

    def get_qr_code(self) -> Dict[str, Any]:
        """Get pairing QR code and pairing code."""
        return self._request("GET", f"instance/connect/{self.instance}", timeout=15)

    def send_message(self, number: str, text: str, link_preview: bool = False) -> Dict[str, Any]:
        """Send a WhatsApp text message to a number or group JID (link_preview: card of the first URL)."""
        payload = {
            "number": number,
            "text": text,
            "delay": 0,
            "linkPreview": link_preview,
        }
        return self._request("POST", f"message/sendText/{self.instance}", payload, timeout=30)

    def send_image(self, number: str, path: str, caption: str = "") -> Dict[str, Any]:
        """Send a local image (PNG/JPEG) with a caption (Evolution v2 /message/sendMedia, base64 body)."""
        import base64
        import mimetypes
        with open(path, "rb") as f:
            media = base64.b64encode(f.read()).decode("ascii")
        payload = {"number": number, "mediatype": "image", "mimetype": mimetypes.guess_type(path)[0] or "image/png",
                   "caption": caption, "media": media, "fileName": os.path.basename(path), "delay": 0}
        return self._request("POST", f"message/sendMedia/{self.instance}", payload, timeout=90)

    def send_audio(self, number: str, path: str) -> Dict[str, Any]:
        """Send a local audio file as a WhatsApp voice note (/message/sendWhatsAppAudio; Ogg/Opus via ffmpeg if present)."""
        import base64
        import subprocess
        import tempfile
        src, tmp = path, None
        if os.path.splitext(path)[1].lower() not in (".ogg", ".opus"):
            tmp = tempfile.mktemp(suffix=".ogg")
            try:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vn", "-c:a", "libopus", "-b:a", "48k",
                                "-ac", "1", "-ar", "48000", tmp], check=True, timeout=90)
                src = tmp
            except (OSError, subprocess.SubprocessError):
                tmp = None  # Evolution converts the original (encoding=true)
        try:
            with open(src, "rb") as f:
                audio = base64.b64encode(f.read()).decode("ascii")
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        payload = {"number": number, "audio": audio, "delay": 0, "encoding": True}
        return self._request("POST", f"message/sendWhatsAppAudio/{self.instance}", payload, timeout=120)

    def send_poll(self, number: str, question: str, options: list, selectable: int = 1) -> Dict[str, Any]:
        """WhatsApp poll (Evolution v2 /message/sendPoll)."""
        payload = {"number": number, "name": question, "selectableCount": selectable, "values": list(options)[:12]}
        return self._request("POST", f"message/sendPoll/{self.instance}", payload, timeout=30)

    def set_webhook(self, webhook_url: str) -> Dict[str, Any]:
        """Configure incoming webhook for WhatsApp events."""
        payload = {
            "webhook": {
                "enabled": True,
                "url": webhook_url,
                "byEvents": False,
                "base64": False,
                "events": [
                    "MESSAGES_UPSERT",
                    "CONNECTION_UPDATE",
                ],
            }
        }
        return self._request("POST", f"webhook/set/{self.instance}", payload)


if __name__ == "__main__":
    client = EvolutionClient()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "status":
        print(json.dumps(client.get_connection_state(), indent=2))
    elif cmd == "qr":
        qr = client.get_qr_code()
        pairing = qr.get("pairingCode")
        code = qr.get("code")
        print(f"Pairing Code: {pairing}")
        print(f"Raw Code: {code}")
    elif cmd == "send" and len(sys.argv) >= 4:
        number = sys.argv[2]
        msg = sys.argv[3]
        print(json.dumps(client.send_message(number, msg), indent=2))
    elif cmd == "webhook" and len(sys.argv) >= 3:
        url = sys.argv[2]
        print(json.dumps(client.set_webhook(url), indent=2))
    else:
        print("Usage: evolution_client.py [status|qr|send <num> <msg>|webhook <url>]")
