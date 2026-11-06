#!/usr/bin/env python3
"""Hermes Skill CLI for Sleeper Fantasy League & Evolution API WhatsApp.

Runs for the *current league* (league_ctx): `LEAGUE_HOME=data/profiles/<slug> python3 sleeper_cli.py standings`.
"""

import json
import os
import sys

# Ensure local directory is on path
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

import league_ctx
from knowledge_service import KnowledgeBaseService

EVOLUTION_URL = os.environ.get("EVOLUTION_API_URL", "http://evolution-api:8080")

EVOLUTION_KEY = os.environ.get("EVOLUTION_API_KEY", "")
EVOLUTION_INSTANCE = os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot")


def main(argv=None):
    argv = list(argv) if argv is not None else sys.argv
    if len(argv) < 2:
        print("Usage: sleeper_cli.py <command> [args...]")
        print("Commands:")
        print("  standings                   - Format league standings for WhatsApp")
        print("  matchups [week]             - Format matchups for a given week")
        print("  matchup_analysis <team>     - Deep matchup breakdown and live win prob")
        print("  h2h <manager1> [manager2]   - Head-to-head rivalry history")
        print("  manager <nome>              - Comprehensive dossier of a league manager")
        print("  records                     - All-time league records, blowouts and fiascos")
        print("  draft_history <team> [ano]  - Historical draft choices")
        print("  franchise_history <team>    - Season by season performance")
        print("  transactions [week]         - Trades & waiver transactions")
        print("  rosters                     - List all rosters with owners and starters")
        print("  team_analysis <time|manager> - Complete roster dossier: 11 starters, bench, stars, needs")
        print("  waivers [filtro|manager]    - Query waiver & FA moves (steals, fiascos, bids, profiles)")
        print("  estatisticas k=v ...        - Exact counts/rankings (entidade=trocas agrupar_por=semana ...)")
        print("  player <nome|id>            - Complete player dossier: origin (draft/waiver), trades, current status")
        print("  roster-dna [time|manager]   - Team's Roster DNA (% homegrown draft, trades, waivers, lifers)")
        print("  send_whatsapp <dest> <msg>  - Send WhatsApp message via Evolution API")
        print("  status                      - Check Sleeper & Evolution API status")
        sys.exit(1)

    cmd = argv[1].lower()
    lctx = league_ctx.current()
    LEAGUE_ID = lctx.league_id
    kb = KnowledgeBaseService()

    def get_sleeper():
        from sleeper_client import SleeperClient
        return SleeperClient(league_id=LEAGUE_ID)

    def get_evolution():
        from evolution_client import EvolutionClient
        return EvolutionClient(base_url=EVOLUTION_URL, api_key=EVOLUTION_KEY, instance=EVOLUTION_INSTANCE)

    if cmd in ["standings", "tabela", "classificacao", "ranking"]:
        year = argv[2] if len(argv) > 2 and argv[2].isdigit() else None
        if year and year != str(lctx.profile.season or get_sleeper().get_nfl_state().get("season")):
            print(kb.get_standings_formatted(year))
        else:
            print(get_sleeper().format_standings_whatsapp())

    elif cmd in ["playoff_seeds", "classificados", "seeds"]:
        print(get_sleeper().format_playoff_picture_whatsapp())

    elif cmd in ["playoffs", "toilet_bowl", "pos_temporada", "decisoes"]:
        year = argv[2] if len(argv) > 2 and argv[2].isdigit() else None
        print(kb.get_playoffs_dossier(year))

    elif cmd in ["draft_order", "ordem_draft", "proximo_draft", "draft_2027"]:
        from draft_order_service import DraftOrderService
        dos = DraftOrderService(league_id=LEAGUE_ID)
        year = argv[2] if len(argv) > 2 and argv[2].isdigit() else None
        print(dos.format_draft_order_whatsapp(season=year))

    elif cmd in ["max_pf", "maxpf", "max_pff", "ppts"]:
        from draft_order_service import DraftOrderService
        dos = DraftOrderService(league_id=LEAGUE_ID)
        week = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        print(dos.format_weekly_max_pf_whatsapp(week))

    elif cmd == "matchups":
        week = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        print(get_sleeper().format_matchups_whatsapp(week))

    elif cmd in ["matchup_analysis", "analysis", "analise"]:
        if len(argv) < 3 or not argv[2].strip():
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        team = argv[2]
        week = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else None
        print(get_sleeper().format_matchup_analysis_whatsapp(team, week))

    elif cmd in ["player_stats", "stats_player", "boxscore", "estatisticas_jogador"]:
        if len(argv) < 3:
            print("Uso: sleeper_cli.py player_stats <nome_jogador> [semana] [ano]")
            sys.exit(1)
        pname = argv[2]
        week = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else None
        season = argv[4] if len(argv) > 4 else None
        st = get_sleeper().get_player_stats(pname, week, season)
        if "error" in st:
            print(f"❌ {st['error']}")
        else:
            w = st["week"]
            print(f"📊 *ESTATÍSTICAS REAIS — {st['name'].upper()} ({st['pos']} - {st['team']}) — SEMANA {w}*")
            print("━━━━━━━━━━━━━━━━━━━━━━")
            if st.get("note"):
                print(f"ℹ️ {st['note']}")
            if st.get("bye"):
                print(f"😴 *BYE WEEK*: o {st['team']} não joga na Semana {w}. Isso NÃO é jogo de 0 pontos "
                      "(fora de médias e de contagem de jogos).")
                print("━━━━━━━━━━━━━━━━━━━━━━")
                sys.exit(0)
            print(f"• Pontos Fantasy: *{st['points_ppr']} pts PPR* (Projeção inicial: {st['projected_ppr']} pts)")
            if st["pos"] in ["WR", "TE", "RB"] or st["targets"] > 0:
                print(f"• Jogo Aéreo: *{st['receptions']} recepções* em {st['targets']} targets | *{st['rec_yards']} jardas* | *{st['rec_td']} TDs*")
            if st["rush_att"] > 0 or st["pos"] == "RB":
                print(f"• Jogo Terrestre: *{st['rush_att']} carregadas* | *{st['rush_yards']} jardas* | *{st['rush_td']} TDs*")
            if st["pass_att"] > 0 or st["pos"] == "QB":
                print(f"• Passes: *{st['pass_cmp']}/{st['pass_att']} comp* | *{st['pass_yards']} jardas* | *{st['pass_td']} TDs* | *{st['pass_int']} INTs*")
            if st["fumbles_lost"] > 0:
                print(f"• Fumbles perdidos: *{st['fumbles_lost']}*")
            if not st["has_played"]:
                print("⚠️ Atleta ainda sem estatísticas registradas nesta semana.")
            print("━━━━━━━━━━━━━━━━━━━━━━")

    elif cmd in ["player_news", "news_player", "lesao", "injury", "noticias_jogador"]:
        if len(argv) < 3:
            print("Uso: sleeper_cli.py player_news <nome_jogador>")
            sys.exit(1)
        pname = " ".join(argv[2:])
        res = get_sleeper().get_player_news(pname)
        if "error" in res:
            print(f"❌ {res['error']}")
        else:
            print(f"🏥 *BOLETIM MÉDICO & NOTÍCIAS — {res['name'].upper()} ({res['pos']} - {res['team']})*")
            print("━━━━━━━━━━━━━━━━━━━━━━")
            if res.get("in_game_alert"):
                print("🚨 *ALERTA DE JOGO AO VIVO:* Atleta sofreu lesão recente ou saiu de campo durante a partida!")
            st_icon = "🟢" if res["injury_status"] in ["Active", "Healthy", "None"] else "🔴"
            print(f"• Status Oficial Sleeper: {st_icon} *{res['injury_status']}*")
            if res.get("forecast"):
                print(f"• Previsão Médica: *{res['forecast']}*")
            if res.get("injury_body_part") and res["injury_body_part"] != "Nenhum":
                print(f"• Região Lesionada: *{res['injury_body_part']}*")
            if res.get("injury_notes"):
                print(f"• Detalhes/Observações: {res['injury_notes']}")
            if res.get("injury_start_date"):
                print(f"• Data do Registro: {res['injury_start_date']}")
            print("")
            if res["recent_news"]:
                print("📰 *ÚLTIMAS ATUALIZAÇÕES DE NOTÍCIAS:*")
                for n in res["recent_news"]:
                    date_str = f"[{n['date']}] " if n.get("date") else ""
                    print(f"• {date_str}*{n.get('headline', '')}*")
                    if n.get("body"):
                        print(f"   ↳ {n['body']}")
                    if n.get("analysis"):
                        print(f"   ↳ 🎙️ _Análise: {n['analysis']}_")
            else:
                print("ℹ️ Nenhuma notícia de última hora reportada nas últimas 48 horas.")
            print("━━━━━━━━━━━━━━━━━━━━━━")

    elif cmd in ["h2h", "rivalry", "rivalidades", "confronto_direto"]:
        if len(argv) < 3:
            print("Uso: sleeper_cli.py h2h <manager1> [manager2]")
            sys.exit(1)
        m1 = argv[2]
        if len(argv) > 3:
            m2 = argv[3]
            print(kb.get_h2h(m1, m2))
        else:
            # Mostra todas as rivalidades do manager
            print(kb.get_manager_dossier(m1))

    elif cmd in ["manager", "dossie", "perfil", "dossier"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        m = argv[2]
        print(kb.get_manager_dossier(m))

    elif cmd in ["manager_profile", "psicologico", "perfil_psicologico", "scouting", "whatsapp_profile"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        m = argv[2]
        print(kb.get_manager_psychological_profile(m))

    elif cmd in ["records", "recordes", "maiores", "fiascos"]:
        print(kb.get_records_formatted())

    elif cmd in ["draft_picks", "picks", "escolhas"]:
        year = argv[2] if len(argv) > 2 else str(get_sleeper().get_nfl_state().get("season"))
        print(kb.get_draft_picks_dossier(year))

    elif cmd in ["trades", "trocas", "all_trades", "search_trades", "buscar_trocas", "top_trades", "polemicas", "trocas_polemicas", "maiores_trocas"]:
        query = argv[2] if len(argv) > 2 else None
        limit = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 12
        if cmd in ["top_trades", "polemicas", "trocas_polemicas", "maiores_trocas"] and not query:
            query = "blockbusters"
        print(kb.get_trades_query(query=query, limit=limit))

    elif cmd in ["trade_picks", "trocas_picks", "pick_trades"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        manager = argv[2]
        print(kb.get_trade_picks_dossier(manager))

    elif cmd in ["manager_picks", "picks_manager", "escolhas_manager"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        manager = argv[2]
        print(kb.get_manager_draft_report(manager))

    elif cmd in ["draft_history", "drafts", "draft"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        team = argv[2]
        season = argv[3] if len(argv) > 3 else None
        print(get_sleeper().format_draft_history_whatsapp(team, season))

    elif cmd in ["franchise_history", "historico", "history", "franchise"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        team = argv[2]
        print(get_sleeper().format_franchise_history_whatsapp(team))

    elif cmd == "transactions":
        week = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        slp = get_sleeper()
        week = week or int(slp.get_nfl_state().get("week", 1))
        db = slp.get_players_db()

        def label(pid):
            p = db.get(str(pid), {})
            return f"{p.get('name') or pid} ({p.get('pos') or '?'} - {p.get('team') or 'FA'})"

        legs = [week, week - 1] if week > 1 else [week]
        txs, seen = [], set()
        for leg in legs:
            for t in slp.get_transactions(leg):
                if t["status"] == "complete" and t["transaction_id"] not in seen:
                    seen.add(t["transaction_id"])
                    txs.append(t)
        txs.sort(key=lambda t: t.get("status_updated") or 0, reverse=True)
        if not txs:
            print(f"Sem transações concluídas nas semanas {', '.join(map(str, legs))}.")
        else:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            print(f"🔄 *TRANSAÇÕES CONCLUÍDAS — SEMANAS {' e '.join(map(str, legs))}* ({len(txs)}):")
            kinds = {"waiver": "WAIVER", "free_agent": "FA", "trade": "TRADE", "commissioner": "COMISSÁRIO"}
            for t in txs:
                when = datetime.fromtimestamp((t.get("status_updated") or 0) / 1000, ZoneInfo("America/Sao_Paulo"))
                bid = (t.get("settings") or {}).get("waiver_bid")
                bid_s = f" ${bid}" if bid else ""
                print(f"• [{kinds.get(t['type'], t['type'])}{bid_s}] {', '.join(t['teams'])} — {when:%d/%m %H:%M}")
                if t["adds"]:
                    print(f"   ➕ {', '.join(label(p) for p in t['adds'])}")
                if t["drops"]:
                    print(f"   ➖ {', '.join(label(p) for p in t['drops'])}")

    elif cmd == "rosters":
        slp = get_sleeper()
        users = slp.get_users()
        rosters = slp.get_rosters()
        print(f"📋 *ROSTERS - {lctx.name.upper()}* ({len(rosters)} times):")
        for r in rosters:
            rid = r.get("roster_id")
            oid = r.get("owner_id")
            u = users.get(oid, {})
            name = u.get("team_name") or f"Team {rid}"
            owner = u.get("display_name") or f"Owner {rid}"
            starters_count = len(r.get("starters") or [])
            total_players = len(r.get("players") or [])
            print(f"• *{name}* ({owner}): {starters_count} titulares, {total_players} jogadores no elenco")

    elif cmd in ["team_analysis", "roster_analysis", "time", "elenco", "equipe", "dossie_time"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        target = argv[2]
        print(kb.get_team_roster_analysis(target))

    elif cmd in ["estatisticas", "stats", "contagem"]:
        # usage: sleeper_cli.py estatisticas entidade=trocas agrupar_por=semana [metrica=.. ano=.. semana=.. alvo=.. fase=.. limite=..]
        kv = dict(a.split("=", 1) for a in argv[2:] if "=" in a)
        import kb_stats
        print(kb_stats.run(entidade=kv.get("entidade", ""), agrupar_por=kv.get("agrupar_por", ""),
                           metrica=kv.get("metrica", ""), ano=kv.get("ano"),
                           semana=int(kv["semana"]) if kv.get("semana", "").isdigit() else None,
                           alvo=kv.get("alvo", ""), limite=int(kv["limite"]) if kv.get("limite", "").isdigit() else None,
                           fase=kv.get("fase", "")))

    elif cmd in ["waivers", "waiver", "freeagent", "free_agent", "faab", "mercado_waivers", "lixo"]:
        query = argv[2] if len(argv) > 2 else ""
        limit = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 12
        print(kb.get_waivers_report(query, limit=limit))

    elif cmd in ["player", "jogador", "atleta", "dossie_jogador", "raio_x_jogador"]:
        if len(argv) < 3:
            print("❌ Informe o alvo (time, manager ou jogador)."); sys.exit(0)
        query = " ".join(argv[2:])
        print(kb.get_player_dossier(query))

    elif cmd in ["roster_dna", "roster-dna", "dna", "dna_elenco", "linhagem"]:
        query = argv[2] if len(argv) > 2 else ""
        print(kb.get_roster_dna_report(query))

    elif cmd in ["draft_grades", "notas_draft", "boletim_draft", "grades"]:
        year = argv[2] if len(argv) > 2 else str(get_sleeper().get_nfl_state().get("season"))
        from draft_evaluator import format_draft_grades_whatsapp
        print(format_draft_grades_whatsapp(year))

    elif cmd in ["nfl_scoreboard", "nfl_games", "espn", "placar_nfl"]:
        week = int(argv[2]) if len(argv) > 2 and argv[2].isdigit() else None
        from nfl_game_watcher import fetch_espn_scoreboard
        games = fetch_espn_scoreboard(week)
        if not games:
            print("Nenhum jogo da NFL encontrado para esta consulta na ESPN.")
        else:
            print(f"🏈 *PLACAR NFL (ESPN) — {len(games)} JOGOS IDENTIFICADOS*:")
            for g in games:
                ld = f" | {', '.join(g['leaders'])}" if g.get("leaders") else ""
                print(f"• {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']} [{g['status'].upper()}{ld}]")

    elif cmd in ["waivers", "radar_waivers"]:
        from scheduler_service import WeeklyScheduler
        sched = WeeklyScheduler()
        print(sched.generate_wednesday_waiver_radar())

    elif cmd in ["news", "noticias", "noticia", "injuries", "boletim_medico"]:
        from news_service import NewsService
        ns = NewsService(league_id=LEAGUE_ID)
        if cmd in ["injuries", "boletim_medico"]:
            week_arg = None
            if len(argv) > 2 and argv[2].isdigit():
                week_arg = int(argv[2])
            print(ns.get_matchup_injury_report(week_arg))
        else:
            category = argv[2] if len(argv) > 2 and argv[2] not in ["all", "geral"] else None
            bulletin = ns.get_league_injury_bulletin()
            print(f"🏥 *BOLETIM MÉDICO DA LIGA ({bulletin['total_starters_at_risk']} titulares em risco)*:")
            for s in bulletin["starters_at_risk"]:
                rep = f" (Sugestão no banco: {', '.join(s.get('bench_replacements', []))})" if s.get('bench_replacements') else " (Sem reserva imediato!)"
                print(f"• *{s['manager']}* ({s['team_name']}): *{s['name']}* ({s['pos']} - {s['official_status']}){rep}")

            recent_news = ns.get_latest_news(category=category, limit=8)
            print(f"\n📰 *NOTÍCIAS RECENTES DA NFL* ({len(recent_news)} encontradas):")
            for n in recent_news:
                print(f"• [{n['category'].upper()}] *{n['title']}* ({n['source']})")
                if n.get("summary"):
                    print(f"   _{n['summary'][:120]}..._")

    elif cmd == "status":
        slp = get_sleeper()
        state = slp.get_nfl_state()
        league = slp.get_league()
        evo_state = get_evolution().get_connection_state()
        print(json.dumps({
            "sleeper_league": league.get("name"),
            "nfl_season": state.get("season"),
            "nfl_week": state.get("week"),
            "evolution_api_instance": EVOLUTION_INSTANCE,
            "evolution_api_url": EVOLUTION_URL,
            "whatsapp_state": evo_state.get("instance", {}).get("state", "unknown"),
        }, indent=2))

    elif cmd in ["send_whatsapp", "send_msg", "enviar_whatsapp", "enviar_msg"]:
        if len(argv) < 4:
            print("Uso: sleeper_cli.py send_whatsapp <destinatario_ou_grupo> <mensagem>")
            sys.exit(1)
        dest = argv[2]
        msg = " ".join(argv[3:])
        evo = get_evolution()
        res = evo.send_message(dest, msg)
        print(json.dumps(res, indent=2))

    elif cmd in ["check_trades", "trades_check"]:
        from transaction_monitor import TransactionMonitor
        mon = TransactionMonitor()
        if "--send" not in argv:
            print("Use --send para anunciar no grupo (sem isso nada é registrado). Diagnóstico: python3 transaction_monitor.py")
            sys.exit(0)
        res = mon.check_and_process_trades(notify_whatsapp=True)
        print(f"Trades verificadas. Novas processadas: {len(res)}")

    elif cmd in ["check_waivers", "waivers_check"]:
        from transaction_monitor import TransactionMonitor
        mon = TransactionMonitor()
        res = mon.check_and_process_waivers()
        print(f"Waivers verificadas. Novas registradas: {len(res)}")

    elif cmd in ["sync_week", "consolidar_rodada"]:
        from transaction_monitor import TransactionMonitor
        mon = TransactionMonitor()
        res = mon.check_and_sync_weekly_results()
        print(f"Resultado da consolidação: {res or 'Nenhuma semana pendente ou já consolidada.'}")

    else:
        print(f"Comando desconhecido: {cmd}")
        sys.exit(1)


def run(args) -> str:
    """Run one command and return what it printed (used by liga_core; no global sys.argv mutation)."""
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            main(["sleeper_cli.py", *[str(a) for a in args]])
        except SystemExit:
            pass
    return buf.getvalue().strip()


if __name__ == "__main__":
    main()
