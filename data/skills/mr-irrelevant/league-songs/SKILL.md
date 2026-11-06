---
name: league-songs
description: "Research the league's data, write a rap/song about a manager, rivalry, player or the league, and sing it (Edge TTS pt-BR Antonio over a generated beat) as a WhatsApp voice note."
version: 1.0.0
author: Mr. Irrelevant
license: MIT
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [music, rap, tts, whatsapp, fantasy, humor, voice]
    related_skills: [league-charts]
---

# League songs: research → lyrics → voice note

A native ability of the bot: when someone **explicitly asks** for a rap, song, parody or "canta", it uses everything it
knows — history DB (games, H2H, records, trades, waivers, drafts), the manager profile from the group chat, live
injuries/news — writes the lyrics itself and sings them. Never do this unprompted; if only the lyrics are requested,
answer in text.

## The three steps (the `liga` tool, works in the WhatsApp chat)
1. **Research**: `liga consulta=musica alvo=<manager|jogador> [alvo2=<rival>] [tema=derrotas|vitórias|trocas|…]`
   returns a fact sheet: career and per-season record, who beat him most / whom he beat, extremes (biggest loss/win,
   closest, lowest/highest score), playoff losses, last games, trades, waivers (best find/biggest burn), first-round picks,
   a short profile excerpt, starters at risk now — plus the writing rules. `alvo2` adds the head-to-head. A player name
   brings the player dossier and latest news; no `alvo` gives the league overview.
2. **Write** the lyrics from *only those facts*: rap in PT-BR, 20–28 verses (one per line, 9–13 syllables, AABB rhymes),
   a 4-verse chorus at the start and again at the end, sharp but friendly tone about the fantasy side. Numbers may be
   digits (they are spelled out before singing: `43` → "quarenta e três", `S14` → "semana catorze", `71,4` → "setenta e um
   vírgula quatro", years included); section labels like `[Refrão]` are ignored.
3. **Sing**: `liga consulta=cantar letra="<verses, one per line>" [estilo=rap|lento]` renders every verse with Edge TTS (voice
   from `tts.edge.voice`, default `pt-BR-AntonioNeural`), fits it to two bars of a synthesized boom-bap beat (110 BPM,
   `lento` = 92) and returns `MEDIA:/opt/data/cache/audio/liga_musicas/<name>.ogg`. **Reply with that line only**; the
   gateway sends it as a WhatsApp voice note. Limits: 4–40 verses, ≤ 140 characters each, ≤ 2400 total. A 28-verse song
   takes ~15–30 s to render and lasts ~2 min.

## Shell path (admin sessions)
```bash
cd /opt/league
python3 song.py brief eduohira --tema derrotas              # prints the fact sheet
python3 song.py brief eduohira --com luizfelipesouza        # with a rivalry
python3 song.py sing letra.txt [--estilo lento]             # renders the .ogg and prints the path
python3 song.py sing letra.txt --send [--dest <jid>]        # …and posts it to WHATSAPP_GROUP_JID as a voice note
```
`--send` is the only thing that leaves the machine: confirm with the admin first when the target is the main group.

## Rules of the craft
- Facts are the joke: a real number lands harder than an invented one. Never add a score, date or event that is not in
  the sheet. Roast the fantasy decisions (lineups, waivers, trades, playoff collapses), not the person's life; the profile
  excerpt is for flavour, used lightly.
- One idea per verse; end verses on the rhyme word; keep each ≤ ~13 syllables or the voice must rush (tempo is clamped
  to 0.85–1.35, longer verses overflow their two bars).
- Mention the opponent who matters (e.g. the one who beat him most) by the name the group uses (Lula, Brunão, Ari, Panda…).
- It is a spoken rap over a drum-and-bass loop, not a produced track: say so if asked.

## Troubleshooting
- "Não consegui cantar: …" → the message names the fix (too few/many verses, verse too long, unknown style).
- "Voz indisponível" → `edge-tts` missing: Hermes installs it on demand (`tools.lazy_deps ensure tts.edge`); the container
  needs outbound access to Microsoft's speech service.
- Audio missing in the chat → the reply must contain the `MEDIA:` line verbatim; check `data/logs/gateway.log` for
  `sendWhatsAppAudio` errors (Evolution API v2 required).
- Files are pruned after 7 days (`$HERMES_HOME/cache/audio/liga_musicas/`).
