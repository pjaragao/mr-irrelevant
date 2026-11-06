---
name: league-charts
description: "Make any chart about the High Stakes Fantasy league (heatmap, lines, columns grouped/stacked, ranked bars, donut, scatter, stat tiles) as a polished PNG + interactive HTML from league data or from numbers you supply, and send it to the WhatsApp group."
version: 2.0.0
author: Mr. Irrelevant
license: MIT
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [charts, dataviz, visualization, whatsapp, fantasy, png, html, svg, heatmap, scatter, donut]
    related_skills: [architecture-diagram]
---

# League charts → PNG + HTML → WhatsApp

A general chart maker for the league. It answers "mostra isso num gráfico" for **any** league topic — trades,
waivers/FAAB, games (wins, points, luck), drafts, players — or for **numbers you give it** (a power ranking you
computed, a week's scoreboard, an election count…). Output is a phone-friendly PNG (1080 px wide, dark) and an
interactive HTML page of the same chart (hover tooltips, light/dark, table view). Both come from one layout, so
they always match. It runs offline inside the bot container (Pillow, no browser).

Code in `/opt/league` (repo `league/`): `charts.py` (engine + spec validation), `chart_service.py` (data → chart →
delivery), `kb_stats.py` (the SQL behind every number), `knowledge_base/league.db` (history index).

## Pick the form by the data's job
| The question is about… | Form | How to ask |
|---|---|---|
| How a count/metric **evolves through the season, year over year** | heatmap (season × week) + cumulative lines | `agrupar_por=semana` (no `ano`) |
| **Ranking** of managers/positions/teams | ranked horizontal bars | `agrupar_por=manager` (or posicao, tier, time_nfl…) |
| **Time series** with few points (≤ 8 seasons/months) | columns (in-progress season hatched) | `agrupar_por=temporada` |
| Time series with many points (a manager's weeks) | one line | `agrupar_por=semana ano=2026 alvo=<manager>` |
| **Two dimensions** (manager × season, manager × week) | heatmap with numbers | `agrupar_por=manager serie=temporada` |
| Same two dimensions, ≤ 6 series, compare sizes | grouped / stacked columns | add `forma=colunas` / `forma=empilhado` |
| Same two dimensions with time on one axis, ≤ 8 series | multi-line | add `forma=linhas` |
| **Share of a whole** (picks by position, FAAB by type) | donut | `forma=pizza` |
| **Two metrics at once** (who scores a lot *and* concedes little) | labelled scatter + league averages | `metrica=media metrica2=media_contra` |
| **Your own numbers** | any block | `dados=<JSON>` (below) |

Rules the engine enforces (don't fight them): one hue sequential ramp for magnitude; fixed categorical colour order,
max 8 series (6 for columns); no dual axes; text always in ink, never the series colour; legend for ≥ 2 series;
fonts shrink to fit; every number also lives in the HTML table view. Too many series → it refuses and tells you to
filter (`alvo`, `limite`) or use `forma=calor`.

## In the WhatsApp chat: the `liga` tool
```
liga consulta=grafico                                                        # trocas por semana, ano a ano
liga consulta=grafico entidade=trocas agrupar_por=manager                    # ranking de quem mais trocou
liga consulta=grafico entidade=jogos agrupar_por=manager metrica=aproveitamento fase=regular
liga consulta=grafico entidade=jogos agrupar_por=manager serie=semana ano=2026 metrica=pontos     # mapa de calor
liga consulta=grafico entidade=jogos agrupar_por=manager metrica=media metrica2=media_contra     # dispersão: sorte × azar
liga consulta=grafico entidade=waivers agrupar_por=temporada serie=tipo forma=empilhado          # waivers vs free agents
liga consulta=grafico entidade=draft agrupar_por=posicao forma=pizza ano=2025
liga consulta=grafico entidade=jogos agrupar_por=semana ano=2026 alvo=Ari metrica=pontos forma=linhas
liga consulta=grafico entidade=pontuacao posicao=QB agrupar_por=jogador metrica=pontos limite=12 ano=2026   # top-12 QB (rótulo: jogador — dono)
```
`liga consulta=ajuda alvo=estatisticas` lists every entity/grouping/metric. The tool replies with a line
`MEDIA:/opt/data/image_cache/liga_graficos/<name>.png` plus facts for a caption. **Put that line, alone, in your reply**
(then 1–2 sentences in the bot's voice); the gateway uploads it to the chat as a photo. Never retype the numbers.

## Your own data: `dados`
Pass a JSON object; it is validated (types, sizes, shapes) before drawing, so mistakes come back as a clear message.
```json
{"title": "Placar da semana 4", "subtitle": "pontos por time", "blocks": [
  {"type": "bars", "title": "Pontos", "orientation": "h", "fmt": "float",
   "items": [{"label": "Ayahuasquers", "value": 185.8}, {"label": "Riedelons", "value": 171.1}]}]}
```
Block types (all fields in `references/spec.md`, runnable examples in `references/*.json`):
`tiles` (3–4 headline numbers) · `heatmap` (rows × cols, `null` = not played yet, hatched) · `lines` (multi-series,
`null` stops a line) · `columns` (grouped, or `stacked: true`) · `bars` (ranked, `orientation` h/v) · `donut` ·
`scatter` (labelled points, league-average guides, optional `quadrants` captions). A chart can stack several blocks.

## Shell path (admin or cron sessions with a terminal)
```bash
cd /opt/league
python3 chart_service.py estatisticas entidade=trocas agrupar_por=semana             # writes PNG + HTML, prints paths + caption
python3 chart_service.py estatisticas entidade=jogos agrupar_por=manager metrica=media metrica2=media_contra
python3 chart_service.py spec references/scatter-example.json --slug minha-dispersao
python3 chart_service.py estatisticas entidade=trocas agrupar_por=semana --send      # …and post the PNG to WHATSAPP_GROUP_JID
python3 chart_service.py estatisticas ... --send --dest <jid>                        # another chat (e.g. the test group)
```
`--send` is the only thing that leaves the machine: confirm with the admin before using it on the main group.
From a cron script, return `MEDIA:<png>` in the message, or call `EvolutionClient.send_image(jid, path, caption)`.

## Quality check before sending
Open the PNG and verify: nothing clipped or overlapping; numbers equal `liga consulta=estatisticas` with the same
parameters; the in-progress season is hatched/“até a S4”; colour is never the only carrier of meaning. Tests:
`python3 -m unittest tests.test_charts` (render tests need Pillow and skip without it).

## Troubleshooting
- "Não consegui montar o gráfico: …" → the message says what to change (filter, other `forma`, missing `metrica2`).
- "Pillow não instalado" → the bot falls back to text; `uv pip install pillow` in the Hermes venv fixes it.
- Image not in the chat → the reply must contain the `MEDIA:` line verbatim, alone on its line, absolute path under
  `$HERMES_HOME`; look for `sendMedia` errors in `data/logs/gateway.log` (Evolution API v2 required).
- Files live in `$HERMES_HOME/image_cache/liga_graficos/` and are pruned after 7 days.
