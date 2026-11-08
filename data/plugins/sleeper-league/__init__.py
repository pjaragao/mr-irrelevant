"""sleeper-league plugin — the Mr Irrelevant league brain as ONE native Hermes tool + temporal/notes hook.

* `liga` tool: one schema (enum of queries) → ~400 tokens/turn instead of dozens of tools; runs the
  league engine in-process via $LEAGUE_DIR/liga_core.py.
* `pre_llm_call` hook: injects authoritative round & temporal state (current week, today's games / MNF)
  plus verified admin notes (ADMIN_NOTES.md).
"""

from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

LEAGUE_DIR = os.environ.get("LEAGUE_DIR", "/opt/league")
if LEAGUE_DIR not in sys.path:
    sys.path.insert(0, LEAGUE_DIR)

_QUERIES = [
    "tabela", "confrontos", "analise_confronto", "lesoes_rodada", "boletim_pre_jogo", "noticias_jogador",
    "stats_jogador", "jogador", "h2h", "manager", "perfil_psicologico", "time", "roster_dna", "franquia",
    "recordes", "playoffs", "classificados", "trocas", "trocas_picks", "draft_historico", "draft_picks", "picks_manager",
    "ordem_draft", "notas_draft", "max_pf", "waivers", "radar_waivers", "transacoes", "placar_nfl",
    "noticias_nfl", "data_lab", "notas", "avaliar_troca", "power_ranking", "chances_playoff", "waivers_semana",
    "estatisticas", "ranking_pontos", "grafico", "musica", "cantar", "piadas", "piadas_enviar", "ajuda",
]

SCHEMA = {
    "name": "liga",
    "description": (
        "Dados REAIS da liga de fantasy deste grupo (Sleeper) e da NFL. Use SEMPRE antes de afirmar números, placares, "
        "lesões, trocas ou histórico. Consultas: tabela, confrontos, analise_confronto, lesoes_rodada (lesões EM CAMPO "
        "depois dos jogos), boletim_pre_jogo (status ANTES dos jogos), noticias_jogador, stats_jogador, jogador, h2h, "
        "manager, perfil_psicologico, time, roster_dna, franquia, recordes, playoffs, trocas (LISTA), trocas_picks, "
        "draft_historico, draft_picks, picks_manager, ordem_draft, notas_draft, max_pf, waivers (LISTA), "
        "radar_waivers, transacoes, placar_nfl, noticias_nfl, data_lab, notas, avaliar_troca, power_ranking, "
        "chances_playoff, waivers_semana. **estatisticas** = QUALQUER contagem, ranking ou comparação entre períodos "
        "(quantas trocas por semana, quem mais trocou, FAAB gasto…): "
        "entidade=trocas|waivers|jogos|draft|jogadores|pontuacao (pontos dos jogadores, ranking por posição com o dono) + agrupar_por + metrica; NUNCA conte linhas de uma lista. "
        "**grafico** = a mesma consulta como IMAGEM (calor, linhas, colunas, pizza, dispersão; ou `dados`): use "
        "quando pedirem gráfico/imagem e inclua a linha MEDIA:<caminho> devolvida. "
        "**musica** alvo=<manager|jogador> [alvo2] [tema] = ficha de pesquisa para compor (dados, rivais, perfil, notícias); "
        "**cantar** letra=<versos> = áudio da música (devolve MEDIA:): só quando pedirem música/rap/paródia. "
        "**piadas**/**piadas_enviar** = piadista: SÓ por ordem do admin verificado no chat privado (recusam fora disso). "
        "**ranking_pontos** listas=RB:24,WR:36,FLEX:36 = top-N por posição (flex = sobras). **ajuda** alvo=<consulta>."),
    "parameters": {
        "type": "object",
        "properties": {
            "consulta": {"type": "string", "enum": _QUERIES},
            "alvo": {"type": "string", "description": "time, manager, jogador ou filtro (ex.: blockbusters, picks)"},
            "alvo2": {"type": "string", "description": "2º manager/time (h2h; em `trocas`: só as trocas ENTRE alvo e alvo2)"},
            "semana": {
                "type": "integer",
                "description": (
                    "Número da rodada da NFL. REGRA DE OURO: OMITE este campo para ver a rodada atual em "
                    "andamento (ou a mais recente). Nunca passe a próxima semana antes do kickoff oficial."
                ),
            },
            "ano": {"type": "string", "description": "temporada (ex.: 2025); omita para todas"},
            "limite": {"type": "integer"},
            "entidade": {"type": "string", "enum": ["trocas", "waivers", "jogos", "draft", "jogadores", "pontuacao"],
                         "description": "só para estatisticas"},
            "agrupar_por": {"type": "string", "enum": ["temporada", "semana", "mes", "manager", "posicao", "rodada",
                                                         "tier", "tipo", "adversario", "origem", "time_nfl", "jogador"],
                            "description": "estatisticas/grafico; semana sem `ano` devolve semanas × temporadas"},
            "metrica": {"type": "string", "enum": ["contagem", "jogadores", "picks", "picks_1a", "faab", "faab_media",
                                                     "perdidas", "taxa_sucesso", "vitorias", "derrotas", "aproveitamento",
                                                     "pontos", "media", "maximo", "minimo", "margem", "contra",
                                                     "media_contra", "via_troca", "pontos_titular", "jogos"],
                        "description": "estatisticas/grafico (padrão: contagem; jogos: vitorias)"},
            "metrica2": {"type": "string", "description": "2ª métrica (mesmos valores de metrica): dispersão/comparação"},
            "serie": {"type": "string", "description": "2ª dimensão (mesmos valores de agrupar_por): matriz agrupar_por × serie"},
            "forma": {"type": "string", "enum": ["auto", "barras", "colunas", "empilhado", "linhas", "calor", "pizza",
                                                   "dispersao"], "description": "só grafico: forma do gráfico (padrão auto)"},
            "tema": {"type": "string", "description": "só musica: foco do pedido (derrotas, vitórias, trocas…)"},
            "letra": {"type": "string", "description": "só cantar: os versos da música, um por linha"},
            "estilo": {"type": "string", "enum": ["rap", "lento"], "description": "só cantar (padrão rap)"},
            "piadas": {"type": "string", "description": "só piadas_enviar: as piadas, uma por linha"},
            "destino": {"type": "string", "enum": ["grupo", "dm"], "description": "só piadas_enviar: grupo da liga ou DM do admin"},
            "formato": {"type": "string", "enum": ["texto", "audio"], "description": "só piadas_enviar: audio = cada piada vira nota de voz"},
            "intervalo": {"type": "integer", "description": "só piadas_enviar: segundos entre uma piada e a seguinte"},
            "dados": {"type": "string", "description": "só grafico: JSON com seus próprios dados (title, blocks[...])"},
            "posicao": {"type": "string", "enum": ["QB", "RB", "WR", "TE", "K", "DEF"],
                        "description": "só estatisticas/grafico de entidade=pontuacao: filtra a posição"},
            "listas": {"type": "string", "description": "só ranking_pontos: ex. RB:24,WR:36,TE:12,FLEX:36 (FLEX/SUPERFLEX = sobra das listas acima)"},
            "fase": {"type": "string", "enum": ["regular", "playoffs", "toilet"], "description": "só estatisticas de jogos: playoffs = mata-mata do título (WC/semi/final/3º/5º); toilet = Toilet Bowl"},
            "detalhe": {"type": "boolean", "description": "trocas: formato completo (padrão compacto)"},
        },
        "required": ["consulta"],
    },
}


def _league():
    """League of the Hermes profile serving this turn. Under `gateway.multiplex_profiles` the gateway pins the routed
    profile's HERMES_HOME for the turn (a ContextVar), so every group reaches ITS league's data; a lone gateway
    resolves to the default profile, i.e. league #1."""
    import league_ctx
    try:
        from hermes_constants import get_hermes_home
        return league_ctx.for_home(str(get_hermes_home()))
    except Exception:  # not inside Hermes (tests/CLI): HERMES_HOME / LEAGUE_HOME
        return league_ctx.for_home()


def _handle(args: dict, **_kw) -> str:
    import league_ctx
    import liga_core
    with league_ctx.use(_league()):
        return liga_core.run_query(str(args.get("consulta") or ""), args)


def _available() -> bool:
    return os.path.isfile(os.path.join(LEAGUE_DIR, "liga_core.py"))


def _inject_context(**kw):
    import league_ctx
    with league_ctx.use(_league()):
        return _inject_context_for_league()


def _inject_context_for_league():
    parts = []
    try:
        import round_context
        ctx_str = round_context.get_current_round_context()
        if ctx_str:
            parts.append(ctx_str)
    except Exception as exc:
        logger.warning("[sleeper-league] failed to load round_context: %s", exc)

    try:
        import admin_notes
        notes = admin_notes.read_notes(1500)
        if notes:
            parts.append("NOTAS VERIFICADAS DO ADMIN (fatos oficiais, respeite):\n" + notes)
    except Exception as exc:
        logger.warning("[sleeper-league] failed to load admin_notes: %s", exc)

    if not parts:
        return None
    return {"context": "\n\n".join(parts)}


def register(ctx) -> None:
    ctx.register_tool(name="liga", toolset="liga", schema=SCHEMA, handler=_handle,
                      check_fn=_available, emoji="🏈",
                      description="Fantasy league data of the group's league (Sleeper/ESPN/nflverse)")
    ctx.register_hook("pre_llm_call", _inject_context)
