#!/usr/bin/env python3
"""ESPN NFL Scoreboard & Live Game Final Watcher.

Ported and enhanced from Sleeper-API (game-watcher.service.ts & nfl-collector.service.ts).
Monitors live NFL games via ESPN public API. When a game reaches 'final' status,
it triggers an immediate flash broadcast to the WhatsApp group.
"""

import json
import logging
import os
import subprocess
import urllib.request
from typing import Any, Dict, List, Optional

import league_ctx

logger = logging.getLogger("NFLGameWatcher")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nfl_games_state.json")


def load_games_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading games state: {e}")
    return {}


def save_games_state(state: Dict[str, Any]):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving games state: {e}")


def fetch_espn_scoreboard(week: Optional[int] = None, season: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch live NFL scoreboard from ESPN Public API."""
    url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    params = []
    if week:
        params.append(f"week={week}")
        params.append("seasontype=2")
    if season:
        params.append(f"season={season}")
    if params:
        url += "?" + "&".join(params)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
            events = data.get("events", [])
            games = []
            for ev in events:
                comp = ev.get("competitions", [{}])[0]
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})

                status_type = comp.get("status", {}).get("type", {})
                state_desc = status_type.get("state")  # 'pre', 'in', 'post'
                status = "scheduled"
                if state_desc == "in":
                    status = "in_progress"
                elif state_desc == "post":
                    status = "final"

                # Extract game leaders
                leaders_summary = []
                for cat in comp.get("leaders", []):
                    top = cat.get("leaders", [{}])[0]
                    if top:
                        ath = top.get("athlete", {}).get("displayName")
                        val = top.get("displayValue")
                        cat_name = cat.get("name")  # passingLeader, rushingLeader, receivingLeader
                        short_cat = "Pass" if "pass" in cat_name.lower() else ("Rush" if "rush" in cat_name.lower() else "Rec")
                        leaders_summary.append(f"{short_cat}: {ath} ({val})")

                games.append({
                    "game_id": str(ev.get("id")),
                    "home_team": home.get("team", {}).get("abbreviation") or home.get("team", {}).get("name"),
                    "away_team": away.get("team", {}).get("abbreviation") or away.get("team", {}).get("name"),
                    "home_score": int(home.get("score") or 0),
                    "away_score": int(away.get("score") or 0),
                    "status": status,
                    "status_detail": status_type.get("detail") or status_type.get("description", ""),
                    "date": comp.get("date"),
                    "leaders": leaders_summary,
                })
            return games
    except Exception as e:
        logger.error(f"Failed to fetch ESPN scoreboard: {e}")
        return []


def format_game_final_flash(game: Dict[str, Any]) -> str:
    """Format an exciting WhatsApp flash alert when an NFL game finishes."""
    away = game["away_team"]
    home = game["home_team"]
    a_score = game["away_score"]
    h_score = game["home_score"]

    winner = home if h_score > a_score else (away if a_score > h_score else "Empate")
    diff = abs(h_score - a_score)

    lines = [
        "🏈 *PLANTÃO NFL: FIM DE JOGO!* 🚨",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏁 *FINAL*: *{away}* {a_score} x {h_score} *{home}*",
    ]

    if diff >= 20:
        lines.append(f"💥 _Atropelo contundente de *{winner}* por +{diff} pontos!_")
    elif diff <= 3:
        lines.append(f"🔥 _Decisão eletrizante decidida no detalhe (+{diff} pts)!_")
    else:
        lines.append(f"✅ _Vitória de *{winner}*._")

    if game.get("leaders"):
        lines.append("\n🌟 *Destaques Individuais:*")
        for lead in game["leaders"]:
            lines.append(f"• {lead}")

    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"⚡ _Abram o Sleeper! Os pontos dos atletas já estão impactando seus duelos na {league_ctx.current().name}!_ 📲"
    ])
    return "\n".join(lines)


def check_and_trigger_finals(send_callback=None) -> List[Dict[str, Any]]:
    """Check games and trigger callback for newly completed games."""
    state = load_games_state()
    games = fetch_espn_scoreboard()
    triggered = []

    for g in games:
        gid = g["game_id"]
        is_final = (g["status"] == "final")
        prev = state.get(gid, {})

        if is_final and not prev.get("notified_final", False):
            msg = format_game_final_flash(g)
            triggered.append(g)
            logger.info(f"Triggering final game flash for {g['away_team']} @ {g['home_team']}")

            if send_callback:
                try:
                    send_callback(msg)
                except Exception as e:
                    logger.error(f"Error executing send callback: {e}")

            state[gid] = {
                "away": g["away_team"],
                "home": g["home_team"],
                "score": f"{g['away_score']}-{g['home_score']}",
                "notified_final": True,
            }

    if triggered:
        save_games_state(state)

    return triggered


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Testing ESPN Scoreboard Fetch...")
    games = fetch_espn_scoreboard()
    print(f"Fetched {len(games)} games from ESPN.")
    for g in games[:3]:
        print(f"• {g['away_team']} {g['away_score']} @ {g['home_team']} {g['home_score']} [{g['status']} - {g['status_detail']}]")
