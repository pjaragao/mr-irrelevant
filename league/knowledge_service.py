#!/usr/bin/env python3
"""Offline Knowledge Base Service of one league (the current LeagueContext's).

Reads and queries local markdown and JSON files instantly without
relying on external network calls. Provides 100% accurate H2H rivalries,
franchise continuity, manager timelines, draft history, trades, and records.
"""

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import h2h_builder
import league_ctx

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("knowledge")



class KnowledgeBaseService:
    def __init__(self, kb_dir: Optional[str] = None):
        ctx = league_ctx.current()
        self.ctx = ctx
        self.kb_dir = kb_dir or ctx.kb_dir
        self.base_dir = ctx.data_dir if kb_dir is None else os.path.dirname(os.path.abspath(self.kb_dir))
        self._managers_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._h2h_cache: Optional[List[Dict[str, Any]]] = None
        self._records_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._franchises_cache: Optional[Dict[int, Dict[str, Any]]] = None
        self._h2h_matrix_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._stage_cache: Optional[h2h_builder.StageIndex] = None

    def _get_candidate_paths(self, filename: str) -> List[str]:
        paths = [os.path.join(self.kb_dir, filename), os.path.join(self.base_dir, filename)]
        if self.ctx.legacy_layout:
            # League #1 kept its sources in several places over time. Any other league reads ONLY its own
            # directories: falling back to the engine dir would serve another league's data.
            script_dir = ENGINE_DIR
            parent_dir = os.path.dirname(script_dir)
            paths += [os.path.join(script_dir, filename), os.path.join(script_dir, "knowledge_base", filename),
                      os.path.join(parent_dir, filename), os.path.join(parent_dir, "knowledge_base", filename)]
        return paths

    def _read_file(self, filename: str) -> str:
        for path in self._get_candidate_paths(filename):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read()
                except Exception as e:
                    print(f"[ERROR] Failed to read {path}: {e}")
        return ""

    def _load_json(self, filename: str) -> Optional[Any]:
        for path in self._get_candidate_paths(filename):
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return json.load(f)
                except Exception as e:
                    print(f"[ERROR] Failed to parse JSON {path}: {e}")
        return None

    def load_franchises(self) -> Dict[int, Dict[str, Any]]:
        """Load franchises history mapping roster_id -> franchise info."""
        if self._franchises_cache is not None:
            return self._franchises_cache

        data = self._load_json("franchises_history.json")
        franchises = {}
        if data:
            for k, v in data.items():
                try:
                    rid = int(k)
                    franchises[rid] = v
                except ValueError:
                    pass
        self._franchises_cache = franchises
        return franchises

    def load_h2h_matrix(self) -> Dict[str, Dict[str, Any]]:
        """Load full H2H matrix (all 66 combinations)."""
        if self._h2h_matrix_cache is not None:
            return self._h2h_matrix_cache

        data = self._load_json("h2h_matrix.json")
        if data:
            self._h2h_matrix_cache = data
            return data
        return {}

    def resolve_roster(self, query: str) -> Optional[int]:
        """Map any manager name, team name, alias, real name, or roster ID to this league's roster id."""
        if not query:
            return None

        import people
        rid = people.resolve(query)
        if rid is not None:
            return rid

        q = str(query).strip().lower().replace("@", "")
        # Remove common prefixes
        for prefix in ["time ", "equipe ", "team ", "roster ", "franquia ", "#"]:
            if q.startswith(prefix):
                q = q[len(prefix):].strip()

        # Direct numeric check
        if q.isdigit():
            rid = int(q)
            if 1 <= rid <= 12:
                return rid

        franchises = self.load_franchises()
        if not franchises:
            return None

        # 1. Exact canonical manager or exact current team or exact alias
        for rid, f in franchises.items():
            if q == f.get("canonical_manager", "").lower():
                return rid
            if q == f.get("real_name", "").lower():
                return rid
            if q == f.get("current_team", "").lower():
                return rid
            aliases = [a.lower() for a in f.get("aliases", [])]
            if q in aliases:
                return rid

        # 2. Historical team names or former manager names
        for rid, f in franchises.items():
            for t in f.get("teams_history", []):
                if q == t.get("team_name", "").lower():
                    return rid
            for m in f.get("managers_history", []):
                if q == m.get("manager_name", "").lower() or q == m.get("sleeper_user", "").lower():
                    return rid

        return None

    # ---- facts about THIS league used in texts (never constants)
    def _first_season(self) -> str:
        seasons = sorted({str(y) for f in self.load_franchises().values() for y in (f.get("seasons") or {})})
        return seasons[0] if seasons else ""

    def _persona(self) -> str:
        return self.ctx.persona_name

    def _persona_full(self) -> str:
        return self.ctx.persona_label("slash")

    def _consolation_span(self) -> str:
        """"7º ao 12º": the teams that miss the playoffs, from the league's own playoff size and team count."""
        p = self.ctx.profile
        if p.playoff_teams and p.total_rosters and p.total_rosters > p.playoff_teams:
            return f"{p.playoff_teams + 1}º ao {p.total_rosters}º"
        return "fora dos playoffs"

    def _live_owner(self, name: str) -> Optional[str]:
        """Team that rosters an athlete right now (live Sleeper), by name; None when unknown/unrostered."""
        try:
            from sleeper_client import SleeperClient
            import players_db
            sc = SleeperClient(self.ctx.league_id)
            low = name.strip().lower()
            ids = [pid for pid, p in players_db.load().items() if low and low in str(p.get("name", "")).lower()]
            users = sc.get_users()
            for r in sc.get_rosters():
                if any(str(p) in ids for p in (r.get("players") or [])):
                    return (users.get(r.get("owner_id")) or {}).get("team_name")
        except Exception:
            return None
        return None

    def _current_season(self) -> str:
        try:
            from sleeper_client import SleeperClient
            return str(SleeperClient().get_nfl_state().get("season"))
        except Exception:
            return str(datetime.now().year)

    def get_managers_summary(self) -> Dict[str, Dict[str, Any]]:
        """Career numbers per manager over COMPLETED seasons (those with a champion in playoffs_history)."""
        if self._managers_cache is not None:
            return self._managers_cache

        franchises = self.load_franchises()
        playoffs = self.get_playoffs_history()
        done = sorted(y for y, v in playoffs.items()
                      if (v.get("playoffs_title") or {}).get("champion") not in (None, "", "A Definir"))
        managers = {}
        for rid, f in franchises.items():
            cname = f["canonical_manager"]
            seasons = f.get("seasons", {})
            yrs = [y for y in done if y in seasons]
            w = sum(seasons[y]["wins"] for y in yrs)
            l = sum(seasons[y]["losses"] for y in yrs)
            pts = sum(seasons[y]["points_for"] for y in yrs)
            tag = f"({cname})"
            titles = [y for y in done if tag in str((playoffs[y].get("playoffs_title") or {}).get("champion", ""))]
            vices = [y for y in done if tag in str((playoffs[y].get("playoffs_title") or {}).get("runner_up", ""))]
            total_g = w + l
            managers[cname.lower()] = {
                "roster_id": rid,
                "manager": cname,
                "real_name": f["real_name"],
                "current_team": f["current_team"],
                "seasons_range": f"{yrs[0]}-{yrs[-1]}" if yrs else "-",
                "wins": w,
                "losses": l,
                "win_rate": f"{(w / total_g * 100):.1f}%" if total_g else "0.0%",
                "total_points": f"{pts:.2f}",
                "avg_per_year": f"{(pts / len(yrs)):.1f}" if yrs else "0",
                "titles": f"🏆 x{len(titles)} ({', '.join(titles)})" if titles else "-",
                "runner_ups": f"🥈 x{len(vices)} ({', '.join(vices)})" if vices else "-",
            }
        self._managers_cache = managers
        return managers

    def _live_record(self, rid: int) -> str:
        try:
            from sleeper_client import SleeperClient
            for t in SleeperClient().get_standings():
                if t["roster_id"] == rid:
                    return f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t["ties"] else "") + \
                           f" ({t['pts_for']:.2f} pts, {t['rank']}º geral)"
        except Exception:
            pass
        return "indisponível"

    def get_manager_dossier(self, query: str) -> str:
        """Format comprehensive manager and franchise profile for WhatsApp."""
        rid = self.resolve_roster(query)
        franchises = self.load_franchises()
        managers = self.get_managers_summary()

        if rid is None or rid not in franchises:
            return f"❌ Franquia ou treinador '{query}' não encontrado na base histórica da liga."

        f_info = franchises[rid]
        cname = f_info["canonical_manager"]
        matched = managers.get(cname.lower(), {})

        rebrandings = [t["team_name"] for t in f_info.get("teams_history", []) if t["team_name"] != f_info["current_team"]]
        rebrand_str = " ➔ ".join(rebrandings) if rebrandings else "Nenhum (manteve o nome original)"

        lines = [
            f"👤 *DOSSIÊ DA FRANQUIA #{rid}: {f_info['current_team'].upper()}*",
            f"🏈 *Treinador Titular:* *{cname}* ({f_info['real_name']})",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🏆 *Títulos:* {matched.get('titles', '-')} | 🥈 *Vices:* {matched.get('runner_ups', '-')}",
            f"📊 *Campanha regular ({matched.get('seasons_range', '-')}):* {matched.get('wins', 0)}V - {matched.get('losses', 0)}D (*{matched.get('win_rate', '0%')}*)",
            f"🎯 *Pontos acumulados:* {matched.get('total_points', '0')} pts (média {matched.get('avg_per_year', '0')} pts/ano)",
            f"📅 *Temporada atual:* {self._live_record(rid)}",
            f"🔄 *Nomes Anteriores (Rebranding):* {rebrand_str}",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        # Pegar todos os confrontos diretos deste treinador (11 rivais)
        rivalries = self.get_manager_rivalries(cname)
        if rivalries:
            lines.append("⚔️ *RETROSPECTO CONTRA TODOS OS RIVAIS (H2H):*")
            for r in rivalries:
                rival = r["opponent"]
                w, l, t = r["wins"], r["losses"], r.get("ties", 0)
                tot = r["games_total"]
                reg = r["games_reg"]
                po = r["games_playoffs"]
                tb = r.get("games_toilet", 0)
                po_str = f" ({reg}R / {po}P" + (f" / {tb}TB)" if tb > 0 else ")") if po > 0 else \
                    (f" ({reg}R / {tb}TB)" if tb > 0 else f" ({reg}R)")
                pts_for, pts_against = r["pts_for"], r["pts_against"]
                tag = r.get("tag") or ("👑 [DOMINA]" if w > l else ("🥔 [FREGUÊS]" if l > w else "🤝 [EQUILÍBRIO]"))
                note = f" — _{r['special_note']}_" if r.get("special_note") else ""
                lines.append(f"• {tag} vs *{rival}*: {w}V - {l}D{f' - {t}E' if t > 0 else ''} em {tot} jogos{po_str} ({round(float(pts_for))} x {round(float(pts_against))} pts){note}")

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ _{self._persona_full()}: A franquia #{rid} tem história, identidade e contas a acertar no grupo!_",
        ])
        return "\n".join(lines)

    def get_h2h_list(self) -> List[Dict[str, Any]]:
        """Return list of all 66 H2H matches."""
        if self._h2h_cache is not None:
            return self._h2h_cache

        h2h_dict = self.load_h2h_matrix()
        if h2h_dict:
            res = list(h2h_dict.values())
            self._h2h_cache = res
            return res

        return []

    def _stage_index(self) -> "h2h_builder.StageIndex":
        if self._stage_cache is None:
            self._stage_cache = h2h_builder.StageIndex(self._load_json("brackets_history.json") or {})
        return self._stage_cache

    def classify_game(self, season: str, week: int, is_playoff: bool, m1: Any, m2: Any) -> tuple:
        """(tag, label) of a game from the official Sleeper brackets (brackets_history.json).

        m1/m2: managers, teams or roster ids. Regular season -> "Reg"; Wild Card/Semi/Final/3º/5º lugar belong to
        the title bracket; "Toilet Bowl" is the consolation bracket; a post-season game missing from the file is
        "Pós-temporada" (never guessed as Toilet Bowl)."""
        if not is_playoff:
            return (h2h_builder.REGULAR, "Temporada Regular")
        ra, rb = self.resolve_roster(str(m1)), self.resolve_roster(str(m2))
        if ra is None or rb is None:
            return (h2h_builder.UNKNOWN, "Pós-temporada (fase não identificada)")
        st = self._stage_index().classify(season, int(week), True, ra, rb)
        return (st["stage"], st["label"])

    def _game_stage(self, g: Dict[str, Any], r_a: int, r_b: int) -> tuple:
        """(tag, label) of a games_log entry: stored there by the matrix builder, else looked up in the brackets."""
        if g.get("stage"):
            return g["stage"], g.get("stage_label") or g["stage"]
        return self.classify_game(g["season"], g["week"], g["is_playoff"], r_a, r_b)

    def get_h2h(self, m1_query: str, m2_query: str) -> str:
        """Get exact head-to-head record between two managers/franchises with complete details and playoff stage weighting."""
        r1 = self.resolve_roster(m1_query)
        r2 = self.resolve_roster(m2_query)

        franchises = self.load_franchises()

        if r1 is None:
            return f"❌ Franquia ou treinador '{m1_query}' não foi reconhecido. Use o nome do time ou do treinador."
        if r2 is None:
            return f"❌ Franquia ou treinador '{m2_query}' não foi reconhecido. Use o nome do time ou do treinador."
        if r1 == r2:
            return f"❌ Você informou a mesma franquia (#{r1} - {franchises[r1]['current_team']}) para ambos os lados!"

        h2h_matrix = self.load_h2h_matrix()
        key = f"{min(r1, r2)}_{max(r1, r2)}"
        pair = h2h_matrix.get(key)

        if not pair:
            return f"❌ Nenhum confronto histórico encontrado entre as franquias #{r1} e #{r2}."

        # Determine alignment based on user query order (r1 on left, r2 on right)
        is_first = (r1 == pair["roster_1"])

        mgrA = pair["manager_1"] if is_first else pair["manager_2"]
        mgrB = pair["manager_2"] if is_first else pair["manager_1"]
        realA = pair["real_name_1"] if is_first else pair["real_name_2"]
        realB = pair["real_name_2"] if is_first else pair["real_name_1"]
        teamA = pair["current_team_1"] if is_first else pair["current_team_2"]
        teamB = pair["current_team_2"] if is_first else pair["current_team_1"]

        winsA = pair["wins_1"] if is_first else pair["wins_2"]
        winsB = pair["wins_2"] if is_first else pair["wins_1"]
        ties = pair["ties"]

        winsA_reg = pair["wins_1_reg"] if is_first else pair["wins_2_reg"]
        winsB_reg = pair["wins_2_reg"] if is_first else pair["wins_1_reg"]
        winsA_po = pair["wins_1_po"] if is_first else pair["wins_2_po"]
        winsB_po = pair["wins_2_po"] if is_first else pair["wins_1_po"]

        ptsA = pair["pts_1"] if is_first else pair["pts_2"]
        ptsB = pair["pts_2"] if is_first else pair["pts_1"]

        winsA_tb = pair.get("wins_1_tb", 0) if is_first else pair.get("wins_2_tb", 0)
        winsB_tb = pair.get("wins_2_tb", 0) if is_first else pair.get("wins_1_tb", 0)

        tot_games = pair["games_total"]
        reg_games = pair["games_reg"]
        po_games = pair["games_playoffs"]  # title bracket only (Wild Card .. Final, 3º, 5º)
        tb_games = pair.get("games_toilet", 0)

        avgA = (ptsA / tot_games) if tot_games > 0 else 0.0
        avgB = (ptsB / tot_games) if tot_games > 0 else 0.0

        blowout = pair.get("biggest_blowout") or {}
        closest = pair.get("closest_match") or {}

        # Manager continuity / history check
        mgr1_hist = pair.get("manager_1_history") or []
        mgr2_hist = pair.get("manager_2_history") or []
        has_change = pair.get("has_manager_change", False)

        coach_details = []
        since = self._first_season() or "o início da liga"
        if has_change:
            histA = mgr1_hist if is_first else mgr2_hist
            histB = mgr2_hist if is_first else mgr1_hist

            coach_details.append(f"• *{teamA} (Franquia #{r1}):*")
            for h in histA:
                yrs = f"{h['start_season']}-{h['end_season']}" if h['start_season'] != h['end_season'] else h['start_season']
                g_c = [g for g in pair["games_log"] if g["season"] in h["seasons"]]
                w_c = sum(1 for g in g_c if g["winner"] == mgrA)
                l_c = len(g_c) - w_c
                coach_details.append(f"  - Treinador *{h['manager_name']}* ({yrs}): {len(g_c)} jogos ({w_c}V - {l_c}D)")

            coach_details.append(f"• *{teamB} (Franquia #{r2}):*")
            for h in histB:
                yrs = f"{h['start_season']}-{h['end_season']}" if h['start_season'] != h['end_season'] else h['start_season']
                g_c = [g for g in pair["games_log"] if g["season"] in h["seasons"]]
                w_c = sum(1 for g in g_c if g["winner"] == mgrB)
                l_c = len(g_c) - w_c
                coach_details.append(f"  - Treinador *{h['manager_name']}* ({yrs}): {len(g_c)} jogos ({w_c}V - {l_c}D)")
        else:
            coach_details.append(f"• *{teamA} (Franquia #{r1}):* Treinador fundador *{mgrA}* ({realA}) comandou em 100% dos jogos desde {since}.")
            coach_details.append(f"• *{teamB} (Franquia #{r2}):* Treinador fundador *{mgrB}* ({realB}) comandou em 100% dos jogos desde {since}.")
            coach_details.append(f"• _Estabilidade:_ Ambas as franquias possuem 100% de continuidade técnica (nenhuma troca de treinador no histórico).")

        # Team names history (Rebrandings)
        teamsA_hist = pair.get("teams_1_history", []) if is_first else pair.get("teams_2_history", [])
        teamsB_hist = pair.get("teams_2_history", []) if is_first else pair.get("teams_1_history", [])

        if len(teamsA_hist) > 1:
            rebrandA_str = " ➔ ".join(f"{t['team_name']} ({t['start_season']}-{t['end_season']})" for t in teamsA_hist)
            coach_details.append(f"• _Rebrandings de {mgrA}:_ {rebrandA_str}")
        if len(teamsB_hist) > 1:
            rebrandB_str = " ➔ ".join(f"{t['team_name']} ({t['start_season']}-{t['end_season']})" for t in teamsB_hist)
            coach_details.append(f"• _Rebrandings de {mgrB}:_ {rebrandB_str}")

        # Chronological games log with the official playoff stage of each game
        games_log_lines = []
        title_wins_A = []
        title_wins_B = []
        elims = []  # post-season meetings in the title bracket, for the verdict
        for g in pair["games_log"]:
            scA = g["score_1"] if is_first else g["score_2"]
            scB = g["score_2"] if is_first else g["score_1"]
            tmA = g["team_1"] if is_first else g["team_2"]
            tmB = g["team_2"] if is_first else g["team_1"]

            tag, label = self._game_stage(g, r1 if is_first else r2, r2 if is_first else r1)
            winner = g.get("winner", "")
            m = f"+{g['margin']} pts"
            if winner == "Empate":
                win_txt = "Empate"
            else:
                win_txt = f"Vitória de *{winner}* ({m})"
            if tag == h2h_builder.FINAL:
                if winner == mgrA:
                    title_wins_A.append((g["season"], g["margin"], scA, scB))
                elif winner == mgrB:
                    title_wins_B.append((g["season"], g["margin"], scB, scA))
                win_txt += f" — CAMPEÃO DA LIGA {g['season']}!"
            elif tag == h2h_builder.SEMI:
                win_txt += " — avançou à Grande Final"
            elif tag == h2h_builder.WILD_CARD:
                win_txt += " — avançou às semifinais"
            elif tag == h2h_builder.TOILET:
                win_txt += " [Toilet Bowl — sem peso de título]"
            if tag in (h2h_builder.FINAL, h2h_builder.SEMI, h2h_builder.WILD_CARD, h2h_builder.THIRD, h2h_builder.FIFTH):
                elims.append((g["season"], label, winner))
            games_log_lines.append(
                f"• {g['season']} Sem. {g['week']:02d} [{label if tag != h2h_builder.REGULAR else 'Reg'}]: "
                f"{tmA} {scA:.2f} x {scB:.2f} {tmB} ➔ {win_txt}")

        # Verdict: only facts that the numbers above support
        def _elim_txt() -> str:
            semis = [e for e in elims if e[1].endswith(("Wild Card", "Semifinal"))]
            if not semis:
                return ""
            return " Eliminações no mata-mata: " + "; ".join(
                f"{y} {lbl.replace('Playoffs — ', '')} → {w}" for y, lbl, w in semis) + "."

        if title_wins_A:
            t_yr, t_mg, t_scA, t_scB = title_wins_A[0]
            veredito = (
                f"👑 *PESO SUPREMO DE TÍTULO:* a Grande Final de {t_yr} ficou com *{mgrA}* sobre *{mgrB}* por +{t_mg} pts "
                f"({t_scA:.2f} x {t_scB:.2f})."
                + (f" No placar geral *{mgrB}* até leva ({winsB} x {winsA}), mas o que vale é o anel de campeão." if winsB > winsA else "")
                + f" *{mgrA}* tem a taça na estante em cima de *{mgrB}*." + _elim_txt())
        elif title_wins_B:
            t_yr, t_mg, t_scB, t_scA = title_wins_B[0]
            veredito = (
                f"👑 *PESO SUPREMO DE TÍTULO:* *{mgrB}* venceu a Grande Final de {t_yr} em cima de *{mgrA}* "
                f"({t_scB:.2f} x {t_scA:.2f}, +{t_mg} pts) e levantou o anel na cara do adversário."
                + (f" No placar geral *{mgrA}* até leva ({winsA} x {winsB}), mas o que vale é o anel de campeão." if winsA > winsB else "")
                + _elim_txt())
        elif winsA != winsB:
            lead, trail = (mgrA, mgrB) if winsA > winsB else (mgrB, mgrA)
            wL, wT = max(winsA, winsB), min(winsA, winsB)
            ptsL, ptsT = (ptsA, ptsB) if winsA > winsB else (ptsB, ptsA)
            poL, poT = (winsA_po, winsB_po) if winsA > winsB else (winsB_po, winsA_po)
            diff = wL - wT
            if diff >= 3:
                veredito = f"Domínio absoluto de *{lead}*: +{diff} vitórias de folga ({wL} x {wT}) sobre *{trail}*."
            else:
                veredito = f"Vantagem de *{lead}* ({wL} x {wT}), num confronto equilibrado com *{trail}*."
            if po_games > 0:
                if poL > poT:
                    veredito += f" No mata-mata do título *{lead}* também leva ({poL} x {poT})."
                elif poT > poL:
                    veredito += f" Mas no mata-mata do título *{trail}* leva ({poT} x {poL})."
                else:
                    veredito += f" No mata-mata do título está empatado ({poL} x {poT})."
            if ptsT > ptsL:
                veredito += f" *{trail}* tem mais pontos acumulados ({ptsT:.2f} x {ptsL:.2f})."
            else:
                veredito += f" Pontos acumulados: {ptsL:.2f} x {ptsT:.2f}."
            veredito += _elim_txt()
        else:
            leader = mgrA if ptsA > ptsB else mgrB
            veredito = (f"Empate no placar de vitórias ({winsA} x {winsB} em {tot_games} jogos); no saldo de pontos "
                        f"({ptsA:.2f} x {ptsB:.2f}) a vantagem é de *{leader}*." + _elim_txt())

        # Format Blowout and Closest match
        b_winner = blowout.get("winner", "-")
        b_margin = blowout.get("margin", 0)
        b_score = blowout.get("score", "-")
        b_season = blowout.get("season", "-")
        b_week = blowout.get("week", "-")
        b_phase = blowout.get("phase", "Temporada Regular")
        b_str = f"*{b_winner}* por +{b_margin} pontos ({b_score}) [Temporada {b_season}, Semana {b_week} - {b_phase}]"

        c_winner = closest.get("winner", "-")
        c_margin = closest.get("margin", 0)
        c_score = closest.get("score", "-")
        c_season = closest.get("season", "-")
        c_week = closest.get("week", "-")
        c_phase = closest.get("phase", "Temporada Regular")
        c_str = f"*{c_winner}* por +{c_margin} pontos ({c_score}) [Temporada {c_season}, Semana {c_week} - {c_phase}]"

        lines = [
            f"⚔️ *CONFRONTO DIRETO HISTÓRICO (H2H)* ⚔️",
            f"*{teamA}* ({mgrA}) vs *{teamB}* ({mgrB})",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📊 *Placar Geral:* *{mgrA}* {winsA} x {winsB} *{mgrB}*{f' ({ties} empates)' if ties > 0 else ''} ({tot_games} jogos disputados)",
            f"   • _Temporada Regular:_ {winsA_reg} x {winsB_reg} ({reg_games} jogos)",
        ]

        if po_games > 0:
            lines.append(f"   • _Playoffs do Título (Wild Card → Final, 3º e 5º lugar):_ {winsA_po} x {winsB_po} ({po_games} jogos)")
        if tb_games > 0:
            lines.append(f"   • _Toilet Bowl (consolação {self._consolation_span()}):_ {winsA_tb} x {winsB_tb} ({tb_games} jogos) [sem peso de título]")

        lines.extend([
            f"🎯 *Pontos Acumulados:* {mgrA} ({ptsA:.2f} pts | média {avgA:.1f}) x {mgrB} ({ptsB:.2f} pts | média {avgB:.1f})",
            f"💥 *Maior Goleada:* {b_str}",
            f"🔥 *Jogo Mais Apertado:* {c_str}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "🏛️ *Linhagem de Treinadores & Franquias:*",
        ])
        lines.extend(coach_details)

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🗓️ *Histórico Jogo a Jogo ({tot_games} partidas disputadas):*",
        ])
        lines.extend(games_log_lines)

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ _Veredito de {self._persona_full()}: {veredito}_",
        ])

        return "\n".join(lines)

    def get_h2h_segregated_summary(self, m1_query: str, m2_query: str) -> Dict[str, Any]:
        """Get structured H2H breakdown segregated by Regular Season, Title Playoffs, and Toilet Bowl, highlighting historical Championship games."""
        r1 = self.resolve_roster(m1_query)
        r2 = self.resolve_roster(m2_query)
        franchises = self.load_franchises()

        if r1 is None or r2 is None or r1 == r2:
            return {
                "text": "Primeiro confronto histórico entre as equipes.",
                "has_history": False,
                "title_games": [],
            }

        h2h_matrix = self.load_h2h_matrix()
        key = f"{min(r1, r2)}_{max(r1, r2)}"
        pair = h2h_matrix.get(key)
        if not pair:
            return {
                "text": "Primeiro confronto histórico entre as equipes.",
                "has_history": False,
                "title_games": [],
            }

        is_first = (r1 == pair["roster_1"])
        mgrA = pair["manager_1"] if is_first else pair["manager_2"]
        mgrB = pair["manager_2"] if is_first else pair["manager_1"]
        teamA = pair["current_team_1"] if is_first else pair["current_team_2"]
        teamB = pair["current_team_2"] if is_first else pair["current_team_1"]

        def side(prefix: str, suffix: str = "") -> tuple:
            """(A, B) of a ``{prefix}_1{suffix}`` / ``{prefix}_2{suffix}`` field, seen from the first manager asked."""
            a, b = pair.get(f"{prefix}_1{suffix}", 0), pair.get(f"{prefix}_2{suffix}", 0)
            return (a, b) if is_first else (b, a)

        winsA, winsB = side("wins")
        ties = pair.get("ties", 0)
        tot_games = pair.get("games_total", winsA + winsB + ties)
        winsA_reg, winsB_reg = side("wins", "_reg")
        po_A, po_B = side("wins", "_po")
        tb_A, tb_B = side("wins", "_tb")

        short = {h2h_builder.WILD_CARD: "WC", h2h_builder.SEMI: "Semi", h2h_builder.FINAL: "Final",
                 h2h_builder.THIRD: "3º lugar", h2h_builder.FIFTH: "5º lugar"}
        title_games = []
        semi_games = []
        playoff_games = []  # every title-bracket meeting, in order

        for g in pair.get("games_log", []):
            tag, label = self._game_stage(g, r1, r2)
            if tag not in short:
                continue
            winner = g.get("winner", "")
            scA = g["score_1"] if is_first else g["score_2"]
            scB = g["score_2"] if is_first else g["score_1"]
            score_str = f"{scA:.2f} x {scB:.2f}"
            entry = {"season": g["season"], "stage": short[tag], "winner": winner, "score": score_str,
                     "side": "a" if winner == mgrA else "b" if winner == mgrB else "tie"}
            playoff_games.append(entry)
            if tag == h2h_builder.FINAL:
                title_games.append({"season": g["season"], "winner": winner, "score": score_str,
                                    "margin": g.get("margin", 0.0)})
            elif tag == h2h_builder.SEMI:
                semi_games.append({"season": g["season"], "winner": winner, "score": score_str})

        lines = [
            f"• *Placar Geral:* *{teamA}* ({mgrA}) *{winsA} x {winsB}* ({mgrB}) *{teamB}* ({tot_games} jogos)",
            f"   ↳ 🔹 *Temporada Regular:* {winsA_reg} x {winsB_reg}",
            f"   ↳ 🏆 *Playoffs (Mata-mata do Título):* {po_A} x {po_B}",
        ]
        if pair.get("games_toilet", 0) > 0:
            lines.append(f"   ↳ 🚽 *Toilet Bowl:* {tb_A} x {tb_B}")
        for pg in playoff_games:
            who = pg["winner"] if pg["side"] != "tie" else "Empate"
            if pg["stage"] == "Final":
                lines.append(f"   ↳ 👑 *GRANDE FINAL DE {pg['season']}*: *{who}* venceu por *{pg['score']}* e conquistou o Título da Liga em confronto direto!")
            else:
                lines.append(f"   ↳ 🔥 *{pg['stage']} {pg['season']}*: *{who}* venceu por *{pg['score']}*")

        return {
            "text": "\n".join(lines),
            "has_history": True,
            "wins_a": winsA,
            "wins_b": winsB,
            "ties": ties,
            "reg_str": f"{winsA_reg} x {winsB_reg}",
            "po_str": f"{po_A} x {po_B}",
            "tb_str": f"{tb_A} x {tb_B}",
            "title_games": title_games,
            "semi_games": semi_games,
            "playoff_games": playoff_games,
        }


    def get_manager_rivalries(self, manager_query: str) -> List[Dict[str, Any]]:
        """Get all head-to-head records for a specific manager across all of the league's other teams with title weighting."""
        rid = self.resolve_roster(manager_query)
        if rid is None:
            return []

        franchises = self.load_franchises()
        h2h_matrix = self.load_h2h_matrix()

        my_manager = franchises[rid]["canonical_manager"]
        results = []

        for opp_rid in sorted(int(k) for k in franchises if str(k).isdigit()):    # this league's rosters, not 1..12
            if opp_rid == rid:
                continue
            key = f"{min(rid, opp_rid)}_{max(rid, opp_rid)}"
            pair = h2h_matrix.get(key)
            if not pair:
                continue

            is_first = (rid == pair["roster_1"])
            opp_mgr = pair["manager_2"] if is_first else pair["manager_1"]
            opp_team = pair["current_team_2"] if is_first else pair["current_team_1"]
            w = pair["wins_1"] if is_first else pair["wins_2"]
            l = pair["wins_2"] if is_first else pair["wins_1"]
            t = pair["ties"]
            pts_for = pair["pts_1"] if is_first else pair["pts_2"]
            pts_against = pair["pts_2"] if is_first else pair["pts_1"]

            # Title Game checks
            won_title_against = []
            lost_title_against = []
            for g in pair["games_log"]:
                tag, _ = self._game_stage(g, rid, opp_rid)
                if tag == h2h_builder.FINAL:
                    if g["winner"] == my_manager:
                        won_title_against.append(g["season"])
                    elif g["winner"] == opp_mgr:
                        lost_title_against.append(g["season"])

            special_note = ""
            if won_title_against:
                tag = f"🏆 [CAMPEÃO {won_title_against[0]}]"
                special_note = f"Campeão da Liga sobre o rival na Grande Final de {won_title_against[0]}!"
            elif lost_title_against:
                tag = f"🥈 [VICE {lost_title_against[0]}]"
                special_note = f"Vice na Grande Final de {lost_title_against[0]}"
            elif w > l:
                tag = "👑 [DOMINA]"
            elif l > w:
                tag = "🥔 [FREGUÊS]"
            else:
                tag = "🤝 [EQUILÍBRIO]"

            results.append({
                "opponent_roster": opp_rid,
                "opponent": opp_mgr,
                "opponent_team": opp_team,
                "wins": w,
                "losses": l,
                "ties": t,
                "games_total": pair["games_total"],
                "games_reg": pair["games_reg"],
                "games_playoffs": pair["games_playoffs"],
                "games_toilet": pair.get("games_toilet", 0),
                "pts_for": str(round(pts_for, 2)),
                "pts_against": str(round(pts_against, 2)),
                "blowout": pair["biggest_blowout"],
                "closest": pair["closest_match"],
                "tag": tag,
                "special_note": special_note,
                "won_title": len(won_title_against) > 0,
            })

        # Sort: Titles won over rival first, then net wins, then net points
        results.sort(key=lambda x: (x["won_title"], (x["wins"] - x["losses"]), float(x["pts_for"]) - float(x["pts_against"])), reverse=True)
        return results

    # ---- history queries: SQLite index (kb_db) ------------------------------------------------------
    def _db_query(self, fn, *args, **kwargs) -> str:
        """Run a kb_queries reader on a fresh read-only connection."""
        import kb_db
        con = kb_db.connect()
        try:
            return fn(con, *args, **kwargs)
        finally:
            con.close()

    def get_records_formatted(self) -> str:
        import kb_queries
        return self._db_query(kb_queries.records_report)

    def get_draft_picks_dossier(self, year: str = "2024") -> str:
        import kb_queries
        return self._db_query(kb_queries.draft_picks_report, year)

    def get_manager_draft_report(self, manager_query: str) -> str:
        import kb_queries
        return self._db_query(kb_queries.manager_draft_report, manager_query)

    def get_trade_picks_dossier(self, manager_query: str) -> str:
        import kb_queries
        return self._db_query(kb_queries.trade_picks_report, manager_query)

    def get_trades_query(self, query: Optional[str] = None, filter_type: Optional[str] = None, limit: int = 12,
                         ano: Optional[str] = None, semana: Optional[int] = None, detalhe: bool = False,
                         contra: Optional[str] = None) -> str:
        """Trades filtered in SQL (query: blockbusters|picks|convertidas|<ano>|<manager>|<texto>; contra = 2nd manager)."""
        import kb_queries
        return self._db_query(kb_queries.trades_report, query or filter_type, limit, ano, semana, detalhe, contra)

    def get_top_trades(self) -> str:
        return self.get_trades_query(query="blockbusters", limit=10)

    def get_stats(self, **kw) -> str:
        import kb_stats
        return kb_stats.run(**kw)


    def get_playoffs_history(self) -> Dict[str, Any]:
        """Load playoffs and divisions history."""
        if hasattr(self, "_playoffs_cache") and self._playoffs_cache is not None:
            return self._playoffs_cache

        data = self._load_json("playoffs_history.json") or {}
        self._playoffs_cache = data
        return data

    def get_standings_formatted(self, season: Optional[str] = None, live_standings: Optional[List[Dict[str, Any]]] = None, completed_week: Optional[int] = None) -> str:
        """Get formatted standings with 3 divisions + overall + playoffs/toilet bowl for any season."""
        playoffs_data = self.get_playoffs_history()
        current = self._current_season()
        yr = str(season).strip() if season else current
        prof = self.ctx.profile
        icons = {str(k): v for k, v in (self.ctx.config.get("division_icons") or {}).items()}

        if yr not in playoffs_data:
            yr = current if current in playoffs_data else sorted(playoffs_data.keys())[-1]

        data = playoffs_data.get(yr, {})
        divisions = data.get("divisions", {})
        overall = data.get("overall_standings", [])
        title_info = data.get("playoffs_title", {})
        toilet_info = data.get("toilet_bowl", {})

        if live_standings is None and yr == current:
            try:
                from sleeper_client import SleeperClient
                s_client = SleeperClient()
                live_standings = s_client.get_standings()
                if completed_week is None:
                    nfl_state = s_client.get_nfl_state()
                    completed_week = max(1, nfl_state.get("week", 3) - 1)
            except Exception as e:
                print(f"[WARN] Failed to load live standings for {yr}: {e}")

        if live_standings:
            divisions = {}
            for t in live_standings:
                did = str(t["division_id"])
                if did not in divisions:
                    divisions[did] = {
                        "division_id": t["division_id"],
                        "division_name": t["division_name"],
                        "teams": []
                    }
                divisions[did]["teams"].append({
                    "roster_id": t.get("roster_id"),
                    "team_name": t["team_name"],
                    "manager_name": t.get("owner_name") or t.get("manager_name", ""),
                    "wins": t["wins"],
                    "losses": t["losses"],
                    "ties": t.get("ties", 0),
                    "pts_for": t["pts_for"],
                    "pts_against": t["pts_against"],
                    "division_rank": t["division_rank"],
                })
            for did in divisions:
                divisions[did]["teams"].sort(key=lambda x: x["division_rank"])

            overall = []
            for t in sorted(live_standings, key=lambda x: x["rank"]):
                overall.append({
                    "roster_id": t.get("roster_id"),
                    "team_name": t["team_name"],
                    "manager_name": t.get("owner_name") or t.get("manager_name", ""),
                    "wins": t["wins"],
                    "losses": t["losses"],
                    "ties": t.get("ties", 0),
                    "pts_for": t["pts_for"],
                    "pts_against": t["pts_against"],
                    "overall_rank": t["rank"],
                    "division_name": t["division_name"],
                })

        if completed_week is not None:
            status_txt = f" (Temporada Em Andamento — Semana {completed_week} Concluída)"
        elif yr == current:
            status_txt = " (Temporada Em Andamento)"
        else:
            status_txt = " (Temporada Regular Completa)"

        lines = [
            f"🏆 *CLASSIFICAÇÃO OFICIAL — {self.ctx.name.upper()} ({yr})*",
            f"_{status_txt.strip()}_",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"📍 *1. CLASSIFICAÇÃO POR DIVISÃO ({len(divisions)} DIVISÕES DA LIGA):*",
        ]

        div_icons = icons
        for div_id in sorted(divisions.keys(), key=lambda x: int(x)):
            d_info = divisions[div_id]
            d_name = d_info.get("division_name", f"Divisão {div_id}")
            icon = div_icons.get(str(div_id), "📍")
            lines.append(f"\n{icon} *{d_name.upper()} (Divisão {div_id})*")
            for t in d_info.get("teams", []):
                rec = f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t.get("ties", 0) > 0 else "")
                mgr = t.get("manager_name") or t.get("canonical_manager") or t.get("manager", "")
                tname = t.get("team_name", f"Time {t.get('roster_id')}")
                pf = t.get("pts_for", 0.0)
                pa = t.get("pts_against", 0.0)
                lines.append(f"  {t.get('division_rank', 1)}º *{tname}* ({mgr}): *{rec}* | PF: {pf:.2f} | PA: {pa:.2f}")

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🌐 *2. CLASSIFICAÇÃO GERAL DA TEMPORADA REGULAR ({yr}):*",
        ])

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for t in sorted(overall, key=lambda x: x.get("overall_rank", x.get("regular_season_rank", x.get("rank", 1)))):
            r = t.get("overall_rank", t.get("regular_season_rank", t.get("rank", 1)))
            ic = medals.get(r, f"{r:2d}.")
            rec = f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t.get("ties", 0) > 0 else "")
            mgr = t.get("manager_name") or t.get("canonical_manager") or t.get("manager", "")
            tname = t.get("team_name", f"Time {t.get('roster_id')}")
            pf = t.get("pts_for", 0.0)
            dname = t.get("division_name", "")
            lines.append(f"{ic} *{tname}* ({mgr}) — *{rec}* ({pf:.2f} pts) [{dname}]")

        if yr != current and title_info and title_info.get("champion") not in (None, "", "A Definir"):
            lines.extend([
                "",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"👑 *3. PLAYOFFS DO TÍTULO — PÓS-TEMPORADA ({yr}):*",
                f"*(Disputa entre os {prof.playoff_teams or 6} melhores da temporada regular)*",
                f"🏆 *CAMPEÃO DA LIGA:* *{title_info['champion']}*",
            ])
            for fmt, key in (("🥈 *Vice-Campeão:* *{}*", "runner_up"), ("🥉 *3º Lugar:* *{}*", "third_place"),
                             ("   • 4º Lugar: {}", "fourth_place"), ("   • 5º Lugar: {}", "fifth_place"),
                             ("   • 6º Lugar: {}", "sixth_place")):
                if title_info.get(key) not in (None, "", "None"):         # a 4-team bracket has no 5th/6th
                    lines.append(fmt.format(title_info[key]))
            if toilet_info.get("toilet_champion") not in (None, "", "None"):
                first = (prof.playoff_teams or 6) + 1                      # consolation starts right below the bracket
                lines.extend([
                    "",
                    "━━━━━━━━━━━━━━━━━━━━━━",
                    f"🚽 *4. TOILET BOWL — COMPETIÇÃO DE CONSOLAÇÃO ({yr}):*",
                    f"*(Competição à parte entre os que ficaram fora dos playoffs — {self._consolation_span()})*",
                    f"• 🚽 *Campeão do Toilet Bowl:* *{toilet_info.get('toilet_champion')}*",
                ])
                for fmt, key in (("• 🥈 *Vice do Toilet Bowl:* *{}*", "toilet_runner_up"),
                                 (f"• {first + 2}º Lugar: {{}}", "ninth_place"),
                                 (f"• {first + 3}º Lugar: {{}}", "tenth_place"),
                                 (f"• {first + 4}º Lugar: {{}}", "eleventh_place"),
                                 ("• 💩 *LANTERNA OFICIAL DA TEMPORADA:* *{}*", "lanterna")):
                    if toilet_info.get(key) not in (None, "", "None"):
                        lines.append(fmt.format(toilet_info[key]))
        elif yr == current:
            lines.extend([
                "",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"⏳ *PÓS-TEMPORADA {yr} (PLAYOFFS & TOILET BOWL):*",
                f"• _A temporada regular {yr} está em andamento. Os Playoffs do Título (Top {prof.playoff_teams or 6}) e o "
                f"Toilet Bowl ({self._consolation_span()}) ocorrem a partir da Semana {prof.playoff_week_start or 15}!_",
            ])

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⚡ _Atualizado via Sleeper Bot & Hermes Agent_",
        ])
        return "\n".join(lines)

    def get_playoffs_dossier(self, season: Optional[str] = None) -> str:
        """Get playoffs and toilet bowl history dossier."""
        playoffs_data = self.get_playoffs_history()
        if season and str(season).strip() in playoffs_data:
            return self.get_standings_formatted(season)

        # Full gallery of champions and consolation bracket, from the league's own playoff history
        seasons = sorted(y for y, d in playoffs_data.items() if isinstance(d, dict))
        span = f"{seasons[0]}-{seasons[-1]}" if seasons else ""
        prof = self.ctx.profile
        lines = [
            f"🏆 *GALERIA HISTÓRICA — PLAYOFFS & TOILET BOWL ({span})*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"👑 *1. CAMPEÕES DO TÍTULO (CHAMPIONSHIP BRACKET — TOP {prof.playoff_teams or 6}):*",
        ]
        done = [(y, playoffs_data[y].get("playoffs_title") or {}) for y in seasons]
        done = [(y, t) for y, t in done if t.get("champion") not in (None, "", "A Definir")]
        if not done:
            return "ℹ️ Ainda não há playoffs concluídos no histórico desta liga."
        for yr, t in done:
            lines.append(f"• Temporada *{yr}*: 🏆 *{t['champion']}* | 🥈 {t.get('runner_up')} | 🥉 {t.get('third_place')}")

        def place(text: Any) -> str:     # "Team (mgr) [7º Geral - Lanterna ...]" -> "Team (mgr) [7º]"
            return re.sub(r"\[(\d+º)[^\]]*\]", r"[\1]", str(text))
        toilets = [(y, (playoffs_data[y].get("toilet_bowl") or {})) for y, _ in done]
        toilets = [(y, t) for y, t in toilets if t.get("toilet_champion")]
        if toilets:
            lines.extend([
                "",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"🚽 *2. HISTÓRICO DO TOILET BOWL (COMPETIÇÃO À PARTE — {self._consolation_span().upper()}):*",
            ])
            for yr, t in toilets:
                lines.append(f"• Temporada *{yr}*: 🚽 Campeão: *{place(t['toilet_champion'])}* | 💩 Lanterna: *{place(t.get('lanterna'))}*")
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ _{self._persona()}: Na {self.ctx.name}, o anel de campeão traz a glória eterna, mas o campeão do Toilet Bowl "
            "celebra não ter ficado com o troféu de cocô na estante!_",
        ])
        return "\n".join(lines)

    def get_manager_psychological_profile(self, query: str) -> str:
        """Behavioral profile of a manager (section of MANAGERS_PSYCHOLOGICAL_PROFILES.md, indexed in the DB)."""
        import kb_queries
        return self._db_query(kb_queries.profile_report, query)


    def get_team_roster_analysis(self, query: str = "") -> str:
        """Return full team and roster dossier for a given manager or franchise name."""
        content = self._read_file("TEAMS_AND_ROSTERS_ANALYSIS.md")
        if not content:
            return "❌ Documento TEAMS_AND_ROSTERS_ANALYSIS.md não encontrado na base de conhecimento."

        q_clean = query.strip().lower()
        if not q_clean or q_clean in ["all", "todos", "geral", "liga"]:
            teams_json = self._load_json("teams_rosters_analysis.json")
            if not teams_json:
                return "❌ teams_rosters_analysis.json não encontrado."
            lines = [
                f"🛡️ *PANORAMA GERAL DOS ELENCOS: {self.ctx.name.upper()} ({self._current_season()})*",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            for rid, t in sorted(teams_json.items(), key=lambda x: int(x[0])):
                lines.append(
                    f"• *{t['current_team']}* ({t['canonical_manager']}) | {t['division']} | {t['record']} ({t['points_for']} PF) | *{t['competitive_status']}*"
                )
            lines.extend([
                "━━━━━━━━━━━━━━━━━━━━━━",
                "💡 _Para ver o dossiê detalhado de um time (11 titulares prováveis, banco por posição, estrelas, pontos fortes e carências), use: `sleeper_cli.py team_analysis <manager_ou_time>`_",
            ])
            return "\n".join(lines)

        # who is being asked about: the league's people registry knows names, nicknames and team names
        rid = self.resolve_roster(query)
        entry = self.ctx.people.entries.get(rid) if rid else None
        matched_key = (entry.get("username") or entry.get("team")) if entry else q_clean

        sections = content.split("\n## 🏈 ")
        for sec in sections[1:]:
            header = sec.split("\n", 1)[0]
            if (matched_key.lower() in header.lower() or q_clean in sec.lower()
                    or (entry and str(entry.get("team", "")).lower() in header.lower())):
                body = sec.split("\n---\n", 1)[0].strip()
                wa_lines = [f"🏈 *{header.replace('*', '')}*"]
                rest = body.split("\n", 1)[1] if "\n" in body else ""
                for line in rest.split("\n"):
                    if line.startswith("### "):
                        title = line.replace("### ", "").strip()
                        wa_lines.append(f"\n*{title}*")
                    else:
                        clean_line = line.replace("**", "*")
                        wa_lines.append(clean_line)
                return "\n".join(wa_lines).strip()

        return f"❌ Franquia ou treinador '{query}' não encontrado no dossiê de elencos. Tente pelo nome do time ou manager (ex: " + ", ".join(
            [e["team"] for _, e in sorted(self.ctx.people.entries.items())[:3]]) + ")."

    def get_waivers_report(self, query: str = "", limit: int = 12, ano: Optional[str] = None) -> str:
        """Waiver/FA market history (steals|fiascos|bids|<manager>|<jogador>|<ano>|geral), computed in SQL."""
        import kb_queries
        return self._db_query(kb_queries.waivers_report, query, limit, ano)


    # -------------------------------------------------------------------------
    # Dossiê e Árvore Genealógica de Atletas (Origem, Trocas, Waivers e Elenco)
    # -------------------------------------------------------------------------
    def get_player_dossier(self, query: str) -> str:
        """Fetch complete career lineage, origin, trades, waivers, and current status of any athlete."""
        registry_path = self.ctx.source_path("players_registry.json")
        if not os.path.exists(registry_path):
            # no curated lineage registry (a league bootstrapped from Sleeper): build the career from the index
            import kb_queries
            return self._db_query(kb_queries.player_report, query, self._live_owner(query))

        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return f"❌ Erro ao ler players_registry.json: {e}"

        players = data.get("players", {})
        q = query.strip().lower()

        # Match by player_id exactly or by name substring
        matched = []
        if q in players:
            matched.append(players[q])
        else:
            for p in players.values():
                p_name = p.get("name", "").lower()
                if q == p_name or q in p_name:
                    matched.append(p)

        if not matched:
            return f"❌ Nenhum atleta encontrado na história da liga para o termo '{query}'. Verifique a grafia ou tente o sobrenome do jogador."

        # Se encontrou mais de um (ex: 'Allen' -> Josh Allen, Keenan Allen)
        p = matched[0]
        extra_note = ""
        if len(matched) > 1:
            other_names = ", ".join([f"{m['name']} ({m['position']})" for m in matched[1:4]])
            extra_note = f"\n_(Também encontramos: {other_names})_"

        name = p.get("name")
        pos = p.get("position")
        team_nfl = p.get("team_nfl")
        inj = p.get("status_nfl", "Active")
        orig = p.get("origin", {})
        status_key = next((k for k in sorted(p, reverse=True) if k.startswith("current_status_")), "current_status")
        cur = p.get(status_key, {})
        status_year = status_key.rsplit("_", 1)[-1] if status_key[-4:].isdigit() else self._current_season()
        mkt = p.get("market_summary", {})
        timeline = p.get("career_timeline", [])

        # Origem
        orig_type = orig.get("type", "desconhecido")
        if orig_type == "draft":
            orig_desc = f"Selecionado no *{orig.get('event_name')}* (Rodada {orig.get('round')}, Escolha Geral #{orig.get('pick_no')}) por *{orig.get('original_manager')}* ({orig.get('original_team_name')})"
        elif orig_type == "waiver":
            orig_desc = f"Garimpado via *Waiver Wire* (${orig.get('bid', 0)} FAAB) por *{orig.get('original_manager')}* ({orig.get('original_team_name')}) na Semana {orig.get('week')} de {orig.get('season')}"
        elif orig_type == "free_agent":
            orig_desc = f"Adicionado como *Free Agent* ($0 FAAB) por *{orig.get('original_manager')}* ({orig.get('original_team_name')}) na Semana {orig.get('week')} de {orig.get('season')}"
        else:
            orig_desc = "Origem histórica não indexada"

        # Status Atual
        lifer_badge = " 👑 *FRANCHISE LIFER (4 ANOS NO MESMO TIME)*" if cur.get("is_franchise_lifer") else ""
        if cur.get("is_owned"):
            role_map = {
                "starter": f"⭐ Titular da Franquia (Slot: {cur.get('slot')})",
                "bench": f"📦 Profundidade de Banco ({cur.get('slot')})",
                "taxi_squad": "🍼 Taxi Squad (Aposta/Stash)",
                "reserve_ir": "🩹 Departamento Médico (IR)",
            }
            role_desc = role_map.get(cur.get("role"), cur.get("role"))
            cur_desc = (
                f"• *Franquia Atual*: *{cur.get('team_name')}* (Treinador: *{cur.get('manager')}* / {cur.get('real_name')})\n"
                f"• *Status no Elenco*: {role_desc}{lifer_badge}\n"
                f"• *Forma de Chegada ao Time*: {cur.get('acquisition_detail')} {cur.get('acquisition_badge')}\n"
                f"• *Produção na temporada*: {cur.get('season_pts', 0.0):.2f} pts"
            )
        else:
            cur_desc = "• *Status Atual*: 🆓 *Livre no Mercado / Sem Time na Liga* (Disponível na Waiver Wire / FA)"

        # Resumo de Mercado
        teams_held_str = " ➔ ".join(mkt.get("teams_held", [])) if mkt.get("teams_held") else "Apenas o time de origem"
        mkt_desc = (
            f"• *Trocas Oficiais*: {mkt.get('total_trades', 0)} trocas na carreira\n"
            f"• *Movimentações de Waiver/FA*: {mkt.get('total_waiver_adds', 0)} via waiver + {mkt.get('total_fa_adds', 0)} via FA | {mkt.get('total_drops', 0)} cortes\n"
            f"• *FAAB Queimado no Atleta*: ${mkt.get('total_faab_invested_league', 0)} FAAB investidos na liga\n"
            f"• *Franquias que já defenderam*: {teams_held_str}"
        )

        lines = [
            f"📋 *RAIO-X DE CARREIRA: {name.upper()} ({pos} - {team_nfl})*",
            f"Status NFL: _{inj}_ | ID Sleeper: #{p.get('player_id')}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🐣 *Certidão de Nascimento na Liga (Origem):*\n• {orig_desc}",
            "",
            f"🏰 *Situação em {status_year}:*\n{cur_desc}",
            "",
            f"📈 *Dinâmica de Mercado & Negociações:*\n{mkt_desc}",
            "",
            "⏳ *Linha do Tempo Completa na Liga:*",
        ]

        for ev in timeline:
            lines.append(f"• {ev.get('date', '')} ({ev.get('season')}): {ev.get('summary')}")

        # Contested Bids
        bids = p.get("contested_bids", [])
        if bids:
            lines.append("\n⚔️ *Guerras de Lances / Disputas Perdidas:*")
            for b in bids[:4]:
                lines.append(f"• {b.get('date')} ({b.get('season')} W{b.get('week')}): *{b.get('manager')}* deu lance de ${b.get('bid')} FAAB e perdeu.")

        lines.extend([
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"🎙️ *Comentário do {self._persona()}:*",
        ])

        if cur.get("is_franchise_lifer"):
            lines.append(f"Fidelidade canina! *{cur.get('manager')}* segurou {name} contra vento e chuva desde o primeiro dia de vida da liga. Um verdadeiro patrimônio histórico da franquia.")
        elif mkt.get("total_trades", 0) >= 2:
            lines.append(f"O verdadeiro caixeiro-viajante da liga! {name} já foi moeda de troca por {mkt.get('total_trades')} vezes e passou pela mão de meio mundo.")
        elif orig_type in ["waiver", "free_agent"] and cur.get("role") == "starter":
            lines.append(f"Aquele famoso ouro no lixo! O gestor achou {name} jogado na calçada e hoje o cidadão é titular provável da equipe. Cinema puro.")
        elif orig_type == "waiver" and orig.get("bid", 0) > 100 and not cur.get("is_owned"):
            lines.append(f"Queima criminosa de FAAB! Gastaram uma nota preta no rapaz e hoje ele não serve nem para amarrar a chuteira no banco de ninguém.")
        else:
            lines.append(f"{name} segue firme na rotação. Peça sólida com histórico transparente nos registros da liga.")

        if extra_note:
            lines.append(extra_note)

        return "\n".join(lines)

    def get_roster_dna_report(self, query: str = "") -> str:
        """Fetch Roster Construction DNA (Draft vs Trades vs Waivers) for a specific team or the whole league."""
        registry_path = self.ctx.source_path("players_registry.json")
        if not os.path.exists(registry_path):
            return "❌ Base de dados de atletas (players_registry.json) não encontrada."

        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return f"❌ Erro ao ler players_registry.json: {e}"

        team_dna = data.get("team_dna", {})
        q = query.strip().lower()

        # Se não especificou time, retorna tabela geral
        if not q:
            lines = [
                f"🧬 *ROSTER DNA — COMO CADA GESTOR MONTOU SEU ELENCO ({self._current_season()})*",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            for rid in sorted(team_dna.keys(), key=lambda x: int(x)):
                d = team_dna[rid]
                lines.append(
                    f"• *{d['team_name']}* ({d['manager']}):\n"
                    f"  🎓 Draft: *{d['drafted_pct']}%* ({d['drafted_count']}) | "
                    f"🤝 Trocas: *{d['trades_pct']}%* ({d['trades_count']}) | "
                    f"🗑️ Waivers: *{d['waivers_fa_pct']}%* ({d['waivers_count'] + d['free_agents_count']})\n"
                    f"  👑 Lifers de {self._first_season() or 'origem'}: *{len(d.get('lifers', []))} atletas*"
                )
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("💡 Use `python3 sleeper_cli.py roster-dna <nome_do_time>` para raio-x detalhado de um gestor.")
            return "\n".join(lines)

        # Buscar franquia por query
        target_dna = None
        resolved = self.resolve_roster(query)  # whole-word registry first: "ari" must not match "marinh012"
        if resolved is not None and str(resolved) in team_dna:
            target_dna = team_dna[str(resolved)]
        for rid, d in ([] if target_dna else team_dna.items()):
            if (
                q == str(rid)
                or q in d["manager"].lower()
                or q in d["real_name"].lower()
                or q in d["team_name"].lower()
            ):
                target_dna = d
                break

        if not target_dna:
            return (f"❌ Nenhuma franquia encontrada para '{query}'. Tente por nome do manager "
                    f"(ex: {self.ctx.people.hint(4)}) ou do time.")

        d = target_dna
        lines = [
            f"🧬 *ROSTER DNA: {d['team_name'].upper()}*",
            f"Treinador: *{d['manager']}* ({d['real_name']}) | Total de Jogadores: {d['total_players']}",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"• 🎓 *Formação Própria (Draft)*: *{d['drafted_count']} jogadores ({d['drafted_pct']}%)*",
            f"• 🤝 *Adquiridos via Troca*: *{d['trades_count']} jogadores ({d['trades_pct']}%)*",
            f"• 🗑️ *Garimpados em Waivers/FA*: *{d['waivers_count'] + d['free_agents_count']} jogadores ({d['waivers_fa_pct']}%)*",
            "",
            f"👑 *Franchise Lifers ({len(d.get('lifers', []))} atletas fiéis desde o Startup {self._first_season()}):*",
        ]

        if d.get("lifers"):
            for l in d["lifers"]:
                lines.append(f"• {l}")
        else:
            lines.append(f"• Nenhum lifer de {self._first_season()}. O gestor renovou 100% dos ativos originais.")

        lines.append("\n🤝 *Principais Ativos Trazidos por Trocas:*")
        if d.get("key_trade_assets"):
            for a in d["key_trade_assets"]:
                lines.append(f"• {a}")
        else:
            lines.append("• Nenhuma peça de rotação adquirida via troca.")

        lines.append("\n🗑️ *Principais Achados no Lixo (Waivers/FA):*")
        if d.get("key_waiver_assets"):
            for w in d["key_waiver_assets"]:
                lines.append(f"• {w}")
        else:
            lines.append("• Nenhuma peça relevante vinda do mercado livre.")
        return "\n".join(lines)

    def get_draft_order_formatted(self, season: str = "2027") -> str:
        """Get formatted draft order by Max PF with traded picks indicated."""
        from draft_order_service import DraftOrderService
        dos = DraftOrderService(base_dir=self.base_dir)
        return dos.format_draft_order_whatsapp(season=season)

    def get_max_pf_formatted(self, week: Optional[int] = None) -> str:
        """Get weekly Max PF breakdown with wasted points on bench."""
        from draft_order_service import DraftOrderService
        dos = DraftOrderService(base_dir=self.base_dir)
        return dos.format_weekly_max_pf_whatsapp(week=week)


if __name__ == "__main__":
    kb = KnowledgeBaseService()
    print("--- Teste H2H eduohira vs FloripaAstros ---")
    print(kb.get_h2h("eduohira", "FloripaAstros"))
    print("\n--- Teste Standings 2024 com Divisões e Toilet Bowl ---")
    print(kb.get_standings_formatted("2024"))
    print("\n--- Teste Galeria de Playoffs ---")
    print(kb.get_playoffs_dossier())

