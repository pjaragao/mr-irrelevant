#!/usr/bin/env python3
"""Draft Order and Max PF (Potential Points For) Engine.

Some leagues order the rookie draft by MAX PF (best-ball potential points, ascending) to remove the incentive to
tank: a house rule Sleeper does not model, switched on per league with `rules: {draft_order: max_pf}` in league.yaml.
Without the rule the order is Sleeper's own (see `format_sleeper_draft_order`). The weekly Max PF itself is the
optimal lineup of THIS league's starting slots (league_profile.optimal_lineup), so it works for any structure.

This engine:
1. Calculates Season Max PF (ppts) and Weekly Max PF (optimal lineup from bench+starters).
2. Maps all traded draft picks (future drafts: 2027, 2028).
3. Produces the official projected Draft Order (Round 1 & Round 2) indicating original vs current owners.
4. Updates the local knowledge base (DRAFT_ORDER_AND_MAX_PF.md) after every round.
"""

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

import league_ctx

logger = logging.getLogger("DraftOrder")


def _api() -> str:
    return os.environ.get("SLEEPER_API_BASE", "https://api.sleeper.app/v1").rstrip("/")


def next_draft_season() -> str:
    """The rookie draft is held after the current season (current season + 1)."""
    try:
        from sleeper_client import SleeperClient
        return str(int(SleeperClient().get_nfl_state().get("season")) + 1)
    except Exception:
        from datetime import datetime
        return str(datetime.now().year + 1)


def _curl_get(url: str) -> Any:
    try:
        out = subprocess.check_output(["curl", "-4", "-s", "--max-time", "6", url])
        if not out:
            return None
        return json.loads(out)
    except Exception as e:
        print(f"[WARN] _curl_get failed for {url}: {e}", file=sys.stderr)
        return None


class DraftOrderService:
    def __init__(self, league_id: Optional[str] = None, base_dir: Optional[str] = None):
        self.ctx = league_ctx.current()
        self.league_id = league_id or self.ctx.league_id
        if base_dir is None:       # normal use: this league's knowledge base, the shared NFL players master
            self.base_dir = self.ctx.shared_dir
            self.kb_dir = self.ctx.kb_dir
        else:                      # tests/tools: everything under one directory
            self.base_dir = base_dir
            self.kb_dir = os.path.join(base_dir, "knowledge_base")
        self._players_db = None

    def rounds(self) -> List[int]:
        """Rounds the Max PF order decides: league.yaml `rules.max_pf_rounds`, by default the first two."""
        r = self.ctx.rule("max_pf_rounds")
        return [int(x) for x in r] if r else [1, 2]

    def uses_max_pf(self) -> bool:
        return str(self.ctx.rule("draft_order") or "").lower() == "max_pf"

    def _get_players_db(self) -> Dict[str, Any]:
        if self._players_db is not None:
            return self._players_db
        p_path = os.path.join(self.base_dir, "master_nfl_players.json")
        if os.path.exists(p_path):
            try:
                with open(p_path, "r", encoding="utf-8") as f:
                    self._players_db = json.load(f)
                    return self._players_db
            except Exception:
                pass
        self._players_db = {}
        return self._players_db

    def get_league_info(self) -> Dict[str, Any]:
        url = f"{_api()}/league/{self.league_id}"
        data = _curl_get(url)
        return data or {"name": self.ctx.name, "season": str(int(next_draft_season()) - 1)}

    def get_users_dict(self) -> Dict[str, Dict[str, Any]]:
        url = f"{_api()}/league/{self.league_id}/users"
        data = _curl_get(url) or []
        return {u["user_id"]: u for u in data}

    def get_rosters_list(self) -> List[Dict[str, Any]]:
        url = f"{_api()}/league/{self.league_id}/rosters"
        return _curl_get(url) or []

    def get_traded_picks_raw(self) -> List[Dict[str, Any]]:
        url = f"{_api()}/league/{self.league_id}/traded_picks"
        return _curl_get(url) or []

    def get_teams_metadata(self) -> Dict[int, Dict[str, Any]]:
        """Map roster_id -> {owner_name, team_name, roster_id, user_id}."""
        users = self.get_users_dict()
        rosters = self.get_rosters_list()
        res = {}
        for r in rosters:
            rid = r.get("roster_id")
            oid = r.get("owner_id")
            u = users.get(oid, {})
            display_name = u.get("display_name") or f"Owner {rid}"
            meta = u.get("metadata") or {}
            team_name = meta.get("team_name") or u.get("display_name") or f"Time {rid}"
            res[rid] = {
                "roster_id": rid,
                "owner_id": oid,
                "owner_name": display_name,
                "team_name": team_name,
            }
        return res

    def get_max_pf_standings(self) -> List[Dict[str, Any]]:
        """Return list of teams sorted by Max PF (ppts) in ASCENDING order (lowest first = Pick 1)."""
        rosters = self.get_rosters_list()
        teams_meta = self.get_teams_metadata()

        results = []
        for r in rosters:
            rid = r.get("roster_id")
            meta = teams_meta.get(rid, {})
            st = r.get("settings") or {}

            fpts = float(f"{st.get('fpts', 0)}.{st.get('fpts_decimal', 0):02d}")
            ppts = float(f"{st.get('ppts', 0)}.{st.get('ppts_decimal', 0):02d}")
            wins = st.get("wins", 0)
            losses = st.get("losses", 0)
            ties = st.get("ties", 0)

            # Bench points left behind: ppts - fpts
            bench_pts = max(0.0, round(ppts - fpts, 2))
            efficiency = round((fpts / ppts * 100.0), 1) if ppts > 0 else 0.0

            results.append({
                "roster_id": rid,
                "owner_name": meta.get("owner_name", f"Manager {rid}"),
                "team_name": meta.get("team_name", f"Time {rid}"),
                "max_pf": ppts,
                "real_pf": fpts,
                "bench_pts": bench_pts,
                "efficiency": efficiency,
                "record": f"{wins}-{losses}" + (f"-{ties}" if ties > 0 else ""),
            })

        # Sort strictly ASCENDING by max_pf (lowest potential points gets pick 1)
        # Tie-breaker: lowest real_pf
        results.sort(key=lambda x: (x["max_pf"], x["real_pf"]))
        for rank, item in enumerate(results, 1):
            item["draft_slot"] = rank
        return results

    def calculate_weekly_max_pf(self, week: int) -> List[Dict[str, Any]]:
        """Calculate optimal theoretical maximum (Best Ball) points for all teams in a specific week."""
        url = f"{_api()}/league/{self.league_id}/matchups/{week}"
        m_list = _curl_get(url) or []
        if not m_list:
            return []

        profile = self.ctx.profile
        players_db = self._get_players_db()
        teams_meta = self.get_teams_metadata()

        results = []
        for m in m_list:
            rid = m.get("roster_id")
            real_pts = float(m.get("points") or 0.0)
            players_pts = m.get("players_points") or {}

            player_scores = []
            for pid, pts in players_pts.items():
                pdata = players_db.get(str(pid), {})
                pos = pdata.get("position") or pdata.get("pos") or "FLEX"
                pname = pdata.get("full_name") or pdata.get("name") or str(pid)
                player_scores.append({"pid": str(pid), "name": pname, "pos": pos, "pts": float(pts)})

            total_max, _lineup = profile.optimal_lineup(player_scores)   # this league's slots, not a fixed shape
            meta = teams_meta.get(rid, {})

            bench_wasted = max(0.0, round(total_max - real_pts, 2))
            eff = round((real_pts / total_max * 100.0), 1) if total_max > 0 else 0.0

            results.append({
                "roster_id": rid,
                "owner_name": meta.get("owner_name", f"Manager {rid}"),
                "team_name": meta.get("team_name", f"Time {rid}"),
                "week": week,
                "max_pf": total_max,
                "real_pf": real_pts,
                "bench_pts": bench_wasted,
                "efficiency": eff,
            })

        results.sort(key=lambda x: x["max_pf"], reverse=True)
        return results

    def build_draft_board(self, season: Optional[str] = None, rounds: Optional[List[int]] = None) -> Dict[str, Any]:
        """Build complete projected rookie draft board for the next draft."""
        season = season or next_draft_season()
        rounds = rounds or self.rounds()
        standings = self.get_max_pf_standings()
        n_teams = len(standings) or self.ctx.profile.total_rosters or 12
        teams_meta = self.get_teams_metadata()
        traded_raw = self.get_traded_picks_raw()

        # Filter traded picks for target season
        traded_map: Dict[Tuple[int, int], int] = {}
        for tp in traded_raw:
            if str(tp.get("season")) == str(season):
                rnd = tp.get("round")
                orig_rid = tp.get("roster_id")
                curr_rid = tp.get("owner_id")
                if rnd and orig_rid and curr_rid:
                    traded_map[(int(rnd), int(orig_rid))] = int(curr_rid)

        board_by_round = {}
        manager_picks_count: Dict[str, Dict[str, int]] = {}

        for rnd in rounds:
            round_picks = []
            for item in standings:
                slot = item["draft_slot"]  # 1..number of teams
                orig_rid = item["roster_id"]
                orig_owner = item["owner_name"]
                orig_team = item["team_name"]
                orig_max_pf = item["max_pf"]

                # Check if this pick was traded
                curr_rid = traded_map.get((rnd, orig_rid), orig_rid)
                is_traded = (curr_rid != orig_rid)

                curr_meta = teams_meta.get(curr_rid, {})
                curr_owner = curr_meta.get("owner_name", f"Owner {curr_rid}")
                curr_team = curr_meta.get("team_name", f"Time {curr_rid}")

                pick_number = f"{rnd}.{slot:02d}"
                overall_pick = (rnd - 1) * n_teams + slot

                round_picks.append({
                    "pick": pick_number,
                    "overall_pick": overall_pick,
                    "round": rnd,
                    "slot": slot,
                    "original_roster_id": orig_rid,
                    "original_owner": orig_owner,
                    "original_team": orig_team,
                    "original_max_pf": orig_max_pf,
                    "current_roster_id": curr_rid,
                    "current_owner": curr_owner,
                    "current_team": curr_team,
                    "is_traded": is_traded,
                })

                # Tally draft capital
                if curr_owner not in manager_picks_count:
                    manager_picks_count[curr_owner] = {"r1": 0, "r2": 0, "total": 0, "team_name": curr_team}
                if rnd == 1:
                    manager_picks_count[curr_owner]["r1"] += 1
                elif rnd == 2:
                    manager_picks_count[curr_owner]["r2"] += 1
                manager_picks_count[curr_owner]["total"] += 1

            board_by_round[rnd] = round_picks

        return {
            "season": season,
            "board": board_by_round,
            "max_pf_standings": standings,
            "manager_picks_count": manager_picks_count,
        }

    def format_sleeper_draft_order(self, season: Optional[str] = None) -> str:
        """Draft order of leagues that do NOT order by Max PF: the slots Sleeper holds for the upcoming draft."""
        drafts = _curl_get(f"{_api()}/league/{self.league_id}/drafts") or []
        pending = [d for d in drafts if str(d.get("status")) in ("pre_draft", "drafting")]
        pick = (pending or sorted(drafts, key=lambda d: str(d.get("season")), reverse=True) or [None])[0]
        if not pick:
            return "ℹ️ Esta liga ainda não tem draft criado na Sleeper."
        slots = {int(k): v for k, v in (pick.get("slot_to_roster_id") or {}).items() if v}
        teams = self.get_teams_metadata()
        lines = [f"🎯 *ORDEM DO DRAFT {pick.get('season') or season or ''} (SLEEPER)*", "━━━━━━━━━━━━━━━━━━━━━━"]
        if not slots:
            lines.append("A ordem ainda não foi definida na Sleeper (sorteio/classificação pendente).")
        for slot in sorted(slots):
            t = teams.get(int(slots[slot]), {})
            lines.append(f"  *1.{slot:02d}*: *{t.get('team_name', f'Time {slots[slot]}')}* ({t.get('owner_name', '?')})")
        st = str(pick.get("status") or "")
        lines += ["", f"_Status do draft: {st or 'indefinido'} · {self.ctx.persona_name}_"]
        return "\n".join(lines)

    def format_draft_order_whatsapp(self, season: Optional[str] = None) -> str:
        """Format the high-impact WhatsApp card for Tuesday morning dispatch."""
        season = season or next_draft_season()
        if not self.uses_max_pf():
            return self.format_sleeper_draft_order(season)
        data = self.build_draft_board(season=season, rounds=[1, 2])
        board_r1 = data["board"].get(1, [])
        board_r2 = data["board"].get(2, [])
        standings = data["max_pf_standings"]
        counts = data["manager_picks_count"]

        lines = [
            f"🎯 *ORDEM PROJETADA DO DRAFT {season} (CRITÉRIO MAX PF)* 🎯",
            f"📅 *Semana em Andamento — Projeção Oficial da Ordem das Escolhas*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🔥 *ROUND 1 (PRIMEIRA RODADA — DRAFT {season}):*",
        ]

        for p in board_r1:
            pick_str = p["pick"]
            if p["is_traded"]:
                lines.append(
                    f"  *{pick_str}*: *{p['current_team']}* ({p['current_owner']}) 🔄 "
                    f"_[Origem: {p['original_team']} / {p['original_owner']} — Max PF: {p['original_max_pf']:.2f}]_"
                )
            else:
                lines.append(
                    f"  *{pick_str}*: *{p['current_team']}* ({p['current_owner']}) "
                    f"_[Pick Própria — Max PF: {p['original_max_pf']:.2f}]_"
                )

        lines.extend([
            "",
            f"📦 *ROUND 2 (SEGUNDA RODADA — DRAFT {season}):*",
        ])

        for p in board_r2:
            pick_str = p["pick"]
            if p["is_traded"]:
                lines.append(
                    f"  *{pick_str}*: *{p['current_team']}* ({p['current_owner']}) 🔄 "
                    f"_[Origem: {p['original_team']}]_"
                )
            else:
                lines.append(
                    f"  *{pick_str}*: *{p['current_team']}* ({p['current_owner']}) _[Pick Própria]_"
                )

        # Destacar os Tubarões do Draft (quem tem mais escolhas de Round 1)
        top_hoarders = sorted(counts.items(), key=lambda x: (x[1]["r1"], x[1]["total"]), reverse=True)
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "💰 *CAPITAL DE DRAFT & TUBARÕES DAS PICKS (ROUND 1):*",
        ])
        for owner, c in top_hoarders:
            if c["r1"] > 1:
                lines.append(f"• 🦈 *{owner}* ({c['team_name']}): *{c['r1']} escolhas de 1ª rodada* no Draft {season}!")
            elif c["r1"] == 0:
                lines.append(f"• 💸 *{owner}*: Sem escolhas de Round 1 em {season} (vendeu o futuro!).")

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ _{self.ctx.persona_name}: Se o seu time está na rabeira do Max PF, comemore: a Pick 1.01 é sua... a menos que você já tenha trocado ela por um saco de pão de queijo! Às terças de manhã o card é atualizado rodada a rodada!_ 📉🎯"
        ])
        return "\n".join(lines)

    def format_weekly_max_pf_whatsapp(self, week: Optional[int] = None) -> str:
        """Format weekly Max PF breakdown with wasted points on bench."""
        if week is None:
            # default to last concluded week
            state = _curl_get(f"{_api()}/state/nfl") or {}
            week = max(1, state.get("week", 3) - 1)

        data = self.calculate_weekly_max_pf(week)
        if not data:
            return f"Nenhum dado de Max PF encontrado para a Semana {week}."

        lines = [
            f"🧠 *MAX PF (POTENCIAL MÁXIMO TEÓRICO) — SEMANA {week}* 🧠",
            "📅 *Ranking de Eficiência e Pontos Desperdiçados no Banco*",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        top_max = data[0]
        worst_max = data[-1]
        lines.extend([
            f"👑 *Teto Máximo da Rodada:* *{top_max['team_name']}* ({top_max['owner_name']}) com brutais *{top_max['max_pf']:.2f} Max PF*!",
            f"🥔 *Piso Mais Baixo:* *{worst_max['team_name']}* ({worst_max['owner_name']}) somou apenas *{worst_max['max_pf']:.2f} Max PF* considerando todo o elenco.",
            "",
            "📊 *TABELA COMPLETA DA SEMANA (MAX PF vs ESCALAÇÃO REAL):*",
        ])

        for idx, t in enumerate(data, 1):
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            ic = medals.get(idx, f"{idx:2d}.")
            lines.append(
                f"{ic} *{t['team_name']}* ({t['owner_name']}): *{t['max_pf']:.2f} pts* "
                f"| Real: {t['real_pf']:.2f} | Banco: *+{t['bench_pts']:.2f}* (Eficiência: {t['efficiency']}%)"
            )

        # Maior "burro" da rodada (deixou mais pontos no banco)
        wasted_sorted = sorted(data, key=lambda x: x["bench_pts"], reverse=True)
        biggest_blunder = wasted_sorted[0]
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🤡 *Troféu 'Professor Pardal' (Mais pontos mofando no banco):* *{biggest_blunder['team_name']}* ({biggest_blunder['owner_name']}) deixou absurdos *+{biggest_blunder['bench_pts']:.2f} pontos* no banco de reservas!",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ _{self.ctx.persona_name}: Escalar o titular certo é uma arte; deixar 40 pontos no banco é a especialidade de metade dessa liga!_ 🤷‍♂️"
        ])
        return "\n".join(lines)

    def sync_draft_knowledge_base(self, season: Optional[str] = None) -> str:
        """Generate and save DRAFT_ORDER_AND_MAX_PF.md in knowledge_base and root."""
        season = season or next_draft_season()
        data = self.build_draft_board(season=season, rounds=[1, 2])
        board_r1 = data["board"].get(1, [])
        board_r2 = data["board"].get(2, [])
        standings = data["max_pf_standings"]
        counts = data["manager_picks_count"]

        md_lines = [
            f"# 🎯 Ordem Oficial do Draft {season} & Dossiê Max PF: {self.ctx.name}",
            "",
            f"**Temporada Vigente**: {int(season) - 1} | **Critério Oficial de Ordem**: Max PF (Ascendente / Anti-Tanking)",
            "",
            "---",
            "",
            "## 📖 1. O que é o Max PF e Como Funciona na Liga?",
            "",
            "- **Definição**: O **Max PF (Maximum Potential Points For)** representa a pontuação máxima teórica que o elenco de uma franquia poderia ter atingido em cada rodada caso tivesse escalado a escalação perfeita (Best Ball), computando tanto os titulares quanto os reservas do banco.",
            "- **Finalidade Suprema (Anti-Tanking)**: Evita que um manager perca jogos de propósito escalando reservas ou lesionados para forçar uma escolha melhor no draft. Não importa se você escala um jogador de 0 pontos; se você tem no banco um atleta que fez 25 pontos, esses 25 pontos contam integralmente no seu Max PF!",
            "- **Regra de Ordem**: O time com o **menor Max PF acumulado da temporada regular** ganha a **Pick 1.01** do próximo draft rookie. O time com o maior Max PF escolhe por último (Pick 1.12).",
            "",
            "---",
            "",
            f"## 🏆 2. Classificação de Max PF Acumulado (Temporada {int(season) - 1})",
            "",
            "| Draft Slot | Franquia | Manager | Max PF Acumulado | PF Real | Pontos no Banco | Eficiência (%) | Campanha |",
            "| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
        ]

        for s in standings:
            md_lines.append(
                f"| **{s['draft_slot']:02d}** | {s['team_name']} | {s['owner_name']} | **{s['max_pf']:.2f}** | {s['real_pf']:.2f} | +{s['bench_pts']:.2f} | {s['efficiency']}% | {s['record']} |"
            )

        md_lines.extend([
            "",
            "---",
            "",
            f"## 🎲 3. Ordem Projetada do Draft {season} — Round 1",
            "",
            "| Pick | Dono Atual da Escolha | Origem da Escolha (Natural Slot) | Status | Max PF da Franquia de Origem |",
            "| :---: | :--- | :--- | :---: | :---: |",
        ])

        for p in board_r1:
            status_tag = "🔄 **VIA TROCA**" if p["is_traded"] else "✅ Própria"
            md_lines.append(
                f"| **{p['pick']}** | **{p['current_team']}** ({p['current_owner']}) | {p['original_team']} ({p['original_owner']}) | {status_tag} | {p['original_max_pf']:.2f} pts |"
            )

        md_lines.extend([
            "",
            "---",
            "",
            f"## 🎲 4. Ordem Projetada do Draft {season} — Round 2",
            "",
            "| Pick | Dono Atual da Escolha | Origem da Escolha (Natural Slot) | Status |",
            "| :---: | :--- | :--- | :---: |",
        ])

        for p in board_r2:
            status_tag = "🔄 **VIA TROCA**" if p["is_traded"] else "✅ Própria"
            md_lines.append(
                f"| **{p['pick']}** | **{p['current_team']}** ({p['current_owner']}) | {p['original_team']} ({p['original_owner']}) | {status_tag} |"
            )

        md_lines.extend([
            "",
            "---",
            "",
            f"## 🦈 5. Panorama de Capital de Draft {season} (Top Donos de Picks)",
            "",
            "| Manager | Franquia | Picks Round 1 | Picks Round 2 | Total de Picks (Top 2 Rounds) |",
            "| :--- | :--- | :---: | :---: | :---: |",
        ])

        for owner, c in sorted(counts.items(), key=lambda x: (x[1]["r1"], x[1]["total"]), reverse=True):
            md_lines.append(
                f"| **{owner}** | {c['team_name']} | **{c['r1']}** | {c['r2']} | **{c['total']}** |"
            )

        md_lines.extend([
            "",
            "---",
            "*Documento gerado automaticamente pelo Sleeper Bot & DraftOrderService.*",
        ])

        content = "\n".join(md_lines)

        kb_path = os.path.join(self.kb_dir, "DRAFT_ORDER_AND_MAX_PF.md")
        os.makedirs(self.kb_dir, exist_ok=True)
        with open(kb_path, "w", encoding="utf-8") as f:
            f.write(content)

        # Also save JSON cache
        json_path = os.path.join(self.kb_dir, "draft_order_projected.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        logger.info(f"Salvo DRAFT_ORDER_AND_MAX_PF.md em {kb_path}")
        return content


if __name__ == "__main__":
    service = DraftOrderService()
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd in ["whatsapp", "card", "msg"]:
            print(service.format_draft_order_whatsapp())
        elif cmd in ["weekly", "rodada"]:
            wk = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else None
            print(service.format_weekly_max_pf_whatsapp(wk))
        elif cmd in ["sync", "kb"]:
            service.sync_draft_knowledge_base()
        else:
            print("Uso: draft_order_service.py [whatsapp | weekly [semana] | sync]")
    else:
        print(service.format_draft_order_whatsapp())
        service.sync_draft_knowledge_base()
