#!/usr/bin/env python3
"""Comprehensive Teams & Rosters Analysis Engine for High Stakes Fantasy.

Analyzes all 12 teams in the league:
- Probable / Ideal Starting Lineup (11 slots: QB, RB1, RB2, WR1, WR2, WR3, TE, FLEX1, FLEX2, FLEX3, SFLEX)
- Bench Depth by position (QBs, RBs, WRs, TEs)
- Taxi Squad and Reserve / IR players
- Franchise Cornerstones / Tier 1 Stars
- Strengths and Critical Needs / Weaknesses
- Surplus & Tradeable Assets (e.g. productive bench players)
- Competitive Status (Contender / Win-Now vs Rebuilding)

Generates:
- knowledge_base/teams_rosters_analysis.json
- knowledge_base/TEAMS_AND_ROSTERS_ANALYSIS.md
"""

import json
import sys
import os
import urllib.request
from typing import Any, Dict, List, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")
import league_ctx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_DIR = None   # override (tests/tools); None = the current league's knowledge base


def _kb() -> str:
    return KB_DIR or league_ctx.current().kb_dir


def _api() -> str:
    return os.environ.get("SLEEPER_API_BASE", "https://api.sleeper.app/v1").rstrip("/")

# The starting layout (slots, FLEX/SUPER_FLEX, bench) is the league's: league_ctx.current().profile

def _injury(p):
    import players_db as _pdb
    return _pdb.injury(p)


def load_data():
    import players_db as _pdb
    players_db = _pdb.load()
    try:
        with open(os.path.join(_kb(), "franchises_history.json"), "r", encoding="utf-8") as f:
            franchises = {int(k): v for k, v in json.load(f).items()}
    except OSError:
        franchises = {}      # a new league without history yet: names come from Sleeper
    return players_db, franchises

def fetch_sleeper_data(get=None):
    def http_get(endpoint):
        req = urllib.request.Request(f"{_api()}/{endpoint}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    get_url = get or http_get     # `get` is injectable (the bootstrap and the tests feed their own Sleeper)

    lid = league_ctx.current().league_id
    rosters = get_url(f"league/{lid}/rosters")
    users = {u["user_id"]: u for u in get_url(f"league/{lid}/users")}
    league = get_url(f"league/{lid}")
    
    # Season production: sum of players_points over every completed week of the current season.
    state = get_url("state/nfl") or {}
    last_done = max(0, int(state.get("week") or 1) - 1)
    if state.get("season_type") in ("regular", "post") and state.get("week"):
        # /rosters misses a lineup saved for the week ahead: this week's matchups carry the real starters
        from sleeper_client import with_week_starters
        rosters = with_week_starters(rosters or [], get_url(f"league/{lid}/matchups/{state['week']}") or [])
    player_pts = {}
    for wk in range(1, last_done + 1):
        for m in get_url(f"league/{lid}/matchups/{wk}") or []:
            for pid, pts in (m.get("players_points") or {}).items():
                player_pts[str(pid)] = round(player_pts.get(str(pid), 0.0) + float(pts or 0.0), 2)

    return rosters, users, league, player_pts

def build_team_analysis(get=None):
    players_db, franchises = load_data()
    rosters, users, league, player_pts = fetch_sleeper_data(get)
    season = str(league.get("season") or "")

    ctx = league_ctx.current()
    profile = ctx.profile
    meta = league.get("metadata", {})
    div_names = {n: meta.get(f"division_{n}") or f"Divisão {n}" for n in range(1, max(profile.divisions, 1) + 1)}
    positions = list(profile.startable_ordered) or ["QB", "RB", "WR", "TE"]
    n_flex = profile.starter_slots.count("FLEX")

    teams_analysis = {}

    for r in rosters:
        rid = int(r["roster_id"])
        owner_id = r.get("owner_id")
        user = users.get(owner_id, {})
        u_meta = user.get("metadata", {})
        
        f_info = franchises.get(rid, {})
        canonical_manager = f_info.get("canonical_manager") or user.get("display_name") or f"Manager_{rid}"
        real_name = f_info.get("real_name", "")
        current_team = u_meta.get("team_name") or user.get("display_name") or f"Team {rid}"
        
        st = r.get("settings", {})
        div_id = st.get("division", 1)
        div_name = div_names.get(div_id, f"Divisão {div_id}")
        wins = st.get("wins", 0)
        losses = st.get("losses", 0)
        ties = st.get("ties", 0)
        fpts = float(f"{st.get('fpts', 0)}.{st.get('fpts_decimal', 0):02d}")
        waiver_budget_used = st.get("waiver_budget_used", 0)
        faab_remaining = int((league.get("settings") or {}).get("waiver_budget") or 100) - waiver_budget_used

        raw_starters = [str(p) for p in (r.get("starters") or []) if p and str(p) != "0"]
        raw_players = [str(p) for p in (r.get("players") or []) if p and str(p) != "0"]
        raw_taxi = [str(p) for p in (r.get("taxi") or []) if p and str(p) != "0"]
        raw_reserve = [str(p) for p in (r.get("reserve") or []) if p and str(p) != "0"]

        # Parse all players with details
        def get_p_info(pid):
            p = players_db.get(str(pid), {})
            pname = p.get("name", f"Player #{pid}")
            pos = p.get("pos") or p.get("position", "FLEX")
            team = p.get("team") or "FA"
            injury = _injury(p) or "Active"
            pts_season = player_pts.get(str(pid), 0.0)
            return {
                "player_id": str(pid),
                "name": pname,
                "position": pos,
                "team": team,
                "injury_status": injury,
                "season_pts": pts_season,
            }

        all_players_details = [get_p_info(pid) for pid in raw_players]
        taxi_details = [get_p_info(pid) for pid in raw_taxi]
        reserve_details = [get_p_info(pid) for pid in raw_reserve]

        # Starters actually used or ideal
        starters_details = [get_p_info(pid) for pid in raw_starters]
        starter_ids = set(raw_starters)
        taxi_ids = set(raw_taxi)
        reserve_ids = set(raw_reserve)

        # Bench players: in raw_players but not starters, taxi, or reserve
        bench_details = [p for p in all_players_details if p["player_id"] not in starter_ids and p["player_id"] not in taxi_ids and p["player_id"] not in reserve_ids]

        # Group bench by position
        bench_by_pos = {p: [] for p in positions}
        for b in bench_details:
            pos = b["position"]
            if pos in bench_by_pos:
                bench_by_pos[pos].append(b)
            else:
                bench_by_pos.setdefault(pos, []).append(b)

        # Sort bench by season_pts and prominence
        for pos in bench_by_pos:
            bench_by_pos[pos].sort(key=lambda x: x["season_pts"], reverse=True)

        # Identify Stars / Cornerstones (Tier 1)
        # Based on starters with high historical prominence or performance
        all_active_p = [p for p in all_players_details if p["player_id"] not in taxi_ids and p["player_id"] not in reserve_ids]
        all_active_p.sort(key=lambda x: x["season_pts"], reverse=True)
        top_performers = all_active_p[:4]

        # Analyze strengths and weaknesses
        pos_counts = {p: 0 for p in positions}
        for p in all_players_details:
            pos = p["position"]
            if pos in pos_counts:
                pos_counts[pos] += 1

        strengths = []
        weaknesses = []
        surplus_assets = []

        # QB situation
        starter_qbs = [p for p in starters_details if p["position"] == "QB"]
        if profile.superflex:
            if len(starter_qbs) >= 2 and all(q["team"] != "FA" for q in starter_qbs):
                strengths.append(f"Superflex consolidado com {', '.join([q['name'] for q in starter_qbs])}")
            elif len(starter_qbs) < 2:
                weaknesses.append("Carência crítica em QB2 para a vaga de Superflex")
        elif "QB" in positions and not starter_qbs:
            weaknesses.append("Sem QB titular definido")

        # RB situation
        bench_rbs = bench_by_pos.get("RB", [])
        if len(bench_rbs) >= 3 and any(r["season_pts"] > 8.0 for r in bench_rbs):
            strengths.append("Profundidade forte de Running Backs")
            # Surplus candidate
            for r in bench_rbs:
                if r["season_pts"] >= 7.0 or r["team"] != "FA":
                    surplus_assets.append(f"{r['name']} (RB - {r['team']}) [Reserva produtivo]")
        elif len(bench_rbs) < 2:
            weaknesses.append("Profundidade escassa em RB (falta de reservas de confiança)")

        # WR situation
        wr_count = pos_counts.get("WR", 0)
        bench_wrs = bench_by_pos.get("WR", [])
        if len(bench_wrs) >= 4:
            strengths.append(f"Arsenal vasto de Recebedores ({wr_count} WRs no elenco)")
            # Notice productive bench WRs! (like Chris Godwin)
            for w in bench_wrs:
                if w["season_pts"] >= 8.0 or w["team"] in ["TB", "KC", "LAR", "BUF", "GB", "HOU", "MIA", "PHI", "DET", "CIN"]:
                    surplus_assets.append(f"{w['name']} (WR - {w['team']}) [Reserva de luxo/flexível]")
        elif len(bench_wrs) < 2:
            weaknesses.append("Falta de profundidade em WR para cobrir " +
                              ((f"os {n_flex} slots de FLEX" if n_flex > 1 else "o slot de FLEX") if n_flex else "as vagas de titular"))

        # TE situation
        te_count = pos_counts.get("TE", 0)
        if "TE" not in positions:
            pass
        elif te_count >= 3:
            strengths.append("Setor de Tight Ends com alternativas")
        elif te_count <= 1:
            weaknesses.append("Dependência exclusiva do TE titular sem backup")

        # Determine competitive stance
        # Contender vs Rebuild
        if wins > 0 or fpts > 140.0 or len(strengths) >= 2:
            status = "Win-Now / Contender (Foco no Anel)"
        elif fpts < 110.0 and len(taxi_details) >= 3:
            status = "Rebuild / Creche de Calouros (Foco no Futuro)"
        else:
            status = "Fringe Playoff / Competidor Equilibrado"

        teams_analysis[rid] = {
            "roster_id": rid,
            "canonical_manager": canonical_manager,
            "real_name": real_name,
            "current_team": current_team,
            "division": div_name,
            "record": f"{wins}-{losses}-{ties}",
            "points_for": fpts,
            "faab_remaining": faab_remaining,
            "competitive_status": status,
            "starters": starters_details,
            "bench_by_position": bench_by_pos,
            "taxi_squad": taxi_details,
            "reserve_ir": reserve_details,
            "core_stars": top_performers,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "surplus_assets": surplus_assets[:4],
            "total_players": len(raw_players),
        }

    # Save JSON
    os.makedirs(_kb(), exist_ok=True)
    out_json = os.path.join(_kb(), "teams_rosters_analysis.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(teams_analysis, f, indent=2, ensure_ascii=False)

    # Generate Markdown Dossier
    md_lines = [
        f"# 🛡️ Dossiê Completo de Elencos e Equipes: {ctx.name} ({season})",
        "",
        "> [!NOTE]",
        f"> Este arquivo mapeia a composição técnica dos {len(teams_analysis)} elencos da liga:",
        f"> - Escalação titular ideal/provável ({profile.n_starters} posições: "
        + ", ".join(f"{n} {s}" for s, n in profile.lineup_requirements().items()) + ")",
        "> - Profundidade de banco, reservas de luxo, pilares intocáveis e carências críticas",
        "> - Utilizado como **contexto obrigatório** para análise de trocas e movimentações de waiver.",
        "",
        "---",
        "",
    ]

    # Sort teams by roster_id (1..12)
    for rid in sorted(teams_analysis.keys()):
        t = teams_analysis[rid]
        md_lines.extend([
            f"## 🏈 {t['current_team']} — Gestor: *{t['canonical_manager']}* ({t['real_name']})",
            f"- **Divisão**: {t['division']} | **Campanha {season}**: {t['record']} ({t['points_for']} PF) | **FAAB**: ${t['faab_remaining']}",
            f"- **Status Competitivo**: *{t['competitive_status']}*",
            f"- **Total de Atletas**: {t['total_players']} (Elenco Principal) + {len(t['taxi_squad'])} Taxi + {len(t['reserve_ir'])} IR",
            "",
            f"### 🌟 Escalação Titular Provável / Ideal ({profile.n_starters} Titulares):",
        ])

        for idx, s in enumerate(t["starters"], 1):
            md_lines.append(f"{idx}. **{s['name']}** ({s['position']} - {s['team']}) — _{s['injury_status']}_ [{s['season_pts']:.2f} pts]")

        md_lines.extend([
            "",
            "### 📦 Profundidade de Banco e Reservas Imediatos:",
        ])
        for pos in positions:
            b_list = t["bench_by_position"].get(pos, [])
            if b_list:
                items_str = ", ".join([f"{b['name']} ({b['team']})" for b in b_list])
                md_lines.append(f"- **{pos}s ({len(b_list)})**: {items_str}")
            else:
                md_lines.append(f"- **{pos}s**: _Sem reservas no banco_")

        if t["taxi_squad"]:
            t_str = ", ".join([f"{p['name']} ({p['position']} - {p['team']})" for p in t["taxi_squad"]])
            md_lines.append(f"- **Taxi Squad (Calouros/Stashes)**: {t_str}")

        if t["reserve_ir"]:
            r_str = ", ".join([f"{p['name']} ({p['position']} - {p['team']}) [{p['injury_status']}]" for p in t["reserve_ir"]])
            md_lines.append(f"- **Reserva por Lesão (IR)**: {r_str}")

        md_lines.extend([
            "",
            "### 🎯 Diagnóstico Técnico & Dinâmica de Mercado:",
            f"- **Pilares / Estrelas**: {', '.join([p['name'] + ' (' + p['position'] + ')' for p in t['core_stars']])}",
            f"- **Pontos Fortes**: {'; '.join(t['strengths']) if t['strengths'] else 'Elenco equilibrado'}",
            f"- **Carências Críticas**: {'; '.join(t['weaknesses']) if t['weaknesses'] else 'Sem carências graves imediatas'}",
            f"- **Ativos Negociáveis / Excedentes de Banco**: {', '.join(t['surplus_assets']) if t['surplus_assets'] else 'Sem excedentes óbvios (elenco enxuto)'}",
            "",
            "---",
            "",
        ])

    md_content = "\n".join(md_lines)
    out_md = os.path.join(_kb(), "TEAMS_AND_ROSTERS_ANALYSIS.md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"Sucesso! Gerados teams_rosters_analysis.json e TEAMS_AND_ROSTERS_ANALYSIS.md para os {len(teams_analysis)} times da liga.",
          file=sys.stderr)     # stdout is what a cron job would deliver: keep it empty

if __name__ == "__main__":
    build_team_analysis()
