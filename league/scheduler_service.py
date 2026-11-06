#!/usr/bin/env python3
"""Weekly editorial scheduler of the league (Brasília time), run by the `editorial-slots` cron every 5 min.

Slots (see `WeeklyScheduler.slot_table`):
- Ter 08h: fechamento → classificação → power ranking → chances de playoff (a partir da semana
       PLAYOFF_ODDS_FROM_WEEK) → ordem do próximo draft · 10h: lesões da rodada (fora do bloco das 08h para não
       empilhar mensagens)
- Qua 08h: radar do waiver wire (só QB/RB/WR/TE: a liga não tem vaga para K, DEF nem IDP)
- Qui 08h: plantão do waiver (lista completa) · 09h: termômetro · 10h–15h: um card por confronto (1/h)
- Sáb 20h: checagem de escalação + jogos cedo de domingo (DM opcional para quem tem telefone cadastrado)
- Dom 08h: waivers + cronograma · 09h: parciais · 10h: boletim médico · inativos 90 min antes de cada
       janela cedo (jogo internacional) · kickoff principal −120 min: aquecimento · −90 min: inativos
       balanço após a 1ª onda (13h ET), após a 2ª onda (16h ET) e após o SNF — cada um sai quando todos os jogos
       da onda terminam (ESPN), no máximo 5 min antes da onda seguinte; o pós-SNF pode sair segunda de madrugada
- Seg 08h: especial Monday Night Football

Every message uses msg_format.Msg (one layout, auto-split into parts). A slot is marked as sent only when
Evolution confirms every part; a partially sent slot resumes from the next part. Failures alert the admin once.
"""

import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import flavor
import league_ctx
import league_profile
import league_time
from evolution_client import EvolutionClient, sent_ok
from knowledge_service import KnowledgeBaseService
from msg_format import Msg, split_parts
from news_service import NewsService
from sleeper_client import SleeperClient

logger = logging.getLogger("SleeperScheduler")

BRT = ZoneInfo("America/Sao_Paulo")   # default zone; a league sets its own with `timezone:` in league.yaml
# Dedupe/failure state files are per league (league_ctx state dir). Tests may pin a path here.
DISPATCH_LOG_FILE: Optional[str] = None
FAILURES_FILE: Optional[str] = None
SLOT_WINDOW_MIN = 60
INJURED = ("Out", "IR", "Doubtful", "Sus", "PUP", "NFI", "DNR")


def _dispatch_file() -> str:
    return DISPATCH_LOG_FILE or league_ctx.current().state_path("dispatched_slots.json")


def _failures_file() -> str:
    return FAILURES_FILE or league_ctx.current().state_path("dispatch_failures.json")


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"Failed to load {path}: {e}")
        return {}


def _save(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_dispatched_slots() -> Dict[str, str]:
    return _load(_dispatch_file())


def save_dispatched_slot(slot_id: str):
    data = load_dispatched_slots()
    data[slot_id] = datetime.now(ZoneInfo(league_ctx.current().timezone)).isoformat()
    _save(_dispatch_file(), data)


def _pick(options: List[str], seed: int) -> str:
    return options[seed % len(options)]


def _env_on(name: str, default: str = "0") -> bool:
    return (os.environ.get(name, default) or "").lower() in ("1", "true", "yes", "on")


class WeeklyScheduler:
    def __init__(self, league_id: Optional[str] = None, group_jid: Optional[str] = None,
                 evolution_url: Optional[str] = None, evolution_key: Optional[str] = None,
                 evolution_instance: Optional[str] = None):
        self.ctx = league_ctx.current()
        self.league_id = league_id or self.ctx.league_id
        # No default group: running the engine outside the container must never post to the real group.
        self.group_jid = group_jid or self.ctx.group_jid
        self.tz = ZoneInfo(self.ctx.timezone)
        self.sleeper = SleeperClient(league_id=self.league_id)
        self.evolution = EvolutionClient(
            base_url=evolution_url or os.environ.get("EVOLUTION_API_URL", "http://evolution-api:8080"),
            api_key=evolution_key or os.environ.get("EVOLUTION_API_KEY", ""),
            instance=evolution_instance or os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot"))
        self.kb = KnowledgeBaseService()
        self.news = NewsService(league_id=self.league_id)
        self._standings_cache: Optional[Dict[int, Dict[str, Any]]] = None
        self._after_send: Dict[str, Callable[[], None]] = {}

    # ------------------------------------------------------------------ helpers
    def _state(self) -> Tuple[str, int]:
        st = self.sleeper.get_nfl_state()
        return str(st.get("season") or datetime.now(self.tz).year), int(st.get("week") or 1)

    @property
    def profile(self):
        """Roster structure and rules of THIS league (Sleeper settings), never constants."""
        return self.ctx.profile

    def _league_name(self) -> str:
        return self.sleeper.get_league().get("name") or self.ctx.name

    def _faab_budget(self) -> int:
        return int((self.sleeper.get_league().get("settings") or {}).get("waiver_budget") or 100)

    def _standings_by_roster(self) -> Dict[int, Dict[str, Any]]:
        if self._standings_cache is None:
            try:
                self._standings_cache = {s["roster_id"]: s for s in self.sleeper.get_standings()}
            except Exception:
                self._standings_cache = {}
        return self._standings_cache

    def _record(self, roster_id: Any) -> str:
        s = self._standings_by_roster().get(roster_id)
        if not s:
            return ""
        return f"{s['wins']}-{s['losses']}" + (f"-{s['ties']}" if s.get("ties") else "") + f", {s['rank']}º geral"

    def _player(self, pid: str) -> Dict[str, Any]:
        return self.news.get_player_info(pid)

    def _player_label(self, pid: str, with_team: bool = True) -> str:
        p = self._player(pid)
        name = p.get("name") or pid
        return f"*{name}* ({p.get('pos') or '?'} - {p.get('team') or 'FA'})" if with_team else f"*{name}*"

    def _alltime_scores(self) -> List[float]:
        out = []
        for g in self.kb._load_json("matchups_history.json") or []:
            for k in ("points_a", "points_b"):
                v = float(g.get(k) or 0.0)
                if v > 0:
                    out.append(v)
        return out

    def _sunday(self, week: int) -> Dict[str, Any]:
        return league_time.sunday_schedule(week)

    def _signature(self, facts: Dict[str, Any], task: str, fallback: str) -> str:
        return "🎙️ " + flavor.line(facts, task, fallback)

    # =========================================================================
    # TERÇA
    # =========================================================================
    def generate_tuesday_recap(self, week: Optional[int] = None) -> str:
        season, current_week = self._state()
        week = week or max(1, current_week - 1)
        matchups = self.sleeper.get_matchups(week)
        teams = [t for m in matchups for t in (m.get("team1"), m.get("team2")) if t]
        msg = Msg("Fechamento da rodada", f"Semana {week} · {season}", emoji="🚨")
        if not teams:
            return msg.text(f"Sem dados registrados para a Semana {week}.").render()

        teams.sort(key=lambda x: x.get("points", 0.0), reverse=True)
        top, low = teams[0], teams[-1]
        hist = self._alltime_scores()
        rank_high = sum(1 for v in hist if v > top["points"]) + 1 if hist else 99
        rank_low = sum(1 for v in hist if v < low["points"]) + 1 if hist else 99
        destaque = [f"• 👑 *Rei:* {top['team_name']} — {top['points']:.2f} pts"
                    + (f" (_{rank_high}ª maior da história_)" if rank_high <= 20 else ""),
                    f"• 🥔 *Mico:* {low['team_name']} — {low['points']:.2f} pts"
                    + (f" (_{rank_low}ª pior da história_)" if rank_low <= 10 else "")]

        results, winners, losers = [], [], []
        for m in sorted(matchups, key=lambda m: m["matchup_id"]):
            t1, t2 = m.get("team1"), m.get("team2")
            if not t2:
                results.append(f"• {t1['team_name']} ({t1['points']:.2f}) — bye")
                continue
            diff = abs(t1["points"] - t2["points"])
            if t1["points"] == t2["points"]:
                results.append(f"• 🤝 {t1['team_name']} {t1['points']:.2f} x {t2['points']:.2f} {t2['team_name']}")
                continue
            w, l = (t1, t2) if t1["points"] > t2["points"] else (t2, t1)
            winners.append((w, l))
            losers.append((l, w))
            tag = "💥 " if diff >= 35 else ("🔥 " if diff <= 5 else "")
            results.append(f"• {tag}*{w['team_name']}* {w['points']:.2f} x {l['points']:.2f} {l['team_name']}")
            results.append(f"   ↳ H2H: {self._get_quick_h2h(w['owner_name'], l['owner_name'])}")

        premios = []
        if winners:
            lucky = min(winners, key=lambda x: x[0]["points"])
            premios.append(f"• 🍀 *Cagão:* {lucky[0]['team_name']} venceu com só {lucky[0]['points']:.2f}")
        if losers:
            median = sorted(t["points"] for t in teams)[len(teams) // 2]
            unlucky = max(losers, key=lambda x: x[0]["points"])
            if unlucky[0]["points"] > median:
                premios.append(f"• 💔 *Injustiçado:* {unlucky[0]['team_name']} fez {unlucky[0]['points']:.2f} e perdeu "
                               f"para {unlucky[1]['team_name']}")

        msg.section("Destaques", destaque).section("Resultados", results).section("Prêmios", premios)
        msg.text(self._signature({"mico": f"{low['team_name']} fez {low['points']:.2f} pts",
                                  "rei": f"{top['team_name']} fez {top['points']:.2f} pts"},
                                 "Zoe o mico da semana e exalte o rei, numa frase.",
                                 "Quem ganhou tem licença pra zoar; quem perdeu, prepara o bolso pro FAAB."))
        return msg.footer("Em instantes: classificação e power ranking.").render()

    def generate_tuesday_standings(self, week: Optional[int] = None) -> str:
        season, current_week = self._state()
        week = week or max(1, current_week - 1)
        return self.kb.get_standings_formatted(season, live_standings=self.sleeper.get_standings(),
                                               completed_week=week) or ""

    def generate_tuesday_power(self) -> str:
        import analytics
        rows = analytics.power_ranking(self.sleeper)
        if not rows:
            return ""
        _, week = self._state()
        msg = Msg("Power ranking", f"Após a Semana {analytics.completed_weeks(self.sleeper)} · "
                                   f"all-play = contra os {max(1, len(rows) - 1)} times toda semana", emoji="📈")
        msg.section("Ranking", [f"{r['rank']}. *{r['team']}* — {r['score']} pts | real {r['record']} · all-play "
                                f"{r['all_play']} · {r['ppg']} pts/jogo" for r in rows])
        lucky = sorted(rows, key=lambda r: -r["luck"])
        bench = sorted(rows, key=lambda r: -r["bench_pts"])
        msg.section("Sorte e azar", [
            f"• 🍀 Mais sortudo: *{lucky[0]['team']}* ({lucky[0]['record']} na vida real, {lucky[0]['all_play']} no all-play)",
            f"• 😤 Mais azarado: *{lucky[-1]['team']}* ({lucky[-1]['record']} na vida real, {lucky[-1]['all_play']} no all-play)",
        ])
        msg.section("Pontos deixados no banco", [f"• *{r['team']}*: {r['bench_pts']:.2f} pts" for r in bench[:3]])
        msg.text(self._signature({"mais_pontos_no_banco": f"{bench[0]['team']} deixou {bench[0]['bench_pts']:.2f} pts no banco"},
                                 "Zoe quem escalou mal, numa frase.", "Escalar também é talento — alguns ainda não descobriram."))
        return msg.render()

    def generate_tuesday_playoff_odds(self) -> str:
        import analytics
        _, week = self._state()
        if week < int(self.ctx.rule("playoff_odds_from_week") or os.environ.get("PLAYOFF_ODDS_FROM_WEEK", "8")):
            return ""
        res = analytics.playoff_odds(self.sleeper)
        if not res["rows"]:
            return ""
        msg = Msg("Chances de playoff", f"{res['sims']:,} simulações das {res['weeks_left']} semanas restantes · "
                                         f"top {res['playoff_teams']} classificam".replace(",", "."), emoji="🎲")
        msg.section("Probabilidades", [f"• *{r['team']}* ({r['record']}): {r['playoff_pct']:.1f}% playoff · "
                                       f"{r['top2_pct']:.1f}% top 2" for r in res["rows"]])
        return msg.footer("Modelo: média de pontos da temporada, puxada para a média da liga.").render()

    def generate_tuesday_draft_order(self) -> str:
        from draft_order_service import DraftOrderService, next_draft_season
        dos = DraftOrderService(league_id=self.league_id)
        if not dos.uses_max_pf():   # only leagues with the Max PF house rule have a weekly projected order to post
            return ""
        season = next_draft_season()
        dos.sync_draft_knowledge_base(season=season)
        return dos.format_draft_order_whatsapp(season=season)

    # =========================================================================
    # QUARTA
    # =========================================================================
    def generate_wednesday_waiver_radar(self) -> str:
        budget = self._faab_budget()
        # 100 = the API's cap: in a deep 12-team league most of the top 25 is rostered, and K/DEF fill the free part
        trending = self.sleeper.get_trending("add", limit=100) or []
        rosters = self.sleeper.get_rosters()
        users = self.sleeper.get_users()
        owned, balances = set(), []
        for r in rosters:
            owned.update(r.get("players") or [])
            used = (r.get("settings") or {}).get("waiver_budget_used", 0)
            balances.append((users.get(r.get("owner_id"), {}).get("team_name") or f"Time {r.get('roster_id')}",
                             max(0, budget - used)))
        balances.sort(key=lambda x: -x[1])

        hot = []
        for p in trending:
            pid = str(p.get("player_id", ""))
            if pid in owned:
                continue
            info = self._player(pid)
            if not self.profile.can_start(info.get("pos")):  # a pickup must be able to start in THIS league
                continue
            inj = f" ⚠️ {info['injury_status']}" if info.get("injury_status") else ""
            hot.append(f"• *{info.get('name')}* ({info.get('pos') or '?'} - {info.get('team') or 'FA'}) — "
                       f"+{p.get('count', 0):,} adições{inj}".replace(",", "."))
            if len(hot) >= 6:
                break
        msg = Msg("Radar do waiver wire", "Os alvos mais quentes ainda livres na liga", emoji="🎯")
        msg.section("Em alta na NFL e livres aqui", hot or ["• Os nomes quentes já têm dono na liga."])
        msg.section(f"FAAB restante (orçamento ${budget})", [f"• {name}: ${left}" for name, left in balances])
        return msg.footer("Os claims processam na madrugada de quarta para quinta.").render()

    # =========================================================================
    # QUINTA
    # =========================================================================
    def generate_opening_day_waivers(self) -> str:
        import waiver_report
        _, week = self._state()
        return waiver_report.build(self.sleeper, self._player_label, self._faab_budget(), hours=30,
                                   title="Plantão do waiver", subtitle=f"Semana {week} · processado nas últimas 30h",
                                   footer="Às 09:00: termômetro da liga.")

    def generate_opening_day_standings(self) -> str:
        season, week = self._state()
        text = self.kb.get_standings_formatted(season, live_standings=self.sleeper.get_standings(),
                                               completed_week=max(1, week - 1))
        return text or self.sleeper.format_standings_whatsapp()

    def generate_matchup_complete_card(self, m: Dict[str, Any], week: int, matchup_index: int = 1,
                                       total: int = 6, bulletin: Optional[Dict[str, Any]] = None) -> str:
        t1, t2 = m.get("team1", {}), m.get("team2", {})
        if not t2:
            return Msg(f"Duelo #{matchup_index}", f"{t1['team_name']} de bye na Semana {week}", emoji="🥊").render()
        n1, n2 = t1.get("team_name", ""), t2.get("team_name", "")
        p1, p2 = float(t1.get("projected_points", 0.0)), float(t2.get("projected_points", 0.0))
        diff = abs(p1 - p2)
        fav = n1 if p1 >= p2 else n2
        prob1 = max(5.0, min(95.0, round(100.0 / (1.0 + math.exp(-(p1 - p2) * 0.08)), 1)))
        prob2 = round(100.0 - prob1, 1)
        h2h = self.kb.get_h2h_segregated_summary(t1.get("owner_name", ""), t2.get("owner_name", ""))

        bulletin = bulletin or self.news.get_league_injury_bulletin(week)
        rids = {str(t1.get("roster_id")), str(t2.get("roster_id"))}
        medical = []
        for s in bulletin.get("starters_at_risk", []):
            if str(s.get("roster_id")) not in rids:
                continue
            raw = s.get("official_status_raw") or s.get("injury_status") or "?"
            bench = f" — banco: {', '.join(s['bench_replacements'][:2])}" if s.get("bench_replacements") else " — sem reserva"
            medical.append(f"• {s['team_name']}: *{s['player_name']}* ({s['position']}, {raw}){bench}")

        rec1, rec2 = self._record(t1.get("roster_id")), self._record(t2.get("roster_id"))
        msg = Msg(f"Duelo #{matchup_index}: {n1} x {n2}", f"Semana {week}", emoji="🥊")
        msg.section("Projeção", [f"• *{n1}*{f' ({rec1})' if rec1 else ''}: {p1:.1f} pts · {prob1}%",
                                 f"• *{n2}*{f' ({rec2})' if rec2 else ''}: {p2:.1f} pts · {prob2}%"])
        msg.section("Histórico", self._h2h_lines(h2h, n1, n2))
        msg.section("Boletim médico", medical or ["• ✅ Nenhum titular com status de lesão."])
        clima = self._duel_banter(t1, t2, h2h, week + matchup_index)
        if clima:
            msg.section("Clima", clima)
        if diff >= 25:
            verdict = f"favoritismo claro do {fav} (+{diff:.1f})"
        elif diff >= 10:
            verdict = f"vantagem do {fav} (+{diff:.1f}), mas dá zebra"
        else:
            verdict = f"fio da navalha (+{diff:.1f})"
        msg.text(f"🎯 *Palpite:* {verdict}.")
        msg.text(self._signature({"duelo": f"{n1} x {n2}", "favorito_nas_projecoes": fav,
                                  "historico": f"{n1} {h2h.get('wins_a', 0)} x {h2h.get('wins_b', 0)} {n2}"},
                                 "Provoque os dois times deste confronto, numa frase.", "Quem perder já prepara a desculpa."))
        nxt = f"Às {10 + matchup_index}:00: Duelo #{matchup_index + 1}." if matchup_index < total else "Fim do desfile de duelos."
        return msg.footer(nxt).render()

    @staticmethod
    def _h2h_lines(h2h: Dict[str, Any], n1: str, n2: str) -> List[str]:
        if not h2h.get("has_history"):
            return ["• Primeiro encontro entre os dois."]
        lines = [f"• Geral: *{n1}* {h2h['wins_a']} x {h2h['wins_b']} *{n2}*",
                 f"• Temporada regular {h2h['reg_str']} · playoffs {h2h['po_str']}"
                 + (f" · toilet bowl {h2h['tb_str']}" if h2h.get("tb_str") not in (None, "0 x 0") else "")]
        who = {"a": n1, "b": n2, "tie": "empate"}
        meetings = h2h.get("playoff_games") or []
        if meetings:  # every title-bracket meeting (WC / Semi / Final / 3º / 5º) and who advanced
            lines.append("• Mata-mata: " + " · ".join(f"{g['season']} {g['stage']} → {who[g['side']]}"
                                                       for g in meetings[-5:]))
        for tg in h2h.get("title_games", []):
            lines.append(f"• 👑 Final {tg['season']}: {tg['winner']} ({tg['score']})")
        return lines

    def _duel_banter(self, t1, t2, h2h, seed: int) -> List[str]:
        n1, n2 = t1.get("team_name", ""), t2.get("team_name", "")
        out = []
        if h2h.get("has_history"):
            wa, wb = int(h2h.get("wins_a", 0)), int(h2h.get("wins_b", 0))
            if abs(wa - wb) >= 3:
                dono, freg = (n1, n2) if wa > wb else (n2, n1)
                out.append(_pick([f"• Freguesia: {dono} lidera por {max(wa, wb)} x {min(wa, wb)}; {freg} tenta quebrar o tabu.",
                                  f"• {freg} entra como cliente fiel de {dono} ({max(wa, wb)} x {min(wa, wb)})."], seed))
            elif wa == wb:
                out.append(f"• Rivalidade empatada em {wa} x {wb}: quem vencer desempata.")
        s1 = self._standings_by_roster().get(t1.get("roster_id"))
        s2 = self._standings_by_roster().get(t2.get("roster_id"))
        if s1 and s2:
            if s1["wins"] == 0 and s2["wins"] == 0:
                out.append("• Duelo de quem ainda não venceu: alguém sai do zero.")
            elif abs(s1["rank"] - s2["rank"]) >= 6:
                top, bot = (n1, n2) if s1["rank"] < s2["rank"] else (n2, n1)
                out.append(f"• {top} no topo; {bot} tentando sair do porão.")
        return out

    def _sorted_matchups(self, week: int) -> List[Dict[str, Any]]:
        return sorted(self.sleeper.get_matchups(week), key=lambda m: m["matchup_id"])

    def generate_matchup_card_by_index(self, idx: int, week: Optional[int] = None) -> str:
        _, cur = self._state()
        week = week or cur
        ms = self._sorted_matchups(week)
        if not 1 <= idx <= len(ms):
            return ""
        m = ms[idx - 1]
        if _env_on("POLLS_ENABLED") and m.get("team2"):  # disabled by default
            opts = [m["team1"]["team_name"], m["team2"]["team_name"]]
            self._after_send[f"opening_matchup_{idx}"] = lambda: self.evolution.send_poll(
                self.group_jid, f"Quem vence o Duelo #{idx}?", opts)
        return self.generate_matchup_complete_card(m, week, idx, len(ms))

    def generate_all_matchup_cards(self, week: Optional[int] = None) -> List[str]:
        _, cur = self._state()
        week = week or cur
        ms = self._sorted_matchups(week)
        bulletin = self.news.get_league_injury_bulletin(week)
        return [self.generate_matchup_complete_card(m, week, i, len(ms), bulletin) for i, m in enumerate(ms, 1)]

    # =========================================================================
    # SÁBADO — checagem de escalação + jogos cedo
    # =========================================================================
    def lineup_issues(self, week: int) -> Dict[int, Dict[str, Any]]:
        """roster_id -> {team, owner, issues[]} for starters whose game has not started yet."""
        import players_db
        db = players_db.load()
        games = league_time.games_by_team(week)
        espn_ok = bool(games)
        users = self.sleeper.get_users()
        out = {}
        for r in self.sleeper.get_rosters():
            rid = int(r["roster_id"])
            u = users.get(r.get("owner_id"), {})
            starters = r.get("starters") or []
            bench = [p for p in (r.get("players") or []) if p not in starters]
            issues = []
            for slot_i, pid in enumerate(starters):
                if not pid or pid == "0":
                    issues.append({"kind": "empty", "team": None, "text": "🕳️ vaga de titular vazia"})
                    continue
                p = db.get(pid, {})
                g = games.get(p.get("team") or "")
                if g and g["state"] != "pre":
                    continue
                name = p.get("name") or pid
                status = players_db.injury(p)
                team = p.get("team")
                if espn_ok and not g:
                    issues.append({"kind": "bye", "team": team,
                                   "text": f"😴 *{name}* ({p.get('pos')}, {team or 'FA'}) está de *bye*"})
                elif status in INJURED:
                    issues.append({"kind": "out", "team": team, "text": f"❌ *{name}* ({p.get('pos')}, {team}) — {status}"})
                elif status == "Questionable":
                    when = f", joga às {league_time.hhmm(g['kickoff'])}" if g and g.get("kickoff") else ""
                    issues.append({"kind": "q", "team": team, "text": f"⚠️ *{name}* ({p.get('pos')}, {team}) — Questionable{when}"})
                else:
                    continue
                slots = self.profile.starter_slots
                eligible = self.profile.slot_positions(slots[slot_i]) if slot_i < len(slots) else \
                    frozenset({league_profile.normalize_pos(p.get("pos"))})
                opts = []
                for b in bench:
                    bp = db.get(b, {})
                    bg = games.get(bp.get("team") or "")
                    if players_db.injury(bp) in INJURED or (espn_ok and not bg) or (bg and bg["state"] != "pre"):
                        continue
                    if league_profile.normalize_pos(bp.get("pos")) in eligible:
                        opts.append(bp.get("name") or b)
                if opts:
                    issues[-1]["text"] += f" → banco: {', '.join(opts[:2])}"
            if issues:
                out[rid] = {"team": u.get("team_name") or f"Time {rid}", "owner": u.get("display_name", ""), "issues": issues}
        return out

    def _early_games_section(self, week: int) -> List[str]:
        import players_db
        sched = self._sunday(week)
        if not sched["early_games"]:
            return []
        db = players_db.load()
        users = self.sleeper.get_users()
        starters_by_team: Dict[str, List[str]] = {}
        for r in self.sleeper.get_rosters():
            owner = users.get(r.get("owner_id"), {}).get("team_name") or f"Time {r['roster_id']}"
            for pid in r.get("starters") or []:
                p = db.get(pid, {})
                if p.get("team"):
                    st = players_db.injury(p)
                    starters_by_team.setdefault(p["team"], []).append(
                        f"{p.get('name')} ({owner}{', ' + st if st else ''})")
        lines = []
        for g in sched["early_games"]:
            ko = g["kickoff"]
            lines.append(f"• *{g['short']}* às *{league_time.hhmm(ko)}* — inativos às {league_time.hhmm(ko - timedelta(minutes=90))}")
            involved = starters_by_team.get(g["home"], []) + starters_by_team.get(g["away"], [])
            lines.append(f"   ↳ titulares da liga: {', '.join(involved) if involved else 'nenhum'}")
        return lines

    def generate_saturday_lineup_check(self) -> str:
        _, week = self._state()
        sched = self._sunday(week)
        issues = self.lineup_issues(week)
        msg = Msg("Checagem de escalação", f"Semana {week} · domingo começa às {league_time.hhmm(sched['first_kickoff'])}",
                  emoji="📋")
        early = self._early_games_section(week)
        if early:
            msg.section("Jogo cedo amanhã — escale hoje à noite", early, emoji="⏰")
        if issues:
            for rid in sorted(issues, key=lambda r: issues[r]["team"]):
                msg.section(issues[rid]["team"], [f"• {i['text']}" for i in issues[rid]["issues"]])
        else:
            msg.text("✅ Nenhuma pendência: ninguém com titular OUT, de bye ou vaga vazia.")
        if issues and _env_on("LINEUP_DM", "1"):
            self._after_send["sat_20h_lineup_check"] = lambda: self._dm_lineup(issues)
        return msg.footer("Status oficial do Sleeper (ao vivo). Questionable = decisão no dia.").render()

    def _dm_lineup(self, issues: Dict[int, Dict[str, Any]]) -> None:
        """Private heads-up to members with a phone in LEAGUE_PEOPLE_PHONES."""
        phones = self.ctx.people.phones()
        for phone, rid in phones.items():
            if rid in issues and len(phone) >= 12 and phone.startswith("55"):
                text = "\n".join([f"📋 *{issues[rid]['team']}*: pendências na escalação desta semana", ""]
                                 + [f"• {i['text']}" for i in issues[rid]["issues"]])
                self.evolution.send_message(phone, text)

    # =========================================================================
    # DOMINGO
    # =========================================================================
    def generate_sunday_waivers_recap(self) -> str:
        import waiver_report
        _, week = self._state()
        sched = self._sunday(week)
        main = sched["main_kickoff"]
        waivers = waiver_report.build(self.sleeper, self._player_label, self._faab_budget(), hours=30,
                                      title="Domingão: waivers de última hora", subtitle=f"Semana {week} · últimas 30h")
        cron = Msg("Cronograma do domingão", "Horário de Brasília", emoji="⏰")
        lines = self._early_games_section(week)
        lines += ["• 09:00 — confrontos com parciais", "• 10:00 — boletim médico",
                  f"• {league_time.hhmm(main - timedelta(minutes=120))} — aquecimento",
                  f"• {league_time.hhmm(main - timedelta(minutes=90))} — 🚨 alerta de inativos",
                  f"• {league_time.hhmm(main)} — kickoff dos jogos principais"]
        cron.section("Hoje", lines)
        return waivers + "\n<<<MSG>>>\n" + cron.render()

    def generate_sunday_matchups_update(self) -> str:
        _, week = self._state()
        sched = self._sunday(week)
        first = league_time.hhmm(sched["first_kickoff"])
        msg = Msg("Confrontos atualizados", f"Semana {week} · parciais e projeções", emoji="⚔️")
        lines = []
        for i, m in enumerate(self._sorted_matchups(week), 1):
            t1, t2 = m.get("team1"), m.get("team2")
            if not t2:
                lines.append(f"• #{i} {t1['team_name']} — bye")
                continue
            a, b = t1.get("points", 0.0), t2.get("points", 0.0)
            score = f"0 x 0 (começa às {first})" if a == 0 and b == 0 else f"{a:.2f} x {b:.2f}"
            lines.append(f"• #{i} *{t1['team_name']}* x *{t2['team_name']}*: {score}")
            lines.append(f"   ↳ projeção {t1.get('projected_points', 0):.1f} x {t2.get('projected_points', 0):.1f}")
        msg.section("Duelos", lines)
        early = self._early_games_section(week)
        if early:
            msg.section("Jogo cedo hoje", early, emoji="⏰")
        return msg.footer("Às 10:00: boletim médico.").render()

    def generate_sunday_injuries(self) -> str:
        _, week = self._state()
        return self.news.get_matchup_injury_report(week, persist=True)

    def generate_sunday_trashtalk(self) -> str:
        _, week = self._state()
        main = self._sunday(week)["main_kickoff"]
        ms = [m for m in self.sleeper.get_matchups(week) if m.get("team2")]
        msg = Msg("Aquecimento pré-kickoff", f"Semana {week} · 2 horas para os jogos das {league_time.hhmm(main)}", emoji="🔥")
        if ms:
            gap = lambda m: abs(m["team1"].get("projected_points", 0) - m["team2"].get("projected_points", 0))  # noqa: E731
            tight, wide = min(ms, key=gap), max(ms, key=gap)
            fav = wide["team1"] if wide["team1"].get("projected_points", 0) >= wide["team2"].get("projected_points", 0) else wide["team2"]
            dog = wide["team2"] if fav is wide["team1"] else wide["team1"]
            lines = [f"• 🎯 Mais parelho: *{tight['team1']['team_name']}* x *{tight['team2']['team_name']}* "
                     f"({tight['team1'].get('projected_points', 0):.1f} x {tight['team2'].get('projected_points', 0):.1f})",
                     f"• 💣 Maior favorito: *{fav['team_name']}* sobre *{dog['team_name']}* (+{gap(wide):.1f})"]
            scored = [m for m in ms if m["team1"].get("points") or m["team2"].get("points")]
            if scored:
                big = max(scored, key=lambda m: abs(m["team1"]["points"] - m["team2"]["points"]))
                lead = big["team1"] if big["team1"]["points"] >= big["team2"]["points"] else big["team2"]
                lines.append(f"• 📈 Maior vantagem: *{lead['team_name']}* (+{abs(big['team1']['points'] - big['team2']['points']):.2f})")
            msg.section("Panorama", lines)
            msg.text(self._signature({"duelo_mais_parelho": f"{tight['team1']['team_name']} x {tight['team2']['team_name']}",
                                      "maior_favorito": fav["team_name"], "azarao": dog["team_name"]},
                                     "Esquente o domingo provocando o azarão, numa frase.", "Abram o Sleeper e façam as últimas checagens."))
        return msg.footer(f"Às {league_time.hhmm(main - timedelta(minutes=90))}: alerta de inativos.").render()

    def generate_sunday_inactives_alert(self, window: Optional[List[str]] = None, kickoff: Optional[datetime] = None) -> str:
        """Starters with an official Out/IR/Doubtful/Sus status whose game has not started.
        `window` = teams of one early kickoff (international games); None = everything not started."""
        _, week = self._state()
        main = kickoff or self._sunday(week)["main_kickoff"]
        issues = self.lineup_issues(week)
        teams_filter = set(window or [])
        lines = []
        for rid in sorted(issues, key=lambda r: issues[r]["team"]):
            items = [i for i in issues[rid]["issues"] if i["kind"] in ("out", "bye", "empty")]
            if teams_filter:
                items = [i for i in items if i["team"] in teams_filter and i["kind"] == "out"]
            if items:
                lines.append(f"*{issues[rid]['team']}*")
                lines.extend(f"• {i['text']}" for i in items)
        label = "do jogo cedo" if window else "dos jogos principais"
        msg = Msg("Alerta vermelho de inativos", f"Semana {week} · 90 min para o kickoff {label} ({league_time.hhmm(main)})",
                  emoji="🚨")
        if lines:
            msg.text(*lines)
            msg.text(f"Troquem até {league_time.hhmm(main)} ou aceitem o zero.")
        else:
            if window:
                return ""  # nothing to warn about for this early window
            msg.text("✅ Nenhum titular OUT/IR/Doubtful, de bye ou vaga vazia.")
        return msg.footer("Status oficial do Sleeper, ao vivo.").render()

    # =========================================================================
    # SEGUNDA
    # =========================================================================
    def generate_monday_decisions(self) -> str:
        _, week = self._state()
        dramas, milagres, fechados = [], [], []
        for m in self.sleeper.get_matchups(week):
            a = self.sleeper.analyze_matchup(str(m["matchup_id"]), week)
            if not a or not a.get("team1") or not a.get("team2"):
                continue
            t1, t2 = a["team1"], a["team2"]
            leader = t1 if t1["current_points"] >= t2["current_points"] else t2
            trailer = t2 if leader is t1 else t1
            lp = a["win_prob_team1"] if leader is t1 else a["win_prob_team2"]
            tp = 100.0 - lp
            diff = abs(t1["current_points"] - t2["current_points"])
            head = f"• *{t1['team_name']}* {t1['current_points']:.2f} x {t2['current_points']:.2f} *{t2['team_name']}*"
            if not trailer.get("pending"):
                fechados.append([head, f"   ↳ {leader['team_name']} venceu (+{diff:.2f})"])
                continue
            pend = ", ".join(f"{p['name']} ({p.get('pos', '?')})" for p in trailer["pending"])
            block = [head, f"   ↳ chance de vitória: {leader['team_name']} {lp:.0f}% x {tp:.0f}% {trailer['team_name']}",
                     f"   ↳ esperança de {trailer['team_name']}: {pend}"]
            (dramas if tp >= 15 or diff <= 30 else milagres).append(block)
        msg = Msg("Especial Monday Night", f"Semana {week} · o que ainda está em jogo", emoji="🏈")
        msg.section("Drama real", [l for b in dramas for l in b], emoji="🔥")
        msg.section("Missão milagre", [l for b in milagres for l in b], emoji="⚡")
        msg.section("Já decididos", [l for b in fechados for l in b], emoji="🔒")
        return msg.footer("Amanhã às 08:00: fechamento da rodada.").render()

    # ------------------------------------------------------------------
    # Balanços do domingo: após a 1ª onda, após a 2ª onda e após o SNF
    # ------------------------------------------------------------------
    WAVE_TITLES = {
        "wave1": ("Balanço da 1ª onda", "jogos das {k} encerrados"),
        "wave2": ("Balanço da 2ª onda", "jogos da tarde encerrados"),
        "snf": ("Fim do domingo", "Sunday Night encerrado"),
    }

    def generate_sunday_wave_report(self, stage: str) -> str:
        """How every league matchup stands after a Sunday wave, and which ones are already decided."""
        _, week = self._state()
        waves = league_time.sunday_waves(week)
        games = league_time.games_by_team(week)
        title, sub = self.WAVE_TITLES[stage]
        w1 = waves.get("wave1")
        sub = sub.format(k=league_time.hhmm(w1["first"]) if w1 else "13h ET")

        decided, open_, tops = [], [], []
        winners, hottest = [], None
        for m in self._sorted_matchups(week):
            if not m.get("team2"):
                continue
            a = self.sleeper.analyze_matchup(str(m["matchup_id"]), week)
            if not a or not a.get("team2"):
                continue
            t1, t2 = a["team1"], a["team2"]
            for t in (t1, t2):
                for p in t["played"] + t["pending"]:
                    if p["points"] > 0:
                        tops.append((p["points"], p["name"], t["team_name"]))
            leader = t1 if t1["current_points"] >= t2["current_points"] else t2
            trailer = t2 if leader is t1 else t1
            lp = a["win_prob_team1"] if leader is t1 else a["win_prob_team2"]
            diff = abs(t1["current_points"] - t2["current_points"])
            score = f"*{t1['team_name']}* {t1['current_points']:.2f} x {t2['current_points']:.2f} *{t2['team_name']}*"
            if not trailer["pending"]:
                decided.append(f"• ✅ {score}\n   ↳ *{leader['team_name']}* venceu — {trailer['team_name']} sem ninguém para jogar")
                winners.append(f"{leader['team_name']} venceu {trailer['team_name']}")
                continue
            if hottest is None or abs(lp - 50) < hottest[0]:
                names = ", ".join(p["name"] for p in trailer["pending"][:3])
                hottest = (abs(lp - 50), f"{trailer['team_name']} perde de {leader['team_name']} por {diff:.2f} "
                                        f"e ainda tem {names} para jogar")
            if lp >= 97:
                tag = "🔒 praticamente definido"
            elif lp >= 75:
                tag = f"📈 {leader['team_name']} encaminhado ({lp:.0f}%)"
            else:
                tag = f"🔥 aberto ({leader['team_name']} {lp:.0f}% x {100 - lp:.0f}%)"

            def left(t):
                if not t["pending"]:
                    return "ninguém"
                return ", ".join(f"{p['name']} ({league_time.game_label(games.get(p['team']))})" for p in t["pending"])
            open_.append(f"• {score} — {tag}")
            open_.append(f"   ↳ faltam {trailer['team_name']} (−{diff:.2f}): {left(trailer)}")
            open_.append(f"   ↳ faltam {leader['team_name']}: {left(leader)}")

        msg = Msg(title, f"Semana {week} · {sub}", emoji="📊" if stage != "snf" else "🌙")
        msg.text(f"{len(decided)} confrontos definidos · {len(open_) // 3} ainda em aberto")
        msg.section("Definidos", decided, emoji="🏁")
        msg.section("Em aberto" if stage != "snf" else "Decide no Monday Night", open_, emoji="⏳")
        if tops:
            tops.sort(reverse=True)
            msg.section("Destaques do dia", [f"• {n} ({team}): {pts:.2f} pts" for pts, n, team in tops[:3]], emoji="⭐")
        if hottest:
            msg.text(self._signature({"confronto_mais_aberto": hottest[1], "ja_decididos": winners},
                                     "Provoque quem ainda depende de jogador em campo no confronto mais aberto, numa frase.",
                                     "Quem ainda tem jogador em campo pode rezar; quem não tem, pode chorar."))
        nxt = {"wave1": "Próximo balanço quando acabar a 2ª onda.",
               "wave2": "Próximo balanço depois do Sunday Night.",
               "snf": "Segunda 08:00: especial Monday Night."}[stage]
        return msg.footer(nxt).render()

    def _wave_slots(self, week: int) -> List[Tuple]:
        """(key, start, fn, end, ready): sent once the wave's games are all final (ESPN), never before
        `start` (last kickoff + 2h30) and at the latest 5 min before the next wave kicks off."""
        waves = league_time.sunday_waves(week)
        out = []
        plan = [("sun_after_wave1", "wave1", ("early", "wave1"), "wave2", timedelta(hours=2, minutes=30)),
                ("sun_after_wave2", "wave2", ("early", "wave1", "wave2"), "snf", timedelta(hours=2, minutes=30)),
                ("sun_after_snf", "snf", ("early", "wave1", "wave2", "snf"), None, timedelta(hours=2, minutes=45))]
        for key, wave, must_end, nxt, after in plan:
            w = waves.get(wave)
            if not w:
                continue
            start = w["last"] + after
            if nxt and waves.get(nxt):
                deadline = waves[nxt]["first"] - timedelta(minutes=5)
                start = min(start, deadline)
                end = waves[nxt]["first"] + timedelta(minutes=30)
            else:
                deadline = w["last"] + timedelta(hours=4, minutes=30)
                end = w["last"] + timedelta(hours=6)
            def ready(now, deadline=deadline, week=week, must_end=must_end):
                if now >= deadline:
                    return True
                fresh = league_time.sunday_waves(week)  # cached 5 min
                return league_time.all_final([g for k in must_end if fresh.get(k) for g in fresh[k]["games"]])
            out.append((key, start, (lambda stage=wave: self.generate_sunday_wave_report(stage)), end, ready))
        return out

    # ------------------------------------------------------------------
    def _get_quick_h2h(self, m1: str, m2: str) -> str:
        info = self.kb.get_h2h_segregated_summary(m1, m2)
        if not info.get("has_history"):
            return "primeiro duelo histórico"
        parts = [f"geral {info['wins_a']} x {info['wins_b']}"]
        if info.get("po_str") not in (None, "0 x 0"):
            parts.append(f"playoffs {info['po_str']}")
        for tg in info.get("title_games", []):
            parts.append(f"final {tg['season']}: {tg['winner']}")
        return " · ".join(parts)

    # =========================================================================
    # Despacho
    # =========================================================================
    def slot_table(self, now: datetime, week: int) -> List[Tuple[str, datetime, Callable[[], str]]]:
        day = now.date()

        def at(h: int, m: int = 0) -> datetime:
            return datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz)

        wd = now.strftime("%a")
        slots: List[Tuple[str, datetime, Callable[[], str]]] = []
        if wd == "Tue":
            import round_injuries
            slots += [
                ("tue_08h_recap", at(8), self.generate_tuesday_recap),
                ("tue_08h_standings", at(8), self.generate_tuesday_standings),
                ("tue_08h_power", at(8), self.generate_tuesday_power),
                ("tue_08h_playoff_odds", at(8), self.generate_tuesday_playoff_odds),
                ("tue_08h_draft_order", at(8), self.generate_tuesday_draft_order),
                ("tue_10h_round_injuries", at(10), lambda: round_injuries.format_report(max(1, week - 1))),
            ]
        elif wd == "Wed":
            slots.append(("wed_08h_waiver_radar", at(8), self.generate_wednesday_waiver_radar))
        elif wd == "Thu":
            slots.append(("opening_08h_waivers", at(8), self.generate_opening_day_waivers))
            slots.append(("opening_09h_standings", at(9), self.generate_opening_day_standings))
            for i in range(1, self.profile.matchups_per_week + 1):   # one card per matchup, one per hour from 10h
                slots.append((f"opening_matchup_{i}", at(9 + i), lambda i=i: self.generate_matchup_card_by_index(i)))
        elif wd == "Sat":
            slots.append(("sat_20h_lineup_check", at(20), self.generate_saturday_lineup_check))
        elif wd == "Sun":
            sched = self._sunday(week)
            main = sched["main_kickoff"]
            slots += [
                ("sun_08h_waivers_recap", at(8), self.generate_sunday_waivers_recap),
                ("sun_09h_matchups_update", at(9), self.generate_sunday_matchups_update),
                ("sun_10h_injuries", at(10), self.generate_sunday_injuries),
            ]
            windows: Dict[datetime, List[str]] = {}
            for g in sched["early_games"]:
                windows.setdefault(g["kickoff"], []).extend([g["home"], g["away"]])
            for ko, teams in sorted(windows.items()):
                slots.append((f"sun_inactives_{ko.strftime('%H%M')}", ko - timedelta(minutes=90),
                              lambda teams=teams, ko=ko: self.generate_sunday_inactives_alert(teams, ko)))
            slots += [
                ("sun_11h_trashtalk", main - timedelta(minutes=120), self.generate_sunday_trashtalk),
                ("sun_11h30_inactives", main - timedelta(minutes=90), self.generate_sunday_inactives_alert),
            ]
            slots += self._wave_slots(week)
            slots.sort(key=lambda s: s[1])
        elif wd == "Mon":
            if now.hour < 8:  # the Sunday-night game ends after midnight
                slots += [s for s in self._wave_slots(week) if s[0] == "sun_after_snf"]
            slots.append(("mon_08h_decisions", at(8), self.generate_monday_decisions))
        return self._apply_schedule(slots)

    def _apply_schedule(self, slots: List[Tuple]) -> List[Tuple]:
        """Per-league cadence from league.yaml: `schedule: {slot_key: "HH:MM"}` moves a slot, `disable_slots` drops it."""
        out: List[Tuple] = []
        for slot in slots:
            key = slot[0]
            if not self.ctx.slot_enabled(key):
                continue
            hhmm = self.ctx.slot_time(key)
            if hhmm:
                try:
                    h, m = (int(x) for x in hhmm.split(":", 1))
                    slot = (key, slot[1].replace(hour=h, minute=m), *slot[2:])
                except ValueError:
                    logger.warning("league %s: invalid schedule time %r for %s", self.ctx.slug, hhmm, key)
            out.append(slot)
        out.sort(key=lambda s: s[1])   # stable: same-start slots keep their order
        return out

    def _send(self, text: str) -> Dict[str, Any]:
        if not self.group_jid:
            raise RuntimeError("WHATSAPP_GROUP_JID não definido — envio bloqueado")
        return self.evolution.send_message(self.group_jid, text)

    def _alert_admin_once(self, slot_id: str, error: str) -> None:
        failures = _load(_failures_file())
        first = not failures.get(slot_id, {}).get("alerted")
        entry = failures.get(slot_id, {})
        entry.update(last_error=error[:300], at=datetime.now(self.tz).isoformat(), count=entry.get("count", 0) + 1, alerted=True)
        failures[slot_id] = entry
        _save(_failures_file(), failures)
        admin = self.ctx.alert_jid
        if first and admin:
            self.evolution.send_message(admin, f"⚠️ {self.ctx.persona_name}: falha ao enviar *{slot_id}* ao grupo.\n"
                                               f"Erro: {error[:300]}\nVou tentar de novo nos próximos minutos.")

    def _send_parts(self, slot_id: str, text: str) -> Tuple[bool, str]:
        """Send every part; remember progress so a retry resumes where it stopped (no duplicates)."""
        parts = split_parts(text)
        failures = _load(_failures_file())
        done = int(failures.get(slot_id, {}).get("parts_sent", 0))
        for i in range(done, len(parts)):
            try:
                res = self._send(parts[i])
            except Exception as e:
                res = {"error": str(e)}
            if not sent_ok(res):
                failures = _load(_failures_file())
                failures.setdefault(slot_id, {})["parts_sent"] = i
                _save(_failures_file(), failures)
                return False, str(res)
            if i < len(parts) - 1:
                time.sleep(2)
        if slot_id in failures:
            failures = _load(_failures_file())
            failures.pop(slot_id, None)
            _save(_failures_file(), failures)
        return True, ""

    def check_and_dispatch(self, now: Optional[datetime] = None) -> List[str]:
        now = now or datetime.now(self.tz)
        season, week = self._state()
        dispatched = load_dispatched_slots()
        sent: List[str] = []
        for slot in self.slot_table(now, week):
            slot_key, start, fn = slot[:3]
            end = slot[3] if len(slot) > 3 and slot[3] else start + timedelta(minutes=SLOT_WINDOW_MIN)
            ready = slot[4] if len(slot) > 4 else None
            slot_id = f"{season}_W{week}_{slot_key}"
            if slot_id in dispatched or not (start <= now < end):
                continue
            if ready is not None and not ready(now):
                continue  # e.g. the wave still has games running: check again on the next tick
            logger.info(f"Disparando slot editorial: {slot_id}")
            try:
                msg = fn()
            except Exception as e:
                logger.exception(f"Erro ao gerar slot {slot_id}")
                self._alert_admin_once(slot_id, f"geração: {e}")
                continue
            if not msg or not msg.strip():
                save_dispatched_slot(slot_id)  # nothing to say this week
                continue
            ok, err = self._send_parts(slot_id, msg)
            if ok:
                save_dispatched_slot(slot_id)
                sent.append(slot_key)
                print(f"[editorial] sent {slot_id}", file=sys.stderr)
                post = self._after_send.pop(slot_key, None)
                if post:
                    try:
                        post()
                    except Exception as e:
                        logger.error(f"pós-envio de {slot_id} falhou: {e}")
                time.sleep(3)
            else:
                print(f"[editorial] FAILED {slot_id}: {err}", file=sys.stderr)
                self._alert_admin_once(slot_id, err)
                break  # keep the order: retry this slot on the next tick
        return sent


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    s = WeeklyScheduler()
    previews = {
        "recap": s.generate_tuesday_recap, "standings": s.generate_tuesday_standings, "power": s.generate_tuesday_power,
        "playoffs": s.generate_tuesday_playoff_odds, "draft_order": s.generate_tuesday_draft_order,
        "radar": s.generate_wednesday_waiver_radar, "waivers": s.generate_opening_day_waivers,
        "termometro": s.generate_opening_day_standings, "sabado": s.generate_saturday_lineup_check,
        "sun_waivers": s.generate_sunday_waivers_recap, "sun_matchups": s.generate_sunday_matchups_update,
        "sun_injuries": s.generate_sunday_injuries, "trashtalk": s.generate_sunday_trashtalk,
        "inactives": s.generate_sunday_inactives_alert, "monday": s.generate_monday_decisions,
        "onda1": lambda: s.generate_sunday_wave_report("wave1"), "onda2": lambda: s.generate_sunday_wave_report("wave2"),
        "pos_snf": lambda: s.generate_sunday_wave_report("snf"),
    }
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if cmd in previews:
        print(previews[cmd]())
    elif cmd in ("matchup", "duelo"):
        print(s.generate_matchup_card_by_index(int(sys.argv[2]) if len(sys.argv) > 2 else 1))
    elif cmd == "check":
        print(f"Check result: {s.check_and_dispatch()}")
    else:
        print("Uso: python3 scheduler_service.py [" + " | ".join(previews) + " | matchup N | check]")
        print("Preview só imprime; 'check' envia ao grupo (WHATSAPP_GROUP_JID).")
