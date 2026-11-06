"""Round & temporal context provider for Mr Irrelevant.

Injected on every user turn via the `pre_llm_call` hook so the LLM always knows:
- Current date and time in Brasília (America/Sao_Paulo).
- Current NFL / Sleeper season & week in progress.
- Today's NFL games (especially Monday Night Football closing the current fantasy week).
- Explicit rule to prevent calling next week before it actually starts.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

import league_ctx

_CACHE: Dict[str, tuple[float, str]] = {}
CACHE_TTL = 180  # 3 minutes


def get_current_round_context() -> str:
    """Return a concise, authoritative temporal & NFL round context string for the LLM."""
    now = time.monotonic()
    lctx = league_ctx.current()
    tz = ZoneInfo(lctx.timezone)
    hit = _CACHE.get(lctx.slug)     # the text carries the league's time zone: one entry per league
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    try:
        from sleeper_client import SleeperClient
        import round_injuries

        s = SleeperClient(lctx.league_id)
        st = s.get_nfl_state()
        week = int(st.get("week") or 1)
        season = str(st.get("season") or "2026")
        dt_now = datetime.now(tz)

        games = round_injuries._scoreboard(week, season)
        today_games: List[str] = []
        in_progress_games: List[str] = []
        pending_games: List[str] = []

        for g in games:
            dt = round_injuries._parse_dt(g.get("date"))
            state = g.get("state", "")
            name = g.get("name", "")
            if state == "in":
                in_progress_games.append(name)
            elif state == "pre":
                pending_games.append(name)

            if dt:
                dt_local = dt.astimezone(tz)
                if dt_local.date() == dt_now.date():
                    time_str = dt_local.strftime("%H:%M")
                    status_str = "ROLANDO AGORA" if state == "in" else ("ENCERRADO" if state == "post" else f"às {time_str}")
                    today_games.append(f"{name} ({status_str})")

        weekday_pt = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira", "Sexta-feira", "Sábado", "Domingo"][dt_now.weekday()]
        date_str = dt_now.strftime("%d/%m/%Y %H:%M")

        lines = [
            f"[CONTEXTO TEMPORAL & DA RODADA — OBRIGATÓRIO RESPEITAR]",
            f"• Momento atual: {date_str} ({weekday_pt}), horário de Brasília.",
            f"• Temporada NFL: {season} | RODADA ATUAL EM ANDAMENTO: SEMANA {week}.",
        ]

        if today_games:
            lines.append(f"• Jogos da NFL HOJE ({weekday_pt}): {', '.join(today_games)}.")
            if pending_games or in_progress_games:
                lines.append(f"  ↳ Esses jogos AINDA FAZEM PARTE DA SEMANA {week}! Atletas desses times continuam pontuando para os confrontos em aberto do fantasy.")
        elif pending_games or in_progress_games:
            lines.append(f"• Jogos ainda pendentes da Semana {week}: {', '.join(pending_games + in_progress_games)}.")
        else:
            lines.append(f"• Todos os jogos da Semana {week} já foram encerrados. A Semana {week + 1} só começa no Kickoff da próxima rodada (quinta-feira).")

        lines.append(f"• REGRA DE OURO: Para qualquer pergunta sobre jogos de hoje, rodada atual ou confrontos, a rodada é a SEMANA {week}. NUNCA consulte a Semana {week + 1} antes do seu kickoff oficial!")

        result = "\n".join(lines)
        _CACHE[lctx.slug] = (now, result)
        return result
    except Exception as exc:
        logger.warning("[round_context] failed to build context: %s", exc)
        dt_now = datetime.now(tz)
        fallback = f"[CONTEXTO TEMPORAL] Data atual: {dt_now.strftime('%d/%m/%Y %H:%M')} (Brasília). Sempre omita o parâmetro 'semana' na ferramenta liga para consultar a rodada atual."
        return fallback
