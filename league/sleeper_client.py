#!/usr/bin/env python3
"""Sleeper API client for one league (the current LeagueContext's, unless a league id is given).

Zero external dependencies - standard library only.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional

import league_ctx

CACHE_DIR = os.path.expanduser("~/.cache/sleeper_bot")

# In-process TTL cache shared by every SleeperClient instance (the engine creates many per query).
_MEM: Dict[str, tuple] = {}
# Endpoints answered from the on-disk copy because the live call failed; liga_core reports them to the user.
STALE: set = set()


def _persona() -> str:
    return league_ctx.current().persona_name


def _ttl_for(endpoint: str) -> float:
    if endpoint.startswith("state/nfl"):
        return 120.0
    if "/matchups/" in endpoint or endpoint.startswith("stats/") or endpoint.startswith("projections/"):
        return 45.0
    if "/transactions/" in endpoint or endpoint.endswith("/rosters"):
        return 60.0
    return 300.0


def with_week_starters(rosters: List[Dict[str, Any]], matchups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Copies of `rosters` whose `starters` come from the week's matchup entries (when they have a lineup)."""
    by_rid = {int(m["roster_id"]): m.get("starters") for m in matchups or [] if m.get("roster_id") is not None}
    out = []
    for r in rosters:
        st = by_rid.get(int(r.get("roster_id", -1)))
        out.append({**r, "starters": [str(p) for p in st]} if st else r)
    return out


def _record_key(t: Dict[str, Any]):
    """Wins (a tie is half a win) DESC, then points for DESC."""
    return (t.get("wins", 0) + 0.5 * t.get("ties", 0), t.get("pts_for", 0.0))


class SleeperClient:
    def __init__(self, league_id: Optional[str] = None):
        self.league_id = str(league_id or league_ctx.current().league_id)
        self.base_url = os.environ.get("SLEEPER_API_BASE", "https://api.sleeper.app/v1")
        self.headers = {"User-Agent": "SleeperBot/1.0 (Fantasy NFL Assistant)"}
        self._stats_cache = {}
        self._proj_cache = {}
        self._players_db = None
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _get(self, endpoint: str) -> Any:
        clean_ep = endpoint.lstrip("/")
        hit = _MEM.get(clean_ep)
        if hit and time.monotonic() - hit[0] < _ttl_for(clean_ep):
            return hit[1]
        cache_file = os.path.join(CACHE_DIR, clean_ep.replace("/", "_").replace("?", "_").replace("&", "_") + ".json")
        url = f"{self.base_url}/{clean_ep}"
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
            _MEM[clean_ep] = (time.monotonic(), data)
            STALE.discard(clean_ep)
            try:
                with open(cache_file, "w", encoding="utf-8") as cf:
                    json.dump(data, cf)
            except Exception:
                pass
            return data
        except Exception as e:
            print(f"[WARN] Sleeper request failed ({e}) for {url}", file=sys.stderr)
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, "r", encoding="utf-8") as cf:
                        data = json.load(cf)
                    STALE.add(clean_ep)
                    return data
                except Exception:
                    pass
            STALE.add(clean_ep)
            return None

    def get_nfl_state(self) -> Dict[str, Any]:
        """Get current NFL state (week, season, season_type)."""
        state = self._get("state/nfl")
        if state:
            return state
        from datetime import datetime
        return {"week": 1, "season": str(datetime.now().year), "season_type": "regular"}

    def get_nfl_schedule(self, season: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get NFL games schedule and live game statuses with local caching."""
        if season is None:
            season = self.get_nfl_state().get("season", "2026")
        if hasattr(self, "_schedule_cache") and self._schedule_cache:
            return self._schedule_cache

        cache_f = f"/tmp/schedule_{season}.json"
        if os.path.exists(cache_f):
            try:
                with open(cache_f, "r") as f:
                    self._schedule_cache = json.load(f)
                    return self._schedule_cache
            except Exception:
                pass

        self._schedule_cache = []
        return []

    def get_nfl_game_statuses(self, week: Optional[int] = None, season: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """Map NFL team abbreviation to their game status for the given week using ESPN scoreboard and stats."""
        if week is None:
            week = self.get_nfl_state().get("week", 1)
        if season is None:
            season = self.get_nfl_state().get("season", "2026")

        team_status = {}
        self._espn_ok = False
        try:
            from nfl_game_watcher import fetch_espn_scoreboard
            espn_games = fetch_espn_scoreboard(week=week, season=season)
            self._espn_ok = bool(espn_games)
            espn_to_sleeper = {
                "WSH": "WAS", "JAC": "JAX", "LAR": "LAR", "LAC": "LAC",
            }
            for g in espn_games:
                h = g.get("home_team", "")
                a = g.get("away_team", "")
                h_slp = espn_to_sleeper.get(h, h)
                a_slp = espn_to_sleeper.get(a, a)
                raw_status = g.get("status", "scheduled")
                if raw_status == "final":
                    st = "complete"
                elif raw_status == "in_progress":
                    st = "in_game"
                else:
                    st = "pre_game"
                
                info = {
                    "status": st,
                    "home": h_slp,
                    "away": a_slp,
                    "date": g.get("date"),
                    "detail": g.get("status_detail", ""),
                }
                if h_slp:
                    team_status[h_slp] = info
                if a_slp:
                    team_status[a_slp] = info
        except Exception as e:
            print(f"[WARN] Failed to load ESPN scoreboard for NFL statuses: {e}", file=sys.stderr)
        if not self._espn_ok:
            STALE.add("espn/scoreboard")
        return team_status

    def get_league(self) -> Dict[str, Any]:
        """Get league details."""
        return self._get(f"league/{self.league_id}") or {}

    def get_users(self) -> Dict[str, Dict[str, Any]]:
        """Get all users mapped by user_id."""
        raw = self._get(f"league/{self.league_id}/users") or []
        users = {}
        for u in raw:
            uid = u.get("user_id")
            team_name = u.get("metadata", {}).get("team_name")
            display_name = u.get("display_name", f"User_{uid}")
            users[uid] = {
                "user_id": uid,
                "display_name": display_name,
                "team_name": team_name or display_name,
                "avatar": u.get("avatar"),
            }
        return users

    def get_users_raw(self) -> List[Dict[str, Any]]:
        """Sleeper's /users list as is (display_name, metadata.team_name, ...)."""
        return self._get(f"league/{self.league_id}/users") or []

    def get_rosters(self) -> List[Dict[str, Any]]:
        """Get all rosters in the league, with `starters` = the lineup set for the CURRENT week.

        /rosters keeps the last lineup Sleeper copied into it and misses a lineup saved for the week ahead
        (set Monday for Thursday's week); the week's /matchups entry is the source of truth."""
        rosters = self._get(f"league/{self.league_id}/rosters") or []
        state = self._get("state/nfl") or {}     # not get_nfl_state(): its fallback (week 1) would be wrong here
        week = int(state.get("week") or 0) if state.get("season_type") in ("regular", "post") else 0
        if not week:
            return rosters
        return with_week_starters(rosters, self._get(f"league/{self.league_id}/matchups/{week}") or [])

    def get_players_db(self) -> Dict[str, Any]:
        """Merged player DB with live team/injury status (see players_db.py)."""
        if self._players_db is None:
            import players_db
            self._players_db = players_db.load()
        return self._players_db

    def find_player(self, query: str) -> Optional[Dict[str, Any]]:
        """Find a player by name in the local database with exact, substring, and fuzzy matching."""
        if not query:
            return None
        q = query.lower().strip()
        db = self.get_players_db()

        # 1. Exact match (case insensitive)
        for pid, p in db.items():
            if not isinstance(p, dict):
                continue
            name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if q == name.lower():
                return {"player_id": pid, **p, "name": name}

        # 2. Substring match
        for pid, p in db.items():
            if not isinstance(p, dict):
                continue
            name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            if q in name.lower() or name.lower() in q:
                return {"player_id": pid, **p, "name": name}

        # 3. Fast fuzzy match using difflib.get_close_matches (pre-filtered for sub-10ms speed)
        import difflib
        q_first = q[0] if q else ""
        q_parts = q.split()
        q_last = q_parts[-1] if len(q_parts) > 1 else ""
        q_last_first = q_last[0] if q_last else ""

        candidate_map = {}
        last_candidate_map = {}

        for pid, p in db.items():
            if not isinstance(p, dict):
                continue
            name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            name_l = name.lower()
            last_l = (p.get("last_name") or (name.split()[-1] if name else "")).lower()

            # Pre-filter: must share initial of full name or last name
            if name_l.startswith(q_first) or (q_last_first and last_l.startswith(q_last_first)):
                candidate_map[name_l] = {"player_id": pid, **p, "name": name}
            if q_last and last_l.startswith(q_last_first) and name_l.startswith(q_first):
                last_candidate_map[last_l] = {"player_id": pid, **p, "name": name}

        # Match full name
        matches = difflib.get_close_matches(q, candidate_map.keys(), n=1, cutoff=0.72)
        if matches:
            return candidate_map[matches[0]]

        # Match last name if multi-word query
        if q_last and last_candidate_map:
            last_matches = difflib.get_close_matches(q_last, last_candidate_map.keys(), n=1, cutoff=0.82)
            if last_matches:
                return last_candidate_map[last_matches[0]]

        return None

    def get_player_stats(self, player_name: str, week: Optional[int] = None, season: Optional[str] = None) -> Dict[str, Any]:
        """Fetch actual box score and fantasy stats for a player in a specific week."""
        if season is None:
            season = self.get_nfl_state().get("season", "2026")
        note = ""
        if week is None:
            week = int(self.get_nfl_state().get("week", 1) or 1)
            try:   # current week not kicked off yet (Tue-Thu): the box score that exists is last week's, not a "0"
                import league_time
                slate = league_time.week_games(week, season)
                if week > 1 and slate and all(g["state"] == "pre" for g in slate):
                    week, note = week - 1, f"Semana {week} ainda não começou — mostrando a Semana {week - 1}."
            except Exception:
                pass

        p = self.find_player(player_name)
        if not p:
            return {"error": f"Jogador '{player_name}' não encontrado na base da liga."}

        pid = p["player_id"]
        stats_dict = self._get(f"stats/nfl/regular/{season}/{week}") or {}
        p_stats = stats_dict.get(pid, {})

        proj_dict = self._get(f"projections/nfl/regular/{season}/{week}") or {}
        p_proj = proj_dict.get(pid, {})

        pts_ppr = float(p_stats.get("pts_ppr", p_stats.get("pts_half_ppr", p_stats.get("pts_std", 0.0))))
        proj_ppr = float(p_proj.get("pts_ppr", p_proj.get("pts_half_ppr", 0.0)))

        # Bye: the player's team is missing from a non-empty slate. A bye is NOT a 0-point game (never average it in).
        bye = False
        try:
            import league_time
            slate = league_time.games_by_team(int(week), str(season))
            bye = bool(slate) and p.get("team") not in slate and p.get("team") not in (None, "", "FA") and not p_stats
        except Exception:
            pass

        return {
            "player_id": pid,
            "name": p.get("name"),
            "team": p.get("team"),
            "pos": p.get("pos"),
            "week": week,
            "season": season,
            "points_ppr": round(pts_ppr, 2),
            "projected_ppr": round(proj_ppr, 2),
            "targets": int(p_stats.get("rec_tgt", 0)),
            "receptions": int(p_stats.get("rec", 0)),
            "rec_yards": float(p_stats.get("rec_yd", 0.0)),
            "rec_td": int(p_stats.get("rec_td", 0)),
            "rush_att": int(p_stats.get("rush_att", 0)),
            "rush_yards": float(p_stats.get("rush_yd", 0.0)),
            "rush_td": int(p_stats.get("rush_td", 0)),
            "pass_att": int(p_stats.get("pass_att", 0)),
            "pass_cmp": int(p_stats.get("pass_cmp", 0)),
            "pass_yards": float(p_stats.get("pass_yd", 0.0)),
            "pass_td": int(p_stats.get("pass_td", 0)),
            "pass_int": int(p_stats.get("pass_int", 0)),
            "fumbles_lost": int(p_stats.get("fum_lost", 0)),
            "has_played": bool(p_stats),
            "bye": bye,
            "note": note,
        }

    def get_player_news(self, player_name: str) -> Dict[str, Any]:
        """Fetch real-time injury status and latest news for a player."""
        p = self.find_player(player_name)
        if not p:
            return {"error": f"Jogador '{player_name}' não encontrado na base da liga."}

        import players_db
        pid = p["player_id"]
        injury_status = players_db.injury(p) or "Active"
        injury_body_part = p.get("injury_body_part") or "Nenhum"
        injury_notes = p.get("injury_notes") or ""
        injury_start_date = p.get("injury_start_date") or ""

        # Fetch recent player news from NewsService (Rotowire, ESPN, FantasyPros, Google News)
        news_items = []
        in_game_alert = False
        forecast = ""
        try:
            from news_service import NewsService
            ns = NewsService()
            player_full_name = p.get("name") or player_name
            found_news = ns.search_player_news(player_full_name)
            # Per-player ESPN overview (Rotowire analysis + ESPN headlines) — far better recall than RSS.
            try:
                import data_lab
                espn_items = data_lab.espn_player_news(pid)
                seen = {(n.get("title") or "").strip().lower() for n in espn_items}
                found_news = espn_items + [n for n in found_news if (n.get("title") or "").strip().lower() not in seen]
            except Exception as exc:
                print(f"[WARN] ESPN player news failed: {exc}", file=sys.stderr)

            for item in found_news:
                news_items.append({
                    "date": item.get("published_at", ""),
                    "headline": item.get("title", ""),
                    "body": item.get("summary", ""),
                    "analysis": item.get("category", ""),
                    "source": item.get("source", ""),
                    "title": item.get("title", ""),
                    "summary": item.get("summary", ""),
                })

            # Classify injury with live in-game detection and forecast
            c_res = ns.classify_injury(p, found_news)
            in_game_alert = c_res.get("in_game_injury", False)
            forecast = c_res.get("forecast", "")
            if c_res.get("body_part") and c_res.get("body_part") != "Nenhum":
                injury_body_part = c_res.get("body_part")
            if c_res.get("official_status"):
                injury_status = c_res.get("official_status")
        except Exception as e:
            import traceback
            traceback.print_exc()

        return {
            "player_id": pid,
            "name": p.get("name"),
            "team": p.get("team"),
            "pos": p.get("pos"),
            "injury_status": injury_status,
            "injury_body_part": injury_body_part,
            "injury_notes": injury_notes,
            "injury_start_date": injury_start_date,
            "recent_news": news_items,
            "in_game_alert": in_game_alert,
            "forecast": forecast,
        }

    def get_trending(self, trend_type: str = "add", lookback_hours: int = 24, limit: int = 25) -> List[Dict[str, Any]]:
        """Get trending NFL player adds or drops."""
        trend_type = "add" if trend_type.lower() == "add" else "drop"
        url = f"players/nfl/trending/{trend_type}?lookback_hours={lookback_hours}&limit={limit}"
        return self._get(url) or []

    def get_standings(self) -> List[Dict[str, Any]]:
        """Calculate sorted league standings with divisions."""
        league = self.get_league()
        meta = league.get("metadata", {})
        num_divs = league.get("settings", {}).get("divisions", 3)
        div_names = {}
        for i in range(1, num_divs + 1):
            div_names[i] = meta.get(f"division_{i}", f"Divisão {i}")

        users = self.get_users()
        rosters = self.get_rosters()
        standings = []

        for r in rosters:
            roster_id = r.get("roster_id")
            owner_id = r.get("owner_id")
            user_info = users.get(owner_id, {})
            team_name = user_info.get("team_name") or f"Team {roster_id}"
            owner_name = user_info.get("display_name") or f"Owner {roster_id}"

            settings = r.get("settings") or {}
            wins = settings.get("wins", 0)
            losses = settings.get("losses", 0)
            ties = settings.get("ties", 0)
            fpts = settings.get("fpts", 0) + (settings.get("fpts_decimal", 0) / 100.0)
            fpts_against = settings.get("fpts_against", 0) + (settings.get("fpts_against_decimal", 0) / 100.0)
            metadata = r.get("metadata") or {}
            streak = metadata.get("streak", "-")
            div_id = settings.get("division", 1)

            standings.append({
                "rank": 0,
                "division_rank": 0,
                "division_id": div_id,
                "division_name": div_names.get(div_id, f"Divisão {div_id}"),
                "roster_id": roster_id,
                "team_name": team_name,
                "owner_name": owner_name,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "pct": (wins + 0.5 * ties) / max(1, wins + losses + ties),
                "pts_for": fpts,
                "pts_against": fpts_against,
                "streak": streak,
                "waiver_pos": settings.get("waiver_position", 0),
            })

        # Division ranks: record (ties = half a win), then points for
        by_div = {}
        for s in standings:
            by_div.setdefault(s["division_id"], []).append(s)
        for d_id, group in by_div.items():
            group.sort(key=_record_key, reverse=True)
            for idx, item in enumerate(group, 1):
                item["division_rank"] = idx

        # Overall rank = playoff seed. House rule `seeding: division_winners`: seeds 1..N are the N division leaders
        # (ordered by record, then PF), seeds N+1.. are everybody else by record, then PF. Without it: plain record.
        rule = None
        try:
            rule = league_ctx.current().rule("seeding", None)
        except Exception:
            pass
        n_div = len(by_div)
        if rule == "division_winners" and n_div > 1:
            leaders = sorted((s for s in standings if s["division_rank"] == 1), key=_record_key, reverse=True)
            rest = sorted((s for s in standings if s["division_rank"] != 1), key=_record_key, reverse=True)
            standings[:] = leaders + rest
        else:
            standings.sort(key=_record_key, reverse=True)
        for i, s in enumerate(standings, 1):
            s["rank"] = i
            s["division_leader"] = s["division_rank"] == 1

        return standings

    def get_matchups(self, week: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get matchups for a specific week (defaults to current week)."""
        if week is None:
            state = self.get_nfl_state()
            week = state.get("week", 1)

        raw_matchups = self._get(f"league/{self.league_id}/matchups/{week}") or []
        users = self.get_users()
        rosters = {r.get("roster_id"): r for r in self.get_rosters()}

        # Group by matchup_id
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for m in raw_matchups:
            mid = m.get("matchup_id")
            if mid is None:
                continue
            roster_id = m.get("roster_id")
            roster = rosters.get(roster_id, {})
            owner_id = roster.get("owner_id")
            user_info = users.get(owner_id, {})

            team_name = user_info.get("team_name") or f"Team {roster_id}"
            owner_name = user_info.get("display_name") or f"Owner {roster_id}"
            points = m.get("points", 0.0) or 0.0

            grouped.setdefault(mid, []).append({
                "roster_id": roster_id,
                "team_name": team_name,
                "owner_name": owner_name,
                "points": float(points),
                "starters": m.get("starters", []),
                "starters_points": m.get("starters_points", []),
            })

        nfl_state = self.get_nfl_state()
        season = nfl_state.get("season", "2026")
        nfl_statuses = self.get_nfl_game_statuses(week, season)

        # Load player DB
        players_db = self.get_players_db()

        def get_active_or_pending_count(starters: List[str]) -> int:
            if not starters:
                return 0
            count = 0
            # ESPN up: a team missing from the scoreboard is on bye (done). ESPN down: unknown → still pending,
            # never "everything is over".
            missing = "complete" if getattr(self, "_espn_ok", False) else "pre_game"
            for p in starters:
                if not p or p == "0":
                    continue
                p_team = players_db.get(p, {}).get("team")
                g_status = nfl_statuses.get(p_team, {}).get("status", missing) if p_team else "complete"
                # If NFL game is in_game (live right now) or pre_game (yet to start), starter is active/pending!
                if g_status in ["in_game", "pre_game"]:
                    count += 1
            return count

        # Load projections and calculate realistic projected points for starters
        proj_dict = self._get(f"projections/nfl/regular/{season}/{week}") or {}
        if not isinstance(proj_dict, dict):
            proj_dict = {}

        pos_baselines = {
            "QB": 17.5, "RB": 12.0, "WR": 12.5, "TE": 9.0,
            "K": 7.5, "DEF": 6.5, "FLEX": 10.5
        }

        def calculate_team_projection(starters_list: List[str]) -> float:
            tot = 0.0
            for pid in starters_list:
                if not pid or pid == "0":
                    continue
                p_data = proj_dict.get(pid, {})
                val = float(p_data.get("pts_ppr") or p_data.get("pts_half_ppr") or 0.0)
                if val <= 0.0:
                    p_pos = players_db.get(pid, {}).get("pos", "FLEX")
                    val = pos_baselines.get(p_pos, 10.0)
                tot += val
            return round(tot, 1)

        # Assign projected_points to all teams in grouped
        for mid_teams in grouped.values():
            for t_item in mid_teams:
                t_item["projected_points"] = calculate_team_projection(t_item.get("starters", []))

        total_week_points = sum(t.get("points", 0.0) for teams in grouped.values() for t in teams)
        week_started = (total_week_points > 0.0)

        matchup_list = []
        for mid, teams in grouped.items():
            if len(teams) == 2:
                team1, team2 = teams[0], teams[1]
                diff = abs(team1["points"] - team2["points"])
                winner = None
                if team1["points"] > team2["points"]:
                    winner = team1["team_name"]
                elif team2["points"] > team1["points"]:
                    winner = team2["team_name"]

                t1_rem = get_active_or_pending_count(team1.get("starters", []))
                t2_rem = get_active_or_pending_count(team2.get("starters", []))

                if not week_started:
                    is_open = False
                    status = "NÃO INICIADO"
                else:
                    is_open = (t1_rem > 0 or t2_rem > 0)
                    status = "EM ABERTO" if is_open else "ENCERRADO"

                matchup_list.append({
                    "matchup_id": mid,
                    "week": week,
                    "team1": team1,
                    "team2": team2,
                    "diff": round(diff, 2),
                    "winner": winner,
                    "is_open": is_open,
                    "status": status,
                    "week_started": week_started,
                    "t1_remaining": t1_rem,
                    "t2_remaining": t2_rem,
                })
            else:
                t1_rem = get_active_or_pending_count(teams[0].get("starters", []))
                status = "NÃO INICIADO" if not week_started else ("EM ABERTO" if t1_rem > 0 else "ENCERRADO")
                matchup_list.append({
                    "matchup_id": mid,
                    "week": week,
                    "team1": teams[0],
                    "team2": None,
                    "diff": 0.0,
                    "winner": teams[0]["team_name"],
                    "is_open": t1_rem > 0 if week_started else False,
                    "status": status,
                    "week_started": week_started,
                    "t1_remaining": t1_rem,
                    "t2_remaining": 0,
                })

        # Order: open matchups first, then sorted by matchup_id
        matchup_list.sort(key=lambda x: (not x["is_open"], x["matchup_id"]))
        return matchup_list

    def get_transactions(self, week: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get waiver/trade/free agent transactions for a week."""
        if week is None:
            state = self.get_nfl_state()
            week = state.get("week", 1)

        raw = self._get(f"league/{self.league_id}/transactions/{week}") or []
        users = self.get_users()
        rosters = {r.get("roster_id"): r for r in self.get_rosters()}

        cleaned = []
        for t in raw:
            ttype = t.get("type")  # waiver, trade, free_agent
            status = t.get("status")  # complete, failed
            roster_ids = t.get("roster_ids", [])
            teams = []
            for rid in roster_ids:
                roster = rosters.get(rid, {})
                owner_id = roster.get("owner_id")
                u = users.get(owner_id, {})
                teams.append(u.get("team_name") or f"Team {rid}")

            adds = t.get("adds") or {}
            drops = t.get("drops") or {}

            cleaned.append({
                "transaction_id": str(t.get("transaction_id")),
                "type": ttype,
                "status": status,
                "teams": teams,
                "roster_ids": roster_ids,
                "adds": adds,
                "drops": drops,
                "settings": t.get("settings") or {},
                "created": t.get("created"),
                "status_updated": t.get("status_updated") or t.get("created"),
                "leg": t.get("leg") or week,
            })
        return cleaned

    def get_recent_transactions(self, hours: float = 30.0, types=("waiver", "free_agent"),
                                only_complete: bool = True) -> List[Dict[str, Any]]:
        """Transactions processed in the last `hours`, from the current AND previous leg.

        Sleeper stores a claim in the leg it was created in, so the Thursday-morning waiver run of week N
        lives in leg N-1 — reading only the current week misses it."""
        week = int(self.get_nfl_state().get("week") or 1)
        cutoff = (time.time() - hours * 3600.0) * 1000.0
        out, seen = [], set()
        for w in ([week, week - 1] if week > 1 else [week]):
            for t in self.get_transactions(w):
                if t["transaction_id"] in seen or t["type"] not in types:
                    continue
                if only_complete and t["status"] != "complete":
                    continue
                if (t.get("status_updated") or 0) < cutoff:
                    continue
                seen.add(t["transaction_id"])
                out.append(t)
        out.sort(key=lambda t: t.get("status_updated") or 0, reverse=True)
        return out

    # Formatters for WhatsApp Messages
    def format_standings_whatsapp(self) -> str:
        league = self.get_league()
        league_name = league.get("name") or league_ctx.current().name
        season = league.get("season", "2026")
        standings = self.get_standings()

        lines = [
            f"🏆 *CLASSIFICAÇÃO OFICIAL — {league_name.upper()} ({season})*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "📍 *CLASSIFICAÇÃO POR DIVISÃO:*",
        ]

        # Group by division
        by_div = {}
        for s in standings:
            d_id = s["division_id"]
            if d_id not in by_div:
                by_div[d_id] = []
            by_div[d_id].append(s)

        ctx = league_ctx.current()
        div_icons = {str(k): v for k, v in (ctx.config.get("division_icons") or {}).items()}
        for div_id in sorted(by_div.keys()):
            d_teams = sorted(by_div[div_id], key=lambda x: x["division_rank"])
            d_name = d_teams[0]["division_name"] if d_teams else f"Divisão {div_id}"
            icon = div_icons.get(str(div_id), "📍")
            lines.append(f"\n{icon} *{d_name.upper()} (Divisão {div_id})*")
            for t in d_teams:
                rec = f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t['ties'] > 0 else "")
                lines.append(f"  {t['division_rank']}º *{t['team_name']}* ({t['owner_name']}): *{rec}* | {t['pts_for']:.2f} pts")

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━",
            "🌐 *CLASSIFICAÇÃO GERAL (OVERALL):*",
        ])

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for s in sorted(standings, key=lambda x: x["rank"]):
            r = s["rank"]
            ic = medals.get(r, f"{r:2d}.")
            rec = f"{s['wins']}-{s['losses']}" + (f"-{s['ties']}" if s['ties'] > 0 else "")
            lines.append(f"{ic} *{s['team_name']}* ({s['owner_name']}) — *{rec}* ({s['pts_for']:.2f} pts) [{s['division_name']}]")

        lines.extend(["", "━━━━━━━━━━━━━━━━━━━━━━"] + self._postseason_structure_lines() + [
            "━━━━━━━━━━━━━━━━━━━━━━",
            "⚡ _Atualizado via Sleeper Bot & Hermes Agent_",
        ])
        return "\n".join(lines)

    @staticmethod
    def _postseason_structure_lines() -> List[str]:
        """Post-season layout of THIS league (teams, weeks, consolation bracket) from its Sleeper settings."""
        ctx = league_ctx.current()
        prof = ctx.profile
        if not prof.playoff_teams:
            return []
        start, end = prof.playoff_week_start, prof.playoff_week_end
        span = f" (SEMANAS {start} A {end})" if start and end > start else (f" (SEMANA {start})" if start else "")
        lines = [f"👑 *ESTRUTURA DE PÓS-TEMPORADA{span}:*",
                 f"• 🏆 *Playoffs do Título (Championship Bracket):* Top {prof.playoff_teams} da Geral disputam o anel e "
                 "taça de campeão."]
        if prof.total_rosters > prof.playoff_teams and ctx.has_consolation():
            lines.append(f"• 🚽 *Toilet Bowl (Consolation Bracket):* {prof.playoff_teams + 1}º ao {prof.total_rosters}º "
                         "disputam o título de consolação e a fuga da lanterna.")
        return lines

    def format_playoff_picture_whatsapp(self) -> str:
        """Who is in the title bracket today, with seeds (house rule: division leaders first)."""
        prof = league_ctx.current().profile
        n = prof.playoff_teams or 6
        st = sorted(self.get_standings(), key=lambda x: x["rank"])
        try:
            wk = max(0, int(self.get_nfl_state().get("week", 1)) - 1)
        except Exception:
            wk = 0
        n_div = len({t["division_id"] for t in st})
        by_rule = league_ctx.current().rule("seeding", None) == "division_winners" and n_div > 1
        lines = [f"🏟️ *CLASSIFICADOS PARA OS PLAYOFFS — SEEDS ATUAIS*",
                 f"_Após a Semana {wk} · Top {n} disputam o título"
                 + (f" (a partir da Semana {prof.playoff_week_start})_" if prof.playoff_week_start else "_"),
                 "━━━━━━━━━━━━━━━━━━━━━━"]

        def row(t):
            rec = f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t['ties'] > 0 else "")
            tag = " 👑 líder da divisão" if t.get("division_leader") else ""
            return f"{t['rank']}º *{t['team_name']}* ({t['owner_name']}) — {rec} · {t['pts_for']:.2f} pts · {t['division_name']}{tag}"

        lines += [row(t) for t in st[:n]]
        if len(st) > n:
            lines += ["", "🔎 *Na bolha:*"] + [row(t) for t in st[n:n + 2]]
        if by_rule:
            lines += ["", f"_Critério: seeds 1–{n_div} = líderes de cada divisão (por campanha e PF); "
                          f"{n_div + 1}º em diante = o resto por campanha e PF._"]
        return "\n".join(lines)

    def format_matchups_whatsapp(self, week: Optional[int] = None) -> str:
        if week is None:
            week = self.get_nfl_state().get("week", 1)

        matchups = self.get_matchups(week)
        league = self.get_league()
        league_name = league.get("name") or league_ctx.current().name

        lines = [
            f"🏈 *PANORAMA DOS CONFRONTOS - SEMANA {week}*",
            f"_{league_name}_",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        if not matchups:
            lines.append("Nenhum confronto encontrado para esta semana.")
            return "\n".join(lines)

        week_started = any(m.get("week_started", False) for m in matchups)
        if not week_started:
            lines = [
                f"📅 *PRÉ-RODADA DOS CONFRONTOS — SEMANA {week}*",
                f"_{league_name}_",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"ℹ️ _A rodada da Semana {week} ainda NÃO começou (Kickoff na Quinta-feira)._",
                "_Confrontos agendados da rodada:_\n"
            ]
            for m in matchups:
                t1, t2 = m.get("team1"), m.get("team2")
                if t1 and t2:
                    lines.append(f"⚔️ *{t1['team_name']}* ({t1['owner_name']})  x  *{t2['team_name']}* ({t2['owner_name']})")
                    lines.append(f"   ↳ Status: 🗓️ *PRÉ-JOGO (NÃO INICIADO)*")
                    lines.append("─" * 22)
                elif t1:
                    lines.append(f"⚔️ *{t1['team_name']}* ({t1['owner_name']}) ➡️ Bye Week")
                    lines.append("─" * 22)
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"🎙️ _{_persona()} — Para análise detalhada de virada ou projeção de um time: mencione o bot com 'analise <time>'_")
            return "\n".join(lines)

        open_matchups = [m for m in matchups if m.get("is_open", False)]
        closed_matchups = [m for m in matchups if not m.get("is_open", False)]

        if open_matchups:
            analyzed_open = []
            for m in open_matchups:
                a = self.analyze_matchup(str(m["matchup_id"]), week)
                t1, t2 = a["team1"], a["team2"]

                if t1["current_points"] >= t2["current_points"]:
                    leader, trailer = t1, t2
                else:
                    leader, trailer = t2, t1

                leader_pending = len(leader["pending"])
                trailer_pending = len(trailer["pending"])

                # Jogo onde só tem jogador do vencedor participando
                only_winner_playing = (trailer_pending == 0 and leader_pending > 0)
                prob_spread = abs(a["win_prob_team1"] - 50.0)

                analyzed_open.append({
                    "analysis": a,
                    "only_winner_playing": only_winner_playing,
                    "prob_spread": prob_spread,
                })

            # Ordenar: mais disputados primeiro (menor spread), jogos com só o vencedor por último
            analyzed_open.sort(key=lambda x: (1 if x["only_winner_playing"] else 0, x["prob_spread"]))

            contested_items = [x for x in analyzed_open if not x["only_winner_playing"]]
            decided_items = [x for x in analyzed_open if x["only_winner_playing"]]

            if contested_items:
                lines.append("🟢 *JOGOS EM ABERTO (MAIS DISPUTADOS & VIRADAS POSSÍVEIS)*:")
                lines.append("")
                for item in contested_items:
                    a = item["analysis"]
                    t1, t2 = a["team1"], a["team2"]
                    diff = abs(a["diff"])
                    leader = t1 if t1["current_points"] > t2["current_points"] else t2

                    lines.append(f"⚔️ *{t1['team_name']}* ({t1['current_points']:.2f})  x  ({t2['current_points']:.2f}) *{t2['team_name']}*")
                    lines.append(f"   ↳ Status: 🟢 *EM ABERTO* | Vantagem: *{leader['team_name']}* (+{diff:.2f} pts)")
                    lines.append(f"   ↳ Probabilidades: *{t1['team_name']}* {a['win_prob_team1']}% x {a['win_prob_team2']}% *{t2['team_name']}*")

                    for t in [t1, t2]:
                        if t["pending"]:
                            sorted_p = sorted(t["pending"], key=lambda x: (x["points"], x["projected"]), reverse=True)
                            top3_names = [f"{p['name']} ({p['pos']} - {p['points']:.1f} pts)" for p in sorted_p[:3]]
                            total_cnt = len(sorted_p)
                            if total_cnt > 3:
                                lines.append(f"   ↳ Em campo ({t['team_name']}): {total_cnt} atletas ativos no total (Top 3: {', '.join(top3_names)} + {total_cnt - 3} outros)")
                            else:
                                lines.append(f"   ↳ Em campo ({t['team_name']} - {total_cnt} ativo{'s' if total_cnt > 1 else ''}): {', '.join(top3_names)}")
                        else:
                            lines.append(f"   ↳ Em campo ({t['team_name']}): 0 atletas restantes (todos já encerraram)")
                    lines.append("─" * 22)
                    lines.append("")

            if decided_items:
                lines.append("⚠️ *JOGOS PRATICAMENTE DECIDIDOS (SÓ O LÍDER PONTUANDO)*:")
                lines.append("")
                for item in decided_items:
                    a = item["analysis"]
                    t1, t2 = a["team1"], a["team2"]
                    diff = abs(a["diff"])
                    leader = t1 if t1["current_points"] > t2["current_points"] else t2
                    loser = t2 if t1["current_points"] > t2["current_points"] else t1

                    lines.append(f"⚔️ *{t1['team_name']}* ({t1['current_points']:.2f})  x  ({t2['current_points']:.2f}) *{t2['team_name']}*")
                    lines.append(f"   ↳ Status: ⚠️ *FATURA LIQUIDADA* | Vantagem: *{leader['team_name']}* (+{diff:.2f} pts)")
                    lines.append(f"   ↳ Probabilidades: *{t1['team_name']}* {a['win_prob_team1']}% x {a['win_prob_team2']}% *{t2['team_name']}*")
                    lines.append(f"   ↳ Em campo ({loser['team_name']}): 0 atletas restantes (já perdeu, sem chances de virada)")

                    sorted_p = sorted(leader["pending"], key=lambda x: (x["points"], x["projected"]), reverse=True)
                    top3_names = [f"{p['name']} ({p['pos']} - {p['points']:.1f} pts)" for p in sorted_p[:3]]
                    total_cnt = len(sorted_p)
                    if total_cnt > 3:
                        lines.append(f"   ↳ Em campo ({leader['team_name']}): {total_cnt} atletas somando pontos extras (Top 3: {', '.join(top3_names)} + {total_cnt - 3} outros)")
                    else:
                        lines.append(f"   ↳ Em campo ({leader['team_name']} - {total_cnt} ativo{'s' if total_cnt > 1 else ''}): {', '.join(top3_names)}")
                    lines.append("─" * 22)
                    lines.append("")
        else:
            lines.append("🟢 *JOGOS EM ABERTO*: Nenhum confronto em aberto no momento.")
            lines.append("")

        if closed_matchups:
            lines.append("🔴 *JOGOS ENCERRADOS (FINAL)*:")
            lines.append("")
            for m in closed_matchups:
                a = self.analyze_matchup(str(m["matchup_id"]), week)
                t1, t2 = a["team1"], a["team2"]
                diff = abs(a["diff"])
                winner = t1 if t1["current_points"] > t2["current_points"] else t2
                loser = t2 if t1["current_points"] > t2["current_points"] else t1

                lines.append(f"⚔️ *{t1['team_name']}* ({t1['current_points']:.2f})  x  ({t2['current_points']:.2f}) *{t2['team_name']}*")
                lines.append(f"   ↳ Status: 🔴 *ENCERRADO* | Vencedor: *{winner['team_name']}* (+{diff:.2f} pts sobre *{loser['team_name']}*)")

                sorted_w = sorted(winner["played"], key=lambda x: x["points"], reverse=True)
                top3_w = [f"{p['name']} ({p['points']:.1f} pts)" for p in sorted_w[:3]]
                sorted_l = sorted(loser["played"], key=lambda x: x["points"], reverse=True)
                top3_l = [f"{p['name']} ({p['points']:.1f} pts)" for p in sorted_l[:3]]

                lines.append(f"   ↳ Top 3 pontuadores ({winner['team_name']} - {len(sorted_w)} titulares): {', '.join(top3_w)}")
                lines.append(f"   ↳ Top 3 pontuadores ({loser['team_name']} - {len(sorted_l)} titulares): {', '.join(top3_l)}")
                lines.append("─" * 22)
                lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🎙️ _{_persona()} — Análise profissional & polêmica_")
        return "\n".join(lines)

    def analyze_matchup(self, team_query: str, week: Optional[int] = None) -> Dict[str, Any]:
        """Deep professional fantasy analysis of a specific matchup."""
        import math
        if week is None:
            week = self.get_nfl_state().get("week", 1)

        matchups = self.get_matchups(week)
        query = team_query.lower().strip()

        # Find target matchup
        target_m = None
        if query.isdigit():
            for m in matchups:
                if str(m.get("matchup_id")) == query:
                    target_m = m
                    break

        if not target_m:
            import people
            rid = people.resolve(team_query)
            if rid is not None:
                for m in matchups:
                    rids = {m["team1"].get("roster_id"), (m.get("team2") or {}).get("roster_id")}
                    if rid in rids:
                        target_m = m
                        break

        if not target_m and len(query) >= 4:
            for m in matchups:
                t1_name = m["team1"]["team_name"].lower()
                t1_owner = m["team1"]["owner_name"].lower()
                t2_name = (m.get("team2") or {}).get("team_name", "").lower()
                t2_owner = (m.get("team2") or {}).get("owner_name", "").lower()

                if (query in t1_name or query in t1_owner or
                    query in t2_name or query in t2_owner or
                    query == str(m["team1"].get("roster_id")) or
                    (m.get("team2") and query == str(m["team2"].get("roster_id")))):
                    target_m = m
                    break

        if not target_m:
            return {"error": f"Nenhum confronto encontrado para '{team_query}' na semana {week}."}

        # Load player DB
        players_db = self.get_players_db()

        season = self.get_nfl_state().get("season", "2026")
        cache_key = f"{season}_{week}"
        if cache_key in self._stats_cache:
            stats = self._stats_cache[cache_key]
        else:
            stats = self._get(f"stats/nfl/regular/{season}/{week}") or {}
            self._stats_cache[cache_key] = stats

        if cache_key in self._proj_cache:
            proj = self._proj_cache[cache_key]
        else:
            proj = self._get(f"projections/nfl/regular/{season}/{week}") or {}
            self._proj_cache[cache_key] = proj
        nfl_statuses = self.get_nfl_game_statuses(week, season)
        espn_ok = getattr(self, "_espn_ok", False)

        def process_team(team_dict):
            if not team_dict:
                return None
            starters = team_dict.get("starters", [])
            points_list = team_dict.get("starters_points", [])
            played = []
            pending = []

            for i, pid in enumerate(starters):
                pts = points_list[i] if i < len(points_list) else 0.0
                pts = float(pts or 0.0)
                p_info = players_db.get(pid, {})
                name = p_info.get("name") or f"Player {pid}"
                pos = p_info.get("pos") or "FLEX"
                team = p_info.get("team") or "NFL"

                p_proj_data = proj.get(pid, {})
                projected_pts = float(p_proj_data.get("pts_ppr") or p_proj_data.get("pts_half_ppr") or 0.0)

                g_info = nfl_statuses.get(team, {})
                g_status = g_info.get("status")
                if g_info:
                    opponent = g_info.get("away") if g_info.get("home") == team else g_info.get("home")
                else:
                    opponent = "bye" if espn_ok else "?"

                if not g_status:
                    if espn_ok or pts > 0.0:
                        g_status = "complete"  # ESPN up + team not playing = bye week
                    else:
                        g_status = "pre_game"  # ESPN down: unknown, keep it pending

                is_live = (g_status == "in_game")
                is_pre = (g_status == "pre_game")
                has_completed = (g_status == "complete")

                if is_live:
                    est_remaining = max(0.0, round((projected_pts - pts) if projected_pts > pts else projected_pts * 0.25, 2))
                    status_desc = f"AO VIVO ({g_info.get('away', '')} @ {g_info.get('home', '')})"
                elif is_pre:
                    est_remaining = projected_pts
                    status_desc = f"PENDENTE ({g_info.get('away', '')} @ {g_info.get('home', '')})"
                else:
                    est_remaining = 0.0
                    status_desc = "ENCERRADO"

                player_obj = {
                    "player_id": pid,
                    "name": name,
                    "pos": pos,
                    "team": team,
                    "points": pts,
                    "projected": projected_pts,
                    "est_remaining": est_remaining,
                    "opponent": opponent,
                    "game_status": g_status,
                    "status_desc": status_desc,
                    "is_live": is_live,
                    "is_pre": is_pre,
                    "has_completed": has_completed,
                }

                if has_completed:
                    played.append(player_obj)
                else:
                    pending.append(player_obj)

            rem_proj = sum(p["est_remaining"] for p in pending)
            current_pts = float(team_dict.get("points", 0.0))
            expected_total = round(current_pts + rem_proj, 2)
            return {
                "team_name": team_dict["team_name"],
                "owner_name": team_dict["owner_name"],
                "roster_id": team_dict["roster_id"],
                "current_points": current_pts,
                "played": played,
                "pending": pending,
                "remaining_projection": round(rem_proj, 2),
                "expected_total": expected_total,
            }

        t1 = process_team(target_m["team1"])
        t2 = process_team(target_m.get("team2"))

        if not t2:
            return {
                "week": week,
                "team1": t1,
                "team2": None,
                "status": "BYE",
                "win_prob_team1": 100.0,
                "win_prob_team2": 0.0,
            }

        # Win probability estimation
        is_open = len(t1["pending"]) > 0 or len(t2["pending"]) > 0
        diff = round(t1["current_points"] - t2["current_points"], 2)

        if not is_open:
            prob1 = 100.0 if t1["current_points"] > t2["current_points"] else (0.0 if t1["current_points"] < t2["current_points"] else 50.0)
            prob2 = round(100.0 - prob1, 1)
        else:
            net_lead = t1["expected_total"] - t2["expected_total"]
            total_pending = len(t1["pending"]) + len(t2["pending"])
            variance = 7.5 * math.sqrt(max(1, total_pending))
            z = net_lead / max(1.0, variance)
            prob1 = round((1.0 / (1.0 + math.exp(-z * 1.6))) * 100.0, 1)
            prob1 = max(1.0, min(99.0, prob1))
            prob2 = round(100.0 - prob1, 1)

        return {
            "week": week,
            "matchup_id": target_m["matchup_id"],
            "is_open": is_open,
            "status": "EM ABERTO" if is_open else "ENCERRADO",
            "team1": t1,
            "team2": t2,
            "diff": diff,
            "win_prob_team1": prob1,
            "win_prob_team2": prob2,
        }

    def format_matchup_analysis_whatsapp(self, team_query: str, week: Optional[int] = None) -> str:
        """Render a deep, professional and spicy breakdown for WhatsApp."""
        analysis = self.analyze_matchup(team_query, week)
        if "error" in analysis:
            return f"❌ {analysis['error']}"

        t1, t2 = analysis["team1"], analysis["team2"]
        w = analysis["week"]
        status_icon = "🟢" if analysis["is_open"] else "🔴"

        lines = [
            f"🏈 *ANÁLISE PROFISSIONAL DO CONFRONTO - SEMANA {w}*",
            "━━━━━━━━━━━━━━━━━━━━━━",
            f"⚔️ *{t1['team_name']}* vs *{t2['team_name']}*",
            f"Status: {status_icon} *{analysis['status']}*",
            "",
            "📊 *PLACAR & PROBABILIDADES*:",
            f"• *{t1['team_name']}*: *{t1['current_points']:.2f} pts* | Proj Final: ~{t1['expected_total']:.2f} pts | Chance: *{analysis['win_prob_team1']}%*",
            f"• *{t2['team_name']}*: *{t2['current_points']:.2f} pts* | Proj Final: ~{t2['expected_total']:.2f} pts | Chance: *{analysis['win_prob_team2']}%*",
        ]

        leading_team = t1["team_name"] if t1["current_points"] > t2["current_points"] else t2["team_name"]
        margin = abs(analysis["diff"])
        lines.append(f"• Vantagem atual: *{leading_team}* (+{margin:.2f} pts)")
        lines.append("")

        lines.append("⏳ *JOGADORES EM CAMPO / PENDENTES*:")
        for t in [t1, t2]:
            if t["pending"]:
                lines.append(f"• *{t['team_name']}* ({len(t['pending'])} atletas ativos, ~{t['remaining_projection']:.2f} pts projetados restantes):")
                for p in t["pending"]:
                    if p["is_live"]:
                        lines.append(f"   ↳ 🔴 *AO VIVO*: {p['name']} ({p['pos']} - {p['team']}) vs {p['opponent']} | Parcial: *{p['points']:.2f} pts* | Proj Restante: ~{p['est_remaining']:.2f} pts")
                    elif p["is_pre"]:
                        lines.append(f"   ↳ ⏳ *PENDENTE*: {p['name']} ({p['pos']} - {p['team']}) vs {p['opponent']} | Proj: *{p['projected']:.2f} pts*")
            else:
                lines.append(f"• *{t['team_name']}*: Todos os titulares já encerraram suas partidas.")
        lines.append("")

        trailing_team = t2 if t1["current_points"] > t2["current_points"] else t1
        leader_team = t1 if t1["current_points"] > t2["current_points"] else t2

        if analysis["is_open"]:
            lines.append("🎯 *CENÁRIO DA DISPUTA*:")
            if trailing_team["pending"]:
                needed = margin + (leader_team["remaining_projection"] if leader_team["pending"] else 0.0) + 0.01
                live_names = ", ".join(p["name"] for p in trailing_team["pending"])
                lines.append(f"• *{trailing_team['team_name']}* está perdendo por *{margin:.2f} pts*, mas conta com *{live_names}* em campo para buscar a virada!")
                lines.append(f"• Para vencer, precisa somar pelo menos *{needed:.2f} pontos* a mais do que o {leader_team['team_name']} produzir até o apito final.")
            else:
                lines.append(f"• *{trailing_team['team_name']}* já não tem mais jogadores em campo e está de mãos atadas, torcendo para *{leader_team['team_name']}* perder pontos ou cometer fumbles!")
        else:
            lines.append("🏁 *VEREDITO FINAL*:")
            lines.append(f"• Jogo encerrado! *{leader_team['team_name']}* garantiu a vitória por {margin:.2f} pontos de vantagem.")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🎙️ _Análise técnica & ácida por {_persona()}_")
        return "\n".join(lines)

    def get_draft_history(self, team_query: Optional[str] = None, season: Optional[str] = None) -> Dict[str, Any]:
        """Fetch historical draft picks of every season on record, optionally filtered by team/owner."""
        draft_file = league_ctx.current().source_path("draft_history.json")
        all_drafts = {}
        if os.path.exists(draft_file):
            try:
                with open(draft_file, "r", encoding="utf-8") as f:
                    all_drafts = json.load(f)
            except Exception:
                pass

        if not all_drafts:
            return {"error": "Histórico de drafts não disponível no momento."}

        target_user_id = None
        target_name = team_query or "Todos os Times"
        if team_query:
            query = team_query.lower().strip()
            users = self.get_users()
            for uid, u in users.items():
                disp = u.get("display_name", "").lower()
                tname = u.get("team_name", "").lower()
                if query in disp or query in tname:
                    target_user_id = uid
                    target_name = u.get("team_name") or u.get("display_name")
                    break

        result = {
            "target_team": target_name,
            "target_user_id": target_user_id,
            "seasons": {}
        }

        seasons_to_check = [str(season)] if season and str(season) in all_drafts else \
            sorted(k for k in all_drafts if str(k).isdigit())
        for s in seasons_to_check:
            if s not in all_drafts:
                continue
            d_info = all_drafts[s]
            picks = d_info.get("picks", [])
            if target_user_id:
                picks = [p for p in picks if p.get("picked_by") == target_user_id]
            result["seasons"][s] = {
                "season": s,
                "draft_name": d_info.get("draft_name"),
                "draft_id": d_info.get("draft_id"),
                "total_picks": len(picks),
                "picks": picks
            }
        return result

    def format_draft_history_whatsapp(self, team_query: str, season: Optional[str] = None) -> str:
        """Format a rich WhatsApp report of historical draft picks."""
        history = self.get_draft_history(team_query, season)
        if "error" in history:
            return f"❌ {history['error']}"

        team_name = history["target_team"]
        lines = [
            f"📋 *HISTÓRICO COMPLETO DE DRAFTS — {team_name.upper()}*",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        seasons = history.get("seasons", {})
        if not seasons:
            lines.append("Nenhum registro de draft encontrado para este time.")
            return "\n".join(lines)

        for s, d in seasons.items():
            picks = d.get("picks", [])
            dname = d.get("draft_name", f"Draft {s}")
            lines.append(f"🏈 *{s} — {dname}* ({len(picks)} escolhas):")
            if not picks:
                lines.append("  ↳ Nenhuma escolha registrada neste draft.")
            else:
                for p in picks:
                    m = p.get("metadata") or {}
                    name = f"{m.get('first_name', '')} {m.get('last_name', '')}".strip() or "Desconhecido"
                    pos = m.get("position", "N/A")
                    nfl_team = m.get("team") or "FA"
                    lines.append(f"  • R{p['round']} (Pick #{p['pick_no']}): *{name}* ({pos} - {nfl_team})")
            lines.append("")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🎙️ _Análise histórica & técnica por {_persona()}_")
        return "\n".join(lines)

    def get_franchise_history(self, team_query: str) -> Dict[str, Any]:
        """Fetch franchise standings, record, and points across every season on record."""
        history_file = league_ctx.current().source_path("seasons_history.json")
        history_data = {}
        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    history_data = json.load(f)
            except Exception:
                pass

        query = team_query.lower().strip()
        users = self.get_users()
        target_uid = None
        target_tname = team_query
        target_owner = team_query
        for uid, u in users.items():
            disp = u.get("display_name", "").lower()
            tname = u.get("team_name", "").lower()
            if query in disp or query in tname:
                target_uid = uid
                target_tname = u.get("team_name") or u.get("display_name")
                target_owner = u.get("display_name")
                break

        seasons_result = []
        tot_wins = 0
        tot_losses = 0
        tot_ties = 0
        tot_pts = 0.0

        current_season = str(self.get_league().get("season") or "")
        for s in sorted(k for k in history_data if str(k).isdigit()):
            s_data = history_data.get(s, {})
            standings = s_data.get("standings", [])
            team_entry = None
            for t in standings:
                if target_uid and t.get("owner_id") == target_uid:
                    team_entry = t
                    break
                if query in t.get("team_name", "").lower() or query in t.get("owner_name", "").lower():
                    team_entry = t
                    break
            if team_entry:
                seasons_result.append({
                    "season": s,
                    "rank": team_entry.get("rank"),
                    "wins": team_entry.get("wins"),
                    "losses": team_entry.get("losses"),
                    "ties": team_entry.get("ties"),
                    "pts_for": team_entry.get("pts_for"),
                    "team_name": team_entry.get("team_name"),
                    "owner_name": team_entry.get("owner_name"),
                })
                if s != current_season:   # the season in progress is not part of the career total
                    tot_wins += team_entry.get("wins", 0)
                    tot_losses += team_entry.get("losses", 0)
                    tot_ties += team_entry.get("ties", 0)
                    tot_pts += team_entry.get("pts_for", 0.0)

        return {
            "team_name": target_tname,
            "owner_name": target_owner,
            "user_id": target_uid,
            "seasons": seasons_result,
            "current_season": current_season,
            "career_totals": {
                "wins": tot_wins,
                "losses": tot_losses,
                "ties": tot_ties,
                "pts_for": round(tot_pts, 2),
            }
        }

    def format_franchise_history_whatsapp(self, team_query: str) -> str:
        """Format a rich WhatsApp report of a team's entire league history."""
        data = self.get_franchise_history(team_query)
        seasons = data.get("seasons", [])
        if not seasons:
            return f"❌ Nenhum histórico encontrado para '{team_query}'."

        tname = data["team_name"]
        owner = data["owner_name"]
        career = data["career_totals"]
        record = f"{career['wins']}-{career['losses']}"
        if career["ties"] > 0:
            record += f"-{career['ties']}"

        current = str(data.get("current_season") or "")
        done = sorted(s["season"] for s in seasons if s["season"] != current)
        span = (f" ({done[0]}-{done[-1]})" if len(done) > 1 else f" ({done[0]})" if done else "")
        lines = [
            f"🏛️ *HISTÓRICO DA FRANQUIA — {tname.upper()}*",
            f"Manager: *{owner}*",
            f"Record Histórico{span}: *{record}* | *{career['pts_for']:.2f} pts*",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        for s in seasons:
            rec = f"{s['wins']}-{s['losses']}"
            if s["ties"] > 0:
                rec += f"-{s['ties']}"
            status_str = f"{s['rank']}º lugar" if s["season"] != current else "Em andamento"
            lines.append(f"• *Temporada {s['season']}*: *{rec}* | *{status_str}* | *{s['pts_for']:.2f} pts*")

        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🎙️ _{league_ctx.current().persona_name} — Retrospectiva oficial_")
        return "\n".join(lines)


if __name__ == "__main__":
    client = SleeperClient()
    action = sys.argv[1] if len(sys.argv) > 1 else "standings"

    def _default_team() -> str:
        """First team of the league's table (CLI default when no team is given)."""
        return str((client.get_standings() or [{}])[0].get("team_name") or "")

    if action == "standings":
        print(client.format_standings_whatsapp())
    elif action == "matchups":
        week = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(client.format_matchups_whatsapp(week))
    elif action == "matchup_analysis":
        team = sys.argv[2] if len(sys.argv) > 2 else _default_team()
        week = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(client.format_matchup_analysis_whatsapp(team, week))
    elif action == "draft_history":
        team = sys.argv[2] if len(sys.argv) > 2 else _default_team()
        season = sys.argv[3] if len(sys.argv) > 3 else None
        print(client.format_draft_history_whatsapp(team, season))
    elif action == "franchise_history":
        team = sys.argv[2] if len(sys.argv) > 2 else _default_team()
        print(client.format_franchise_history_whatsapp(team))
    elif action == "json_standings":
        print(json.dumps(client.get_standings(), indent=2))
    elif action == "json_matchups":
        week = int(sys.argv[2]) if len(sys.argv) > 2 else None
        print(json.dumps(client.get_matchups(week), indent=2))
    else:
        print(f"Unknown action: {action}. Use standings, matchups, matchup_analysis, draft_history, franchise_history, json_standings, json_matchups.")
