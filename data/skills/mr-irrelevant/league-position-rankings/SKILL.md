---
name: league-position-rankings
description: "Multi-step ranking research over player scoring: several top-N lists by position in ONE call (e.g. top-24 RB, top-36 WR, top-12 TE, then a top-36 FLEX of whoever was left out), by points or average, with the owning team, and follow-ups in a reply thread."
version: 1.0.0
author: Mr. Irrelevant
license: MIT
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [ranking, players, flex, lists, research, fantasy]
    related_skills: [league-player-points, league-charts]
---

# Position rankings: lists, flex and "excluding the ones already listed"

For requests that chain lists — *"top-24 RB, top-36 WR e top-12 TE; depois o top-36 FLEX excluindo os que entraram nas
listas anteriores"*, *"melhores 20 RB por média"*, *"top-10 de cada posição"*. Do it in **one** call; never page through
`estatisticas` and subtract by hand.

## The call
```
liga consulta=ranking_pontos listas="RB:24,WR:36,TE:12,FLEX:36" [ano=2026] [metrica=pontos|media]
```
- `listas` = comma-separated `POSIÇÃO:QUANTIDADE`, answered **in the order given**. Positions: QB, RB, WR, TE, K, DEF.
- Pools: `FLEX` = RB/WR/TE · `SUPERFLEX` = QB/RB/WR/TE · or any slash combo (`RB/WR:20`).
- **Each list skips every player already placed in an earlier list**, so a FLEX list placed after RB/WR/TE is exactly
  "the best of the rest". Put the flex list last. The order of the lists is the whole "excluding" logic: to rank the
  flex *without* excluding, ask a separate call with only `FLEX:36`.
- `metrica=pontos` (season total, default) or `media` (per game played; bye and DNP weeks are not games — never call a
  bye a zero).
- Output per line: `n. Player (NFL team) · fantasy team · value`. Points are the league's own scoring through the last
  finished week (the header says "Semanas 1–N").

## Working a conversation
1. Read the whole reply thread first (the message carries it): a follow-up like "agora só os managers" or "tira o
   Josh Allen" refers to the lists already answered — rerun with the adjusted `listas`, do not ask again.
2. Several lists can be long (100+ lines). WhatsApp-friendly answer: keep the tool's order, one list per message block,
   short intro, no commentary per line. If the user asked for "only the managers/teams", print just that column.
3. Chart on request: for a single position use `liga consulta=grafico entidade=pontuacao posicao=RB agrupar_por=jogador
   metrica=pontos limite=24` (see league-charts); multi-list flex has no chart, give the text.
4. Name the cutoff honestly: players with no game yet are not ranked ("só N jogadores com jogo"). Numbers come only from
   the tool; if it errors, say the source failed.

## Variants and how to express them
| Request | `listas` / call |
|---|---|
| top-12 QB | `QB:12` |
| top-24 RB, 36 WR, 12 TE | `RB:24,WR:36,TE:12` |
| …and top-36 flex without the listed ones | `RB:24,WR:36,TE:12,FLEX:36` |
| best 10 K and 10 DEF | `K:10,DEF:10` |
| superflex pool after QB list | `QB:12,SUPERFLEX:24` |
| one position by manager | `estatisticas entidade=pontuacao posicao=WR agrupar_por=manager` (league-player-points) |
| by average | add `metrica=media` |

## Extending
New pools: add to `FLEX_POOLS` in `league/player_scores.py`. New metrics: add a key function in `ranking_lists`
(the SQL view is the `player_weeks` table; see league-player-points).
