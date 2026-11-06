#!/usr/bin/env python3
"""Periodic Transaction & Weekly Results Sync Monitor for Sleeper Fantasy Bot.

- Trades (cron trades-watch, 15 min): detects new completed trades, writes the verdict with the LLM
  (OpenRouter, deterministic fallback), sends the announcement and only then records the trade in
  trades_history.json / synced_transactions.json.
- Waivers (cron league-sync): ingests processed waivers/FA from the current AND previous leg.
- Weekly results (cron league-sync, Tuesdays): consolidates the finished week into matchups_history /
  h2h_matrix once every NFL game of the week is final; Wed/Thu re-check applies stat corrections.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

import league_ctx

KB_DIR: Optional[str] = None   # override (tests/tools); None = the current league's knowledge base
BRT = ZoneInfo("America/Sao_Paulo")   # default zone; the league's own comes from league.yaml (self.tz)
logger = logging.getLogger("TransactionMonitor")


def _sent_ok(res: Any) -> bool:
    from evolution_client import sent_ok
    return sent_ok(res)


def _llm_chat(prompt: str) -> str:
    import llm
    return llm.chat(prompt, max_tokens=900, timeout=60)


class TransactionMonitor:
    def __init__(
        self,
        league_id: Optional[str] = None,
        group_jid: Optional[str] = None,
        evolution_url: Optional[str] = None,
        evolution_key: Optional[str] = None,
        evolution_instance: Optional[str] = None,
    ):
        from sleeper_client import SleeperClient
        from evolution_client import EvolutionClient

        self.league_id = league_id or self.ctx.league_id
        self.group_jid = group_jid or self.ctx.group_jid
        evo_url = evolution_url or os.environ.get("EVOLUTION_API_URL", "http://evolution-api:8080")
        evo_key = evolution_key or os.environ.get("EVOLUTION_API_KEY", "")
        evo_instance = evolution_instance or os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot")

        self.sleeper = SleeperClient(league_id=self.league_id)
        self.evolution = EvolutionClient(base_url=evo_url, api_key=evo_key, instance=evo_instance)
        self.players_db = self._load_players_db()

    # League-scoped paths and identity resolve from the current league each time (no state captured at import).
    @property
    def ctx(self):
        return league_ctx.current()

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.ctx.timezone)

    @property
    def kb_dir(self) -> str:
        return KB_DIR or self.ctx.kb_dir

    @property
    def synced_tx_file(self) -> str:
        return os.path.join(self.kb_dir, "synced_transactions.json")

    @property
    def synced_weeks_file(self) -> str:
        return os.path.join(self.kb_dir, "synced_weeks.json")

    @property
    def waivers_file(self) -> str:
        return os.path.join(self.kb_dir, "waivers_activity.json")

    def _load_players_db(self) -> Dict[str, Any]:
        import players_db
        return players_db.load()

    def load_synced_transactions(self) -> Set[str]:
        if os.path.exists(self.synced_tx_file):
            try:
                with open(self.synced_tx_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data.get("transactions", []))
            except Exception as e:
                logger.error(f"Failed to read synced transactions: {e}")
        return set()

    def save_synced_transaction(self, tx_id: str):
        current = self.load_synced_transactions()
        current.add(str(tx_id))
        try:
            with open(self.synced_tx_file, "w", encoding="utf-8") as f:
                json.dump({"transactions": sorted(list(current)), "last_update": datetime.now(self.tz).isoformat()}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save synced transaction {tx_id}: {e}")

    def load_synced_weeks(self) -> Set[str]:
        if os.path.exists(self.synced_weeks_file):
            try:
                with open(self.synced_weeks_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data.get("weeks", []))
            except Exception as e:
                logger.error(f"Failed to read synced weeks: {e}")
        return set()

    def save_synced_week(self, week_key: str):
        current = self.load_synced_weeks()
        current.add(str(week_key))
        try:
            with open(self.synced_weeks_file, "w", encoding="utf-8") as f:
                json.dump({"weeks": sorted(list(current)), "last_update": datetime.now(self.tz).isoformat()}, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save synced week {week_key}: {e}")

    def resolve_player_label(self, player_id: str) -> str:
        p = self.players_db.get(str(player_id))
        if p:
            name = p.get("name", f"Player {player_id}")
            pos = p.get("pos") or p.get("position", "FLEX")
            team = p.get("team") or "FA"
            return f"{name} ({pos} - {team})"
        return f"Jogador #{player_id}"

    # -------------------------------------------------------------------------
    # 1. Checagem e Notificação de Trades
    # -------------------------------------------------------------------------
    def check_and_process_trades(self, notify_whatsapp: bool = True) -> List[Dict[str, Any]]:
        """Check for new completed trades in current/previous week."""
        synced_ids = self.load_synced_transactions()
        state = self.sleeper.get_nfl_state()
        current_week = state.get("week", 1)
        season = state.get("season") or str(datetime.now(self.tz).year)

        weeks_to_check = [current_week]
        if current_week > 1:
            weeks_to_check.append(current_week - 1)

        users = self.sleeper.get_users()
        rosters = {r.get("roster_id"): r for r in self.sleeper.get_rosters()}

        new_trades_processed = []

        for w in weeks_to_check:
            raw_txs = self.sleeper._get(f"league/{self.league_id}/transactions/{w}") or []
            for t in raw_txs:
                if t.get("type") != "trade" or t.get("status") != "complete":
                    continue

                tx_id = str(t.get("transaction_id"))
                if not tx_id or tx_id in synced_ids:
                    continue

                logger.info(f"Nova trade detectada: {tx_id} (Semana {w})")
                trade_info = self._parse_trade(t, users, rosters, w, season)
                if not trade_info:
                    continue

                if notify_whatsapp:
                    analysis = self._generate_trade_analysis(trade_info)
                    msg = self._format_trade_announcement(trade_info, analysis)
                    res = self._send_group(msg)
                    if not _sent_ok(res):
                        # Not recorded: the next run retries the announcement (no duplicate history entry).
                        logger.error(f"Falha ao anunciar trade {tx_id}: {res}")
                        print(f"[trades] FAILED to announce {tx_id}: {res}", file=sys.stderr)
                        continue

                self._record_trade(trade_info)
                self.save_synced_transaction(tx_id)
                synced_ids.add(tx_id)
                new_trades_processed.append(trade_info)

        # 6. Atualização Incremental do Grafo de Atletas e Elencos se houve trocas
        if new_trades_processed:
            logger.info("Novas trocas processadas! Disparando atualização do grafo de atletas e elencos...")
            self._refresh_derived("pós-trade")

        return new_trades_processed

    def _parse_trade(self, t: Dict[str, Any], users: Dict[str, Any], rosters: Dict[int, Any], week: int, season: str) -> Optional[Dict[str, Any]]:
        tx_id = str(t.get("transaction_id"))
        created_ts = t.get("created", 0) / 1000.0
        dt = datetime.fromtimestamp(created_ts, tz=self.tz)
        dt_str = dt.strftime("%d/%m/%Y")

        roster_ids = t.get("roster_ids", [])
        if len(roster_ids) < 2:
            return None

        sides = {}
        for rid in roster_ids:
            r = rosters.get(rid, {})
            u = users.get(r.get("owner_id"), {})
            sides[rid] = {
                "roster_id": rid,
                "team_name": u.get("team_name") or f"Team {rid}",
                "manager_name": u.get("display_name") or f"Manager {rid}",
                "players_received": [],
                "player_details_received": [],
                "picks_received": [],
                "faab_received": 0,
            }

        # Jogadores recebidos e mapeamento de origem
        drops_map = t.get("drops") or {}
        for pid, dest_rid in (t.get("adds") or {}).items():
            if dest_rid in sides:
                orig_rid = drops_map.get(pid)
                if not orig_rid:
                    for other_rid in sides.keys():
                        if other_rid != dest_rid:
                            orig_rid = other_rid
                            break
                orig_u = users.get(rosters.get(orig_rid, {}).get("owner_id"), {})
                orig_name = orig_u.get("display_name") or f"Manager {orig_rid}"
                orig_team = orig_u.get("team_name") or f"Team {orig_rid}"

                p_label = self.resolve_player_label(pid)
                sides[dest_rid]["players_received"].append(p_label)
                sides[dest_rid]["player_details_received"].append({
                    "player_id": str(pid),
                    "label": p_label,
                    "from_roster_id": orig_rid,
                    "from_manager": orig_name,
                    "from_team": orig_team,
                })

        # Picks recebidas
        for dp in (t.get("draft_picks") or []):
            owner_rid = dp.get("owner_id")
            if owner_rid in sides:
                orig_rid = dp.get("roster_id")
                orig_u = users.get(rosters.get(orig_rid, {}).get("owner_id"), {})
                orig_name = orig_u.get("display_name") or f"Team {orig_rid}"
                dp_season = dp.get("season", season)
                dp_round = dp.get("round", 1)
                sides[owner_rid]["picks_received"].append({
                    "season": dp_season,
                    "round": dp_round,
                    "original_owner": orig_name,
                    "label": f"Pick Round {dp_round} ({dp_season}) via {orig_name}",
                })

        # FAAB
        for wb in (t.get("waiver_budget") or []):
            dest = wb.get("receiver")
            if dest in sides:
                sides[dest]["faab_received"] += wb.get("amount", 0)

        return {
            "transaction_id": tx_id,
            "date_str": dt_str,
            "season": season,
            "week": week,
            "sides": list(sides.values()),
        }

    def _record_trade(self, trade_info: Dict[str, Any]) -> None:
        """Append the trade to knowledge_base/trades_history.json (source of truth); league.db re-indexes itself."""
        import kb_db
        if kb_db.append_trade(trade_info):
            logger.info("trades_history.json atualizado com a nova troca.")

    def _load_teams_rosters_analysis(self) -> Dict[str, Any]:
        """Load teams rosters analysis JSON if present."""
        for p in [os.path.join(self.kb_dir, "teams_rosters_analysis.json")]:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    logger.error(f"Erro ao carregar teams_rosters_analysis.json: {e}")
        return {}

    def _get_trade_roster_context(self, trade_info: Dict[str, Any]) -> Dict[str, Any]:
        """Extract deep roster context for traded players from teams_rosters_analysis.json.
        Determines whether players were starters or bench/reserve on their original team,
        and how they fit into the receiving team (supplying needs, adding depth, etc.).
        """
        teams_data = self._load_teams_rosters_analysis()
        sides = trade_info.get("sides", [])
        
        context_notes = []
        detailed_players = []
        
        for side in sides:
            dest_rid = str(side.get("roster_id"))
            dest_mgr = side.get("manager_name", f"Manager {dest_rid}")
            dest_team_name = side.get("team_name", f"Team {dest_rid}")
            dest_info = teams_data.get(dest_rid, {})
            dest_status = dest_info.get("competitive_status", "Competidor")
            dest_weaknesses = dest_info.get("weaknesses", [])
            
            p_details = side.get("player_details_received", [])
            for pd in p_details:
                pid = pd["player_id"]
                label = pd["label"]
                from_rid = str(pd.get("from_roster_id", ""))
                from_mgr = pd.get("from_manager", "Outro time")
                from_team_name = pd.get("from_team", "")
                
                from_info = teams_data.get(from_rid, {})
                orig_starters = [str(s["player_id"]) for s in from_info.get("starters", [])]
                
                # Checar função no time de origem
                orig_bench_pids = []
                for b_list in from_info.get("bench_by_position", {}).values():
                    orig_bench_pids.extend([str(b["player_id"]) for b in b_list])
                orig_taxi_pids = [str(tp["player_id"]) for tp in from_info.get("taxi_squad", [])]
                orig_ir_pids = [str(ip["player_id"]) for ip in from_info.get("reserve_ir", [])]
                
                if pid in orig_starters:
                    role_orig = "Titular indispensável / Peça chave da escalação"
                    role_short = "titular absoluto"
                elif pid in orig_bench_pids:
                    role_orig = "Reserva de luxo / Profundidade de banco"
                    role_short = "reserva de luxo no banco"
                elif pid in orig_taxi_pids:
                    role_orig = "Calouro no Taxi Squad em desenvolvimento"
                    role_short = "aposta de Taxi Squad"
                elif pid in orig_ir_pids:
                    role_orig = "Reserva lesionado (IR)"
                    role_short = "atleta no departamento médico (IR)"
                else:
                    role_orig = "Peça de rotação do elenco"
                    role_short = "peça de rotação"

                # Checar se o destino tem carência nessa posição
                pos = ""
                if "(" in label and " - " in label:
                    pos = label.split("(")[1].split(" - ")[0].strip()
                
                need_addressed = False
                for w in dest_weaknesses:
                    if pos and pos in w:
                        need_addressed = True
                        break

                dest_impact = "chega com forte probabilidade de assumir a titularidade imediata" if (need_addressed or "Win-Now" in dest_status) else "acrescenta profundidade qualificada ao elenco"
                
                note = (
                    f"*{label}*: No elenco de origem (*{from_team_name}* / {from_mgr}), era {role_short}, "
                    f"já que a equipe contava com opções consolidadas. Já no elenco de destino (*{dest_team_name}* / {dest_mgr}), "
                    f"que está com postura de *{dest_status}*, o jogador {dest_impact}."
                )
                context_notes.append(note)
                detailed_players.append({
                    "player": label,
                    "from_manager": from_mgr,
                    "from_team": from_team_name,
                    "to_manager": dest_mgr,
                    "to_team": dest_team_name,
                    "role_orig": role_orig,
                    "role_short": role_short,
                    "dest_status": dest_status,
                    "need_addressed": need_addressed
                })

            # Analisar picks recebidas
            this_season = int(self.sleeper.get_league().get("season") or datetime.now(self.tz).year)
            for dp in side.get("picks_received", []):
                season_pick = dp.get("season")
                try:
                    yr = int(season_pick)
                except Exception:
                    yr = this_season
                if yr > this_season:
                    note_pick = (
                        f"*{dp['label']}*: *{dest_team_name}* ({dest_mgr}) adquire capital de draft futuro de longo prazo ({season_pick}), "
                        f"acumulando ativos para o horizonte além de {this_season}."
                    )
                    context_notes.append(note_pick)

        return {
            "notes": context_notes,
            "detailed_players": detailed_players,
        }

    def _generate_trade_analysis(self, trade_info: Dict[str, Any]) -> str:
        """Generate sharp, tailored analysis using Hermes Agent or intelligent local fallback."""
        sides = trade_info["sides"]
        m1 = sides[0]["manager_name"]
        t1 = sides[0]["team_name"]
        m2 = sides[1]["manager_name"]
        t2 = sides[1]["team_name"]

        summary_parts = []
        for s in sides:
            recs = s["players_received"] + [p["label"] for p in s["picks_received"]]
            if s["faab_received"] > 0:
                recs.append(f"${s['faab_received']} FAAB")
            summary_parts.append(f"{s['team_name']} ({s['manager_name']}) recebe: {', '.join(recs) if recs else 'nada'}")
        trade_summary = " | ".join(summary_parts)

        # Extrair contexto detalhado de elenco
        ctx = self._get_trade_roster_context(trade_info)
        roster_context_text = "\n".join([f"• {n}" for n in ctx["notes"]]) if ctx["notes"] else "Contexto padrão de elenco."

        # 1. LLM (Gemini, OpenRouter fallback); any failure → deterministic fallback below.
        prompt = (
            f"Você é o {self.ctx.persona_label()}, analista ácido, sarcástico e técnico da liga {self.ctx.name} "
            f"(Sleeper; {self.ctx.profile.format_label()}). Escreva em PT-BR um veredito curto (máximo 3 parágrafos curtos) sobre "
            "esta trade. Use SÓ os fatos abaixo, não invente números nem eventos.\n\n"
            f"Trade: {trade_summary}\n\nContexto dos elencos:\n{roster_context_text}\n\n"
            "Considere: titular indispensável vs reserva de luxo na origem, encaixe no destino, horizonte das picks "
            "(win-now vs reconstrução). Alfinete os dois gestores. Formatação WhatsApp: *negrito* com um asterisco, "
            "sem títulos, sem tabelas. Responda só com o veredito."
        )
        text = _llm_chat(prompt)
        if text:
            return text.strip().strip("`").strip()

        # 2. Fallback Inteligente Contextualizado com Elencos
        fallback_lines = []
        if ctx["detailed_players"]:
            dp = ctx["detailed_players"][0]
            fallback_lines.append(
                f"Movimento cirúrgico no mercado! De um lado, *{dp['to_manager']}* ({dp['to_team']}) "
                f"reforça sua formação com *{dp['player']}*, que vinha atuando como {dp['role_short']} no qualificado elenco de *{dp['from_manager']}* ({dp['from_team']}) "
                f"e agora tem tudo para assumir papel decisivo de titularidade."
            )
            pick_notes = [n for n in ctx["notes"] if "capital de draft" in n or "Pick Round" in n]
            if pick_notes:
                fallback_lines.append(
                    f"Na contrapartida, *{dp['from_manager']}* capitaliza com precisão a profundidade do seu banco, "
                    f"transformando uma peça excedente em escolha de draft para o futuro da franquia."
                )
            else:
                fallback_lines.append(
                    f"Resta ver se o impacto imediato em campo justificará o preço pago nas próximas rodadas."
                )
        else:
            fallback_lines.append(
                f"Negociação que movimenta o tabuleiro da liga! De um lado, *{m1}* ({t1}) ajusta as peças do elenco; "
                f"do outro, *{m2}* ({t2}) responde na mesma moeda buscando equilibrar suas forças."
            )
            fallback_lines.append(
                f"O impacto real vai depender de como cada peça responder nas próximas semanas decisivas."
            )

        fallback_lines.append(
            f"_Veredito do {self.ctx.persona_name}: Movimento audacioso. O mercado de trocas segue fervendo na {self.ctx.name}!_"
        )
        return "\n\n".join(fallback_lines)

    def _format_trade_announcement(self, trade_info: Dict[str, Any], analysis: str) -> str:
        sides = trade_info["sides"]
        lines = [
            f"🚨 *BOMBA NA {self.ctx.name.upper()}: TRADE OFICIALIZADA!* 🚨",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🤝 *Acordo Fechado:* *{sides[0]['team_name']}* ({sides[0]['manager_name']}) ⇄ *{sides[1]['team_name']}* ({sides[1]['manager_name']})",
            "",
        ]

        for s in sides:
            lines.append(f"📦 *{s['team_name']}* ({s['manager_name']}) recebe:")
            items = []
            for p in s["players_received"]:
                items.append(f"  • {p}")
            for dp in s["picks_received"]:
                items.append(f"  • {dp['label']}")
            if s["faab_received"] > 0:
                items.append(f"  • ${s['faab_received']} FAAB")
            if not items:
                items.append("  • Nenhuma peça adicional")
            lines.extend(items)
            lines.append("")

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ *Veredito do {self.ctx.persona_label()}:*",
            analysis,
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⚡ _Histórico oficial atualizado na base de trocas_",
        ])
        return "\n".join(lines)

    def _send_group(self, text: str) -> Dict[str, Any]:
        if not self.group_jid:
            return {"error": "grupo do WhatsApp não configurado (group_jid / WHATSAPP_GROUP_JID)"}
        return self.evolution.send_message(self.group_jid, text)

    # -------------------------------------------------------------------------
    # Derived knowledge refresh (what the bot reads after something changed)
    # -------------------------------------------------------------------------
    def _refresh_derived(self, when: str) -> None:
        """Roster dossier for every league; the curated player-lineage graph only for the league whose sources
        (raw trade dumps, waiver tiers) it was written for: the others would read — and rewrite — league #1's files."""
        try:
            from build_teams_rosters_analysis import build_team_analysis
            build_team_analysis()
            if self.ctx.legacy_layout:
                from build_league_players_knowledge_base import build_players_knowledge_base
                build_players_knowledge_base()
            logger.info(f"Dossiê de elencos{' e grafo de atletas' if self.ctx.legacy_layout else ''} atualizados {when}")
        except Exception as e:
            logger.error(f"Erro ao atualizar o dossiê de elencos {when}: {e}")

    def _append_unclassified_waivers(self, new_waivers: List[Dict[str, Any]]) -> None:
        """Keep waivers_market_classified.json (the SQL index source) current for leagues without a curated
        classifier: same shape the bootstrap writes, no quality tiers."""
        path = os.path.join(self.kb_dir, "waivers_market_classified.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {"summary": {"source": "monitor (no tiers)"}, "manager_profiles": {}, "transactions": []}
        known = {str(t.get("transaction_id")) for t in data.get("transactions", [])}

        def label(a: Dict[str, Any]) -> Dict[str, Any]:
            return {**a, "label": f"{a.get('name')} ({a.get('position') or '?'} - {a.get('team') or 'FA'})"}
        for w in new_waivers:
            if str(w["transaction_id"]) in known:
                continue
            data.setdefault("transactions", []).append({
                "transaction_id": str(w["transaction_id"]), "season": w["season"], "week": w["week"], "date": w["date"],
                "created_ts": w.get("created_ts"), "type": w["type"], "status": "complete", "roster_id": w.get("roster_id"),
                "manager": w["manager"], "real_name": w["manager"], "team_name": w["team_name"],
                "bid": int(w.get("bid") or 0), "quality_tier": None, "tier_badge": "", "quality_reason": "",
                "adds": [label(a) for a in w["adds"]], "drops": [label(a) for a in w["drops"]]})
        os.makedirs(self.kb_dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)

    # -------------------------------------------------------------------------
    # 2. Checagem Periódica de Waivers & Free Agents
    # -------------------------------------------------------------------------
    def check_and_process_waivers(self) -> List[Dict[str, Any]]:
        """Track and log recent waiver wire additions into waivers_activity.json."""
        state = self.sleeper.get_nfl_state()
        current_week = int(state.get("week", 1))
        season = str(state.get("season"))
        # Claims are stored in the leg they were created in: the Thursday run of week N lives in leg N-1.
        raw_txs = []
        for leg in ([current_week, current_week - 1] if current_week > 1 else [current_week]):
            raw_txs += [dict(t, _leg=leg) for t in (self.sleeper._get(f"league/{self.league_id}/transactions/{leg}") or [])]

        synced_ids = self.load_synced_transactions()
        users = self.sleeper.get_users()
        rosters = {r.get("roster_id"): r for r in self.sleeper.get_rosters()}

        # Carregar atividades existentes
        existing_activity = []
        if os.path.exists(self.waivers_file):
            try:
                with open(self.waivers_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    existing_activity = data.get("activity", [])
            except Exception as e:
                logger.error(f"Failed to read waivers_activity.json: {e}")

        new_waivers = []
        for t in raw_txs:
            typ = t.get("type")
            if typ not in ["waiver", "free_agent"] or t.get("status") != "complete":
                continue
            tx_id = str(t.get("transaction_id"))
            if tx_id and tx_id not in synced_ids:
                created_ts = t.get("created", 0) / 1000.0
                dt = datetime.fromtimestamp(created_ts, tz=self.tz)
                rid = (t.get("roster_ids") or [None])[0]
                r = rosters.get(rid, {})
                u = users.get(r.get("owner_id"), {})

                adds = []
                for pid in (t.get("adds") or {}).keys():
                    pinfo = self.players_db.get(pid, {})
                    adds.append({
                        "player_id": pid,
                        "name": pinfo.get("name", f"Player {pid}"),
                        "position": pinfo.get("pos") or pinfo.get("position", ""),
                        "team": pinfo.get("team", "")
                    })

                drops = []
                for pid in (t.get("drops") or {}).keys():
                    pinfo = self.players_db.get(pid, {})
                    drops.append({
                        "player_id": pid,
                        "name": pinfo.get("name", f"Player {pid}"),
                        "position": pinfo.get("pos") or pinfo.get("position", ""),
                        "team": pinfo.get("team", "")
                    })

                waiver_entry = {
                    "transaction_id": tx_id,
                    "roster_id": rid,
                    "created_ts": created_ts,
                    "type": typ,
                    "date": dt.strftime("%d/%m/%Y %H:%M"),
                    "week": t.get("_leg", current_week),
                    "season": season,
                    "manager": u.get("display_name") or f"Manager {rid}",
                    "team_name": u.get("team_name") or f"Team {rid}",
                    "bid": (t.get("settings") or {}).get("waiver_bid", 0),
                    "adds": adds,
                    "drops": drops
                }
                new_waivers.append(waiver_entry)
                self.save_synced_transaction(tx_id)
                synced_ids.add(tx_id)

        if new_waivers:
            logger.info(f"{len(new_waivers)} novas movimentações de waiver/FA registradas.")
            updated_list = new_waivers + existing_activity
            # Manter as últimas 300 movimentações
            updated_list = updated_list[:300]
            out_data = {
                "activity": updated_list,
                "total_moves": len(updated_list),
                "last_update": datetime.now(self.tz).isoformat()
            }
            with open(self.waivers_file, "w", encoding="utf-8") as f:
                json.dump(out_data, f, indent=2)

            if self.ctx.legacy_layout:
                # League #1: waiver market with the curated 5-tier classification
                try:
                    from build_full_waivers_database import build_waivers_database
                    build_waivers_database()
                    logger.info("Base de waivers e dossiê (WAIVERS_AND_FA_DOSSIER.md) atualizados com sucesso pós-waivers!")
                except Exception as e:
                    logger.error(f"Erro ao atualizar base de waivers: {e}")
            else:
                self._append_unclassified_waivers(new_waivers)
            self._refresh_derived("pós-waivers")

        return new_waivers

    # -------------------------------------------------------------------------
    # 3. Sincronização Semanal dos Jogos da temporada (H2H, Standings, Records)
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # 3. Consolidação semanal (H2H, histórico, recordes)
    # -------------------------------------------------------------------------
    def _week_final(self, week: int, season: str) -> bool:
        """True only when ESPN reports every game of the NFL week as final (MNF included)."""
        import league_time
        games = league_time.week_games(week, season)
        return bool(games) and all(g["state"] == "post" for g in games)

    def _load_history(self):
        """(matchups_history, franchises). h2h_matrix.json is derived, never read back and patched."""
        with open(os.path.join(self.kb_dir, "matchups_history.json"), "r", encoding="utf-8") as f:
            matchups_history = json.load(f)
        with open(os.path.join(self.kb_dir, "franchises_history.json"), "r", encoding="utf-8") as f:
            franchises = {int(k): v for k, v in json.load(f).items()}
        return matchups_history, franchises

    def _save_history(self, matchups_history, franchises) -> None:
        """Write matchups_history.json and regenerate h2h_matrix.json from it (official brackets included)."""
        import h2h_builder
        with open(os.path.join(self.kb_dir, "matchups_history.json"), "w", encoding="utf-8") as f:
            json.dump(matchups_history, f, indent=2, ensure_ascii=False)
        matrix = h2h_builder.build_matrix(matchups_history, franchises, h2h_builder.load_brackets(self.kb_dir))
        with open(os.path.join(self.kb_dir, "h2h_matrix.json"), "w", encoding="utf-8") as f:
            json.dump(matrix, f, indent=2, ensure_ascii=False)

    def _refresh_brackets(self, season: str) -> None:
        """Pull this season's decided winners/losers bracket games so playoff stages are classified from Sleeper."""
        import h2h_builder
        try:
            h2h_builder.refresh_brackets({season: str(self.league_id)}, [season], get=self.sleeper._get, kb_dir=self.kb_dir)
        except Exception as e:
            logger.error(f"Erro ao atualizar brackets de {season}: {e}")

    def _week_pairs(self, week: int) -> List[tuple]:
        """[(matchup_id, (roster, pts), (roster, pts))] with the lower roster first."""
        raw = self.sleeper._get(f"league/{self.league_id}/matchups/{week}") or []
        grouped: Dict[Any, list] = {}
        for m in raw:
            if m.get("matchup_id") is not None:
                grouped.setdefault(m["matchup_id"], []).append(m)
        pairs = []
        for mid, pair in grouped.items():
            if len(pair) != 2:
                continue
            a, b = sorted(pair, key=lambda x: int(x.get("roster_id")))
            pairs.append((mid, (int(a["roster_id"]), float(a.get("points") or 0.0)),
                          (int(b["roster_id"]), float(b.get("points") or 0.0))))
        return pairs

    def check_and_sync_weekly_results(self) -> Optional[str]:
        """Consolidate the last finished week into matchups_history and regenerate h2h_matrix (once, after every game is final)."""
        state = self.sleeper.get_nfl_state()
        season = str(state.get("season"))
        current_week = int(state.get("week", 1))
        target_week = max(1, current_week - 1)
        week_key = f"{season}_week_{target_week}"
        if week_key in self.load_synced_weeks():
            return None
        if not self._week_final(target_week, season):
            logger.info(f"Semana {target_week} ainda tem jogo em aberto na ESPN — consolidação adiada.")
            return None
        pairs = self._week_pairs(target_week)
        if not pairs:
            return None

        try:
            matchups_history, franchises = self._load_history()
        except Exception as e:
            logger.error(f"Erro ao ler histórico de matchups e franquias: {e}")
            return None

        playoff_start = int((self.sleeper.get_league().get("settings") or {}).get("playoff_week_start") or 15)
        is_playoff = target_week >= playoff_start
        phase = "Playoffs" if is_playoff else "Temporada Regular"

        new_games_added = 0
        for mid, (low_r, low_pts), (high_r, high_pts) in pairs:
            f1, f2 = franchises.get(low_r, {}), franchises.get(high_r, {})
            low_c = f1.get("canonical_manager", f"Roster_{low_r}")
            high_c = f2.get("canonical_manager", f"Roster_{high_r}")
            low_t = f1.get("seasons", {}).get(season, {}).get("team_name") or f1.get("current_team", f"Team {low_r}")
            high_t = f2.get("seasons", {}).get(season, {}).get("team_name") or f2.get("current_team", f"Team {high_r}")

            exists = any(
                str(g.get("season")) == season and g.get("week") == target_week and
                {g.get("roster_a"), g.get("roster_b")} == {low_r, high_r}
                for g in matchups_history
            )
            if exists:
                continue
            matchups_history.append({
                "season": season, "week": target_week, "is_playoff": is_playoff, "phase": phase,
                "roster_a": low_r, "roster_b": high_r, "manager_a": low_c, "manager_b": high_c,
                "team_a": low_t, "team_b": high_t, "points_a": round(low_pts, 2), "points_b": round(high_pts, 2),
            })
            new_games_added += 1

        if new_games_added:
            if is_playoff:
                self._refresh_brackets(season)  # Wild Card / Semi / Final vs Toilet Bowl come from Sleeper's brackets
            self._save_history(matchups_history, franchises)
            logger.info(f"Semana {target_week} consolidada: {new_games_added} jogos.")
            try:
                from build_teams_rosters_analysis import build_team_analysis
                build_team_analysis()
            except Exception as e:
                logger.error(f"Erro ao atualizar dossiê de elencos no fechamento da semana: {e}")

        self.save_synced_week(week_key)
        return week_key

    def reconcile_week(self, week: Optional[int] = None) -> int:
        """Apply Sleeper stat corrections to an already consolidated week. Returns games corrected."""
        state = self.sleeper.get_nfl_state()
        season = str(state.get("season"))
        week = week or max(1, int(state.get("week", 1)) - 1)
        if f"{season}_week_{week}" not in self.load_synced_weeks():
            return 0
        try:
            matchups_history, franchises = self._load_history()
        except Exception as e:
            logger.error(f"reconcile: erro ao ler histórico: {e}")
            return 0
        fixed = 0
        for mid, (low_r, low_pts), (high_r, high_pts) in self._week_pairs(week):
            for g in matchups_history:
                if str(g.get("season")) != season or g.get("week") != week:
                    continue
                if {g.get("roster_a"), g.get("roster_b")} != {low_r, high_r}:
                    continue
                old_low = g["points_a"] if g["roster_a"] == low_r else g["points_b"]
                old_high = g["points_b"] if g["roster_a"] == low_r else g["points_a"]
                if abs(old_low - low_pts) < 0.005 and abs(old_high - high_pts) < 0.005:
                    break
                if g["roster_a"] == low_r:
                    g["points_a"], g["points_b"] = round(low_pts, 2), round(high_pts, 2)
                else:
                    g["points_a"], g["points_b"] = round(high_pts, 2), round(low_pts, 2)
                fixed += 1
                break
        if fixed:
            self._save_history(matchups_history, franchises)  # the matrix is regenerated from the corrected games
            logger.info(f"reconcile: {fixed} placares corrigidos na semana {week}")
        return fixed


if __name__ == "__main__":
    # Read-only diagnostics (nothing is written or sent).
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    monitor = TransactionMonitor()
    synced = monitor.load_synced_transactions()
    st = monitor.sleeper.get_nfl_state()
    wk = int(st.get("week", 1))
    for leg in (wk, wk - 1):
        txs = monitor.sleeper._get(f"league/{monitor.league_id}/transactions/{leg}") or []
        pend = [t for t in txs if t.get("status") == "complete" and str(t.get("transaction_id")) not in synced]
        print(f"leg {leg}: {len(pend)} transações concluídas ainda não registradas "
              f"({sum(t['type'] == 'trade' for t in pend)} trades)")
    print("semana anterior final na ESPN:", monitor._week_final(max(1, wk - 1), str(st.get("season"))))
