#!/usr/bin/env python3
"""Draft Analytics & Grading Engine for High Stakes Fantasy.

Ported and enhanced from Sleeper-API (draft-analytics.service.ts).
Evaluates draft performance, calculates grades (A+ to F), steals, reaches,
and provides Mr. Irrelevant's sharp critique of each franchise's draft class.
"""

import json
import os
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def calculate_grade(score: float) -> str:
    if score >= 90: return "A+"
    if score >= 85: return "A"
    if score >= 80: return "A-"
    if score >= 75: return "B+"
    if score >= 70: return "B"
    if score >= 65: return "B-"
    if score >= 60: return "C+"
    if score >= 55: return "C"
    if score >= 50: return "C-"
    if score >= 45: return "D+"
    if score >= 40: return "D"
    return "F"


def evaluate_draft(season: str = "2023") -> Dict[str, Any]:
    """Evaluate draft picks for a given season and calculate franchise grades."""
    import league_ctx
    lctx = league_ctx.current()
    draft_file = lctx.source_path("draft_history.json")
    franchises_file = lctx.source_path("franchises_history.json")

    if not os.path.exists(draft_file):
        return {"error": "draft_history.json not found"}

    with open(draft_file, "r", encoding="utf-8") as f:
        drafts = json.load(f)

    with open(franchises_file, "r", encoding="utf-8") as f:
        franchises = json.load(f)

    dinfo = drafts.get(str(season), {})
    picks = dinfo.get("picks", [])
    if not picks:
        return {"error": f"Nenhum pick encontrado para o draft de {season}"}

    # Group picks by roster
    team_picks: Dict[int, List[Dict[str, Any]]] = {}
    for p in picks:
        rid = p.get("roster_id")
        if rid is not None:
            team_picks.setdefault(rid, []).append(p)

    # Notable historical steals & hits
    notable_gems = {
        "9493": 35.0,  # Puka Nacua (Ultimate Steal)
        "9478": 25.0,  # Sam LaPorta (Elite TE in round 2)
        "9226": 20.0,  # Tank Dell (Round 3 hit)
        "9221": 18.0,  # Chase Brown
        "11632": 22.0, # Bucky Irving (Round 3 RB steal)
        "11566": 15.0, # Braelon Allen
        "9509": 15.0,  # Tucker Kraft
        "9228": 25.0,  # C.J. Stroud
        "11560": 25.0, # Brock Bowers
        "11563": 25.0, # Jayden Daniels
        "11628": 25.0, # Malik Nabers
    }

    results = []
    for rid in range(1, (league_ctx.current().profile.total_rosters or 12) + 1):
        fdata = franchises.get(str(rid), {})
        sdata = fdata.get("seasons", {}).get(str(season), {})
        tname = sdata.get("team_name") or fdata.get("current_team", f"Time {rid}")
        mgr = sdata.get("manager_name") or fdata.get("canonical_manager", f"Manager {rid}")

        r_picks = team_picks.get(rid, [])
        if not r_picks:
            continue

        base_score = 70.0
        steals = []
        player_names = []

        for p in r_picks:
            meta = p.get("metadata", {})
            pid = str(p.get("player_id", ""))
            pname = f"{meta.get('first_name', '')} {meta.get('last_name', '')}".strip()
            rnd = p.get("round", 1)
            pos = meta.get("position", "-")
            nfl = meta.get("team", "-")

            player_names.append(f"{pname} ({pos} - R{rnd})")

            # Check steal value
            if pid in notable_gems:
                bonus = notable_gems[pid]
                base_score += bonus
                steals.append(f"*{pname}* (R{rnd} - Steal de peso!)")

            # Premium position adjustments
            if rnd == 1:
                base_score += 3.0

        # Cap score between 35 and 98
        final_score = min(98.0, max(35.0, base_score))
        grade = calculate_grade(final_score)

        results.append({
            "roster_id": rid,
            "team_name": tname,
            "manager_name": mgr,
            "grade": grade,
            "score": round(final_score, 1),
            "picks_count": len(r_picks),
            "steals": steals,
            "players": player_names,
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return {
        "season": season,
        "draft_name": dinfo.get("draft_name", f"Rookie Draft {season}"),
        "teams": results,
    }


def format_draft_grades_whatsapp(season: str = "2023") -> str:
    """Format an entertaining draft grade report for WhatsApp."""
    eval_data = evaluate_draft(season)
    if "error" in eval_data:
        return f"❌ {eval_data['error']}"

    s = eval_data["season"]
    lines = [
        f"🎓 *BOLETIM DE NOTAS DO DRAFT — TEMPORADA {s}* 📋",
        f"_{eval_data['draft_name']} — Avaliação Técnica do Mr. Irrelevant_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Quem brilhou no scout e quem queimou escolhas preciosas:\n",
    ]

    for idx, tm in enumerate(eval_data["teams"], 1):
        grade = tm["grade"]
        tname = tm["team_name"]
        mgr = tm["manager_name"]
        steals_str = f" | 💎 {', '.join(tm['steals'])}" if tm["steals"] else ""

        lines.append(f"*{idx}º* [{grade}] *{tname}* ({mgr}) — {tm['picks_count']} escolhas{steals_str}")
        lines.append(f"   ↳ {', '.join(tm['players'][:4])}")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "🎙️ _Mr. Irrelevant: Nota boa no draft é bonito no papel. O que define uma franquia é saber lapidar talento e não jogar escolha de 4ª rodada no lixo!_"
    ])
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_draft_grades_whatsapp("2023"))
