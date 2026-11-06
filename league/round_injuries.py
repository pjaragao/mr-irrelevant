#!/usr/bin/env python3
"""Lesões DA RODADA (pós-jogo) — o que aconteceu dentro de campo, não o boletim pré-jogo.

Fonte: ESPN game summary (``/summary?event=<id>``) de cada jogo da semana já iniciado.
Critério: entrada de lesão com ``date`` >= kickoff do jogo (surgiu/atualizou durante ou
depois da partida) + manchetes da ESPN sobre lesão publicadas após o kickoff.
Cruza com os elencos da liga (titular da semana vs banco) via nome normalizado.
Cache em disco de 10 min para respostas instantâneas.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import league_ctx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Pre-kickoff ESPN injury lists are NFL-level (same for every league): one shared file.
SNAPSHOT_FILE = os.path.join(BASE_DIR, "injury_snapshots.json")


def _cache_file() -> str:
    """The collected report carries WHO owns each player, so its cache belongs to the league, not to the engine."""
    return league_ctx.current().state_path("round_injuries_cache.json")
CACHE_TTL = 600
ESPN = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "application/json"}
SKILL_POS = {"QB", "RB", "WR", "TE", "K", "FB"}
INJ_WORDS = re.compile(r"injur|hurt|left the game|carted|concussion|questionable to return|ruled out|"
                       r"won't return|will not return|hamstring|ankle|knee|acl|mcl|achilles|groin|"
                       r"shoulder|ribs|sprain|strain|fracture|torn|tear", re.I)
STATUS_PT = {"Out": "🔴 FORA", "Questionable": "🟡 Dúvida", "Doubtful": "🟠 Improvável",
             "Injured Reserve": "⛔ IR", "Day-To-Day": "🟡 Dia-a-dia", "Suspension": "⚫ Suspenso"}
BODY_PT = {"Hamstring": "fio de presunto (hamstring)", "Ankle": "tornozelo", "Knee": "joelho",
           "Concussion": "concussão", "Head": "cabeça", "Shoulder": "ombro", "Ribs": "costelas",
           "Back": "costas", "Foot": "pé", "Hand": "mão", "Calf": "panturrilha", "Groin": "virilha",
           "Quadriceps": "quadríceps", "Hip": "quadril", "Elbow": "cotovelo", "Wrist": "punho",
           "Neck": "pescoço", "Chest": "peito", "Toe": "dedo do pé", "Achilles": "tendão de Aquiles",
           "Illness": "doença", "Thigh": "coxa", "Oblique": "oblíquo", "Abdomen": "abdômen",
           "Thumb": "polegar", "Finger": "dedo", "Forearm": "antebraço", "Biceps": "bíceps", "Heel": "calcanhar",
           "Knee - ACL": "joelho (LCA)", "Knee - MCL": "joelho (LCM)", "Knee - Meniscus": "joelho (menisco)",
           "Personal": "motivo pessoal", "Pectoral": "peitoral", "Triceps": "tríceps", "Shin": "canela"}
DETAIL_PT = {"strain": "estiramento", "sprain": "entorse", "fracture": "fratura", "surgery": "cirurgia",
             "dislocated": "luxação", "tear": "ruptura", "contusion": "contusão", "stinger": "stinger",
             "bruise": "contusão", "soreness": "dores", "tightness": "rigidez", "spasms": "espasmos"}


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", s)
    return re.sub(r"[^a-z]", "", s)


def _get(url: str, timeout: float = 8.0) -> Any:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _scoreboard(week: int, season: str) -> List[Dict[str, Any]]:
    data = _get(f"{ESPN}/scoreboard?week={week}&season={season}")
    games = []
    for e in data.get("events", []):
        st = e.get("status", {}).get("type", {})
        games.append({"id": e.get("id"), "name": e.get("shortName"), "date": e.get("date"),
                      "state": st.get("state"), "detail": st.get("shortDetail") or st.get("description")})
    return games


def _game_injuries(game: Dict[str, Any]) -> Dict[str, Any]:
    kickoff = _parse_dt(game["date"])
    out = {"game": game["name"], "items": [], "news": []}
    pre = (_load_snapshots().get(str(game["id"])) or {}).get("injuries")
    try:
        d = _get(f"{ESPN}/summary?event={game['id']}")
    except Exception as exc:  # network hiccup → skip this game, report it
        out["error"] = str(exc)[:80]
        return out
    for team in d.get("injuries", []) or []:
        abbr = (team.get("team") or {}).get("abbreviation", "")
        for i in team.get("injuries", []) or []:
            when = _parse_dt(i.get("date", ""))
            if not (kickoff and when and when >= kickoff):
                continue
            a = i.get("athlete") or {}
            det = i.get("details") or {}
            out["items"].append({
                "name": a.get("displayName", ""), "pos": (a.get("position") or {}).get("abbreviation", ""),
                "team": abbr, "status": i.get("status", ""), "body": det.get("type", ""),
                "detail": det.get("detail", ""), "return": det.get("returnDate", ""),
                # None = no snapshot for this game; True = not on the pre-game list (new in-game injury)
                "new": None if pre is None else _norm(a.get("displayName", "")) not in pre,
            })
    for art in (d.get("news") or {}).get("articles", []) or []:
        pub = _parse_dt(art.get("published", ""))
        head = art.get("headline", "")
        if kickoff and pub and pub >= kickoff:
            out["news"].append({"headline": head, "desc": (art.get("description") or "")[:300]})
    return out


ESPN_TO_SLEEPER = {"WSH": "WAS", "JAC": "JAX"}


def _played_index(sleeper, season: str, week: int) -> Dict[str, bool]:
    """'normname|TEAM' and 'normname' -> took the field this week (gp/snaps > 0)."""
    stats = sleeper._get(f"stats/nfl/regular/{season}/{week}") or {}
    players = sleeper.get_players_db()
    out: Dict[str, bool] = {}
    for pid, st in stats.items():
        if not isinstance(st, dict):
            continue
        played = bool(st.get("gp") or st.get("off_snp") or st.get("gms_active") and st.get("pts_ppr"))
        info = players.get(str(pid)) or {}
        name = _norm(info.get("name") or info.get("full_name") or "")
        if not name:
            continue
        out[f"{name}|{info.get('team', '')}"] = played or out.get(f"{name}|{info.get('team', '')}", False)
        out[name] = played or out.get(name, False)
    return out


def _load_snapshots() -> Dict[str, Any]:
    try:
        return json.load(open(SNAPSHOT_FILE, encoding="utf-8"))
    except Exception:
        return {}


def snapshot_pre_kickoff(hours_before: float = 4.0) -> int:
    """Save each game's injury list shortly BEFORE kickoff (run every 30 min). Returns games saved."""
    from sleeper_client import SleeperClient
    state = SleeperClient().get_nfl_state()
    season, week = str(state.get("season") or datetime.now().year), int(state.get("week") or 1)
    snaps = _load_snapshots()
    now = datetime.now(timezone.utc)
    saved = 0
    for g in _scoreboard(week, season):
        ko = _parse_dt(g["date"])
        if g["state"] != "pre" or not ko or not (0 <= (ko - now).total_seconds() <= hours_before * 3600):
            continue
        try:
            d = _get(f"{ESPN}/summary?event={g['id']}")
        except Exception:
            continue
        snaps[g["id"]] = {"taken": now.isoformat(), "game": g["name"], "injuries": {
            _norm((i.get("athlete") or {}).get("displayName", "")): i.get("status", "")
            for t in d.get("injuries", []) or [] for i in t.get("injuries", []) or []}}
        saved += 1
    if saved:
        if len(snaps) > 400:  # keep ~a season
            for k in sorted(snaps, key=lambda k: snaps[k].get("taken", ""))[:len(snaps) - 400]:
                snaps.pop(k)
        tmp = SNAPSHOT_FILE + f".{os.getpid()}.tmp"      # several leagues' crons share this file: swap atomically
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snaps, f, ensure_ascii=False)
        os.replace(tmp, SNAPSHOT_FILE)
    return saved


def _injury_headlines(items: List[Dict[str, str]]) -> List[str]:
    """Keep headlines that report a player injury. Jev (one fan-out request) → regex fallback."""
    if not items:
        return []
    try:
        import jev
        if jev.available():
            qs = {f"h{i}": {"type": "noul", "instructions":
                            f"Does `items[{i}]` report that an NFL player got injured, left a game hurt, or has a new "
                            f"injury diagnosis/status (not just a lineup, trade or performance story)?"}
                  for i in range(min(len(items), 40))}
            ans = jev.decide({"items": items[:40]}, qs, cache=True)
            if ans:
                return [items[i]["headline"] for i in range(min(len(items), 40)) if jev.noul(ans, f"h{i}") >= 0.6]
    except Exception:
        pass
    return [n["headline"] for n in items if INJ_WORDS.search(n["headline"] + " " + n.get("desc", ""))]


def _league_index(sleeper, week: int) -> Dict[str, Dict[str, Any]]:
    """normalized player name -> {manager, team_name, starter}."""
    users = sleeper.get_users()
    players = sleeper.get_players_db()
    starters = set()
    try:  # raw per-roster endpoint: the week's real starters
        for m in sleeper._get(f"league/{sleeper.league_id}/matchups/{week}") or []:
            starters.update(str(x) for x in (m.get("starters") or []))
    except Exception:
        pass
    idx: Dict[str, Dict[str, Any]] = {}
    for r in sleeper.get_rosters():
        u = users.get(r.get("owner_id"), {})
        starters_r = set(r.get("starters") or [])
        for pid in r.get("players") or []:
            info = players.get(str(pid)) or {}
            name = info.get("name") or info.get("full_name") or ""
            if not name:
                continue
            idx[_norm(name)] = {"manager": u.get("display_name", ""), "team_name": u.get("team_name", ""),
                                "starter": str(pid) in starters if starters else str(pid) in {str(x) for x in starters_r}}
    return idx


def collect(week: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
    from sleeper_client import SleeperClient
    sleeper = SleeperClient()
    state = sleeper.get_nfl_state()
    season = str(state.get("season") or datetime.now().year)
    week = int(week or state.get("week") or 1)
    key = f"{season}-{week}"
    cache_file = _cache_file()
    if not force and os.path.exists(cache_file):
        try:
            cached = json.load(open(cache_file, encoding="utf-8"))
            if cached.get("key") == key and time.time() - cached.get("ts", 0) < CACHE_TTL:
                return cached
        except Exception:
            pass
    games = _scoreboard(week, season)
    started = [g for g in games if g["state"] in ("in", "post")]
    if not started and week > 1:  # e.g. Wednesday: nothing played yet → last completed round
        week -= 1
        key = f"{season}-{week}"
        games = _scoreboard(week, season)
        started = [g for g in games if g["state"] in ("in", "post")]
    with ThreadPoolExecutor(max_workers=8) as pool:
        per_game = list(pool.map(_game_injuries, started))
    idx = _league_index(sleeper, week)
    played_idx = _played_index(sleeper, season, week)
    rows = []
    for g in per_game:
        for it in g["items"]:
            n = _norm(it["name"])
            owner = idx.get(n)
            team = ESPN_TO_SLEEPER.get(it["team"], it["team"])
            played = played_idx.get(f"{n}|{team}", played_idx.get(n, False))
            it.update({"game": g["game"], "owner": owner, "played": played})
            if owner or (it["pos"] in SKILL_POS and played):
                rows.append(it)
    uniq, seen = [], set()
    for n in (n for g in per_game for n in g["news"]):
        if n["headline"] not in seen:
            seen.add(n["headline"])
            uniq.append(n)
    news = _injury_headlines(uniq)
    result = {
        "key": key, "ts": time.time(), "season": season, "week": week,
        "played": [f"{g['name']} ({g['detail']})" for g in started],
        "pending": [f"{g['name']} ({g['detail']})" for g in games if g["state"] == "pre"],
        "rows": rows,
        "news": news[:8],
        "errors": [g["game"] for g in per_game if g.get("error")],
    }
    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        json.dump(result, open(cache_file, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return result


def _fmt_row(r: Dict[str, Any], with_owner: bool) -> str:
    status = STATUS_PT.get(r["status"], r["status"] or "?")
    body = BODY_PT.get(r["body"], r["body"]) if r["body"] else "não especificado"
    det = (r.get("detail") or "").strip()
    extra = f", {DETAIL_PT.get(det.lower(), det.lower())}" if det and det != "Not Specified" else ""
    ret = f" | retorno est. {r['return'][8:10]}/{r['return'][5:7]}" if r.get("return") else ""
    who = ""
    if with_owner and r.get("owner"):
        o = r["owner"]
        who = f" → *{o['team_name']}* ({o['manager']})"
    new = " 🆕" if r.get("new") else ""
    return f"• *{r['name']}* ({r['pos']}-{r['team']}){new}: {status} — {body}{extra}{ret}{who}"


def format_report(week: Optional[int] = None) -> str:
    d = collect(week)
    rows = d["rows"]
    hurt = [r for r in rows if r.get("played")]
    starters = [r for r in hurt if r.get("owner") and r["owner"]["starter"]]
    bench = [r for r in hurt if r.get("owner") and not r["owner"]["starter"]]
    others = [r for r in hurt if not r.get("owner")]
    already = [r for r in rows if not r.get("played") and r.get("owner")]
    from msg_format import Msg
    msg = Msg("Lesões da rodada", f"Semana {d['week']} · {len(d['played'])} jogos disputados, {len(d['pending'])} pendentes",
              emoji="🏥")
    msg.section("Titulares da liga", [_fmt_row(r, True) for r in starters] or ["• nenhum ✅"], emoji="🚑")
    msg.section("Banco da liga (jogaram na NFL)", [_fmt_row(r, True) for r in bench])
    msg.section("Sem dono na liga", [_fmt_row(r, False) for r in others[:6]])
    if already:
        msg.section("Já estavam fora (não jogaram)", ["• " + "; ".join(
            f"{r['name']} ({r['owner']['team_name']})" for r in already[:25])])
    msg.section("Manchetes pós-jogo (ESPN)", [f"• {h}" for h in d["news"][:6]])
    if d["pending"]:
        msg.text("⏳ Ainda vão jogar: " + ", ".join(p.split(" (")[0] for p in d["pending"]))
    if d["errors"]:
        msg.text("⚠️ Sem dados de: " + ", ".join(d["errors"]))
    if any(r.get("new") is not None for r in rows):
        msg.text("🆕 = não constava no relatório de lesões antes do kickoff.")
    return msg.footer("Fonte: ESPN (relatórios pós-jogo) × Sleeper (quem jogou).").render()

if __name__ == "__main__":
    import sys
    wk = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    print(format_report(wk))
