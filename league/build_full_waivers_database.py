#!/usr/bin/env python3
"""Build Comprehensive Waivers & Free Agents Knowledge Base & 5-Tier Classification Engine.

Processes all 2,565 historical market transactions (2022-2026):
- 1,910 waivers (835 completed + 1,075 failed / outbid claims)
- 655 free agents
Total: 2,565 transactions.

Applies 6 Quality Tiers (0 to 5):
- Tier 5: 💎 Steal Lendário / Ouro Puro
- Tier 4: ⭐ Grande Achado / Starter Relevante
- Tier 3: 🛡️ Role Player Sólido / Contribuidor Regular
- Tier 2: 🩹 Tampão Emergencial / Backup Pontual
- Tier 1: 🎲 Especulação Fracassada / Stash Inócuo
- Tier 0: 🗑️ Bagre Total / Queima de FAAB

Generates:
- knowledge_base/waivers_market_classified.json
- knowledge_base/WAIVERS_AND_FA_DOSSIER.md
- waivers_market_classified.json (root)
- WAIVERS_AND_FA_DOSSIER.md (root)
"""

import urllib.request
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

BRT = ZoneInfo("America/Sao_Paulo")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(BASE_DIR, "knowledge_base")

SEASONS = [
    ("2022", "813915269117358080"),
    ("2023", "916296059507384320"),
    ("2024", "1048199280277835776"),
    ("2025", "1180282402537496576"),
    ("2026", "1312694161610113024"),
]

def load_references():
    import players_db as _pdb
    players_db = _pdb.load()

    with open(os.path.join(KB_DIR, "franchises_history.json"), "r", encoding="utf-8") as f:
        franchises_raw = json.load(f)
        franchises = {int(k): v for k, v in franchises_raw.items()}

    teams_analysis = {}
    analysis_file = os.path.join(KB_DIR, "teams_rosters_analysis.json")
    if os.path.exists(analysis_file):
        with open(analysis_file, "r", encoding="utf-8") as f:
            teams_analysis = json.load(f)

    return players_db, franchises, teams_analysis

def resolve_player(pid: str, players_db: Dict[str, Any]) -> Dict[str, str]:
    p = players_db.get(str(pid), {})
    name = p.get("name", f"Player #{pid}")
    pos = p.get("pos") or p.get("position", "FLEX")
    team = p.get("team") or "NFL"
    return {
        "player_id": str(pid),
        "name": name,
        "position": pos,
        "team": team,
        "label": f"{name} ({pos} - {team})"
    }

def fetch_all_transactions():
    cache_path = os.path.join(BASE_DIR, "all_sleeper_waivers_raw_2565.json")
    cached_txs = []
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached_txs = json.load(f)
        except Exception:
            cached_txs = []

    # Se não temos cache histórico, buscamos todas as temporadas
    if not cached_txs:
        seasons_to_fetch = SEASONS
    else:
        # Temos cache: apenas atualizamos a temporada atual 2026!
        seasons_to_fetch = [s for s in SEASONS if s[0] == "2026"]

    fetched_new = []
    for yr, lid in seasons_to_fetch:
        for w in range(1, 19):
            url = f"https://api.sleeper.app/v1/league/{lid}/transactions/{w}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    txs = json.loads(resp.read().decode("utf-8"))
                    for t in txs:
                        typ = t.get("type")
                        status = t.get("status")
                        if typ in ["waiver", "free_agent"] and status in ["complete", "failed"]:
                            t["_season"] = yr
                            t["_week"] = w
                            fetched_new.append(t)
            except Exception:
                pass

    # Combinar histórico em cache + novos fetches deduplicando por transaction_id
    dedup = {}
    for t in cached_txs:
        tid = str(t.get("transaction_id"))
        if tid:
            dedup[tid] = t
    for t in fetched_new:
        tid = str(t.get("transaction_id"))
        if tid:
            dedup[tid] = t

    all_raw = list(dedup.values())
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(all_raw, f, indent=2)

    return all_raw

def classify_transaction(t: Dict[str, Any], players_db: Dict[str, Any], franchises: Dict[int, Any], teams_analysis: Dict[str, Any], all_starters_2026: set, all_bench_2026: set, all_core_2026: set, all_rostered_2026: set) -> Dict[str, Any]:
    tx_id = str(t.get("transaction_id"))
    typ = t.get("type", "waiver")
    status = t.get("status", "complete")
    season = t.get("_season", "2026")
    week = t.get("_week", 1)
    
    created_ts = t.get("created", 0) / 1000.0
    dt = datetime.fromtimestamp(created_ts, tz=BRT)
    date_str = dt.strftime("%d/%m/%Y %H:%M")

    roster_ids = t.get("roster_ids", [])
    rid = roster_ids[0] if roster_ids else None
    
    # Manager e time
    f_info = franchises.get(rid, {}) if rid else {}
    canonical_mgr = f_info.get("canonical_manager") or f"Manager_{rid}"
    real_name = f_info.get("real_name", "")
    team_name = f_info.get("seasons", {}).get(season, {}).get("team_name") or f_info.get("current_team", f"Time {rid}")

    bid = int((t.get("settings") or {}).get("waiver_bid", 0)) if typ == "waiver" else 0

    adds = []
    for pid in (t.get("adds") or {}).keys():
        adds.append(resolve_player(pid, players_db))

    drops = []
    for pid in (t.get("drops") or {}).keys():
        drops.append(resolve_player(pid, players_db))

    # Algoritmo de Classificação de Qualidade (Tiers 0 a 5)
    quality_tier = 2
    tier_badge = "Tier 2: 🩹 Tampão Emergencial"
    quality_reason = "Movimentação de profundidade pontual de elenco."

    if status == "failed":
        quality_tier = 1
        tier_badge = "Disputa Perdida (Outbid / Falha)"
        quality_reason = f"Tentativa de claim com lance de ${bid} FAAB que foi superada na ordem ou orçamento."
    else:
        # Status complete
        p_main = adds[0] if adds else None
        p_main_id = p_main["player_id"] if p_main else None

        if p_main_id:
            # Checar status em 2026
            is_starter = p_main_id in all_starters_2026
            is_core = p_main_id in all_core_2026
            is_bench = p_main_id in all_bench_2026
            is_rostered = p_main_id in all_rostered_2026

            if is_core or (is_starter and bid <= 10):
                quality_tier = 5
                tier_badge = "Tier 5: 💎 Steal Lendário"
                quality_reason = f"{p_main['name']} virou pilar/titular absoluto em 2026 com custo de ${bid} FAAB; retorno monumental."
            elif is_starter:
                quality_tier = 4
                tier_badge = "Tier 4: ⭐ Grande Achado"
                quality_reason = f"{p_main['name']} conquistou vaga de titular provável da equipe; produção de alto nível."
            elif is_bench:
                quality_tier = 3
                tier_badge = "Tier 3: 🛡️ Role Player Sólido"
                quality_reason = f"{p_main['name']} permanece ativo no elenco em 2026 como reserva imediato e flex de profundidade."
            elif is_rostered:
                quality_tier = 2
                tier_badge = "Tier 2: 🩹 Stash / Profundidade"
                quality_reason = f"{p_main['name']} segue retido em elenco/taxi squad como aposta complementar."
            else:
                # Não está em nenhum elenco em 2026 (virou descarte)
                if bid >= 20:
                    quality_tier = 0
                    tier_badge = "Tier 0: 🗑️ Bagre Total / Queima de FAAB"
                    quality_reason = f"Desperdício pesado de ${bid} FAAB em {p_main['name']}, que não se firmou e virou lixo tóxico."
                elif bid >= 10:
                    quality_tier = 1
                    tier_badge = "Tier 1: 🎲 Especulação Fracassada"
                    quality_reason = f"Investimento de ${bid} FAAB em {p_main['name']} que não gerou retorno sustentável."
                else:
                    quality_tier = 2
                    tier_badge = "Tier 2: 🩹 Tampão Emergencial"
                    quality_reason = f"Adição barata (${bid} FAAB) de {p_main['name']} para tapar buraco pontual de rodada."

    return {
        "transaction_id": tx_id,
        "season": season,
        "week": week,
        "date": date_str,
        "created_ts": created_ts,
        "type": typ,
        "status": status,
        "roster_id": rid,
        "manager": canonical_mgr,
        "real_name": real_name,
        "team_name": team_name,
        "bid": bid,
        "quality_tier": quality_tier,
        "tier_badge": tier_badge,
        "quality_reason": quality_reason,
        "adds": adds,
        "drops": drops,
    }

def build_waivers_database():
    players_db, franchises, teams_analysis = load_references()

    # Mapear conjuntos de atletas em 2026
    all_starters_2026 = set()
    all_bench_2026 = set()
    all_core_2026 = set()
    all_rostered_2026 = set()

    for rid_str, t_data in teams_analysis.items():
        for s in t_data.get("starters", []):
            pid = str(s["player_id"])
            all_starters_2026.add(pid)
            all_rostered_2026.add(pid)
        for c in t_data.get("core_stars", []):
            pid = str(c["player_id"])
            all_core_2026.add(pid)
            all_rostered_2026.add(pid)
        for b_list in t_data.get("bench_by_position", {}).values():
            for b in b_list:
                pid = str(b["player_id"])
                all_bench_2026.add(pid)
                all_rostered_2026.add(pid)
        for tp in t_data.get("taxi_squad", []):
            pid = str(tp["player_id"])
            all_rostered_2026.add(pid)
        for ip in t_data.get("reserve_ir", []):
            pid = str(ip["player_id"])
            all_rostered_2026.add(pid)

    raw_txs = fetch_all_transactions()
    
    # Classificar todas as transações
    classified_txs = []
    for t in raw_txs:
        c = classify_transaction(t, players_db, franchises, teams_analysis, all_starters_2026, all_bench_2026, all_core_2026, all_rostered_2026)
        classified_txs.append(c)

    # Ordenar por timestamp decrescente (mais recentes primeiro)
    classified_txs.sort(key=lambda x: x["created_ts"], reverse=True)

    # Métricas agregadas por Manager
    manager_stats = {}
    for t in classified_txs:
        mgr = t["manager"]
        if mgr not in manager_stats:
            manager_stats[mgr] = {
                "manager": mgr,
                "real_name": t["real_name"],
                "team_name": t["team_name"],
                "total_moves": 0,
                "waivers_won": 0,
                "waivers_failed": 0,
                "free_agents": 0,
                "total_faab_spent": 0,
                "tier_counts": {0: 0, 1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
                "steals": [],
                "fiascos": [],
            }
        ms = manager_stats[mgr]
        if t["status"] == "complete":
            ms["total_moves"] += 1
            ms["total_faab_spent"] += t["bid"]
            if t["type"] == "waiver":
                ms["waivers_won"] += 1
            else:
                ms["free_agents"] += 1
            
            q = t["quality_tier"]
            ms["tier_counts"][q] = ms["tier_counts"].get(q, 0) + 1
            
            if q == 5:
                p_name = t["adds"][0]["label"] if t["adds"] else "Atleta"
                ms["steals"].append(f"{p_name} (${t['bid']} FAAB, {t['season']} W{t['week']})")
            elif q == 0:
                p_name = t["adds"][0]["label"] if t["adds"] else "Atleta"
                ms["fiascos"].append(f"{p_name} (${t['bid']} FAAB, {t['season']} W{t['week']})")
        else:
            ms["waivers_failed"] += 1

    # Calcular médias
    for ms in manager_stats.values():
        w_total = ms["waivers_won"] + ms["waivers_failed"]
        ms["waiver_success_rate"] = round((ms["waivers_won"] / w_total * 100.0) if w_total > 0 else 0.0, 1)
        ms["avg_faab_per_waiver"] = round((ms["total_faab_spent"] / ms["waivers_won"]) if ms["waivers_won"] > 0 else 0.0, 1)

    # Top Steals da História (Tier 5)
    all_steals = [t for t in classified_txs if t["quality_tier"] == 5 and t["status"] == "complete"]
    all_steals.sort(key=lambda x: (x["bid"], -x["created_ts"]))

    # Top Fiascos de FAAB (Tier 0)
    all_fiascos = [t for t in classified_txs if t["quality_tier"] == 0 and t["status"] == "complete"]
    all_fiascos.sort(key=lambda x: -x["bid"])

    # Top Maiores Lances de FAAB da História
    all_biggest_bids = [t for t in classified_txs if t["status"] == "complete" and t["bid"] > 0]
    all_biggest_bids.sort(key=lambda x: -x["bid"])

    # Salvar JSON estruturado
    full_output = {
        "summary": {
            "total_transactions": len(classified_txs),
            "waivers_total": sum(1 for t in classified_txs if t["type"] == "waiver"),
            "waivers_complete": sum(1 for t in classified_txs if t["type"] == "waiver" and t["status"] == "complete"),
            "waivers_failed": sum(1 for t in classified_txs if t["type"] == "waiver" and t["status"] == "failed"),
            "free_agents_complete": sum(1 for t in classified_txs if t["type"] == "free_agent"),
            "total_faab_spent_league": sum(t["bid"] for t in classified_txs if t["status"] == "complete"),
            "last_updated": datetime.now(BRT).isoformat(),
        },
        "manager_profiles": manager_stats,
        "transactions": classified_txs,
    }

    json_path = os.path.join(KB_DIR, "waivers_market_classified.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(full_output, f, indent=2, ensure_ascii=False)

    print(f"Banco estruturado salvo em {json_path} ({len(classified_txs)} transações)!")

    # Gerar Markdown Dossier
    md_lines = [
        "# 📡 Dossiê Histórico de Mercado: Waivers & Free Agents (2022–2026)",
        "",
        "> [!NOTE]",
        f"> Base de conhecimento auditada de **{len(classified_txs):,} movimentações de mercado** na High Stakes Fantasy:",
        f"> - **Waivers Processadas**: {full_output['summary']['waivers_total']:,} ({full_output['summary']['waivers_complete']:,} completadas e {full_output['summary']['waivers_failed']:,} disputas perdidas/falhas)",
        f"> - **Free Agents Adicionados**: {full_output['summary']['free_agents_complete']:,}",
        f"> - **FAAB Total Queimado na Liga**: ${full_output['summary']['total_faab_spent_league']:,}",
        "> - **Escala de Qualidade**: Tier 5 (Steal Lendário) a Tier 0 (Bagre Total / Queima de FAAB)",
        "",
        "---",
        "",
        "## 💎 Top 15 Steals Lendários da História (Tier 5 — Ouro no Lixo)",
        "Atletas reivindicados no waiver ou free agent que se transformaram em pilares da franquia ou titulares absolutos em 2026:",
        "",
    ]

    for idx, st in enumerate(all_steals[:15], 1):
        p_name = st["adds"][0]["label"] if st["adds"] else "Atleta"
        md_lines.append(
            f"{idx}. **{p_name}** ➔ **{st['manager']}** ({st['team_name']}) | Custo: **${st['bid']} FAAB** ({st['season']} - Sem. {st['week']})  \n"
            f"   _Diagnóstico_: {st['quality_reason']}"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "## 🗑️ Top 15 Piores Queimas de FAAB da História (Tier 0 — Bagre Total)",
        "Os maiores gastos de orçamento de waivers em atletas que não duraram no elenco e viraram puro lixo tóxico:",
        "",
    ])

    for idx, fsc in enumerate(all_fiascos[:15], 1):
        p_name = fsc["adds"][0]["label"] if fsc["adds"] else "Atleta"
        drop_name = f", dropando {fsc['drops'][0]['name']}" if fsc["drops"] else ""
        md_lines.append(
            f"{idx}. **{p_name}** ➔ **{fsc['manager']}** ({fsc['team_name']}) | Desperdiçou: **${fsc['bid']} FAAB** ({fsc['season']} - Sem. {fsc['week']}){drop_name}  \n"
            f"   _Diagnóstico_: {fsc['quality_reason']}"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "## 💰 Top 10 Maiores Lances de FAAB da História da Liga",
        "",
    ])

    for idx, b in enumerate(all_biggest_bids[:10], 1):
        p_name = b["adds"][0]["label"] if b["adds"] else "Atleta"
        md_lines.append(
            f"{idx}. **${b['bid']} FAAB**: **{p_name}** arrematado por **{b['manager']}** ({b['team_name']}) — _{b['season']} Sem. {b['week']}_ [{b['tier_badge']}]"
        )

    md_lines.extend([
        "",
        "---",
        "",
        "## 📊 Raio-X de Mercado por Treinador (Comportamento na Varredura do Lixo)",
        "",
    ])

    # Ordenar managers por total de movimentações
    sorted_managers = sorted(manager_stats.values(), key=lambda x: x["total_moves"], reverse=True)
    for ms in sorted_managers:
        tc = ms["tier_counts"]
        steals_str = "; ".join(ms["steals"][:3]) if ms["steals"] else "Nenhum steal lendário (Tier 5)"
        fiascos_str = "; ".join(ms["fiascos"][:3]) if ms["fiascos"] else "Sem queimas graves de FAAB (Tier 0)"
        md_lines.extend([
            f"### 🏈 {ms['manager']} ({ms['real_name']}) — *{ms['team_name']}*",
            f"- **Volume Total**: {ms['total_moves']} adições ({ms['waivers_won']} waivers + {ms['free_agents']} free agents) | {ms['waivers_failed']} lances perdidos",
            f"- **Eficiência em Disputas**: {ms['waiver_success_rate']}% de sucesso | FAAB Total Gasto: **${ms['total_faab_spent']}** (Média: ${ms['avg_faab_per_waiver']}/waiver)",
            f"- **Distribuição por Tier**: 💎 Tier 5: {tc.get(5, 0)} | ⭐ Tier 4: {tc.get(4, 0)} | 🛡️ Tier 3: {tc.get(3, 0)} | 🩹 Tier 2: {tc.get(2, 0)} | 🎲 Tier 1: {tc.get(1, 0)} | 🗑️ Tier 0: {tc.get(0, 0)}",
            f"- **Maiores Steals**: {steals_str}",
            f"- **Piores Queimas de FAAB**: {fiascos_str}",
            "",
        ])

    md_lines.extend([
        "---",
        "",
        "⚡ _Base gerada automaticamente via API Sleeper com classificação 5-Tier (High Stakes Fantasy)_",
    ])

    dossier_content = "\n".join(md_lines)
    dossier_path = os.path.join(KB_DIR, "WAIVERS_AND_FA_DOSSIER.md")
    with open(dossier_path, "w", encoding="utf-8") as f:
        f.write(dossier_content)

    print(f"Dossiê Markdown salvo em {dossier_path}!")

if __name__ == "__main__":
    build_waivers_database()
