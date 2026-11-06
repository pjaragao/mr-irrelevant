#!/usr/bin/env python3
"""H2H: fases dos playoffs a partir dos brackets oficiais do Sleeper e reconstrução da matriz.

Fonte da verdade
  * ``matchups_history.json``  — todos os jogos (conferidos 1:1 com ``/league/{id}/matchups/{week}``).
  * ``brackets_history.json``  — ``/winners_bracket`` e ``/losers_bracket`` de cada temporada: quem jogou
    quê em cada rodada (Wild Card, Semifinal, Final, 3º/5º lugar; Toilet Bowl = bracket de consolação).
  * ``franchises_history.json`` — nomes, treinadores e histórico de times.

``h2h_matrix.json`` é **derivado**: nunca se edita nem se incrementa à mão. ``build_matrix`` regenera tudo (66 pares)
a partir das três fontes acima, então não há deriva entre jogos, placares, recordes e fases.

Semântica dos recortes (somam o total): ``reg`` = temporada regular · ``po`` = bracket do título (winners:
Wild Card, Semifinal, Final, 3º e 5º lugar) · ``tb`` = Toilet Bowl (losers bracket, 7º ao 12º).

Uso (na raiz do repo):
    python3 league/h2h_builder.py --refresh-brackets   # baixa os brackets do Sleeper
    python3 league/h2h_builder.py --rebuild --write    # reconstrói h2h_matrix.json (sem --write: só mostra o diff)
    python3 league/h2h_builder.py --rivalries-md       # regenera H2H_RIVALRIES.md
    python3 league/h2h_builder.py --verify             # (rede) confere jogos e brackets contra o Sleeper
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import league_ctx


def default_kb_dir() -> str:
    """Knowledge base of the current league (functions below take `kb_dir=None` to mean "this league's")."""
    return league_ctx.current().kb_dir


BRACKETS_FILE = "brackets_history.json"
MATRIX_FILE = "h2h_matrix.json"
SLEEPER = os.environ.get("SLEEPER_API_BASE", "https://api.sleeper.app/v1")

# Tags are what knowledge_service compares against; labels are what people read.
FINAL = "FINAL 🏆"
SEMI = "Semi PO"
WILD_CARD = "Wild Card"
THIRD = "3º Lugar 🥉"
FIFTH = "5º Lugar"
TOILET = "Toilet Bowl"
REGULAR = "Reg"
UNKNOWN = "Pós-temporada"

BRACKET_REGULAR, BRACKET_WINNERS, BRACKET_LOSERS = "regular", "winners", "losers"


# --------------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------------
def stage_of(bracket: str, rnd: int, place: Optional[int], first_spot: int = 7,
             last_spot: int = 12) -> Tuple[str, str]:
    """(tag, label) of a bracket game. ``place`` is Sleeper's ``p`` (1 = final, 3 = 3rd, 5 = 5th). The consolation
    bracket decides the places ``first_spot``..``last_spot`` (the teams below the title bracket: 7th-12th by default)."""
    if bracket == BRACKET_WINNERS:
        if place == 1:
            return FINAL, "GRANDE FINAL (Disputa de Título 🏆)"
        if place == 3:
            return THIRD, "Playoffs — disputa de 3º lugar 🥉"
        if place == 5:
            return FIFTH, "Playoffs — disputa de 5º lugar"
        if place and place > 5 and place % 2:
            return FIFTH, f"Playoffs — disputa de {place}º lugar"
        if rnd <= 1:
            return WILD_CARD, "Playoffs — Wild Card"
        return SEMI, "Playoffs — Semifinal"
    if place and place % 2:                       # p = 1, 3, 5...: the game that decides spots first+p-1 and first+p
        a, b = first_spot + place - 1, first_spot + place
        tail = " — final do Toilet Bowl" if place == 1 else (" — disputa da lanterna" if b >= last_spot else "")
        return TOILET, f"Toilet Bowl — {a}º/{b}º lugar{tail}"
    return TOILET, "Toilet Bowl — 1ª rodada" if rnd <= 1 else "Toilet Bowl — semifinal"


def phase_name(bracket: Optional[str]) -> str:
    return {BRACKET_REGULAR: "Temporada Regular", BRACKET_WINNERS: "Playoffs",
            BRACKET_LOSERS: "Toilet Bowl"}.get(bracket or "", "Pós-temporada")


# --------------------------------------------------------------------------------------------------
# Brackets file
# --------------------------------------------------------------------------------------------------
def load_brackets(kb_dir: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(os.path.join(kb_dir or default_kb_dir(), BRACKETS_FILE), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_brackets(data: Dict[str, Any], kb_dir: Optional[str] = None) -> None:
    path = os.path.join(kb_dir or default_kb_dir(), BRACKETS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def _http_get(path: str) -> Any:
    with urllib.request.urlopen(f"{SLEEPER}/{path.lstrip('/')}", timeout=20) as r:
        return json.load(r)


def fetch_season(season: str, league_id: str, get: Callable[[str], Any] = _http_get) -> Dict[str, Any]:
    """Official bracket of one season. Only *decided* games with both rosters known are kept, so a season in
    progress never stores Sleeper's projected pairings (they change until the regular season ends)."""
    league = get(f"league/{league_id}") or {}
    settings = league.get("settings") or {}
    start = int(settings.get("playoff_week_start") or 15)
    per_round = 2 if int(settings.get("playoff_round_type") or 0) == 1 else 1
    first_spot = int(settings.get("playoff_teams") or 6) + 1
    last_spot = int(league.get("total_rosters") or settings.get("num_teams") or 12)
    games: List[Dict[str, Any]] = []
    for bracket, endpoint in ((BRACKET_WINNERS, "winners_bracket"), (BRACKET_LOSERS, "losers_bracket")):
        for g in get(f"league/{league_id}/{endpoint}") or []:
            t1, t2, w = g.get("t1"), g.get("t2"), g.get("w")
            if not all(isinstance(x, int) for x in (t1, t2, w)):
                continue
            rnd, place = int(g["r"]), g.get("p")
            tag, label = stage_of(bracket, rnd, place, first_spot, last_spot)
            games.append({"bracket": bracket, "round": rnd, "week": start + (rnd - 1) * per_round,
                          "match": int(g["m"]), "place": place, "roster_a": t1, "roster_b": t2,
                          "winner": w, "loser": g.get("l"), "stage": tag, "label": label})
    games.sort(key=lambda x: (x["week"], x["bracket"], x["match"]))
    return {"league_id": str(league_id), "playoff_week_start": start, "playoff_teams": settings.get("playoff_teams"),
            "status": league.get("status"), "games": games}


def refresh_brackets(league_ids: Dict[str, str], seasons: Optional[List[str]] = None,
                     get: Callable[[str], Any] = _http_get, kb_dir: Optional[str] = None) -> Dict[str, Any]:
    """Re-download the given seasons (default: all) and write brackets_history.json."""
    kb_dir = kb_dir or default_kb_dir()
    data = load_brackets(kb_dir)
    for season in seasons or sorted(league_ids):
        fresh = fetch_season(season, league_ids[season], get)
        if fresh["games"] or season not in data:
            data[season] = fresh
    data["_meta"] = {"source": "Sleeper /winners_bracket + /losers_bracket",
                     "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     "note": "só jogos decididos; semana = playoff_week_start + rodada - 1"}
    save_brackets(data, kb_dir)
    return data


def league_ids_from_kb(kb_dir: Optional[str] = None) -> Dict[str, str]:
    kb_dir = kb_dir or default_kb_dir()
    for name in ("playoffs_history.json", "seasons_history.json"):
        try:
            with open(os.path.join(kb_dir, name), encoding="utf-8") as f:
                ids = {s: str(v["league_id"]) for s, v in json.load(f).items()
                       if isinstance(v, dict) and v.get("league_id")}
        except OSError:
            continue
        if ids:
            return ids
    return {}


class StageIndex:
    """(season, week, roster, roster) -> bracket game, built once from brackets_history.json."""

    def __init__(self, brackets: Optional[Dict[str, Any]] = None):
        self._idx: Dict[Tuple[str, int, frozenset], Dict[str, Any]] = {}
        self.seasons = set()
        for season, data in (brackets if brackets is not None else load_brackets()).items():
            if not isinstance(data, dict) or "games" not in data:
                continue
            self.seasons.add(str(season))
            for g in data["games"]:
                self._idx[(str(season), int(g["week"]), frozenset((g["roster_a"], g["roster_b"])))] = g

    def lookup(self, season: Any, week: int, roster_a: int, roster_b: int) -> Optional[Dict[str, Any]]:
        return self._idx.get((str(season), int(week), frozenset((int(roster_a), int(roster_b)))))

    def classify(self, season: Any, week: int, is_playoff: bool, roster_a: int, roster_b: int) -> Dict[str, Any]:
        """{bracket, stage, label} of a game; regular season games never need the file."""
        if not is_playoff:
            return {"bracket": BRACKET_REGULAR, "stage": REGULAR, "label": "Temporada Regular"}
        g = self.lookup(season, week, roster_a, roster_b)
        if g is None:
            return {"bracket": None, "stage": UNKNOWN, "label": "Pós-temporada (fase não identificada)"}
        return {"bracket": g["bracket"], "stage": g["stage"], "label": g["label"]}


# --------------------------------------------------------------------------------------------------
# Matrix
# --------------------------------------------------------------------------------------------------
def _side(game: Dict[str, Any], low: int) -> Tuple[float, float, str, str, str, str]:
    """(pts_low, pts_high, mgr_low, mgr_high, team_low, team_high) of a matchups_history entry."""
    if game["roster_a"] == low:
        return (float(game["points_a"]), float(game["points_b"]), game["manager_a"], game["manager_b"],
                game["team_a"], game["team_b"])
    return (float(game["points_b"]), float(game["points_a"]), game["manager_b"], game["manager_a"],
            game["team_b"], game["team_a"])


def _mark(g: Dict[str, Any]) -> Dict[str, Any]:
    winner = g["winner"]
    return {"winner": winner,
            "winner_team": g["team_1"] if winner == g["_m1"] else g["team_2"] if winner == g["_m2"] else "-",
            "loser_team": g["team_2"] if winner == g["_m1"] else g["team_1"] if winner == g["_m2"] else "-",
            "margin": g["margin"], "season": g["season"], "week": g["week"], "phase": g["phase"],
            "score": f"{g['score_1']} x {g['score_2']}",
            "winner_pts": max(g["score_1"], g["score_2"]), "loser_pts": min(g["score_1"], g["score_2"])}


def build_pair(low: int, high: int, games: List[Dict[str, Any]], franchises: Dict[int, Dict[str, Any]],
               stages: StageIndex) -> Dict[str, Any]:
    f1, f2 = franchises[low], franchises[high]
    m1, m2 = f1["canonical_manager"], f2["canonical_manager"]
    log: List[Dict[str, Any]] = []
    for g in sorted((x for x in games if {x["roster_a"], x["roster_b"]} == {low, high}),
                    key=lambda x: (str(x["season"]), int(x["week"]))):
        s1, s2, mg1, mg2, t1, t2 = _side(g, low)
        if s1 == 0 and s2 == 0:
            continue
        st = stages.classify(g["season"], g["week"], bool(g.get("is_playoff")), low, high)
        log.append({
            "season": str(g["season"]), "week": int(g["week"]), "phase": phase_name(st["bracket"]),
            "is_playoff": bool(g.get("is_playoff")), "bracket": st["bracket"], "stage": st["stage"],
            "stage_label": st["label"], "score_1": round(s1, 2), "score_2": round(s2, 2),
            "margin": round(abs(s1 - s2), 2), "winner": mg1 if s1 > s2 else mg2 if s2 > s1 else "Empate",
            "team_1": t1, "team_2": t2, "_m1": mg1, "_m2": mg2})

    def tally(sel) -> Dict[str, Any]:
        gs = [g for g in log if sel(g)]
        return {"n": len(gs), "w1": sum(g["score_1"] > g["score_2"] for g in gs),
                "w2": sum(g["score_2"] > g["score_1"] for g in gs),
                "t": sum(g["score_1"] == g["score_2"] for g in gs),
                "p1": round(sum(g["score_1"] for g in gs), 2), "p2": round(sum(g["score_2"] for g in gs), 2)}

    tot = tally(lambda g: True)
    reg = tally(lambda g: g["bracket"] == BRACKET_REGULAR)
    po = tally(lambda g: g["bracket"] == BRACKET_WINNERS)
    tb = tally(lambda g: g["bracket"] == BRACKET_LOSERS)
    decided = [g for g in log if g["winner"] != "Empate"]
    blow = max(decided, key=lambda g: g["margin"], default=None)  # first on ties
    close = min(log, key=lambda g: g["margin"], default=None)
    pair = {
        "roster_1": low, "roster_2": high,
        "manager_1": m1, "manager_2": m2,
        "real_name_1": f1["real_name"], "real_name_2": f2["real_name"],
        "current_team_1": f1["current_team"], "current_team_2": f2["current_team"],
        "has_manager_change": len(f1.get("managers_history", [])) > 1 or len(f2.get("managers_history", [])) > 1,
        "manager_1_history": f1.get("managers_history", []), "manager_2_history": f2.get("managers_history", []),
        "teams_1_history": f1.get("teams_history", []), "teams_2_history": f2.get("teams_history", []),
        "games_total": tot["n"], "games_reg": reg["n"], "games_playoffs": po["n"], "games_toilet": tb["n"],
        "games_unclassified": tot["n"] - reg["n"] - po["n"] - tb["n"],
        "wins_1": tot["w1"], "wins_2": tot["w2"], "ties": tot["t"],
        "wins_1_reg": reg["w1"], "wins_2_reg": reg["w2"], "ties_reg": reg["t"],
        "wins_1_po": po["w1"], "wins_2_po": po["w2"], "ties_po": po["t"],
        "wins_1_tb": tb["w1"], "wins_2_tb": tb["w2"], "ties_tb": tb["t"],
        "pts_1": tot["p1"], "pts_2": tot["p2"],
        "pts_1_reg": reg["p1"], "pts_2_reg": reg["p2"],
        "pts_1_po": po["p1"], "pts_2_po": po["p2"],
        "pts_1_tb": tb["p1"], "pts_2_tb": tb["p2"],
        "biggest_blowout": _mark(blow) if blow else {},
        "closest_match": _mark(close) if close else {},
        "games_log": [{k: v for k, v in g.items() if not k.startswith("_")} for g in log],
    }
    return pair


def build_matrix(matchups: List[Dict[str, Any]], franchises: Dict[int, Dict[str, Any]],
                 brackets: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """All 66 pairs, keyed ``"{low}_{high}"``, rebuilt from the raw games."""
    stages = StageIndex(brackets)
    ids = sorted(int(k) for k in franchises)
    return {f"{lo}_{hi}": build_pair(lo, hi, matchups, franchises, stages)
            for i, lo in enumerate(ids) for hi in ids[i + 1:]}


def _load(kb_dir: str, name: str) -> Any:
    with open(os.path.join(kb_dir, name), encoding="utf-8") as f:
        return json.load(f)


def load_inputs(kb_dir: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    kb_dir = kb_dir or default_kb_dir()
    matchups = _load(kb_dir, "matchups_history.json")
    franchises = {int(k): v for k, v in _load(kb_dir, "franchises_history.json").items()}
    return matchups, franchises


def rebuild(kb_dir: Optional[str] = None, write: bool = False) -> Dict[str, Dict[str, Any]]:
    kb_dir = kb_dir or default_kb_dir()
    matchups, franchises = load_inputs(kb_dir)
    matrix = build_matrix(matchups, franchises, load_brackets(kb_dir))
    if write:
        path = os.path.join(kb_dir, MATRIX_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(matrix, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return matrix


# --------------------------------------------------------------------------------------------------
# Verification against Sleeper (network)
# --------------------------------------------------------------------------------------------------
def verify(kb_dir: Optional[str] = None, get: Callable[[str], Any] = _http_get, max_week: int = 17) -> List[str]:
    """Problems found comparing matchups_history + brackets_history with the live Sleeper API (empty = OK)."""
    kb_dir = kb_dir or default_kb_dir()
    problems: List[str] = []
    matchups = _load(kb_dir, "matchups_history.json")
    have = {(str(g["season"]), int(g["week"]), frozenset((g["roster_a"], g["roster_b"]))):
            (g["roster_a"], round(g["points_a"], 2), g["roster_b"], round(g["points_b"], 2)) for g in matchups}
    stages = StageIndex(load_brackets(kb_dir))
    latest = max(s for s, _, _ in have)
    synced_to = max(w for s, w, _ in have if s == latest)  # later weeks of the running season are not consolidated yet
    for season, lid in sorted(league_ids_from_kb(kb_dir).items()):
        for week in range(1, max_week + 1):
            groups: Dict[Any, list] = {}
            for e in get(f"league/{lid}/matchups/{week}") or []:
                if e.get("matchup_id") is not None:
                    groups.setdefault(e["matchup_id"], []).append(e)
            for pair in groups.values():
                if len(pair) != 2 or not any(e.get("points") for e in pair):
                    continue
                key = (season, week, frozenset(int(e["roster_id"]) for e in pair))
                if key not in have:
                    if not (season == latest and week > synced_to):
                        problems.append(f"{season} S{week}: jogo {sorted(key[2])} existe no Sleeper e falta no histórico")
                    continue
                pts = {int(e["roster_id"]): round(float(e["points"] or 0), 2) for e in pair}
                ra, pa, rb, pb = have[key]
                if abs(pts[ra] - pa) > 0.005 or abs(pts[rb] - pb) > 0.005:
                    problems.append(f"{season} S{week}: placar {ra}x{rb} difere (Sleeper {pts[ra]} x {pts[rb]}, "
                                    f"histórico {pa} x {pb})")
    for season, data in load_brackets(kb_dir).items():
        if not isinstance(data, dict) or "games" not in data:
            continue
        for g in data["games"]:
            if g["bracket"] not in (BRACKET_WINNERS, BRACKET_LOSERS):
                problems.append(f"bracket {season}: jogo com bracket inválido {g['bracket']!r}")
            key = (season, g["week"], frozenset((g["roster_a"], g["roster_b"])))
            if key not in have:
                problems.append(f"bracket {season} S{g['week']}: {g['roster_a']}x{g['roster_b']} sem jogo no histórico")
                continue
            ra, pa, rb, pb = have[key]
            hi = ra if pa > pb else rb
            if pa != pb and hi != g["winner"]:
                problems.append(f"bracket {season} S{g['week']}: vencedor {g['winner']} difere do placar ({pa} x {pb})")
        # every playoff-week game must be found in the bracket
        for (s, w, pair), _ in have.items():
            if s == season and w >= data["playoff_week_start"] and not stages.lookup(s, w, *sorted(pair)):
                problems.append(f"{s} S{w}: jogo {sorted(pair)} de pós-temporada fora do bracket")
    return problems


# --------------------------------------------------------------------------------------------------
# H2H_RIVALRIES.md
# --------------------------------------------------------------------------------------------------
def rivalries_markdown(matrix: Dict[str, Dict[str, Any]]) -> str:
    rows = sorted(matrix.values(), key=lambda p: (-p["games_total"], p["roster_1"], p["roster_2"]))
    out = [
        f"# ⚔️ Matriz Completa de Rivalidades (Head-to-Head): {league_ctx.current().name}", "",
        "> Arquivo **gerado** por `league/h2h_builder.py --rivalries-md` a partir de `matchups_history.json` +"
        " `brackets_history.json`. Não editar à mão.", "",
        "> Recortes: **Reg** = temporada regular · **PO** = playoffs do título (Wild Card, Semifinal, Final, 3º e 5º lugar)"
        " · **TB** = Toilet Bowl (consolação). Reg + PO + TB = total.", "",
        "| Duelo | Jogos | Reg | PO | TB | Vitórias A | Vitórias B | Empates | Pontos | Maior goleada | Mais apertado |",
        "|---|---|---|---|---|---|---|---|---|---|---|"]
    for p in rows:
        def mk(m: Dict[str, Any]) -> str:
            return f"{m['winner']} (+{m['margin']}) [{m['season']} S{m['week']}]" if m else "-"
        out.append(
            f"| **{p['manager_1']}** vs **{p['manager_2']}** | {p['games_total']} | "
            f"{p['wins_1_reg']}-{p['wins_2_reg']} | {p['wins_1_po']}-{p['wins_2_po']} | "
            f"{p['wins_1_tb']}-{p['wins_2_tb']} | **{p['wins_1']}** | **{p['wins_2']}** | {p['ties']} | "
            f"{p['pts_1']:.0f} x {p['pts_2']:.0f} | {mk(p['biggest_blowout'])} | {mk(p['closest_match'])} |")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------------------
def _diff(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    lines = []
    for key in sorted(new, key=lambda k: tuple(map(int, k.split("_")))):
        o, n = old.get(key, {}), new[key]
        keys = ("games_total", "games_reg", "games_playoffs", "wins_1", "wins_2", "wins_1_po", "wins_2_po",
                "pts_1", "pts_2")
        ch = [f"{k} {o.get(k)}→{n[k]}" for k in keys if o.get(k) != n[k]]
        if len(o.get("games_log", [])) != len(n["games_log"]):
            ch.append(f"games_log {len(o.get('games_log', []))}→{len(n['games_log'])}")
        if ch:
            lines.append(f"  {key} {n['manager_1']} x {n['manager_2']}: " + ", ".join(ch))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-brackets", action="store_true", help="baixa winners/losers bracket de todas as temporadas")
    ap.add_argument("--rebuild", action="store_true", help="reconstrói a matriz a partir dos jogos brutos")
    ap.add_argument("--write", action="store_true", help="grava o resultado (sem isso, só mostra o diff)")
    ap.add_argument("--rivalries-md", action="store_true", help="regenera H2H_RIVALRIES.md")
    ap.add_argument("--verify", action="store_true", help="confere histórico e brackets contra o Sleeper (rede)")
    args = ap.parse_args(argv)
    if not any((args.refresh_brackets, args.rebuild, args.rivalries_md, args.verify)):
        ap.print_help()
        return 0
    if args.refresh_brackets:
        data = refresh_brackets(league_ids_from_kb())
        print("brackets:", {s: len(v["games"]) for s, v in data.items() if s != "_meta"})
    if args.rebuild or args.rivalries_md:
        old = _load(default_kb_dir(), MATRIX_FILE)
        new = rebuild(default_kb_dir(), write=args.rebuild and args.write)
        diff = _diff(old, new)
        print(f"matriz: {len(new)} pares, {len(diff)} com mudança" + (" (gravada)" if args.rebuild and args.write else ""))
        print("\n".join(diff))
        if args.rivalries_md:
            with open(os.path.join(default_kb_dir(), "H2H_RIVALRIES.md"), "w", encoding="utf-8") as f:
                f.write(rivalries_markdown(new))
            print("H2H_RIVALRIES.md regenerado")
    if args.verify:
        problems = verify()
        print("verify:", "OK — histórico e brackets batem com o Sleeper" if not problems else f"{len(problems)} problema(s)")
        for p in problems:
            print("  -", p)
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
