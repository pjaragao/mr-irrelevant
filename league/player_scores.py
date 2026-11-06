"""Player points per week (the league's own scoring), the source of `estatisticas entidade=pontuacao`.

Sleeper's `league/{id}/matchups/{week}` carries `players_points` (every rostered player, bench included, already in
THIS league's scoring) and `starters`. We keep one JSON per league (`player_points.json`, next to the other
knowledge-base sources) that kb_db loads into the `player_weeks` table; the SQL layer then ranks by position, manager,
team, week… exactly like every other entity.

Bye weeks: a player whose NFL team is not on the week's ESPN slate is flagged `bye` (row stays, pts 0). Averages,
minimums and game counts skip those rows, so a bye never shows up as a "0-point game".

    python3 player_scores.py            # refresh (incremental: finished weeks are never refetched)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import league_ctx

logger = logging.getLogger("player_scores")

FILE = "player_points.json"
MAX_AGE = 6 * 3600          # lazy refresh horizon for the in-progress week
SCHEMA = 1


def _path() -> str:
    import kb_db
    return os.path.join(kb_db.kb_dir(), FILE)


def load() -> Dict[str, Any]:
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"schema": SCHEMA, "updated": 0, "seasons": {}, "players": {}}


def _playing(week: int, season: str) -> Optional[List[str]]:
    """NFL teams with a game that week (None = ESPN down: bye unknown, never guessed)."""
    try:
        import league_time
        games = league_time.week_games(week, season)
    except Exception:
        return None
    teams = sorted({t for g in games for t in (g["home"], g["away"]) if t})
    return teams or None


def _week_final(week: int, season: str) -> bool:
    try:
        import league_time
        return league_time.all_final(league_time.week_games(week, season))
    except Exception:
        return False


def refresh(get: Optional[Callable[[str], Any]] = None, force: bool = False) -> str:
    """Fetch the weeks that are finished and not stored yet (plus the live one, marked partial). Returns the path."""
    import sleeper_client
    cli = sleeper_client.SleeperClient()
    get = get or cli._get
    lid = league_ctx.current().league_id
    state = get("state/nfl") or {}
    season, cur = str(state.get("season") or ""), int(state.get("week") or 1)
    if not season:
        return _path()
    data = load()
    if state.get("season_type") == "post":
        cur = max(cur, 18)
    sdata = data.setdefault("seasons", {}).setdefault(season, {"weeks": {}})
    weeks = sdata["weeks"]
    changed = False
    for wk in range(1, min(cur, 18) + 1):
        have = weeks.get(str(wk))
        if have and not have.get("partial") and not force:
            continue
        raw = get(f"league/{lid}/matchups/{wk}") or []
        rosters: Dict[str, Dict[str, List[float]]] = {}
        for m in raw:
            pp = m.get("players_points") or {}
            started = {str(p) for p in (m.get("starters") or []) if p and str(p) != "0"}
            if pp:
                rosters[str(m["roster_id"])] = {str(pid): [round(float(v or 0.0), 2), int(str(pid) in started)]
                                                for pid, v in pp.items()}
        if not any(v[0] for r in rosters.values() for v in r.values()):
            continue                                    # week not started
        final = wk < cur or _week_final(wk, season)
        weeks[str(wk)] = {"playing": _playing(wk, season), "rosters": rosters, "partial": not final}
        changed = True
    if changed or force or not data.get("players"):
        import players_db
        pdb = players_db.load()
        seen = {pid for w in weeks.values() for r in w["rosters"].values() for pid in r}
        data["players"] = {pid: [(pdb.get(pid) or {}).get("name") or f"#{pid}", (pdb.get(pid) or {}).get("pos") or "?",
                                 (pdb.get(pid) or {}).get("team") or "FA"] for pid in seen}
    data["updated"] = time.time()
    data["schema"] = SCHEMA
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    return path


def ensure_fresh() -> None:
    """Lazy refresh for query time; a failure keeps serving the last file (kb_stats says nothing is there if empty)."""
    d = load()
    if d.get("seasons") and time.time() - float(d.get("updated") or 0) < MAX_AGE:
        return
    try:
        refresh()
    except Exception:
        logger.exception("player_scores: refresh failed")


def rows(data: Optional[Dict[str, Any]] = None):
    """(season, week, roster_id, player_id, name, pos, nfl_team, pts, started, bye) for kb_db."""
    data = data if data is not None else load()
    players = data.get("players") or {}
    for season, sd in (data.get("seasons") or {}).items():
        for wk, w in (sd.get("weeks") or {}).items():
            playing = set(w.get("playing") or [])
            for rid, plist in (w.get("rosters") or {}).items():
                for pid, (pts, started) in plist.items():
                    name, pos, team = players.get(pid) or (f"#{pid}", "?", "FA")
                    bye = int(bool(playing) and team not in playing and team != "FA" and not pts)
                    yield (season, int(wk), int(rid), pid, name, pos, team, float(pts), int(started), bye)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    print(refresh(force="--force" in sys.argv))


# --------------------------------------------------------------------------- multi-list rankings (`ranking_pontos`)
FLEX_POOLS = {"FLEX": ("RB", "WR", "TE"), "SUPERFLEX": ("QB", "RB", "WR", "TE"), "WRRB": ("RB", "WR"), "WRT": ("RB", "WR", "TE")}
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def parse_lists(spec: str) -> List[tuple]:
    """"RB:24,WR:36,TE:12,FLEX:36" -> [("RB",24), ...]; a pool name or "RB/WR" is a flex-style list. ValueError if bad."""
    out = []
    for part in str(spec or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, n = part.partition(":")
        name = name.strip().upper().replace(" ", "")
        if name == "DST":
            name = "DEF"
        if not n.strip().isdigit() or not 0 < int(n) <= 100:
            raise ValueError(f"lista '{part}': use POSIÇÃO:QUANTIDADE (ex.: RB:24)")
        pool = FLEX_POOLS.get(name) or tuple(name.split("/"))
        if any(p not in POSITIONS for p in pool):
            raise ValueError(f"posição inválida em '{part}'. Use {', '.join(POSITIONS)}, FLEX, SUPERFLEX ou RB/WR.")
        out.append((name, int(n), pool))
    if not out:
        raise ValueError("informe `listas`, ex.: RB:24,WR:36,TE:12,FLEX:36")
    if len(out) > 8 or sum(n for _, n, _ in out) > 200:
        raise ValueError("no máximo 8 listas e 200 posições no total")
    return out


def ranking_lists(con, spec: str, ano: str = "", metrica: str = "pontos", excluir_anteriores: bool = True) -> str:
    """Season ranking by position in the league's scoring. A list never repeats a player taken by an earlier list
    (that is what makes a FLEX list "the best of the rest"). Cells: Player (NFL team) · fantasy team · value."""
    lists = parse_lists(spec)
    metrica = (metrica or "pontos").strip().lower()
    if metrica not in ("pontos", "media"):
        raise ValueError("metrica deve ser `pontos` ou `media`")
    season = str(ano or "") or (con.execute("SELECT MAX(season) FROM player_weeks").fetchone()[0] or "")
    if not season:
        return "Sem pontuação de jogadores registrada ainda."
    rows = con.execute(
        """SELECT pw.player_id pid, pw.name, pw.pos, pw.nfl_team, ROUND(SUM(pw.pts),2) pts,
                  SUM(CASE WHEN pw.bye=0 AND pw.pts<>0 THEN 1 ELSE 0 END) jogos,
                  (SELECT f.current_team FROM player_weeks x JOIN franchises f ON f.roster_id=x.roster_id
                    WHERE x.player_id=pw.player_id AND x.season=pw.season ORDER BY x.week DESC LIMIT 1) dono,
                  MAX(pw.week) ult
           FROM player_weeks pw WHERE pw.season=? GROUP BY pw.player_id""", (season,)).fetchall()
    weeks = con.execute("SELECT MIN(week), MAX(week) FROM player_weeks WHERE season=?", (season,)).fetchone()
    byes = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT week FROM player_weeks WHERE season=? AND bye=1", (season,)))
    taken: set = set()
    key = (lambda r: (-(r["pts"] or 0), r["name"])) if metrica == "pontos" else \
          (lambda r: (-((r["pts"] or 0) / r["jogos"]) if r["jogos"] else 0, r["name"]))
    out = [f"📊 *RANKING POR POSIÇÃO · {season} · {'pontos' if metrica == 'pontos' else 'média por jogo'}* "
           f"(Semanas {weeks[0]}–{weeks[1]}, pontuação da liga)"]
    for name, n, pool in lists:
        cand = [r for r in rows if r["pos"] in pool and r["jogos"] > 0 and (not excluir_anteriores or r["pid"] not in taken)]
        cand.sort(key=key)
        top = cand[:n]
        taken.update(r["pid"] for r in top)
        title = f"TOP {n} {name}" + (f" (sem repetir as listas acima; {'/'.join(pool)})" if name in FLEX_POOLS or "/" in name else "")
        out += ["", f"*{title}*"]
        for i, r in enumerate(top, 1):
            val = f"{r['pts']:.1f}" if metrica == "pontos" else f"{r['pts'] / r['jogos']:.1f} ({r['jogos']}j)"
            out.append(f"{i}. {r['name']} ({r['nfl_team']}) · {r['dono'] or 'sem time'} · {val}")
        if len(top) < n:
            out.append(f"_(só {len(top)} jogadores com jogo nessa lista)_")
    if byes:
        out += ["", "_Bye não conta como 0: média usa só jogos disputados._"]
    return "\n".join(out)
