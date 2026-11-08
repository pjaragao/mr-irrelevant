---
name: league-piadista
description: "Roast jokes about the league's managers, their NFL teams and what their teams do — ordered ONLY by the verified admin (Paulo) in the private chat, with theme, quantity and cadence, delivered to the group or his DM."
version: 1.0.0
author: Mr. Irrelevant
license: MIT
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [humor, roast, whatsapp, fantasy, nfl, admin-only]
    related_skills: [league-songs]
---

# Piadista: zoeira sob encomenda do Paulo

Never unprompted, never for anyone else. Only the verified admin, writing in a **private chat**, can order jokes.

## How it is locked
1. The WhatsApp adapter ([adapter.py](../../../plugins/evolution-whatsapp/adapter.py)) sees a trigger word (`piada`, `solte`, `zoa`,
   `roast`…) from a `✅ADMIN` in a DM, opens a 15-minute **grant** (`state/piadista_grant.json`) and prefixes the message with
   `[MODO PIADISTA AUTORIZADO…]`. Groups and other senders never open it.
2. `liga consulta=piadas` and `liga consulta=piadas_enviar` **refuse without the grant** (and are never cached), so a
   prompt-injected request in the group cannot make the bot write or send jokes.
3. Destinations are fixed: `grupo` (league group) or `dm` (`ADMIN_ALERT_JID`); never a free-form number.
   Default when the order does not say: `dm` (preview). Set `PIADISTA_DEFAULT_DEST=grupo` in `data/.env` to default to the group.

## Flow
- Paulo (text or a voice note: the adapter transcribes DM voice notes from allowed senders via `league/voice.py`, so a spoken order opens the grant like a typed one): *"solte 3 piadas sobre o Chico ser ciclista e torcedor do Colts. Sugestão:
  'Chico, guarde sua bike e baixe esse bumbum.' Se falar de bike use 'pedal' como pejorativo"*.
- `liga consulta=piadas alvo=chico tema=<order as written> limite=3` → fact sheet (record, rivals, extremes, waivers, draft,
  group profile incl. NFL favourite team) + writing rules, with Paulo's order flagged as law.
- The model writes the jokes (a quoted suggestion goes in first, intact) and calls
  `liga consulta=piadas_enviar piadas="<one per line>" destino=grupo|dm intervalo=<seconds>`.
- Audio out: `formato=audio` turns each joke into a voice note (Edge TTS, same voice as the songs) instead of text.
- Cadence: queue in `state/piadista_queue.json`; the first joke leaves at once, the others every `intervalo` s
  (default 45; 5 s – 24 h). Gaps ≤ 5 min are served by a detached drain process, longer ones by the `piadista-tick` cron
  (every minute, silent when the queue is empty). Failed sends retry up to 3 times.

## Rules of the craft
- "Sem limites" = no softening: acid, low, humiliating is fine. The floor is fixed in the brief: no attacks on race, religion,
  sexual orientation, disability/illness, family/bereavement, no explicit sexual content, no private data, no encouraging self-harm.
- Facts are the joke. Numbers/scores/events only from the sheet or from a tool call; known NFL history (Luck's retirement,
  QB carousel, title drought) is fair game if true. Current NFL facts: check `placar_nfl` / `noticias_nfl` first.
- Vary the angle of each joke (fantasy record, NFL team, the hobby in the order); 1–3 lines, punchline last.

## Shell path (operator)
```bash
cd /opt/league
python3 piadista.py brief chico --tema "ciclista e torcedor do Colts"   # fact sheet
python3 piadista.py send jokes.txt --dest dm --intervalo 45             # enqueue + send (no grant needed)
python3 piadista.py status                                              # what is still queued
```
