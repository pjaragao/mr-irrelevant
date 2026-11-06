"""League context: WHICH league the running code serves, WHERE its data lives and WHAT its rules are.

One engine, many leagues. Everything that differs between two leagues hangs off a `LeagueContext`:

    identity   slug, Sleeper league id, WhatsApp group, persona name, timezone        (league.yaml)
    data       knowledge base, state files, media output — one private directory per league
    secrets    admin numbers, people phones, alert JID — `<home>/.env`, never the process environment
    rules      roster structure / scoring / playoffs / FAAB, read from Sleeper         (LeagueProfile)
    people     managers (roster -> names, nicknames, phones)                           (people.People)

A league "home" is a directory with a `league.yaml`. The default Hermes profile (`data/`) is league #1 and keeps its
data where it always was (`legacy_layout: true`: `league/knowledge_base`, state files next to the code); every other
league is a Hermes profile `data/profiles/<slug>/` whose data sits in `<home>/league/` — see docs/MULTILIGA.md.

Resolution of "the current league" (`current()`), first hit wins:
  1. `with use(ctx):` — the plugin/adapter/tests pin a context for the duration of a call (a ContextVar, so
     concurrent turns of different leagues in the same gateway process never see each other's);
  2. `$LEAGUE_HOME` — explicit override for CLI/dev runs;
  3. `$HERMES_HOME` — cron scripts run with their profile's home (Hermes sets it per job);
  4. `/opt/data` (the container's default profile), else the repo's `data/` (dev machine).
A home without `league.yaml` behaves as the legacy single-league setup (league id from `$SLEEPER_LEAGUE_ID`).
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import league_profile
from league_profile import LeagueProfile

logger = logging.getLogger("league_ctx")

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DATA_DIR = os.path.join(os.path.dirname(ENGINE_DIR), "data")
CONFIG_NAME = "league.yaml"
LEGACY_LEAGUE_ID = "1312694161610113024"   # the only literal left: league #1, used when nothing configures a league
DEFAULT_HOME = "/opt/data"
REMOTE_FILE = "league_profile.json"        # cached Sleeper-derived state: rules + manager directory
REMOTE_TTL = 6 * 3600                      # league settings change a few times a year, rosters' owners rarely
_MEM_TTL = 300.0

_CURRENT: "contextvars.ContextVar[Optional[LeagueContext]]" = contextvars.ContextVar("league_ctx", default=None)
_HOMES: Dict[str, "LeagueContext"] = {}
_HOMES_LOCK = threading.Lock()
_ENV_CACHE: Dict[str, tuple] = {}


# --------------------------------------------------------------------------- small readers
def _read_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # malformed YAML must not take the bot down: report and run with defaults
        logger.error("cannot read %s: %s", path, exc)
        return {}


def read_env_file(path: str) -> Dict[str, str]:
    """KEY=VALUE lines of a .env file (comments/blank/`export` tolerated), cached by mtime. Never touches os.environ."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _ENV_CACHE.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    out: Dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k.strip()] = v
    _ENV_CACHE[path] = (mtime, out)
    return out


def _csv(value: str) -> List[str]:
    return [p.strip() for p in str(value or "").split(",") if p.strip()]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-") or "liga"


def build_directory(users: List[Dict[str, Any]], rosters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Managers of a league from Sleeper's /users + /rosters: roster_id <-> owner -> display name + team name."""
    by_uid = {str(u.get("user_id")): u for u in users or [] if isinstance(u, dict)}
    out = []
    for r in sorted((r for r in rosters or [] if isinstance(r, dict)), key=lambda r: int(r.get("roster_id") or 0)):
        rid = int(r.get("roster_id") or 0)
        u = by_uid.get(str(r.get("owner_id"))) or {}
        display = str(u.get("display_name") or f"Time {rid}")
        team = str((u.get("metadata") or {}).get("team_name") or display)
        out.append({"roster_id": rid, "owner_id": str(r.get("owner_id") or ""), "display_name": display,
                    "team_name": team})
    return out


# --------------------------------------------------------------------------- the context
class LeagueContext:
    def __init__(self, home: Optional[str] = None, config: Optional[Dict[str, Any]] = None,
                 legacy_layout: Optional[bool] = None, profile: Optional[LeagueProfile] = None,
                 directory: Optional[List[Dict[str, Any]]] = None,
                 fetch: Optional[Callable[[], Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]]] = None):
        self.home = os.path.abspath(home) if home else ""
        self.config: Dict[str, Any] = dict(config or {})
        configured = bool(self.config)
        self.legacy_layout = (not configured) if legacy_layout is None else bool(legacy_layout)
        # An unconfigured home is the single-league setup: identity from the environment, as before multi-league.
        self.league_id = str(self.config.get("sleeper_league_id")
                             or (os.environ.get("SLEEPER_LEAGUE_ID") if not configured else "")
                             or LEGACY_LEAGUE_ID)
        self.slug = str(self.config.get("slug") or _slug(os.path.basename(self.home) if configured else "default"))
        self._fetch = fetch                                  # tests: () -> (league, users, rosters)
        self._state_cache: Optional[Dict[str, Any]] = None
        self._state_at = 0.0
        if profile is not None or directory is not None:     # fixture-injected (tests): never touches the network
            self._state_cache = {"profile": profile or league_profile.default_profile(self.league_id),
                                 "directory": list(directory or [])}
            self._state_at = float("inf")
        self._people: Any = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ identity
    @property
    def name(self) -> str:
        return str(self.config.get("name") or self.profile.name or self.slug)

    @property
    def timezone(self) -> str:
        return str(self.config.get("timezone") or "America/Sao_Paulo")

    @property
    def group_jid(self) -> str:
        return str(self.config.get("group_jid") or self.env("WHATSAPP_GROUP_JID") or "")

    @property
    def group_jids(self) -> List[str]:
        out = [self.group_jid] + [str(g) for g in (self.config.get("groups") or [])]
        return [g for i, g in enumerate(out) if g and g not in out[:i]]

    @property
    def persona(self) -> Dict[str, Any]:
        p = dict(self.config.get("persona") or {})
        p.setdefault("name", "Mr. Irrelevant")
        p.setdefault("mention_regex", self.env("EVOLUTION_MENTION_REGEX"))
        p.setdefault("admin_name", "o admin")
        return p

    @property
    def persona_name(self) -> str:
        return str(self.persona["name"])

    @property
    def persona_aka(self) -> str:
        """Optional nickname of the persona ("Brock Purdy" for Mr. Irrelevant); "" when it has none."""
        return str(self.persona.get("aka") or "")

    def persona_label(self, style: str = "paren") -> str:
        """"Mr. Irrelevant (Brock Purdy)" (paren) or "Brock Purdy / Mr. Irrelevant" (slash); the plain name if no aka."""
        aka = self.persona_aka
        if not aka:
            return self.persona_name
        return f"{self.persona_name} ({aka})" if style == "paren" else f"{aka} / {self.persona_name}"

    def rule(self, name: str, default: Any = None) -> Any:
        """House rule from league.yaml `rules:` (things Sleeper does not model, e.g. `draft_order: max_pf`)."""
        return (self.config.get("rules") or {}).get(name, default)

    # ------------------------------------------------------------------ editorial schedule
    def slot_time(self, slot_key: str) -> Optional[str]:
        """HH:MM override for a weekly slot (league.yaml `schedule: {tue_08h_power: "09:30"}`), else None."""
        v = (self.config.get("schedule") or {}).get(slot_key)
        return str(v) if v else None

    def slot_enabled(self, slot_key: str) -> bool:
        return slot_key not in set(self.config.get("disable_slots") or [])

    # ------------------------------------------------------------------ paths
    @property
    def data_dir(self) -> str:
        return ENGINE_DIR if self.legacy_layout else os.path.join(self.home, "league")

    @property
    def kb_dir(self) -> str:
        return os.path.join(self.data_dir, "knowledge_base")

    @property
    def state_dir(self) -> str:
        return ENGINE_DIR if self.legacy_layout else os.path.join(self.data_dir, "state")

    @property
    def shared_dir(self) -> str:
        """NFL-level caches (players master, nflverse, news, ESPN): identical for every league, shared on purpose."""
        return ENGINE_DIR

    def kb_path(self, *parts: str) -> str:
        return os.path.join(self.kb_dir, *parts)

    def source_path(self, name: str) -> str:
        """A league data source (history JSON): knowledge_base/ first, then the data root, where league #1 kept some
        files. Not found -> the knowledge_base/ path, which is where a writer should create it."""
        for d in (self.kb_dir, self.data_dir):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        return os.path.join(self.kb_dir, name)

    def state_path(self, *parts: str) -> str:
        return os.path.join(self.state_dir, *parts)

    def has_consolation(self) -> bool:
        """Does this league play a consolation bracket ("Toilet Bowl")? The house rule `rules.toilet_bowl` decides when
        set; otherwise whether any played season has losers-bracket games in its knowledge base."""
        rule = self.rule("toilet_bowl", None)
        if rule is not None:
            return bool(rule)
        try:
            with open(self.source_path("brackets_history.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        return any(g.get("bracket") == "losers" for s in data.values() if isinstance(s, dict)
                   for g in (s.get("games") or []))

    def shared_path(self, *parts: str) -> str:
        return os.path.join(self.shared_dir, *parts)

    def media_dir(self, kind: str) -> str:
        """Generated images/audio, under the league's Hermes home so the gateway may deliver them."""
        base = self.home or os.environ.get("HERMES_HOME") or DEFAULT_HOME
        return os.path.join(base, "image_cache", f"liga_{kind}")

    # ------------------------------------------------------------------ private settings
    def env(self, name: str, default: str = "") -> str:
        """League-private setting (admins, phones, alert JID). League #1 keeps reading the container environment;
        every other league reads ONLY its own `<home>/.env` — never another league's value."""
        if self.legacy_layout:       # the container's env_file already put data/.env into the process environment
            return os.environ.get(name, default)
        return read_env_file(os.path.join(self.home, ".env")).get(name, default) if self.home else default

    @property
    def admins(self) -> List[str]:
        return _csv(self.env("LEAGUE_ADMINS") or self.env("EVOLUTION_ADMINS"))

    @property
    def dm_allowed(self) -> List[str]:
        return _csv(self.env("LEAGUE_DM_ALLOWED") or self.env("EVOLUTION_DM_ALLOWED"))

    @property
    def alert_jid(self) -> str:
        return self.env("ADMIN_ALERT_JID")

    # ------------------------------------------------------------------ rules + managers (Sleeper)
    def _remote_path(self) -> str:
        return self.state_path(REMOTE_FILE)

    def _download(self) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        if self._fetch is not None:
            return self._fetch()
        sc = self.sleeper()
        return sc.get_league(), sc.get_users_raw(), sc.get_rosters()

    def _state(self) -> Dict[str, Any]:
        """{'profile': LeagueProfile, 'directory': [...]}: memory (5 min) -> disk (6 h) -> Sleeper -> last known /
        flagged default. Never raises: the bot keeps answering with what it knew."""
        with self._lock:
            now = time.time()
            if self._state_cache is not None and now - self._state_at < _MEM_TTL:
                return self._state_cache
            path = self._remote_path()
            cached: Optional[Dict[str, Any]] = None
            try:
                with open(path, encoding="utf-8") as f:
                    cached = json.load(f)
                if str((cached.get("profile") or {}).get("league_id")) != self.league_id:
                    cached = None            # a cache written for another league id is not ours
            except Exception:
                cached = None
            if cached and now - float(cached.get("fetched_at") or 0) < REMOTE_TTL:
                self._state_cache = {"profile": LeagueProfile.from_dict(cached["profile"]),
                                     "directory": list(cached.get("directory") or [])}
                self._state_at = now
                return self._state_cache
            try:
                league, users, rosters = self._download()
                self._store_state(league, users, rosters, now)
            except Exception as exc:
                logger.warning("league %s: Sleeper refresh failed (%s); using %s", self.league_id, exc,
                               "the cached copy" if cached else "defaults")
                if cached:
                    self._state_cache = {"profile": LeagueProfile.from_dict(cached["profile"]),
                                         "directory": list(cached.get("directory") or [])}
                elif self._state_cache is None:
                    self._state_cache = {"profile": league_profile.default_profile(self.league_id), "directory": []}
                self._state_at = now - _MEM_TTL + 60   # retry in a minute, not on every call
            return self._state_cache

    def _store_state(self, league: Optional[Dict[str, Any]], users: List[Dict[str, Any]],
                     rosters: List[Dict[str, Any]], now: float) -> None:
        """Rules + managers from a Sleeper payload -> memory and disk caches (caller holds the lock)."""
        if not league or not league.get("roster_positions"):
            raise RuntimeError("Sleeper returned no league settings")
        prof = LeagueProfile.from_sleeper(league)
        directory = build_directory(users, rosters)
        self._state_cache = {"profile": prof, "directory": directory}
        self._state_at = now
        path = self._remote_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now, "profile": prof.to_dict(), "directory": directory}, f)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("cannot cache the league state: %s", exc)

    def prime(self, league: Dict[str, Any], users: List[Dict[str, Any]], rosters: List[Dict[str, Any]]) -> LeagueProfile:
        """Seed the rules/managers cache with a payload the caller already holds (league creation and refresh), so the
        league answers from the same data its persona was written from instead of asking Sleeper again."""
        with self._lock:
            self._people = None
            self._store_state(league, users, rosters, time.time())
            return self._state_cache["profile"]  # type: ignore[index]

    @property
    def profile(self) -> LeagueProfile:
        return self._state()["profile"]

    @property
    def directory(self) -> List[Dict[str, Any]]:
        return self._state()["directory"]

    def refresh(self) -> LeagueProfile:
        """Drop memory and disk caches and ask Sleeper again (CLI `liga.py sync`)."""
        with self._lock:
            self._state_cache, self._state_at, self._people = None, 0.0, None
            try:
                os.remove(self._remote_path())
            except OSError:
                pass
            return self.profile

    @property
    def people(self):
        import people as _people
        with self._lock:
            if self._people is None:
                self._people = _people.build(self)
            return self._people

    def sleeper(self):
        from sleeper_client import SleeperClient
        return SleeperClient(self.league_id)

    def summary(self) -> Dict[str, Any]:
        return {"slug": self.slug, "league_id": self.league_id, "home": self.home, "legacy_layout": self.legacy_layout,
                "group_jid": self.group_jid, "kb_dir": self.kb_dir, "state_dir": self.state_dir,
                "persona": self.persona_name}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LeagueContext {self.slug} {self.league_id} home={self.home or '-'}>"


# --------------------------------------------------------------------------- resolution
def default_home() -> str:
    explicit = os.environ.get("LEAGUE_HOME") or os.environ.get("HERMES_HOME")
    if explicit:
        return explicit
    if os.path.isdir(DEFAULT_HOME):
        return DEFAULT_HOME
    return REPO_DATA_DIR if os.path.isdir(REPO_DATA_DIR) else DEFAULT_HOME


def for_home(home: Optional[str] = None) -> LeagueContext:
    """Context of a league home directory (cached; re-read when its league.yaml changes)."""
    home = os.path.abspath(home or default_home())
    cfg_path = os.path.join(home, CONFIG_NAME)
    try:
        mtime = os.path.getmtime(cfg_path)
    except OSError:
        mtime = 0.0
    with _HOMES_LOCK:
        hit = _HOMES.get(home)
        if hit is not None and getattr(hit, "_cfg_mtime", None) == mtime:
            return hit
        cfg = _read_yaml(cfg_path) if mtime else {}
        ctx = LeagueContext(home=home, config=cfg, legacy_layout=(bool(cfg.get("legacy_layout")) if cfg else True))
        ctx._cfg_mtime = mtime  # type: ignore[attr-defined]
        _HOMES[home] = ctx
        return ctx


def current() -> LeagueContext:
    return _CURRENT.get() or for_home()


@contextlib.contextmanager
def use(ctx: Optional[LeagueContext]) -> Iterator[Optional[LeagueContext]]:
    """Pin `ctx` as the current league for the enclosed code (restores the previous one on exit)."""
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def reset_cache() -> None:
    """Forget cached contexts (tests, `liga.py sync`)."""
    with _HOMES_LOCK:
        _HOMES.clear()
    _ENV_CACHE.clear()


# --------------------------------------------------------------------------- registry
def discover(root: Optional[str] = None) -> List[LeagueContext]:
    """Every configured league under the gateway root: the default home plus `profiles/*/league.yaml`."""
    root = os.path.abspath(root or os.environ.get("HERMES_HOME") or default_home())
    out: List[LeagueContext] = []
    if os.path.isfile(os.path.join(root, CONFIG_NAME)):
        out.append(for_home(root))
    pdir = os.path.join(root, "profiles")
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        names = []
    for n in names:
        if os.path.isfile(os.path.join(pdir, n, CONFIG_NAME)):
            out.append(for_home(os.path.join(pdir, n)))
    return out


def for_group(jid: str, root: Optional[str] = None) -> Optional[LeagueContext]:
    """League served in a WhatsApp group (None = the group belongs to no configured league)."""
    jid = str(jid or "")
    if not jid:
        return None
    for ctx in discover(root):
        if jid in ctx.group_jids:
            return ctx
    return None


def league_for_group(jid: str, root: Optional[str] = None) -> LeagueContext:
    """League of a WhatsApp group; a group no league.yaml lists belongs to the default league (the single-league
    behaviour: its group is only known through the environment)."""
    return for_group(jid, root) or for_home(root and os.path.abspath(root))


def _same_phone(a: str, b: str) -> bool:
    """Same WhatsApp number, with or without the Brazilian 9th digit (country + DDD + last 8 digits)."""
    da, db = re.sub(r"\D", "", str(a)), re.sub(r"\D", "", str(b))
    if not da or not db:
        return False
    if da == db:
        return True
    return len(da) >= 10 and len(db) >= 10 and da[-8:] == db[-8:] and da[:4] == db[:4]


def league_for_sender(sender_ids: List[str], root: Optional[str] = None) -> LeagueContext:
    """League a direct message belongs to. A DM has no group to tell, so: the first league (the default one first)
    that lists the sender as a DM contact — the same lists the gateway's `profile_routes` are generated from, so the
    adapter and Hermes agree on the profile — then the first whose phone book knows the sender, else the default."""
    leagues = discover(root)
    for lg in leagues:
        try:
            allowed = lg.dm_allowed
            if any(_same_phone(i, n) for i in sender_ids for n in allowed):
                return lg
        except Exception:  # a broken league config must not take the others down
            logger.exception("league %s: DM contact lookup failed", lg.slug)
    for lg in leagues:
        try:
            if lg.people.resolve_sender(list(sender_ids)) is not None:
                return lg
        except Exception:
            logger.exception("league %s: sender lookup failed", lg.slug)
    return for_home(root and os.path.abspath(root))


def all_group_jids(root: Optional[str] = None) -> set:
    return {g for lg in discover(root) for g in lg.group_jids}


def all_dm_allowed(root: Optional[str] = None) -> set:
    return {n for lg in discover(root) for n in lg.dm_allowed}


if __name__ == "__main__":  # pragma: no cover - `python3 league_ctx.py [home]`
    import sys
    c = for_home(sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(c.summary(), ensure_ascii=False, indent=2))
    print("\n".join(c.profile.describe_pt()))
