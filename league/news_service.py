#!/usr/bin/env python3
"""News & Injury Scraper Service for NFL & Fantasy Football.

Collects breaking news and injury reports from multiple trusted sources:
- ESPN NFL News API
- Rotowire NFL News RSS
- FantasyPros NFL News RSS
- Sleeper Player Updates

Matches reported players directly with the rosters of the current league (league_ctx).
Identifies whether the player belongs to a league team, who is the owner,
and whether the player is currently slated as a STARTER.
"""

import json
import logging
import os
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import league_ctx
import league_profile

# Set global socket timeout to prevent any network hangs
socket.setdefaulttimeout(4)

logger = logging.getLogger("NFLNewsService")
BRT = ZoneInfo("America/Sao_Paulo")   # default zone; the league's own comes from league.yaml (self.tz)

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_cache.json")   # NFL-level: shared
NEWS_MAX_AGE_H = 96.0  # older articles never drive an injury verdict


def parse_published(value: Any) -> Optional[datetime]:
    """ESPN ISO ('2026-10-04T13:00:02Z') or RSS RFC-822 dates → aware datetime (None if unparseable)."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:  # Rotowire: 'Thu, 01 Oct 2026 10:02:00 AM PDT'
        base = datetime.strptime(" ".join(s.split()[:6]), "%a, %d %b %Y %I:%M:%S %p")
        offset = {"PDT": -7, "PST": -8, "EDT": -4, "EST": -5}.get(s.split()[-1], 0)
        from datetime import timedelta
        return base.replace(tzinfo=timezone(timedelta(hours=offset)))
    except Exception:
        return None


IN_GAME_EXIT = ("left the game", "left sunday", "left monday", "left thursday", "carted off", "did not return",
                "exited", "questionable to return", "doubtful to return", "ruled out for the rest", "helped off",
                "evaluated for a concussion", "evaluated for concussion", "went to the locker room")


def is_recent(item: Dict[str, Any], max_age_h: float = NEWS_MAX_AGE_H) -> bool:
    dt = parse_published(item.get("published_at"))
    if dt is None:
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_h * 3600.0



def _jev_article_signals(player: str, team: str, articles: List[Dict[str, Any]]) -> Optional[List[Dict[str, float]]]:
    """Jev (TypeSafe via OpenRouter): per article → about / injured / cleared probabilities. None = unavailable."""
    if not articles:
        return []
    try:
        import jev
    except Exception:
        return None
    if not jev.available():
        return None
    arts = articles[:6]
    state = {"player": f"{player} ({team})",
             "articles": [{"title": a.get("title", ""), "summary": (a.get("summary") or "")[:400]} for a in arts]}
    qs = {}
    for i in range(len(arts)):
        qs[f"about{i}"] = {"type": "noul", "instructions": f"Is `articles[{i}]` about `player` specifically (not another person with a similar name)?"}
        qs[f"hurt{i}"] = {"type": "noul", "instructions": f"Does `articles[{i}]` report that `player` got injured, left a game hurt, is limited/out/doubtful because of an injury, or needs tests (MRI, concussion protocol)?"}
        qs[f"clear{i}"] = {"type": "noul", "instructions": f"Does `articles[{i}]` report that `player` is healthy, cleared, practicing fully or expected to play?"}
    ans = jev.decide(state, qs, cache=True)
    if not ans:
        return None
    return [{"about": jev.noul(ans, f"about{i}"), "hurt": jev.noul(ans, f"hurt{i}"),
             "clear": jev.noul(ans, f"clear{i}")} for i in range(len(arts))]

class NewsService:
    def __init__(self, league_id: Optional[str] = None):
        self.ctx = league_ctx.current()
        self.league_id = league_id or self.ctx.league_id
        self.tz = ZoneInfo(self.ctx.timezone)
        self.base_dir = os.path.dirname(os.path.abspath(__file__))   # shared (NFL-level) files
        self._players_db = None
        self._players_registry = None
        self._master_players = None

    @property
    def players_db(self) -> Dict[str, Any]:
        """Merged player DB; team and injury status come from the live Sleeper master (players_db.py)."""
        if self._players_db is None:
            import players_db
            self._players_db = players_db.load()
        return self._players_db

    @property
    def players_registry(self) -> Dict[str, Any]:
        if self._players_registry is None:
            path = self.ctx.source_path("players_registry.json")   # league-scoped
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._players_registry = json.load(f)
            except Exception:
                self._players_registry = {}
        return self._players_registry

    @property
    def master_players(self) -> Dict[str, Any]:
        if self._master_players is None:
            self._master_players = self._load_json("master_nfl_players.json")
        return self._master_players

    def _load_json(self, filename: str) -> Dict[str, Any]:
        filepath = os.path.join(self.base_dir, filename)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Erro ao carregar {filename}: {e}")
        return {}

    def get_player_info(self, player_id: str) -> Dict[str, Any]:
        """Resolve player info from league DB or NFL master DB."""
        pid = str(player_id)
        if pid in self.players_db:
            return self.players_db[pid]
        if pid in self.master_players:
            return self.master_players[pid]
        # Defesas abreviadas (ex: TB, SF, KC, PHI)
        nfl_defs = {
            "TB": "Tampa Bay Buccaneers", "SF": "San Francisco 49ers", "KC": "Kansas City Chiefs",
            "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills", "DAL": "Dallas Cowboys",
            "PHI": "Philadelphia Eagles", "DET": "Detroit Lions", "MIA": "Miami Dolphins",
            "PIT": "Pittsburgh Steelers", "CLE": "Cleveland Browns", "NYJ": "New York Jets",
            "CIN": "Cincinnati Bengals", "HOU": "Houston Texans", "GB": "Green Bay Packers",
            "MIN": "Minnesota Vikings", "CHI": "Chicago Bears", "DEN": "Denver Broncos",
            "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
            "SEA": "Seattle Seahawks", "ARI": "Arizona Cardinals", "NO": "New Orleans Saints",
            "ATL": "Atlanta Falcons", "CAR": "Carolina Panthers", "IND": "Indianapolis Colts",
            "JAX": "Jacksonville Jaguars", "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
            "NYG": "New York Giants", "NE": "New England Patriots"
        }
        if pid in nfl_defs:
            return {"name": nfl_defs[pid], "pos": "DEF", "team": pid}
        return {"name": f"Jogador {pid}", "pos": "FLEX", "team": "NFL"}

    def fetch_espn_news(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch latest NFL news from ESPN Public API."""
        url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "curl/7.81.0",
                    "Accept": "application/json, text/plain, */*",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                articles = data.get("articles", [])
                news_items = []
                for a in articles[:limit]:
                    title = a.get("headline", "")
                    description = a.get("description", "")
                    published = a.get("published", "")
                    links = a.get("links", {}).get("web", {})
                    link = links.get("href", "")
                    category = self._detect_category(f"{title} {description}")

                    news_items.append({
                        "source": "ESPN",
                        "title": title,
                        "summary": description,
                        "link": link,
                        "category": category,
                        "published_at": published,
                    })
                return news_items
        except Exception as e:
            logger.warning(f"Falha ao buscar notícias da ESPN: {e}")
            return []

    def fetch_rss_feed(self, url: str, source_name: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch general NFL RSS feed."""
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                xml_data = response.read()
                root = ET.fromstring(xml_data)
                channel = root.find("channel")
                news_items = []
                if channel is not None:
                    for item in channel.findall("item")[:limit]:
                        title = item.findtext("title", "")
                        desc = item.findtext("description", "")
                        link = item.findtext("link", "")
                        pub_date = item.findtext("pubDate", "")
                        clean_desc = re.sub(r'<[^>]+>', '', desc).strip()
                        category = self._detect_category(f"{title} {clean_desc}")

                        news_items.append({
                            "source": source_name,
                            "title": title,
                            "summary": clean_desc[:250],
                            "link": link,
                            "category": category,
                            "published_at": pub_date,
                        })
                return news_items
        except Exception as e:
            logger.warning(f"Falha ao buscar RSS de {source_name}: {e}")
            return []

    def _detect_category(self, text: str) -> str:
        lower = text.lower()
        if any(k in lower for k in ["injur", "questionable", "doubtful", "out for", "ir ", "injured reserve", "concussion", "ankle", "knee", "hamstring", "surgery", "dnp", "inactive", "did not practice", "pup"]):
            return "injury"
        if any(k in lower for k in ["waiver", "faab", "claim", "pickup"]):
            return "waiver"
        if any(k in lower for k in ["trade", "traded", "swap", "deal"]):
            return "trade"
        return "general"

    def sync_news(self) -> List[Dict[str, Any]]:
        """Fetches from all sources and merges into cache."""
        all_news = []
        all_news.extend(self.fetch_espn_news(limit=25))
        all_news.extend(self.fetch_rss_feed("https://www.rotowire.com/rss/news.php?sport=NFL", "Rotowire", limit=20))
        all_news.extend(self.fetch_rss_feed("https://www.fantasypros.com/feed/", "FantasyPros", limit=15))
        all_news.extend(self.fetch_rss_feed("https://sports.yahoo.com/nfl/rss/", "Yahoo NFL", limit=25))
        all_news.extend(self.fetch_rss_feed("https://www.cbssports.com/rss/headlines/nfl/", "CBS Sports", limit=20))

        # Merge with existing cache to avoid losing recent items
        existing = []
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        seen_titles = set()
        merged = []
        for item in all_news + existing:
            t = item.get("title", "").strip().lower()
            if t and t not in seen_titles:
                seen_titles.add(t)
                merged.append(item)

        self._save_to_cache(merged)
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        merged.sort(key=lambda n: parse_published(n.get("published_at")) or epoch, reverse=True)
        return merged[:150]

    def _save_to_cache(self, items: List[Dict[str, Any]]):
        """Helper to safely save news items to cache file."""
        existing = []
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        seen = set()
        merged = []
        for item in items + existing:
            t = item.get("title", "").strip().lower()
            if t and t not in seen:
                seen.add(t)
                merged.append(item)

        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        merged.sort(key=lambda n: parse_published(n.get("published_at")) or epoch, reverse=True)
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(merged[:150], f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Erro ao salvar news cache: {e}")

    def search_player_news(self, player_name: str, trigger_sync: bool = False) -> List[Dict[str, Any]]:
        """Search targeted live news for a specific player across cache, ESPN, Rotowire, Yahoo, and sports feeds."""
        p_clean = player_name.strip()
        p_lower = p_clean.lower()
        last_name = p_lower.split()[-1] if p_lower else ""

        results = []
        seen_titles = set()

        def scan_items(items):
            matched = []
            for n in items:
                t = (n.get("title") or "").lower()
                s = (n.get("summary") or "").lower()
                full_text = f"{t} {s}"
                # Full name only: "Allen" alone would mix Josh Allen and Keenan Allen.
                if p_lower and p_lower in full_text:
                    norm_t = n.get("title", "").strip().lower()
                    if norm_t not in seen_titles:
                        seen_titles.add(norm_t)
                        matched.append(n)
            return matched

        # 1. Search in existing cache
        all_news = self.get_latest_news(limit=100)
        results.extend(scan_items(all_news))

        # 2. If no news found or explicitly requested, trigger quick sync
        if not results and trigger_sync:
            fresh_news = self.sync_news()
            results.extend(scan_items(fresh_news))

        return results

    def get_latest_news(self, category: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Retrieve news from cache or fetch live."""
        news_list = []
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    news_list = json.load(f)
            except Exception:
                news_list = []

        if not news_list:
            news_list = self.sync_news()

        if category:
            news_list = [n for n in news_list if n.get("category") == category]

        return news_list[:limit]

    def match_news_to_league_players(self, news_items: List[Dict[str, Any]], rosters_data: Optional[List[Dict[str, Any]]] = None, users_data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Cross-references news with the current league's players."""
        from sleeper_client import SleeperClient
        if rosters_data is None or users_data is None:
            client = SleeperClient(league_id=self.league_id)
            rosters_data = client.get_rosters()
            users_data = client.get_users()

        # Build player -> (manager_name, team_name, is_starter) mapping
        player_ownership = {}
        for r in rosters_data:
            oid = r.get("owner_id")
            user = users_data.get(oid, {})
            manager_name = user.get("display_name") or f"Owner {r.get('roster_id')}"
            team_name = user.get("team_name") or f"Time {r.get('roster_id')}"
            starters_set = set(r.get("starters") or [])
            all_players = r.get("players") or []

            for pid in all_players:
                player_ownership[pid] = {
                    "manager": manager_name,
                    "team": team_name,
                    "is_starter": pid in starters_set,
                    "roster_id": r.get("roster_id"),
                }

        matched_news = []
        for n in news_items:
            full_text = f"{n.get('title', '')} {n.get('summary', '')}".lower()
            detected_players = []

            # Check league players
            for pid, p_info in self.players_db.items():
                p_name = p_info.get("name", "")
                if len(p_name) > 4 and p_name.lower() in full_text:
                    owner_info = player_ownership.get(pid)
                    detected_players.append({
                        "player_id": pid,
                        "name": p_name,
                        "pos": p_info.get("pos"),
                        "nfl_team": p_info.get("team"),
                        "manager": owner_info["manager"] if owner_info else "Livre",
                        "team_name": owner_info["team"] if owner_info else "Free Agent",
                        "is_starter": owner_info["is_starter"] if owner_info else False,
                    })

            if detected_players:
                item_copy = dict(n)
                item_copy["players"] = detected_players
                matched_news.append(item_copy)

        return matched_news

    def classify_injury(self, p_info: Dict[str, Any], news_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Classifies player injury severity, practice status, in-game risk, return-to-play, and news forecast."""
        import players_db
        official_status_raw = players_db.injury(p_info) or "Active"
        official_status = official_status_raw
        body_part = p_info.get("injury_body_part") or "Nenhum"
        notes = p_info.get("injury_notes") or ""

        stat_upper = official_status_raw.upper().strip()

        # Severity Tier:
        # Tier 1: GTD / Questionable / Doubtful (Decisão semana a semana, monitoramento ativo)
        # Tier 2: Out (Confirmado fora da rodada)
        # Tier 3: IR / PUP / NFI / Sus (Longa duração, fora da temporada)
        if stat_upper in ["QUESTIONABLE", "DOUBTFUL", "GTD"]:
            tier = 1
            tier_label = "GTD / Dúvida Semanal"
        elif stat_upper in ["OUT", "DNP"]:
            tier = 2
            tier_label = "Desfalque Confirmado da Semana"
        elif stat_upper in ["IR", "PUP", "NFI", "OUT FOR SEASON", "SUS", "SUSPENDED", "DNR"]:
            tier = 3
            tier_label = "Longa Duração / IR"
        elif official_status not in ["Active", "None", "Healthy", ""]:
            tier = 2
            tier_label = f"Restrição ({official_status})"
        else:
            tier = 0
            tier_label = "Saudável"

        base_tier = tier
        p_name = (p_info.get("name") or f"{p_info.get('first_name', '')} {p_info.get('last_name', '')}").strip().lower()
        last_name = (p_info.get("last_name") or (p_name.split()[-1] if p_name else "")).strip().lower()

        matched_articles = []
        in_game_injury = False
        in_game_detail = ""
        latest_headline = ""
        latest_date = ""

        # Comprehensive injury keywords (English and Portuguese)
        injury_keywords = [
            "injured", "injury", "injuries", "hurt", "sofre lesão", "sofreu lesão", "lesão", "lesionado",
            "machucou", "perna falhou", "perna", "joelho", "tornozelo", "left game", "ruled out",
            "questionable to return", "helped off", "carted off", "carted to", "evaluated for concussion",
            "concussion protocol", "limped to locker room", "went to locker room", "head to locker room",
            "exited sunday", "exited monday", "exited thursday", "exited the game", "exited game",
            "exiting sunday", "exiting monday", "exiting thursday", "exiting the game", "exiting game",
            "suffered an injury in", "suffered injury in", "did not return", "left sunday's", "left monday's",
            "left thursday's", "ankle injury", "knee injury", "hamstring injury", "groin injury",
            "undergo an mri", "undergo mri", "mri on monday", "mri on tuesday", "mri", "ressonância",
            "injured in sunday", "injured in monday", "injured in thursday", "left the game",
            "left with an injury", "left with a", "injured during", "leaving", "leaves", "exits",
            "limping", "limped", "sprain", "strain", "sidelined", "cirurgia", "surgery", "dnp",
            "fio de presunto", "fisgada no presunto", "presunto"
        ]

        return_keywords = [
            "returns", "return", "returned", "returning", "cleared", "cleared protocol",
            "cleared concussion protocol", "full practice", "full participant", "practiced in full",
            "expected to play", "expected to start", "will play", "good to go", "set to start",
            "back on track", "activated", "off injury report", "removed from injury report",
            "healthy", "upgraded", "ready to go", "vai pro jogo", "liberado", "treinou normalmente",
            "starts", "starting"
        ]

        for n in news_items:
            title = (n.get("title") or "").lower()
            summary = (n.get("summary") or "").lower()
            text = f"{title} {summary}"

            is_match = bool(p_name and p_name in text and is_recent(n))

            if is_match:
                matched_articles.append(n)
                if not latest_headline:
                    latest_headline = n.get("title", "")
                    latest_date = n.get("published_at", "")

        is_cleared = False
        return_headline = ""
        sleeper_outdated = False

        # Jev judgment replaces the keyword heuristics when available (falls back automatically).
        jev_sig = _jev_article_signals(p_info.get("name") or p_name, p_info.get("team") or "", matched_articles)
        if jev_sig is not None:
            pairs = [(a, sg) for a, sg in zip(matched_articles[:len(jev_sig)], jev_sig) if sg["about"] >= 0.5]
            matched_articles = [a for a, _ in pairs]
            for a, sg in pairs:  # newest first: the latest decisive article wins
                if sg["clear"] >= 0.7 and sg["clear"] > sg["hurt"]:
                    is_cleared, return_headline = True, a.get("title") or a.get("summary", "")
                    break
                if sg["hurt"] >= 0.7 and (tier > 0 or is_recent(a, 24)):
                    in_game_injury, in_game_detail = True, a.get("title", "")
                    break

        # Avaliar se a notícia mais recente sinaliza retorno / liberação médica / vai para o jogo
        for n in (matched_articles if jev_sig is None else []):
            t = (n.get("title") or "").lower()
            s = (n.get("summary") or "").lower()
            comb = f"{t} {s}"

            has_return_keyword = (
                any(k in t for k in ["cleared", "good to go", "will play", "expected to play", "activated"]) or
                any(k in comb for k in [
                    "cleared protocol", "cleared concussion protocol", "full practice", "full participant",
                    "practiced in full", "expected to play", "expected to start", "back on track",
                    "will play", "off injury report", "removed from injury report", "vai pro jogo", "liberado"
                ])
            )
            # Evita falso positivo em notícias de "did not return to game"
            if "did not return" in comb or "ruled out" in t:
                has_return_keyword = False

            if has_return_keyword:
                is_cleared = True
                return_headline = n.get("title") or n.get("summary", "")
                break

        if is_cleared and tier > 0:
            # The official status is live (Sleeper /players/nfl), so it wins over a news headline: keep the
            # player in his tier and only show the positive news as context.
            is_cleared = False
            forecast = f"📰 Notícia positiva: {return_headline[:90]} — status oficial segue *{official_status_raw}*."
        elif is_cleared:
            in_game_injury = False
            forecast = f"🟢 Saudável e confirmado para jogar! [{return_headline}]"
        else:
            for n in matched_articles:
                t = (n.get("title") or "").lower()
                s = (n.get("summary") or "").lower()
                text = f"{t} {s}"
                jev_hit = jev_sig is not None and in_game_injury and n.get("title", "") == in_game_detail
                if tier == 0:
                    # Officially healthy: only a fresh in-game exit counts (the official list is live).
                    kw_hit = is_recent(n, 24) and any(kw in text for kw in IN_GAME_EXIT)
                else:
                    kw_hit = any(kw in text for kw in injury_keywords) or n.get("category") == "injury"
                if jev_hit or (jev_sig is None and kw_hit):
                    in_game_injury = True
                    if not in_game_detail:
                        in_game_detail = n.get("title", "")

                    if body_part in ["Nenhum", "", None]:
                        if any(k in text for k in ["knee", "joelho"]):
                            body_part = "Joelho (Knee)"
                        elif any(k in text for k in ["ankle", "tornozelo"]):
                            body_part = "Tornozelo (Ankle)"
                        elif any(k in text for k in ["hamstring", "coxa", "fio de presunto", "isquiotibiais", "presunto"]):
                            body_part = "Posterior de Coxa / Fio de Presunto (Hamstring)"
                        elif any(k in text for k in ["concussion", "concussão"]):
                            body_part = "Concussão Cerebral"
                        elif any(k in text for k in ["shoulder", "ombro"]):
                            body_part = "Ombro (Shoulder)"
                        elif any(k in text for k in ["perna", "leg", "foot", "pé"]):
                            body_part = "Perna / Pé (Leg/Foot)"

            if in_game_injury and base_tier == 0:
                if tier == 0:
                    tier = 1
                    tier_label = "⚠️ Alerta de Lesão em Jogo / Notícia"
                if official_status in ["Active", "Healthy", "None", ""]:
                    official_status = "Active (⚠️ Alerta de Lesão Recente em Notícia)"
                forecast = f"🚨 Machucou recentemente / saiu de campo! [{in_game_detail[:80]}...]"
            elif any("concussion" in (n.get("title", "") + n.get("summary", "")).lower() for n in matched_articles):
                forecast = "⚠️ Protocolo de concussão — liberação depende de avaliação médica independente."
                if tier == 0: tier = 1
            elif any("game-time decision" in (n.get("title", "") + n.get("summary", "")).lower() for n in matched_articles):
                forecast = "⚠️ Decisão no aquecimento (GTD) — prepare o substituto!"
            elif any("limited" in (n.get("title", "") + n.get("summary", "")).lower() for n in matched_articles):
                forecast = "📈 Treinou com limitações — tendência positiva com monitoramento de snaps."
            elif any("did not practice" in (n.get("title", "") + n.get("summary", "")).lower() for n in matched_articles):
                forecast = "📉 Não treinou nos últimos dias — alto risco de inatividade."
            elif stat_upper == "QUESTIONABLE":
                forecast = "⚠️ Decisão no aquecimento (GTD) — checar lista de inativos 90min antes."
            elif stat_upper == "DOUBTFUL":
                forecast = "❌ Dúvida grave — menos de 25% de chance histórica de atuar."
            elif stat_upper == "OUT":
                forecast = "❌ Confirmado fora desta rodada pela comissão técnica."
            elif stat_upper in ["IR", "PUP", "OUT FOR SEASON"]:
                forecast = "🏥 Lista de lesionados (IR) — fora de combate por múltiplas semanas."
            else:
                forecast = f"Status: {official_status}"
            if base_tier > 0 and in_game_detail:
                forecast = f"{forecast} 📰 {in_game_detail[:90]}"

        return {
            "tier": tier,
            "tier_label": tier_label,
            "official_status": official_status,
            "official_status_raw": official_status_raw,
            "body_part": body_part,
            "notes": notes,
            "forecast": forecast,
            "in_game_injury": in_game_injury,
            "in_game_detail": in_game_detail,
            "latest_headline": return_headline or latest_headline,
            "latest_date": latest_date,
            "matched_articles_count": len(matched_articles),
            "is_cleared": is_cleared,
            "sleeper_outdated": sleeper_outdated,
            "return_headline": return_headline,
        }

    def sync_injury_intelligence(self, week: Optional[int] = None, persist: bool = True) -> Dict[str, Any]:
        """Builds the structured injury picture of every roster (persisted only when `persist`)."""
        import players_db
        from sleeper_client import SleeperClient
        client = SleeperClient(league_id=self.league_id)
        rosters = client.get_rosters()
        users = client.get_users()
        news_items = self.sync_news()

        all_injured = []
        by_roster = {}
        slots = self.ctx.profile.starter_slots

        for r in rosters:
            rid = str(r.get("roster_id"))
            oid = r.get("owner_id")
            user = users.get(oid, {})
            m_name = user.get("display_name") or f"Manager {rid}"
            t_name = user.get("team_name") or f"Time {rid}"

            starters = set(r.get("starters") or [])
            # slot each starter occupies (Sleeper lists `starters` in roster_positions order) -> who can replace him
            slot_of = {pid: slots[i] for i, pid in enumerate(r.get("starters") or [])
                       if pid and pid != "0" and i < len(slots)}
            all_players = r.get("players") or []
            bench = [p for p in all_players if p not in starters]

            by_roster[rid] = []

            for pid in all_players:
                if not pid or pid == "0":
                    continue
                p_info = self.players_db.get(pid, {})
                inj = players_db.injury(p_info)
                is_starter = pid in starters

                # Se for titular e tiver status de lesão, fazer busca ativa e dirigida de notícias
                player_news = list(news_items)
                if is_starter and inj and inj not in ["Active", "None", "Healthy", ""]:
                    p_name = p_info.get("name") or pid
                    specific = self.search_player_news(p_name)
                    if specific:
                        seen_t = set()
                        merged_news = []
                        for item in specific + player_news:
                            t = (item.get("title") or "").strip().lower()
                            if t and t not in seen_t:
                                seen_t.add(t)
                                merged_news.append(item)
                        player_news = merged_news

                if not inj and is_starter:
                    full = (p_info.get("name") or "").lower()
                    if not full or not any(full in f"{n.get('title', '')} {n.get('summary', '')}".lower()
                                           for n in player_news if is_recent(n, 24)):
                        continue  # officially healthy and no fresh news about him: nothing to report
                if (inj and inj not in ["Active", "None", "Healthy", ""]) or is_starter:
                    c = self.classify_injury(p_info, player_news)
                    if c.get("tier", 0) > 0 or (inj and inj not in ["Active", "None", "Healthy", ""]):
                        pos = p_info.get("pos")
                        eligible = self.ctx.profile.slot_positions(slot_of[pid]) if pid in slot_of else \
                            frozenset({league_profile.normalize_pos(pos)})
                        bench_replacements = []
                        for b_pid in bench:
                            b_info = self.players_db.get(b_pid, {})
                            b_inj = players_db.injury(b_info)
                            if b_inj in ["Out", "IR", "Doubtful", "PUP", "Sus", "DNR", "NFI"]:
                                continue
                            b_pos = b_info.get("pos")
                            if b_pos == pos:
                                bench_replacements.insert(0, b_info.get("name") or b_pid)
                            elif league_profile.normalize_pos(b_pos) in eligible:   # the slot he vacates takes him too
                                bench_replacements.append(f"{b_info.get('name') or b_pid} ({b_pos})")

                        record = {
                            "player_id": pid,
                            "name": p_info.get("name") or f"Player {pid}",
                            "player_name": p_info.get("name") or f"Player {pid}",
                            "pos": pos,
                            "position": pos,
                            "team": p_info.get("team") or "NFL",
                            "roster_id": rid,
                            "manager": m_name,
                            "team_name": t_name,
                            "is_starter": is_starter,
                            "bench_replacements": bench_replacements[:3],
                            "injury_status": c.get("official_status_raw") or c.get("official_status"),
                            **c
                        }
                        all_injured.append(record)
                        by_roster[rid].append(record)

        data = {
            "last_updated": datetime.now(self.tz).isoformat(),
            "total_injured": len(all_injured),
            "by_roster": by_roster,
            "all_injured": all_injured
        }
        if not persist:
            return data

        # Save to JSON
        os.makedirs(self.ctx.kb_dir, exist_ok=True)
        kb_registry_file = self.ctx.kb_path("injuries_registry.json")
        for fpath in [kb_registry_file]:
            try:
                with open(fpath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Erro ao salvar {fpath}: {e}")

        # Write INJURY_REPORT.md
        self._write_injury_report_markdown(data)

        return data

    def _write_injury_report_markdown(self, data: Dict[str, Any]):
        """Writes structured INJURY_REPORT.md to knowledge base."""
        all_injured = data.get("all_injured", [])
        gtd_list = [p for p in all_injured if p.get("tier") == 1]
        out_list = [p for p in all_injured if p.get("tier") == 2]
        ir_list = [p for p in all_injured if p.get("tier") == 3]

        md_lines = [
            "# 🏥 Base de Conhecimento de Lesões & Status Clínico da Liga",
            "",
            f"*Última atualização:* {datetime.now(self.tz).strftime('%d/%m/%Y %H:%M BRT')} | *Total de Lesionados:* {len(all_injured)}",
            "",
            "## 📊 Resumo Executivo",
            f"- **⚠️ GTDs / Dúvidas Semanais (Tier 1):** {len(gtd_list)} atletas ({sum(1 for p in gtd_list if p['is_starter'])} titulares)",
            f"- **❌ Desfalques da Semana / Out (Tier 2):** {len(out_list)} atletas ({sum(1 for p in out_list if p['is_starter'])} titulares)",
            f"- **🏥 Longa Duração / IR / Fora da Temporada (Tier 3):** {len(ir_list)} atletas",
            "",
            "## ⚠️ Tier 1: Game-Time Decisions (GTDs & Dúvidas Semanais)",
            "| Atleta | Pos | Time NFL | Franquia | Titular? | Status | Parte do Corpo | Previsão Baseada em Notícias |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        for p in gtd_list:
            is_st = "🔥 SIM" if p["is_starter"] else "Banco"
            md_lines.append(f"| **{p['name']}** | {p['pos']} | {p['team']} | {p['team_name']} ({p['manager']}) | {is_st} | `{p['official_status']}` | {p['body_part']} | {p['forecast']} |")

        md_lines.extend([
            "",
            "## ❌ Tier 2: Desfalques Confirmados da Semana (Out)",
            "| Atleta | Pos | Time NFL | Franquia | Titular? | Status | Previsão Baseada em Notícias |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for p in out_list:
            is_st = "🚨 ALERTA (TITULAR)" if p["is_starter"] else "Banco"
            md_lines.append(f"| **{p['name']}** | {p['pos']} | {p['team']} | {p['team_name']} ({p['manager']}) | {is_st} | `{p['official_status']}` | {p['forecast']} |")

        md_lines.extend([
            "",
            "## 🏥 Tier 3: Longa Duração / IR / Fora da Temporada",
            "| Atleta | Pos | Time NFL | Franquia | Status | Previsão |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for p in ir_list:
            md_lines.append(f"| **{p['name']}** | {p['pos']} | {p['team']} | {p['team_name']} ({p['manager']}) | `{p['official_status']}` | {p['forecast']} |")

        os.makedirs(self.ctx.kb_dir, exist_ok=True)
        target_file = self.ctx.kb_path("INJURY_REPORT.md")
        try:
            with open(target_file, "w", encoding="utf-8") as f:
                f.write("\n".join(md_lines))
        except Exception as e:
            logger.error(f"Erro ao escrever {target_file}: {e}")

    def get_matchup_injury_report(self, week: Optional[int] = None, persist: bool = False) -> str:
        """Generates comprehensive matchup-centric Sunday Medical Report with GTD prioritization."""
        from sleeper_client import SleeperClient
        client = SleeperClient(league_id=self.league_id)
        state = client.get_nfl_state()
        curr_week = week or state.get("week", 1)
        league = client.get_league()
        league_name = league.get("name") or self.ctx.name

        intel = self.sync_injury_intelligence(curr_week, persist=persist)
        injuries_by_roster = intel.get("by_roster", {})
        news_items = self.get_latest_news(category="injury", limit=15)
        matchups = client.get_matchups(curr_week)

        from msg_format import Msg
        msg = Msg("Boletim médico", f"Semana {curr_week} · status oficial do Sleeper por confronto", emoji="🏥")
        irs = []
        for idx, m in enumerate(sorted(matchups, key=lambda x: x["matchup_id"]), 1):
            t1 = m.get("team1") or {}
            t2 = m.get("team2") or {}
            inj = injuries_by_roster.get(str(t1.get("roster_id")), []) + injuries_by_roster.get(str(t2.get("roster_id")), [])
            gtd = [p for p in inj if p.get("is_starter") and p.get("tier") == 1]
            out = [p for p in inj if p.get("is_starter") and p.get("tier") == 2]
            irs += [f"{p['name']} ({p['team_name']})" for p in inj if p.get("tier") == 3]
            lines = []
            for p in out:
                bench = f" → banco: {', '.join(p['bench_replacements'][:2])}" if p.get("bench_replacements") else " → sem reserva!"
                lines.append(f"• ❌ *{p['name']}* ({p['pos']}, {p['team_name']}) — OUT{bench}")
            for p in gtd:
                body = f", {p['body_part']}" if p.get("body_part") and p["body_part"] != "Nenhum" else ""
                bench = f" → banco: {', '.join(p['bench_replacements'][:2])}" if p.get("bench_replacements") else ""
                lines.append(f"• ⚠️ *{p['name']}* ({p['pos']}, {p['team_name']}) — {p.get('official_status_raw') or p['official_status']}{body}{bench}")
            msg.section(f"#{idx} {t1.get('team_name')} x {t2.get('team_name')}", lines or ["• ✅ titulares sem status de lesão"])
        if irs:
            msg.section("IR / longa duração", [", ".join(irs[:16])])

        import league_time
        from datetime import timedelta
        sched = league_time.sunday_schedule(curr_week)
        reminders = [f"• {g['short']} às {league_time.hhmm(g['kickoff'])} → inativos às "
                     f"{league_time.hhmm(g['kickoff'] - timedelta(minutes=90))}" for g in sched["early_games"]]
        main = sched["main_kickoff"]
        reminders.append(f"• Jogos das {league_time.hhmm(main)} → inativos às {league_time.hhmm(main - timedelta(minutes=90))}")
        msg.section("Listas de inativos", reminders, emoji="⏰")
        if news_items:
            msg.section("Plantão da NFL", [f"• {n.get('title', '')}" for n in news_items if is_recent(n)][:3])
        return msg.footer("Quem escala jogador machucado merece perder de goleada.").render()

    def get_league_injury_bulletin(self, week: Optional[int] = None, day_focus: str = "all",
                                   persist: bool = False) -> Dict[str, Any]:
        """Structured bulletin: starters at risk (official live status), bench injuries and injury news."""
        intel = self.sync_injury_intelligence(week, persist=persist)
        all_injured = intel.get("all_injured", [])
        starters_at_risk = [p for p in all_injured if p.get("is_starter") and p.get("tier") in [1, 2]]
        bench_injuries = [p for p in all_injured if not p.get("is_starter")]
        matched_news = [n for n in self.get_latest_news(category="injury", limit=15) if is_recent(n)][:5]

        return {
            "starters_at_risk": starters_at_risk,
            "bench_injuries": bench_injuries,
            "matched_external_news": matched_news,
            "total_starters_at_risk": len(starters_at_risk),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ns = NewsService()
    print("Testando NewsService...")
    news = ns.sync_news()
    print(f"Total de notícias sincronizadas: {len(news)}")
    for n in news[:3]:
        print(f"• [{n['category'].upper()}] {n['title']} ({n['source']})")

    print("\nTestando Boletim Médico da Liga:")
    bulletin = ns.get_league_injury_bulletin()
    print(f"Titulares em risco na liga: {bulletin['total_starters_at_risk']}")
    for s in bulletin["starters_at_risk"][:5]:
        rep = f" (Opções no banco: {', '.join(s.get('bench_replacements', []))})" if s.get('bench_replacements') else " (Sem substituto imediato no banco!)"
        print(f"• *{s.get('manager')}* ({s.get('team_name')}): *{s.get('name')}* ({s.get('pos')} - {s.get('official_status')}){rep}")

    print("\n=== TESTE: BOLETIM MÉDICO POR CONFRONTO ===")
    print(ns.get_matchup_injury_report())
