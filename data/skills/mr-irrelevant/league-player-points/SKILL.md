---
name: league-player-points
description: "Answer player-points questions with exact numbers: top-N by position (QB/RB/WR/TE/K/DEF), who owns them, season totals, averages, week-by-week lines, per-manager production — in the league's own scoring, with bye weeks never counted as 0."
version: 1.0.0
author: Mr. Irrelevant
license: MIT
dependencies: []
platforms: [linux]
metadata:
  hermes:
    tags: [stats, players, ranking, points, fantasy, bye]
    related_skills: [league-charts]
---

# League player points: rankings and lines from the league's own scoring

Questions like *"top-12 QB da temporada"*, *"quem tem o melhor RB"*, *"quantos pontos o Bryce fez"*, *"média do Josh Allen"*,
*"qual manager tem os melhores WRs"*, *"lista só os managers dos 12 melhores QBs"*. **Never** answer "the system has no
player ranking": it does, one call away, and it is the same `estatisticas` tool the other counts use.

## The call
```
liga consulta=estatisticas entidade=pontuacao [posicao=QB|RB|WR|TE|K|DEF] agrupar_por=<…> metrica=<…> [ano] [semana] [alvo] [limite]
```
| Want | agrupar_por | metrica | extra |
|---|---|---|---|
| Top-N players of a position (with owner) | `jogador` | `pontos` | `posicao=QB limite=12` |
| Same, by average per game played | `jogador` | `media` | `posicao=QB` |
| Best single game / worst real game | `jogador` | `maximo` / `minimo` | |
| Only points that counted in the lineup | `jogador` | `pontos_titular` | |
| Games played (bye and DNP excluded) | `jogador` | `jogos` | |
| One player, week by week | `semana` | `pontos` | `alvo=<player>` |
| Production of a manager's whole roster | `posicao` or `semana` | `pontos` | `alvo=<manager>` |
| Which manager scores most at a position | `manager` | `pontos` | `posicao=WR` |
| Matrix (manager × week) | `manager` | `pontos` | `serie=semana` |
| As a picture | same params with `consulta=grafico` | | |

Each `jogador` row reads `Player · Team`: the team (= manager) is who holds him now (last roster of the season).
Points are the league's scoring (Sleeper `players_points`, bench included unless `pontos_titular`).

## Rules
- **"Cite only the manager / the team"**: run the call above and print only the part after "·", keeping the rank order
  and repeating a manager as many times as he appears (3 QBs in the top-12 = 3 lines). Do not add the player name.
- **Bye is not zero.** The tool marks it (`_bye (sem jogo, NÃO é 0): S5/2026_`); averages, minimums and `jogos` skip it.
  When you quote a total, say "N jogos + bye", never "0 pts na S5". `stats_jogador` also prints `BYE WEEK` and, when the
  current week has not kicked off, shows the last played week with a note instead of a fake 0.
- **Ties / "season"**: omit `ano` for the current season. Sum is over finished weeks (the live week counts once its
  games are final); say "até a Semana N" using the highest week in the table.
- Different totals from the Sleeper app's PPR box score are expected: ours use this league's scoring settings.
- Numbers come only from the tool. If it errors or returns no rows, say the source failed; never estimate a ranking.
- Position names in the filter are Sleeper's: QB, RB, WR, TE, K, DEF (the flex is not a position).

## Adding more (scalable by design)
The data sits in the `player_weeks` table (kb_db) fed by `league/player_scores.py` (one JSON per league, refreshed
incrementally at query time and by the weekly sync). New questions are new `agrupar_por` / `metrica` entries on the
`pontuacao` entity in `league/kb_stats.py` — no new tool, charts and `estatisticas` get them for free.
