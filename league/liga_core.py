"""Entry point of the `liga` tool (sleeper-league plugin): validates/normalizes args, runs the query, caches."""

from __future__ import annotations

import contextlib
import subprocess
import io
import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import league_ctx  # noqa: E402

logger = logging.getLogger("liga")
MAX_CHARS = int(os.environ.get("LIGA_MAX_CHARS", "6000"))
CACHE_TTL = 90.0
_lock = threading.Lock()
_cache: Dict[str, tuple[float, str]] = {}

# consulta -> (sleeper_cli command | internal handler, positional arg spec)
CMDS: Dict[str, tuple[str, List[str]]] = {
    "tabela": ("standings", ["ano"]),
    "confrontos": ("matchups", ["semana"]),
    "analise_confronto": ("matchup_analysis", ["alvo", "semana"]),
    "lesoes_rodada": ("__round_injuries__", ["semana"]),
    "boletim_pre_jogo": ("injuries", ["semana"]),
    "noticias_jogador": ("player_news", ["alvo"]),
    "stats_jogador": ("player_stats", ["alvo", "semana", "ano"]),
    "jogador": ("player", ["alvo"]),
    "h2h": ("h2h", ["alvo", "alvo2"]),
    "manager": ("manager", ["alvo"]),
    "perfil_psicologico": ("manager_profile", ["alvo"]),
    "time": ("team_analysis", ["alvo"]),
    "roster_dna": ("roster_dna", ["alvo"]),
    "franquia": ("franchise_history", ["alvo"]),
    "recordes": ("records", []),
    "playoffs": ("playoffs", ["ano"]),
    "classificados": ("playoff_seeds", []),
    "trocas": ("__trades__", ["alvo", "limite", "ano", "semana", "detalhe", "alvo2"]),
    "trocas_picks": ("trade_picks", ["alvo"]),
    "draft_historico": ("draft_history", ["alvo", "ano"]),
    "draft_picks": ("draft_picks", ["ano"]),
    "picks_manager": ("manager_picks", ["alvo"]),
    "ordem_draft": ("draft_order", ["ano"]),
    "notas_draft": ("draft_grades", ["ano"]),
    "max_pf": ("max_pf", ["semana"]),
    "waivers": ("__waivers__", ["alvo", "limite", "ano"]),
    "radar_waivers": ("radar_waivers", []),
    "transacoes": ("transactions", ["semana"]),
    "placar_nfl": ("nfl_scoreboard", ["semana"]),
    "noticias_nfl": ("news", []),
    "data_lab": ("__data_lab__", ["alvo", "semana"]),
    "notas": ("__notes__", []),
    "avaliar_troca": ("__trade_value__", ["alvo"]),
    "power_ranking": ("__power__", []),
    "chances_playoff": ("__playoff_odds__", []),
    "waivers_semana": ("__waivers_week__", ["limite"]),
    "estatisticas": ("__stats__", ["entidade", "agrupar_por", "metrica", "ano", "semana", "alvo", "fase", "limite", "serie",
                                   "metrica2", "posicao"]),
    "grafico": ("__chart__", ["entidade", "agrupar_por", "metrica", "ano", "semana", "alvo", "fase", "limite", "serie",
                              "forma", "metrica2", "dados", "posicao"]),
    "ranking_pontos": ("__ranking__", ["listas", "ano", "metrica"]),
    "musica": ("__song_brief__", ["alvo", "alvo2", "tema"]),
    "cantar": ("__sing__", ["letra", "estilo"]),
    "ajuda": ("__help__", ["alvo"]),
}

# Per-query usage, served by `ajuda` so the tool schema (sent on every turn) can stay short.
HELP: Dict[str, str] = {
    "tabela": "tabela [ano] — classificação (atual ou de um ano passado)",
    "confrontos": "confrontos [semana] — placares da rodada (omita semana = rodada atual)",
    "analise_confronto": "analise_confronto alvo=time/manager [semana] — quem já jogou, quem falta, projeção, chance",
    "lesoes_rodada": "lesoes_rodada [semana] — lesões que ACONTECERAM nos jogos (depois dos jogos)",
    "boletim_pre_jogo": "boletim_pre_jogo [semana] — status GTD/Out/IR ANTES dos jogos, por confronto",
    "noticias_jogador": "noticias_jogador alvo=jogador — boletim médico e notícias",
    "stats_jogador": "stats_jogador alvo=jogador [semana] [ano] — box score",
    "jogador": "jogador alvo=jogador — origem (draft/waiver), trocas, papel",
    "h2h": "h2h alvo alvo2 — confronto direto entre dois managers",
    "manager": "manager alvo — dossiê de carreira (títulos, campanha, H2H contra todos)",
    "perfil_psicologico": "perfil_psicologico alvo — perfil comportamental do manager",
    "time": "time alvo — elenco completo", "roster_dna": "roster_dna alvo — % de draft/trocas/waivers no elenco",
    "franquia": "franquia alvo — temporada a temporada", "recordes": "recordes — maiores pontuações, fiascos, goleadas",
    "playoffs": "playoffs [ano] — decisões, Toilet Bowl",
    "classificados": "classificados — quem está nos playoffs agora, com os seeds (líderes de divisão 1–3, depois o resto)",
    "trocas": ("trocas [alvo=blockbusters|picks|convertidas|manager|jogador] [alvo2=outro manager: só as trocas ENTRE os dois] "
               "[ano] [semana] [limite] [detalhe=true] — "
               "LISTA de trocas (mais recentes primeiro, já filtradas; o cabeçalho diz 'N de TOTAL'). "
               "Trocas entre dois managers = alvo=<A> alvo2=<B>, numa chamada só. "
               "Para CONTAR ou comparar períodos use `estatisticas`."),
    "trocas_picks": "trocas_picks alvo=manager — trocas com picks e no que viraram",
    "draft_historico": "draft_historico alvo=time [ano]", "draft_picks": "draft_picks [ano] — todas as escolhas do draft",
    "picks_manager": "picks_manager alvo=manager — todas as escolhas de um manager", "ordem_draft": "ordem_draft [ano]",
    "notas_draft": "notas_draft [ano]", "max_pf": "max_pf [semana]",
    "waivers": ("waivers [alvo=steals|fiascos|bids|manager|jogador] [ano] [limite] — mercado de waivers/FA "
                "(sem alvo = resumo). Para contar use `estatisticas`."),
    "radar_waivers": "radar_waivers", "transacoes": "transacoes [semana]", "placar_nfl": "placar_nfl [semana]",
    "noticias_nfl": "noticias_nfl", "data_lab": "data_lab alvo=jogador/time [semana] — métricas avançadas NFL",
    "notas": "notas — notas do admin", "avaliar_troca": 'avaliar_troca alvo="Josh Allen + 2027 1st por Bijan Robinson"',
    "power_ranking": "power_ranking", "chances_playoff": "chances_playoff", "waivers_semana": "waivers_semana [limite=horas]",
    "estatisticas": (
        "estatisticas entidade=<trocas|waivers|jogos|draft|jogadores|pontuacao> [agrupar_por=<temporada|semana|mes|manager|posicao|"
        "rodada|tier|tipo|adversario|origem|time_nfl>] [serie=<2ª dimensão: vira uma matriz agrupar_por × serie>] "
        "[metrica=<contagem|jogadores|picks|picks_1a|faab|faab_media|perdidas|taxa_sucesso|vitorias|derrotas|aproveitamento|"
        "pontos|media|maximo|minimo|margem|contra|media_contra|via_troca>] [metrica2=<2ª métrica>] [ano] [semana] "
        "[alvo=manager ou jogador] [fase=regular|playoffs|toilet (jogos; playoffs = mata-mata do título, toilet = Toilet Bowl) ou offseason|temporada (trocas/waivers: offseason = antes do kickoff da NFL, que a Sleeper chama de semana 1)] [limite]. Contagens, rankings e comparações EXATAS "
        "(nunca conte linhas de listas). Ex.: trocas por semana em todas as temporadas = entidade=trocas agrupar_por=semana; "
        "quem mais trocou = entidade=trocas agrupar_por=manager; pontos por manager e semana = entidade=jogos "
        "agrupar_por=manager serie=semana ano=2026 metrica=pontos. "
        "PONTOS DE JOGADORES (pontuação da liga; bye ≠ 0): entidade=pontuacao posicao=<QB|RB|WR|TE|K|DEF> "
        "agrupar_por=<jogador|manager|posicao|time_nfl|semana|temporada> metrica=<pontos|pontos_titular|media|maximo|minimo|jogos>. "
        "Ex.: top-12 QB da temporada = entidade=pontuacao posicao=QB agrupar_por=jogador metrica=pontos limite=12 "
        "(cada linha traz 'Jogador — Manager'); semana a semana de um jogador = alvo=<nome> agrupar_por=semana. "
        "Veja a skill league-player-points."),
    "grafico": (
        "grafico = IMAGEM (PNG) + página HTML. Mesmos parâmetros de `estatisticas` (entidade, agrupar_por, serie, metrica, "
        "metrica2, ano, semana, alvo, fase, limite) + forma=<auto|barras|colunas|empilhado|linhas|calor|pizza|dispersao> "
        "+ dados=<JSON próprio>. Sem parâmetros = trocas por semana, ano a ano. Auto: agrupar_por=semana (sem ano) → mapa de "
        "calor + ritmo acumulado; temporada/mês (≤ 8) → colunas, (> 8) → linha; manager/posição/tier → ranking; "
        "agrupar_por+serie → mapa de calor (forma=linhas/colunas/empilhado se ≤ 8/6 séries); metrica+metrica2 → "
        "dispersão (ex.: jogos agrupar_por=manager metrica=media metrica2=media_contra); forma=pizza → rosca "
        "(partes de um todo). `dados` = qualquer gráfico com números seus: {\"title\", \"subtitle\", \"blocks\": "
        "[{\"type\": \"bars|columns|lines|heatmap|donut|scatter|tiles\", ...}]} (veja a skill league-charts). "
        "A ferramenta devolve a linha MEDIA:<caminho>: copie-a na resposta para a imagem ir ao chat."),
    "ranking_pontos": ("ranking_pontos listas=\"RB:24,WR:36,TE:12,FLEX:36\" [ano] [metrica=pontos|media] — várias listas de "
                       "top-N por posição numa chamada só, na pontuação da liga, com o time dono; cada lista NÃO repete quem "
                       "já entrou nas anteriores (FLEX = melhores RB/WR/TE que sobraram; SUPERFLEX inclui QB; também RB/WR)."),
    "musica": ("musica [alvo=manager|jogador] [alvo2=rival] [tema=derrotas|vitórias|trocas…] — só a PESQUISA para uma música: "
               "ficha com dados, rivais, marcas, perfil e notícias + regras de escrita. Use quando pedirem rap/música/paródia; "
               "escreva a letra e chame `cantar`."),
    "cantar": ("cantar letra=<versos, um por linha> [estilo=rap|lento] — transforma a letra em ÁUDIO (voz do Antonio sobre uma "
               "base de rap) e devolve a linha MEDIA:<caminho>. 4 a 40 versos de até 140 caracteres."),
    "ajuda": "ajuda [alvo=consulta] — uso detalhado de uma consulta (ou de todas)",
}


# History queries: `semana` may be any past week of any season, so the "week hasn't started" guard must not rewrite it.
HISTORICAL = {"trocas", "waivers", "estatisticas", "grafico"}

# Queries that make no sense without a target (the CLI used to silently pick a default team).
NEEDS_ALVO = {"avaliar_troca", "analise_confronto", "noticias_jogador", "stats_jogador", "jogador", "h2h", "manager",
              "perfil_psicologico", "time", "franquia", "trocas_picks", "draft_historico", "picks_manager"}


def normalize_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """Models fill every optional field (semana=0, limite=0, alvo=""): treat empty/zero as "not given"."""
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
            if v == "" or (k in ("semana", "limite", "ano") and v in ("0", "null", "None")):
                continue
        if k == "dados" and isinstance(v, (dict, list)):
            import json as _json
            v = _json.dumps(v, ensure_ascii=False)
        if k == "detalhe":
            if v in (True, 1) or str(v).lower() in ("true", "1", "sim", "yes"):
                out[k] = True
            continue
        if k in ("semana", "limite"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v <= 0:
                continue
        if k == "ano":
            v = str(v)
            if not (v.isdigit() and len(v) == 4):
                continue
        out[k] = v
    return out


def _argv(spec: List[str], args: Dict[str, Any]) -> List[str]:
    out = ["" if args.get(k) is None else str(args.get(k)).strip() for k in spec]
    while out and out[-1] == "":
        out.pop()
    return out


def _run_cli(argv: List[str]) -> str:
    import sleeper_cli
    return sleeper_cli.run(argv)


_week_started: Dict[int, tuple] = {}


def _week_has_games(week: int) -> bool:
    """True if at least one NFL game of `week` has kicked off (ESPN scoreboard, cached 10 min)."""
    hit = _week_started.get(week)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    try:
        import data_lab
        import round_injuries
        ok = any(g["state"] in ("in", "post") for g in round_injuries._scoreboard(week, data_lab._season()))
    except Exception:
        ok = True  # unknown → don't second-guess the caller
    _week_started[week] = (time.monotonic(), ok)
    return ok


def _truncate(out: str, limit: int = MAX_CHARS) -> str:
    """Cut at a line boundary (never mid-record) and say how much is missing."""
    if len(out) <= limit:
        return out
    cut = out.rfind("\n", 0, limit)
    cut = cut if cut > limit // 2 else limit
    kept, rest = out[:cut], out[cut:]
    missing = rest.count("\n")
    return (kept.rstrip() + f"\n…[resultado truncado: faltam ~{missing} linhas — peça com filtro "
            "(ano/semana/manager), `limite` menor ou use `estatisticas` para contagens]")


def _ensure_scores(args: Dict[str, Any]) -> None:
    """`pontuacao` reads player points per week: pull the finished weeks from Sleeper first (no-op when fresh)."""
    if str(args.get("entidade") or "").lower() == "pontuacao":
        import player_scores
        player_scores.ensure_fresh()


def run_query(consulta: str, args: Dict[str, Any] | None = None) -> str:
    args = normalize_args(dict(args or {}))
    consulta = str(consulta or "").strip().lower()
    if consulta not in CMDS:
        return f"Consulta inválida '{consulta}'. Válidas: {', '.join(CMDS)}"
    if consulta in NEEDS_ALVO and not args.get("alvo"):
        return (f"A consulta '{consulta}' precisa de `alvo` (time, manager ou jogador). "
                "Se a pergunta é sobre o próprio time de quem perguntou, use o time que aparece após '|' no nome.")
    if consulta == "h2h" and not args.get("alvo2"):
        return "A consulta 'h2h' precisa de `alvo` e `alvo2` (os dois times/managers)."
    note = ""
    # Guard against models hallucinating next week when current week is still active or future week hasn't kicked off
    if args.get("semana") and consulta not in HISTORICAL:
        try:
            wk = int(args["semana"])
            from sleeper_client import SleeperClient
            st = SleeperClient().get_nfl_state()
            cur_week = int(st.get("week") or 1)
            if wk > cur_week and not _week_has_games(wk):
                args["semana"] = cur_week
                note = f"(Semana {wk} ainda não começou — mostrando a Semana {cur_week}, a rodada atual em andamento.)\n"
            elif consulta in ("stats_jogador", "data_lab") and wk > 1 and not _week_has_games(wk):
                args["semana"] = wk - 1
                note = (f"(Semana {wk} ainda não teve jogos — mostrando a Semana {wk - 1}, "
                        f"a última rodada jogada.)\n")
        except Exception:
            pass
    cmd, spec = CMDS[consulta]
    argv = _argv(spec, args)
    key = f"{league_ctx.current().slug}|{consulta}|{'|'.join(argv)}"   # answers carry one league's data: never shared
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    t0 = time.monotonic()
    try:
        import sleeper_client
        sleeper_client.STALE.clear()
    except Exception:
        sleeper_client = None
    try:
        with _lock:
            if cmd == "__round_injuries__":
                import round_injuries
                out = round_injuries.format_report(int(args["semana"]) if args.get("semana") else None)
            elif cmd == "__data_lab__":
                import data_lab
                out = data_lab.query(args.get("alvo") or "", int(args["semana"]) if args.get("semana") else None)
            elif cmd == "__trades__":
                from knowledge_service import KnowledgeBaseService
                out = KnowledgeBaseService().get_trades_query(
                    query=args.get("alvo"), limit=int(args.get("limite") or 12), ano=args.get("ano"),
                    semana=args.get("semana"), detalhe=bool(args.get("detalhe")), contra=args.get("alvo2"))
            elif cmd == "__waivers__":
                from knowledge_service import KnowledgeBaseService
                out = KnowledgeBaseService().get_waivers_report(
                    args.get("alvo") or "", limit=int(args.get("limite") or 12), ano=args.get("ano"))
            elif cmd == "__stats__":
                import kb_stats
                _ensure_scores(args)
                out = kb_stats.run(entidade=args.get("entidade") or "", agrupar_por=args.get("agrupar_por") or "",
                                   metrica=args.get("metrica") or "", ano=args.get("ano"), semana=args.get("semana"),
                                   alvo=args.get("alvo") or "", limite=args.get("limite"), fase=args.get("fase") or "",
                                   serie=args.get("serie") or "", metrica2=args.get("metrica2") or "",
                                   posicao=args.get("posicao") or "")
            elif cmd == "__chart__":
                import chart_service
                _ensure_scores(args)
                try:
                    if args.get("dados"):  # the caller supplies the numbers (any chart); validated before drawing
                        res = chart_service.generate_from_json(args["dados"], args.get("alvo") or "")
                    else:
                        res = chart_service.generate(
                            args.get("entidade") or "trocas", args.get("agrupar_por") or "semana", args.get("metrica") or "",
                            args.get("ano"), args.get("semana"), args.get("alvo") or "", args.get("limite"),
                            args.get("fase") or "", args.get("serie") or "", args.get("forma") or "", args.get("metrica2") or "",
                            args.get("posicao") or "")
                    out = chart_service.media_reply(res)
                except ValueError as exc:
                    out = f"Não consegui montar o gráfico: {exc}"
                except ImportError:
                    out = "Gráficos indisponíveis aqui (Pillow não instalado). Use `estatisticas` e responda em texto."
            elif cmd == "__ranking__":
                import kb_db
                import player_scores
                player_scores.ensure_fresh()
                con = kb_db.connect()
                try:
                    out = player_scores.ranking_lists(con, args.get("listas") or "", args.get("ano") or "",
                                                      args.get("metrica") or "pontos")
                except ValueError as exc:
                    out = f"Não consegui montar o ranking: {exc}"
                finally:
                    con.close()
            elif cmd == "__song_brief__":
                import song
                out = song.brief(args.get("alvo") or "", args.get("alvo2") or "", args.get("tema") or "")
            elif cmd == "__sing__":
                import song
                try:
                    out = song.media_reply(song.sing(args.get("letra") or "", args.get("estilo") or "rap"))
                except (ValueError, RuntimeError) as exc:
                    out = f"Não consegui cantar: {exc}"
                except (OSError, subprocess.SubprocessError) as exc:
                    out = f"Não consegui cantar (erro de áudio: {exc.__class__.__name__}). Responda a letra em texto."
            elif cmd == "__help__":
                topic = str(args.get("alvo") or "").strip().lower()
                out = HELP.get(topic) or "\n".join(f"• {v}" for v in HELP.values())
            elif cmd == "__notes__":
                import admin_notes
                out = admin_notes.read_notes(4000) or "(sem notas do admin)"
            elif cmd == "__trade_value__":
                import trade_values
                out = trade_values.evaluate(args.get("alvo") or "")
            elif cmd in ("__power__", "__playoff_odds__", "__waivers_week__"):
                from scheduler_service import WeeklyScheduler
                ws = WeeklyScheduler()
                if cmd == "__power__":
                    out = ws.generate_tuesday_power()
                elif cmd == "__playoff_odds__":
                    import analytics
                    res = analytics.playoff_odds(ws.sleeper, sims=3000)
                    out = "\n".join([f"🎲 Chances de playoff ({res['sims']} simulações, {res['weeks_left']} semanas restantes, "
                                     f"top {res.get('playoff_teams', 6)} classificam):"]
                                    + [f"• {r['team']} ({r['record']}): {r['playoff_pct']}% playoff, {r['top2_pct']}% top 2"
                                       for r in res["rows"]])
                else:
                    import waiver_report
                    hours = float(args.get("limite") or 168)
                    out = waiver_report.build(ws.sleeper, ws._player_label, ws._faab_budget(), hours=hours,
                                              title="Waivers", subtitle=f"Todas as movimentações das últimas {int(hours)}h")
            else:
                out = _run_cli([cmd, *argv])
    except Exception as exc:
        logger.exception("[liga] %s failed", consulta)
        return f"Erro ao consultar '{consulta}': {exc}. Não invente dados; diga que a fonte falhou."
    stale = sorted(sleeper_client.STALE) if sleeper_client else []
    if stale:
        note += ("⚠️ AVISO: fonte ao vivo indisponível agora; parte destes dados veio de cópia local antiga "
                 f"({', '.join(stale[:3])}). Avise o usuário que podem estar desatualizados.\n")
    out = note + (out or "(sem resultado)")
    out = _truncate(out)
    logger.info("[liga] %s %s -> %d chars in %.2fs", consulta, argv, len(out), time.monotonic() - t0)
    _cache[key] = (now, out)
    return out
