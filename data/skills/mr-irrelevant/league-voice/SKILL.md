---
name: league-voice
description: "Voice in and out on WhatsApp: DM voice notes are transcribed and answered with a voice note; the piadista can send jokes as audio."
version: 1.0.0
author: Mr. Irrelevant
license: MIT
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [voice, stt, tts, whatsapp]
    related_skills: [league-songs, league-piadista]
---

# Voice in / voice out

## In
- Only **DMs from allowed senders** (`EVOLUTION_DM_ALLOWED`); voice notes in groups are still ignored (no cost, no abuse).
- The adapter downloads the clip from Evolution (`/chat/getBase64FromMediaMessage`), `league/voice.py` sends it to the agy shim
  (`POST /v1/task kind=transcribe`, Gemini through Antigravity: no API credits needed) and the transcript is handled exactly like
  typed text — admin commands and the piadista grant included. Limit 3 min / 8 MB. Latency ≈ 30–60 s.
- Failures answer in text ("Não consegui entender o áudio…"); nothing is silently dropped.

## Out
- A reply to a voice note (within 4 min) is rendered by Edge TTS (`tts.edge.voice`, default Antonio) and sent as a voice note;
  numbers/weeks/percent are spelled out (`song.speak_text`), text is cut at ~900 chars. If TTS fails, the answer goes as text.
- The model is told (`[MENSAGEM DE VOZ transcrita…]`) to answer short and spoken, with no markdown.
- Piadista: `piadas_enviar … formato=audio` sends each joke as a voice note.

## Operate
```bash
docker exec mr-irrelevant sh -c 'cd /opt/league && python3 voice.py say "texto"'      # prints the .ogg path
docker exec mr-irrelevant sh -c 'cd /opt/league && python3 voice.py hear arq.ogg'     # transcript
```
- 403 from `speech.platform.bing.com` = Edge TTS client too old: update `edge_tts` in `data/lazy-packages/` (7.3.1 works).
- Empty transcript: check `systemctl --user status agy-shim` and `AGY_SHIM_TOKEN`.
