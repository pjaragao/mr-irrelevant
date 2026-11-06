<div align="center">

# 🏈 Mr. Irrelevant

### A sarcastic, tool-grounded WhatsApp analyst for fantasy football leagues

*An LLM agent that answers league questions in a group chat, posts a weekly editorial calendar on its own, and is hard-wired to never invent a number.*

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-container-2496ED?logo=docker&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-knowledge%20base-003B57?logo=sqlite&logoColor=white)
![WhatsApp](https://img.shields.io/badge/WhatsApp-Evolution%20API-25D366?logo=whatsapp&logoColor=white)
![LLM](https://img.shields.io/badge/LLM-Gemini%20%2B%20open%20fallbacks-8E75B2)
![Tests](https://img.shields.io/badge/tests-24%20offline%20suites-brightgreen)

</div>

---

## What is it?

**Mr. Irrelevant** (named after the last pick of the NFL draft, and answering to *Brock Purdy*) is the official analyst of a 12-team dynasty
fantasy football league on [Sleeper](https://sleeper.com). It lives inside the league's **WhatsApp group**:

- you tag it and ask anything ("do I still have a shot this week?", "is this trade fair?", "who got hurt on Sunday?") and it answers in a few lines, with venom;
- on its own it publishes a **weekly editorial calendar** (standings, power rankings, waiver radar, lineup checks, injury reports, Monday-night wrap-up);
- it **announces and grades every trade** the minute it happens, and can even send **charts** and **voice notes / rap songs** about a rival on request.

The persona is a joke. The engineering behind it is not: the agent is **forbidden from stating any fact that did not come from a tool call**.

> The bot speaks Brazilian Portuguese (the league's language). Every sample below is translated in the captions.

## 💬 What you can ask it (in the group chat)

Tag it by name (*Mr. Irrelevant* / *Brock*) or reply to one of its messages:

| You write (PT-BR) | What happens |
|---|---|
| "tenho chance de virar essa semana?" | `analise_confronto`: live score, starters still to play, projections, win probability. If everyone already played it says the matchup is over instead of guessing. |
| "essa troca é boa? dou X por Y" | `avaliar_troca`: dynasty value of both sides, roster fit and age, then a grade and a roast. |
| "quem mais trocou na temporada?" | `estatisticas`: an exact SQL count over the knowledge base, never an LLM tally. |
| "manda um gráfico de trocas por semana" | `grafico`: renders a PNG chart and sends it to the group as an image. |
| "canta um rap sobre o fulano" | `musica` + `cantar`: fact sheet, lyrics by the model, TTS over a generated beat, sent as a voice note. |
| "anota aí: ..." (admin only) | the only way facts enter the knowledge base; anyone else gets refused with a joke. |

> The windows below are **simulated WhatsApp conversations** (example messages in the bot's real response format, with fictional league data), not captures of a real group.

<table>
<tr>
<td width="50%" valign="top">

**Live matchup analysis**
<img src="docs/img/matchup.png" alt="Simulated WhatsApp window: matchup analysis">

*"Do I have a chance to turn it around?" The bot reads the live score, starters still to play and projections, and gives a win probability. If everyone already played it says the matchup is over.*

</td>
<td width="50%" valign="top">

**Trade evaluation**
<img src="docs/img/trade.png" alt="Simulated WhatsApp window: trade evaluation">

*"Is this trade good?" Dynasty value, roster fit and age, then a grade and a roast.*

</td>
</tr>
<tr>
<td width="50%" valign="top">

**Guardrails and league slang**
<img src="docs/img/guardrails.png" alt="Simulated WhatsApp window: guardrails">

*A member tries to rewrite history and then to leak the prompt; both are refused with a joke. Injury status stays official.*

</td>
<td width="50%" valign="top">

**Scheduled posts (cron, no LLM)**
<img src="docs/img/scheduled.png" alt="Simulated WhatsApp window: scheduled posts">

*Waiver list and standings posted by the engine, marked as sent only after delivery is confirmed.*

</td>
</tr>
</table>

Style rules from the persona (`data/SOUL.md`): short WhatsApp-formatted answers (3 to 12 lines), venom without ever changing a fact,
official injury status never upgraded, and prompt-injection attempts ("ignore your instructions") are brushed off.

## ✨ Features

| | |
|---|---|
| 🧠 **Tool-grounded answers** | One tool (`liga`) with ~40 queries over a local knowledge base + live Sleeper/ESPN data. Counting and ranking go through an exact SQL query (`estatisticas`), never through "counting rows" in the LLM. |
| 📅 **Editorial calendar** | Tuesday recap/power ranking/playoff odds, Wednesday waiver radar, Thursday matchup cards, Saturday lineup check, Sunday waivers/inactives/medical report, Monday MNF. Pure cron scripts. |
| 🔁 **Trade desk** | Detects new trades every 15 minutes, asks the LLM for a verdict, announces it (deterministic text if every model fails). |
| 📊 **Charts on demand** | "Chart of trades per week" → heatmap / lines / bars rendered to PNG and sent to the group as an image. |
| 🎤 **Voice and rap** | Only on explicit request: fact sheet → lyrics written by the model → Edge TTS over a generated beat → WhatsApp voice note. |
| 🏟️ **Multi-league** | One container serves many leagues, each an isolated Hermes profile (own persona, secrets, sessions, cron and knowledge base), bootstrapped from **nothing but the Sleeper league id**. |
| 🛡️ **Safety** | Admin verified by phone number (a display name proves nothing), prompt-injection refusals, rate limiting, no docker.sock, API bound to localhost, read-only analyst (it cannot change data or rules). |
| 🩺 **Resilience** | Model fallback chain (Gemini Flash → free open models), provider watchdog that DMs the admin on failure/recovery, bounded tool rounds so a chat never ends in a silent failure. |

## 🏗️ Architecture

```mermaid
flowchart LR
    WA[WhatsApp group] <--> EV[Evolution API]
    EV -- webhook --> GW
    subgraph C[Container: mr-irrelevant]
        GW[Hermes gateway<br/>evolution-whatsapp plugin<br/>mention / DM / admin gating]
        GW --> AG[LLM agent<br/>SOUL.md persona]
        AG -- one tool: liga --> EN[League engine<br/>Python stdlib]
        EN --> KB[(SQLite knowledge base)]
        CR[Cron scripts] --> EN
    end
    EN --> S[Sleeper API]
    EN --> E[ESPN / RSS / nflverse]
    AG --> SH[Model shim<br/>Gemini + fallbacks]
    CR -- sendText --> EV
```

**Design decisions worth reading**

1. **Facts come from tools, tone comes from the model.** The persona (`data/SOUL.md`) has a "golden rule": any score, injury, stat, trade or history claim must be fetched first. Jokes may never change a fact.
2. **Deterministic where it matters.** Scheduled posts and trade announcements are produced by the engine; the LLM is optional garnish with a deterministic fallback.
3. **Isolation by profile.** Each league is a separate profile with its own secrets and data. Tests assert that one league can never read another's.
4. **Cheap and robust model layer.** An OpenAI-compatible shim fronts the model chain and handles audio/OCR tasks; health checks alert the admin before the group notices.
5. **Everything is testable offline.** 24 unit-test suites plus a throwaway-container end-to-end run with mocked WhatsApp, LLM and Sleeper.

## 🚀 Quick start

> Requires Docker, a running [Evolution API](https://github.com/EvolutionAPI/evolution-api) instance connected to a WhatsApp number, and an LLM endpoint (OpenAI-compatible).

```bash
git clone https://github.com/pjaragao/mr-irrelevant.git && cd mr-irrelevant
cp data/.env.example data/.env      # fill in the Evolution URL/key, webhook token, model keys
$EDITOR data/league.yaml            # your Sleeper league id, group JID, admin phone
docker compose up -d
python3 ops/liga.py groups          # list the WhatsApp groups the number is in
```

Add another league to the same instance:

```bash
ops/liga.py new my-league --league-id <SLEEPER_ID> --group <JID>@g.us --admin 5548999990000 --admin-name "Name"
ops/liga.py cron my-league --apply && docker restart mr-irrelevant
```

Run the tests:

```bash
python3 -m unittest discover -s tests      # offline unit tests
python3 tests/e2e/e2e.py run               # throwaway-container end-to-end check
```

## 📚 Documentation (PT-BR)

- [docs/ARQUITETURA.md](docs/ARQUITETURA.md): full architecture and behavior
- [docs/DIAGNOSTICO.md](docs/DIAGNOSTICO.md): audit of problems found and how they were fixed
- [docs/MULTILIGA.md](docs/MULTILIGA.md): several leagues on one instance

## 🗺️ Repository map

| Path | What |
|---|---|
| `docker-compose.yml` | container `mr-irrelevant` — pinned image, no docker.sock, API server/dashboard on 127.0.0.1 only, 1 GB / 1 CPU |
| `data/` (`/opt/data`) | Hermes home: `config.yaml`, `SOUL.md`, `.env` (600), sessions, logs, cron (only config, persona, plugins and scripts are versioned) |
| `data/plugins/evolution-whatsapp/` | platform adapter: webhook in, sendText out, mention/DM gating, admin tag by number, sender's team tag, rate limit |
| `data/plugins/sleeper-league/` | the `liga` tool (40 queries, incl. `estatisticas` for exact counts, `grafico` for images, `musica`/`cantar` for songs and `ajuda`) + per-turn round/temporal context |
| `data/skills/mr-irrelevant/` | Hermes skill `league-charts`: charts (heatmap/lines/bars) as interactive HTML + PNG, delivered to the WhatsApp group (`liga grafico` in chat, `league/chart_service.py --send` from a shell) |
| `league/song.py` + skill `league-songs` | native songs: `liga musica` (fact sheet) → the bot writes the lyrics → `liga cantar` (Edge TTS pt-BR Antonio over a generated beat) → voice note; only on explicit request |
| `data/scripts/` | cron scripts — the engine sends the messages itself and only marks them done after Evolution confirms |
| `league/` (`/opt/league`) | engine code + knowledge base: tracked JSON sources indexed into a rebuildable SQLite `knowledge_base/league.db` (`kb_db.py`) |
| `league/league_profile.py` · `league_ctx.py` · `kb_bootstrap.py` | multi-league core: roster structure/rules read from Sleeper (`LeagueProfile`), which league serves a call and where its data/secrets live (`LeagueContext`), knowledge base built from nothing but the league id |
| `data/league.yaml` · `data/profiles/<slug>/` | league #1's config · one Hermes profile per extra league (own persona, secrets, sessions, cron, knowledge base) |
| `ops/liga.py` | multi-league CLI: `groups`, `new`, `cron`, `refresh`, `bootstrap`, `sync`, `doctor`, `remove` |
| `ops/agy_shim.py` + `agy-shim.service` | host-side OpenAI-compatible shim that makes the Antigravity CLI (`agy`, always the newest Gemini Flash via `gemini-flash-latest`) the chat model; systemd user unit keeps it up; free OpenRouter fallbacks behind it |
| `ops/crons.sh` | source of truth for the cron schedule (`--apply` recreates the jobs) |
| `ops/cutover.sh` / `ops/rollback.sh` | switch the group to this bot / back to the legacy sleeper-bot |
| `tests/test_*.py` | offline unit tests (engine, KB, charts, multi-league core/isolation/bootstrap/CLI): `python3 -m unittest discover -s tests` |
| `tests/e2e/` | throwaway-container end-to-end check of the multi-league gateway (same pinned image, no network, mocked WhatsApp/LLM/Sleeper): `python3 tests/e2e/e2e.py run` |
| `tests/fire.py` | simulate a message in the test group (run on the `services_internal` network) |

## Another league, same instance
```bash
ops/liga.py groups                                    # JIDs of the WhatsApp groups the bot's number is in
ops/liga.py new my-league --league-id <SLEEPER_ID> --group <JID>@g.us --admin 5548999990000 --admin-name "Name"
ops/liga.py cron my-league --apply && docker restart mr-irrelevant
```
Starting slots, bench/IR, K/DEF/IDP, scoring, FAAB, playoffs and dynasty/redraft are read from Sleeper and flow into the
persona, the tools and the schedule; house rules live in `data/profiles/<slug>/league.yaml`. Each league is a Hermes
profile routed by group (`gateway.profile_routes`): its own persona, sessions, secrets, notes, schedule and knowledge base.

## Cron (inside the container: `hermes cron list`) — all no-agent, deliver `local`
| Job | Schedule | What |
|---|---|---|
| `editorial-slots` | `*/5 * * * *` | Tue recap/standings/power ranking/playoff odds/draft order/round injuries · Wed waiver radar · Thu full waiver list, standings, 6 matchup cards · Sat lineup check + early games · Sun waivers, partials, medical report, early-window + main inactives (times from ESPN), warm-up · Mon MNF |
| `trades-watch` | `*/15 * * * *` | new trade → LLM verdict (same model chain as the chat: agy → Nemotron 3 Ultra → Gemma 4 31B → Nemotron 3.5 Lightning; deterministic text if all fail) → announcement |
| `league-sync` | `*/30 * * * *` | live player DB refresh (team + injury status), pre-kickoff injury snapshot, waiver ingestion, weekly consolidation + stat corrections |
| `data-lab-*` | Tue/Wed/Fri | Raio-X, tendências, matchup lab (nflverse) |
| `session-reset` | `0 5 * * *` | fresh daily conversation per group (Hermes no longer resets by time) |
| `live-watch` | `*/5 * * * *` | during games: league starter injured in-game, matchup lead changes |
| `provider-watch` | `17 * * * *` | chat model health; DMs the admin on failure (e.g. 402 no credit) and recovery |

## Operate
```bash
C="docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v $PWD:$PWD -w $PWD docker:cli docker compose -p mr-irrelevant"
$C up -d                      # start / apply compose or .env changes (recreates)
docker restart mr-irrelevant  # reload config.yaml / SOUL.md / plugins / engine code used by the chat
docker exec -u hermes mr-irrelevant /opt/hermes/.venv/bin/hermes cron list
tail -f data/logs/agent.log data/logs/league-cron.log
docker exec -u hermes -w /opt/league mr-irrelevant /opt/hermes/.venv/bin/python scheduler_service.py inactives   # preview a slot (never sends)
```
Cron scripts run as separate processes, so engine changes reach them on the next tick; the chat tool needs a restart.
Extra leagues have their own jobs (`hermes -p <slug> cron list`; created by `ops/liga.py cron <slug> --apply`).
Knowledge edits (profiles, facts) are done by BotMaster in `league/`; the verified admin can add facts with `anota aí: …`.

---

<div align="center">

Built by **Paulo Aragão Jr** · [GitHub](https://github.com/pjaragao) · open to AI / automation engineering roles

</div>
