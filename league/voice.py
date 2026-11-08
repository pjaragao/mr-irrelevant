#!/usr/bin/env python3
"""Voice for the WhatsApp bot: speech-to-text for incoming voice notes and text-to-speech for spoken replies.

* ``transcribe``: the agy shim's ``/v1/task kind=transcribe`` (Gemini via Antigravity, AGY_SHIM_TOKEN) hears the voice
  note and returns the words. Any failure returns "" (the caller says it could not understand).
* ``speak``: plain Edge TTS voice (same voice as the songs, ``tts.edge.voice``), no beat, as an Ogg/Opus voice note;
  numbers, weeks and percentages are spelled out by ``song.speak_text``.
CLI:  python3 voice.py say "texto"        # prints the .ogg path
      python3 voice.py hear arquivo.ogg   # prints the transcript
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_SPEAK_CHARS = 900
KEEP_DAYS = 2


SHIM_URL = os.environ.get("VOICE_STT_URL", "http://172.26.0.1:8787/v1/task")   # the agy shim on the host (ops/agy_shim.py)


def transcribe(data: bytes, mime: str = "audio/ogg", timeout: float = 120.0) -> str:
    """Words spoken in `data`, or "" when the shim/agy could not hear them. Uses the shim's `/v1/task kind=transcribe`
    (Gemini through the Antigravity CLI): no extra API key or credits, same path as the chat model."""
    token = os.environ.get("AGY_SHIM_TOKEN", "")
    if not token or not data or len(data) > MAX_AUDIO_BYTES:
        return ""
    body = {"kind": "transcribe", "mime": (mime or "audio/ogg").split(";")[0].strip(),
            "data_b64": base64.b64encode(data).decode("ascii"),
            "prompt": "Mensagem de voz de WhatsApp em português do Brasil sobre fantasy football/NFL."}
    req = urllib.request.Request(SHIM_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(json.loads(resp.read().decode("utf-8")).get("text") or "").strip()
    except Exception:
        return ""


def out_dir() -> str:
    import league_ctx
    ctx = league_ctx.current()
    home = ctx.home or os.environ.get("HERMES_HOME")
    d = os.path.join(home, "cache", "audio", "voz") if home else os.path.join(tempfile.gettempdir(), "liga_voz")
    os.makedirs(d, exist_ok=True)
    cutoff = time.time() - KEEP_DAYS * 86400
    for n in os.listdir(d):
        try:
            if os.path.getmtime(os.path.join(d, n)) < cutoff:
                os.remove(os.path.join(d, n))
        except OSError:
            pass
    return d


def speak(text: str) -> str:
    """Render `text` as a voice note (.ogg/Opus); raises RuntimeError/ValueError/OSError on failure."""
    import song
    spoken = song.speak_text(text)[:MAX_SPEAK_CHARS * 2]
    if not spoken:
        raise ValueError("nada para falar")
    if len(spoken) > MAX_SPEAK_CHARS:
        spoken = spoken[:MAX_SPEAK_CHARS].rsplit(" ", 1)[0]
    d = out_dir()
    with tempfile.TemporaryDirectory(prefix="voz_") as w:
        mp3 = song._synth_lines([spoken], w)[0]
        out = os.path.join(d, f"voz-{int(time.time() * 1000)}.ogg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-vn", "-c:a", "libopus", "-b:a", "32k",
                        "-ac", "1", "-ar", "48000", out], check=True, timeout=60)
    return out


def main(argv) -> int:
    if len(argv) < 3 or argv[1] not in ("say", "hear"):
        print(__doc__)
        return 2
    if argv[1] == "say":
        print(speak(argv[2]))
    else:
        with open(argv[2], "rb") as f:
            print(transcribe(f.read(), "audio/ogg"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
