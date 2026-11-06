"""Live scoring DM for the admin's own matchup — no model call, only Sleeper + ESPN.

Every tick (cron, 5 min) while a game of one of the two teams' starters is on:
* the points of every starter of MY team and of my OPPONENT are compared with the previous snapshot; the raw stat
  line (`stats/nfl/regular/<season>/<week>`) gives the "what": yards, receptions, TDs…;
* plays pile up in `pending` and go out as ONE message — one player per line, with everything he scored since the last
  message — when LIVE_SCORE_MIN_GAP minutes passed since the last one (a single play of LIVE_SCORE_BIG+ points goes
  out at once);
* with nothing new, the scoreboard alone still goes out every LIVE_SCORE_HEARTBEAT minutes (default 30);
* a final message closes the matchup when every starter of both teams is done.
Pause/resume: watch_prefs ("parar placar" / "continuar placar"); the instruction is printed in every message.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import league_ctx
import watch_prefs

logger = logging.getLogger("live_score")

MIN_GAP = float(os.environ.get("LIVE_SCORE_MIN_GAP", "10")) * 60
HEARTBEAT = float(os.environ.get("LIVE_SCORE_HEARTBEAT", "30")) * 60
BIG = float(os.environ.get("LIVE_SCORE_BIG", "6"))
FILE = "live_score_state.json"

# stat key -> (singular, plural) shown in the play list; order = display order
LABELS: List[Tuple[str, str, str]] = [
    ("pass_td", "TD de passe", "TDs de passe"), ("pass_yd", "jd de passe", "jds de passe"),
    ("pass_int", "INT", "INTs"), ("pass_2pt", "2pt de passe", "2pts de passe"),
    ("rush_td", "TD corrido", "TDs corridos"), ("rush_yd", "jd corrida", "jds corridos"),
    ("rush_2pt", "2pt corrido", "2pts corridos"),
    ("rec", "recepção", "recepções"), ("rec_yd", "jd recebida", "jds recebidos"),
    ("rec_td", "TD recebido", "TDs recebidos"), ("rec_2pt", "2pt recebido", "2pts recebidos"),
    ("fum_lost", "fumble perdido", "fumbles perdidos"), ("fum", "fumble", "fumbles"),
    ("fum_rec", "fumble recuperado", "fumbles recuperados"), ("fum_rec_td", "TD de fumble", "TDs de fumble"),
    ("sack", "sack", "sacks"), ("int", "interceptação", "interceptações"),
    ("def_td", "TD defensivo", "TDs defensivos"), ("def_st_td", "TD de ST", "TDs de ST"),
    ("st_td", "TD de ST", "TDs de ST"), ("safe", "safety", "safeties"), ("ff", "fumble forçado", "fumbles forçados"),
    ("def_st_fum_rec", "fumble rec. (ST)", "fumbles rec. (ST)"), ("blk_kick", "chute bloqueado", "chutes bloqueados"),
    ("fgm_0_19", "FG 0-19", "FGs 0-19"), ("fgm_20_29", "FG 20-29", "FGs 20-29"),
    ("fgm_30_39", "FG 30-39", "FGs 30-39"), ("fgm_40_49", "FG 40-49", "FGs 40-49"),
    ("fgm_50p", "FG 50+", "FGs 50+"), ("fgmiss", "FG errado", "FGs errados"),
    ("xpm", "XP", "XPs"), ("xpmiss", "XP errado", "XPs errados"),
]
LABEL_KEYS = {k for k, _, _ in LABELS}


def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def stat_delta(prev: Dict[str, Any], cur: Dict[str, Any], keys: Optional[set] = None) -> Dict[str, float]:
    """Raw stat increases (or corrections) between two snapshots, only the keys that score."""
    out: Dict[str, float] = {}
    for k in (keys or LABEL_KEYS) | {"pts_allow"}:
        d = _num(cur.get(k)) - _num((prev or {}).get(k))
        if abs(d) > 1e-9:
            out[k] = d
    return out


def _fmt(n: float) -> str:
    return str(int(round(n))) if abs(n - round(n)) < 1e-6 else f"{n:.1f}"


def describe(delta: Dict[str, float]) -> str:
    """'1 recepção, 24 jds recebidos, 1 TD recebido' (yards stay signed when negative)."""
    parts = []
    for key, one, many in LABELS:
        d = delta.get(key)
        if not d:
            continue
        n = abs(d)
        sign = "-" if d < 0 else ""
        parts.append(f"{sign}{_fmt(n)} {one if n == 1 else many}")
    if delta.get("pts_allow"):
        parts.append(f"{_fmt(delta['pts_allow'])} pts sofridos")
    return ", ".join(parts)


def _signed(v: float) -> str:
    return f"{v:+.1f}".replace(".", ",") if abs(v) >= 0.05 else "0,0"


def _pts(v: float) -> str:
    return f"{v:.2f}".replace(".", ",")


class LiveScore:
    def __init__(self, ctx=None, get: Optional[Callable[[str], Any]] = None, games_fn=None, players=None,
                 users=None, rosters=None, send: Optional[Callable[[str], bool]] = None, state_path: str = "",
                 targets: Optional[List[Dict[str, Any]]] = None, dry: bool = False):
        self.ctx = ctx or league_ctx.current()
        self._get_fn = get
        self._games_fn = games_fn
        self._players = players
        self._users = users
        self._rosters = rosters
        self._send_fn = send
        self.state_path = state_path or self.ctx.state_path(FILE)
        self._targets = targets
        self.dry = dry
        self.out: List[str] = []          # what was (or would be) sent

    # ------------------------------------------------------------------ io
    def _sleeper(self):
        if not hasattr(self, "_cli"):
            from sleeper_client import SleeperClient
            self._cli = SleeperClient()
        return self._cli

    def get(self, endpoint: str) -> Any:
        return self._get_fn(endpoint) if self._get_fn else self._sleeper()._get(endpoint)

    def load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_state(self, st: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------------ helpers
    def _team_names(self) -> Dict[int, str]:
        users = self._users if self._users is not None else self._sleeper().get_users()
        rosters = self._rosters if self._rosters is not None else self._sleeper().get_rosters()
        out = {}
        for r in rosters:
            u = users.get(r.get("owner_id"), {})
            out[int(r["roster_id"])] = u.get("team_name") or u.get("display_name") or f"Time {r['roster_id']}"
        return out

    def _player_db(self) -> Dict[str, Any]:
        if self._players is not None:
            return self._players
        import players_db
        return players_db.load()

    # ------------------------------------------------------------------ tick
    def tick(self, season: str, week: int, now: Optional[float] = None) -> List[str]:
        """Run one pass. Returns the messages produced (also sent unless dry)."""
        now = now if now is not None else time.time()
        self.out = []
        if watch_prefs.is_paused("placar"):
            return []
        targets = self._targets if self._targets is not None else watch_prefs.targets(self.ctx)
        if not targets:
            return []
        games = self._games_fn(week, season) if self._games_fn else __import__("league_time").week_games(week, season)
        by_team: Dict[str, Dict[str, Any]] = {}
        for g in games:
            by_team[g["home"]] = g
            by_team[g["away"]] = g
        st = self.load_state()
        key = f"{season}-{week}"
        if st.get("week") != key:
            st = {"week": key, "targets": {}}
        live_any = any(g.get("state") == "in" for g in games)
        if not live_any and not any(t.get("active") and not t.get("final") for t in st["targets"].values()):
            return []
        raw = self.get(f"league/{self.ctx.league_id}/matchups/{week}") or []
        stats = self.get(f"stats/nfl/regular/{season}/{week}") or {}
        db = self._player_db()
        names = self._team_names()
        for tg in targets:
            self._tick_target(st, tg, raw, stats, db, names, by_team, now, season, week)
        if not self.dry:
            self.save_state(st)
        return self.out

    def _tick_target(self, st, tg, raw, stats, db, names, by_team, now, season, week) -> None:
        rid = int(tg["rid"])
        mine = next((m for m in raw if int(m.get("roster_id", -1)) == rid), None)
        if not mine or mine.get("matchup_id") is None:
            return
        opp = next((m for m in raw if m.get("matchup_id") == mine["matchup_id"]
                    and int(m.get("roster_id", -1)) != rid), None)
        t = st["targets"].setdefault(str(rid), {"snap": {}, "pending": [], "last_sent": 0.0, "active": False,
                                                "final": False, "first": True})
        scoring = self.ctx.profile.scoring
        keys = LABEL_KEYS | {k for k, v in scoring.items() if v}
        sides = [("mine", mine), ("opp", opp)] if opp else [("mine", mine)]
        remaining = {"mine": 0, "opp": 0}
        playing = False
        first = t.pop("first", False) or not t["snap"]
        for side, m in sides:
            pp = m.get("players_points") or {}
            for pid in m.get("starters") or []:
                if not pid or pid == "0":
                    continue
                p = db.get(pid, {})
                g = by_team.get(p.get("team") or "")
                if g and g.get("state") in ("pre", "in"):
                    remaining[side] += 1
                if g and g.get("state") == "in":
                    playing = True
                cur_pts = round(_num(pp.get(pid)), 2)
                cur_stats = {k: v for k, v in (stats.get(pid) or {}).items() if k in keys or k == "pts_allow"}
                prev = t["snap"].get(pid)
                t["snap"][pid] = {"pts": cur_pts, "stats": cur_stats}
                if prev is None:
                    # first sight of this player this week: anything he already has is "new" unless we just started
                    prev = {"pts": 0.0, "stats": {}} if not first else {"pts": cur_pts, "stats": cur_stats}
                d = round(cur_pts - _num(prev["pts"]), 2)
                if abs(d) < 0.01:
                    continue
                t["pending"].append({"pid": pid, "side": side, "d": d, "at": now,
                                     "name": p.get("name") or pid, "pos": p.get("pos") or "",
                                     "team": p.get("team") or "",
                                     "stats": stat_delta(prev["stats"], cur_stats, keys)})
        if playing or t["pending"]:
            t["active"] = True
        score = {"mine": _num(mine.get("points")), "opp": _num(opp.get("points")) if opp else 0.0}
        done = (bool(by_team) and t["active"] and not t["final"] and not playing
                and sum(remaining.values()) == 0)
        big = any(abs(e["d"]) >= BIG for e in t["pending"])
        since = now - _num(t.get("last_sent"))
        kind = ""
        if done:
            kind = "final"
        elif t["pending"] and (since >= MIN_GAP or big or not t.get("last_sent")):
            kind = "plays"
        elif playing and since >= HEARTBEAT:
            kind = "score"
        if not kind:
            return
        text = self.render(kind, week, names.get(rid, "Meu time"), names.get(int(opp["roster_id"])) if opp else "",
                           score, remaining, t["pending"], t.get("last_sent") or 0.0, now)
        ok = True
        if self.dry:
            self.out.append(text)
            return
        ok = self._send(tg, text)
        if ok:
            self.out.append(text)
            t["pending"] = []
            t["last_sent"] = now
            if kind == "final":
                t["final"] = True

    def _send(self, tg: Dict[str, Any], text: str) -> bool:
        if self._send_fn:
            return bool(self._send_fn(text))
        from evolution_client import EvolutionClient
        if not hasattr(self, "_evo"):
            self._evo = EvolutionClient()
        return watch_prefs.dm(self._evo, tg["dms"], text)

    # ------------------------------------------------------------------ text
    def render(self, kind: str, week: int, my_name: str, opp_name: str, score: Dict[str, float],
               remaining: Dict[str, int], pending: List[Dict[str, Any]], last_sent: float, now: float) -> str:
        tz = self.ctx.timezone
        from zoneinfo import ZoneInfo
        clock = datetime.fromtimestamp(now, ZoneInfo(tz)).strftime("%H:%M")
        title = {"final": "🏁 *FIM DO CONFRONTO*", "plays": "🏈 *PLACAR AO VIVO*", "score": "⏱️ *PLACAR*"}[kind]
        lines = [f"{title} — Semana {week} · {clock}"]
        diff = score["mine"] - score["opp"]
        if opp_name:
            lead = ("🟢 na frente" if diff > 0.004 else "🔴 atrás" if diff < -0.004 else "🟡 empatado")
            lines += ["", f"*{my_name}* {_pts(score['mine'])} x {_pts(score['opp'])} *{opp_name}*",
                      f"{lead} ({_signed(diff)})" if abs(diff) > 0.004 else lead]
            if kind == "final":
                res = "✅ Vitória" if diff > 0.004 else "❌ Derrota" if diff < -0.004 else "🤝 Empate"
                lines[-1] = f"{res} por {abs(diff):.2f}".replace(".", ",") if abs(diff) > 0.004 else res
        else:
            lines += ["", f"*{my_name}* {_pts(score['mine'])} (sem adversário)"]
        if pending:
            since = ""
            if last_sent:
                since = " desde " + datetime.fromtimestamp(last_sent, ZoneInfo(tz)).strftime("%H:%M")
            lines += ["", f"*Pontuações{since}*"]
            for side, label in (("mine", f"🟦 {my_name}"), ("opp", f"🟥 {opp_name or 'Adversário'}")):
                agg: Dict[str, Dict[str, Any]] = {}
                for e in pending:
                    if e["side"] != side:
                        continue
                    a = agg.setdefault(e["pid"], {"name": e["name"], "pos": e["pos"], "team": e["team"],
                                                  "d": 0.0, "stats": {}})
                    a["d"] += e["d"]
                    for k, v in e["stats"].items():
                        a["stats"][k] = a["stats"].get(k, 0.0) + v
                if not agg:
                    continue
                lines.append(label)
                for a in sorted(agg.values(), key=lambda x: -abs(x["d"])):
                    what = describe(a["stats"])
                    lines.append(f"• {a['name']} ({a['pos']}·{a['team']}) *{_signed(a['d'])}*"
                                 + (f" — {what}" if what else ""))
        elif kind != "final":
            lines += ["", "_Sem pontuação nova dos titulares desde a última mensagem._"]
        if kind != "final" and opp_name:
            lines += ["", f"Ainda a jogar: {remaining['mine']} seus · {remaining['opp']} do adversário"]
        lines += ["", f"_{watch_prefs.instructions('placar')}_"]
        return "\n".join(lines)
