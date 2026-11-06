"""Registry of the people of ONE league -> roster_id, shared by every lookup in the engine and the WhatsApp adapter.

Each league has its own `People` (built from its LeagueContext): managers come from Sleeper (`/users` + `/rosters`),
and `league.yaml` -> `people:` adds the curated names/nicknames the group actually uses. The module-level functions
(`resolve`, `label`, `resolve_sender`, ...) answer for the *current* league, so callers do not change.

Matching is accent-insensitive and works on whole words only, so "Ari" never matches "marinh012" and a nickname such
as "Brunão" resolves to its roster. Phones/LIDs (optional) come from the league's private `.env`
(`LEAGUE_PEOPLE_PHONES="4:5548999990001,98765432109876;8:5548999990000"`: roster, then phones/LIDs) so no personal
number lives in git.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

import league_ctx

_STOP = {"the", "of", "and", "team", "club", "time", "dos", "das", "los", "las", "fc", "ff", "fantasy", "football"}


def norm(text: object) -> str:
    s = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    s = s.replace("’", "'").replace("_", " ")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def auto_nicks(display_name: str, team_name: str = "") -> List[str]:
    """Nicknames a manager nobody curated is probably called by: the words of the Sleeper handle
    ("JonathanMachado17" -> jonathan, machado), the first word of the team name, and the full handle/team."""
    out: List[str] = []
    handle = str(display_name or "")
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+", handle.replace("_", " ").replace("-", " "))
    for w in words:
        if len(w) >= 3 and w.lower() not in _STOP:
            out.append(w.lower())
    if len(words) > 1:
        out.append(" ".join(w.lower() for w in words))
    first = norm(team_name).split(" ")[0] if team_name else ""
    if len(first) >= 4 and first not in _STOP:
        out.append(first)
    return list(dict.fromkeys(out))


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


class People:
    def __init__(self, entries: Dict[int, Dict[str, Any]], franchises_path: Optional[str] = None,
                 phones_raw: Any = ""):
        self.entries: Dict[int, Dict[str, Any]] = {int(k): dict(v) for k, v in entries.items()}
        self._franchises_path = franchises_path
        # a string, or a callable read on every use (the league's private env may change while the bot runs)
        self._phones_src = phones_raw if callable(phones_raw) else (lambda raw=phones_raw: raw or "")
        self._aliases: Optional[Dict[int, List[str]]] = None
        self._phones: Optional[Dict[str, int]] = None
        self._phones_raw = None

    # ------------------------------------------------------------------ aliases
    def aliases(self) -> Dict[int, List[str]]:
        if self._aliases is None:
            self._aliases = self._build_aliases()
        return self._aliases

    def _build_aliases(self) -> Dict[int, List[str]]:
        out: Dict[int, set] = {rid: set() for rid in self.entries}
        for rid, p in self.entries.items():
            for v in [p.get("name"), p.get("username"), p.get("team"), *(p.get("nicks") or [])]:
                if v:
                    out[rid].add(norm(v))
        if self._franchises_path:
            try:
                with open(self._franchises_path, encoding="utf-8") as f:
                    fr = json.load(f)
                for k, v in fr.items():
                    rid = int(k)
                    if rid not in out:
                        continue
                    vals = [v.get("canonical_manager"), v.get("real_name"), v.get("current_team"),
                            *(v.get("aliases") or [])]
                    vals += [t.get("team_name") for t in v.get("teams_history", [])]
                    vals += [s.get("sleeper_display_name") for s in (v.get("seasons") or {}).values()]
                    out[rid].update(norm(x) for x in vals if x)
            except Exception:
                pass
        # Names that collide across rosters are useless for resolution.
        owners: Dict[str, set] = {}
        for rid, names in out.items():
            for n in names:
                owners.setdefault(n, set()).add(rid)
        return {rid: sorted((n for n in names if n and len(owners[n]) == 1), key=len, reverse=True)
                for rid, names in out.items()}

    # ------------------------------------------------------------------ resolution
    def resolve(self, query: object) -> Optional[int]:
        """roster_id for a manager/team/nickname/roster number, or None if unknown/ambiguous."""
        q = norm(query)
        for prefix in ("time ", "equipe ", "team ", "roster ", "franquia ", "do ", "da "):
            if q.startswith(prefix):
                q = q[len(prefix):]
        if not q:
            return None
        if q.isdigit() and int(q) in self.entries:
            return int(q)
        al = self.aliases()
        for rid, names in al.items():
            if q in names:
                return rid
        # Whole-word containment, longest alias first: "time do brunao" -> its roster.
        padded = f" {q} "
        best: Optional[tuple] = None
        for rid, names in al.items():
            for n in names:
                if len(n) >= 3 and f" {n} " in padded:
                    if best is None or len(n) > best[0]:
                        best = (len(n), rid)
                    elif len(n) == best[0] and rid != best[1]:
                        best = (len(n), None)
                    break
        return best[1] if best else None

    def phones(self) -> Dict[str, int]:
        raw = self._phones_src() or ""
        if self._phones is None or raw != self._phones_raw:
            self._phones_raw = raw
            out: Dict[str, int] = {}
            for chunk in raw.split(";"):
                if ":" not in chunk:
                    continue
                rid, nums = chunk.split(":", 1)
                for n in nums.split(","):
                    d = _digits(n)
                    if d and rid.strip().isdigit():
                        out[d] = int(rid)
            self._phones = out
        return self._phones

    def resolve_sender(self, ids: List[str], push_name: str = "") -> Optional[int]:
        """Who is talking: verified phone/LID first, WhatsApp display name as a fallback."""
        phones = self.phones()
        for i in ids:
            d = _digits(i)
            if not d:
                continue
            if d in phones:
                return phones[d]
            for num, rid in phones.items():  # Brazilian 9th digit: compare DDD + last 8 digits
                if len(num) >= 10 and len(d) >= 10 and num[-8:] == d[-8:] and num[:4] == d[:4]:
                    return rid
        return self.resolve(push_name) if push_name else None

    def label(self, rid: Optional[int]) -> str:
        p = self.entries.get(rid or 0)
        return f"{p['team']} ({p['name']})" if p else ""

    def team(self, rid: Optional[int]) -> str:
        p = self.entries.get(rid or 0)
        return str(p["team"]) if p else (f"Time {rid}" if rid else "")

    def hint(self, n: int = 6) -> str:
        """A few names this league's group actually uses ("Mario, Panda, Paulo"), for "not found" messages."""
        out: List[str] = []
        for rid in sorted(self.entries):
            p = self.entries[rid]
            nick = next((str(x) for x in (p.get("nicks") or []) if x), "") or str(p.get("name") or "").split(" ")[0]
            nick = nick.strip().title() if nick.islower() else nick.strip()
            if nick and nick not in out:
                out.append(nick)
            if len(out) >= n:
                break
        return ", ".join(out)


def build(ctx: "league_ctx.LeagueContext") -> People:
    """People of a league: curated `league.yaml` entries win; every other roster comes from Sleeper with
    auto-generated nicknames (so a league works with nothing but its Sleeper id)."""
    entries: Dict[int, Dict[str, Any]] = {}
    curated = {int(k): v for k, v in (ctx.config.get("people") or {}).items() if str(k).isdigit() and isinstance(v, dict)}
    for d in ctx.directory:
        rid = int(d["roster_id"])
        if rid in curated:
            continue
        disp = str(d.get("display_name") or f"Time {rid}")
        team = str(d.get("team_name") or disp)
        entries[rid] = {"name": disp, "username": disp, "team": team, "nicks": auto_nicks(disp, team)}
    for rid, c in curated.items():
        entries[rid] = {"name": str(c.get("name") or c.get("username") or f"Time {rid}"),
                        "username": str(c.get("username") or c.get("name") or ""),
                        "team": str(c.get("team") or c.get("name") or f"Time {rid}"),
                        "nicks": [str(n) for n in (c.get("nicks") or [])]}
    return People(entries, franchises_path=ctx.kb_path("franchises_history.json"),
                  phones_raw=lambda: ctx.env("LEAGUE_PEOPLE_PHONES"))


# --------------------------------------------------------------------------- module API (current league)
def _current() -> People:
    return league_ctx.current().people


def entries() -> Dict[int, Dict[str, Any]]:
    return _current().entries


def aliases() -> Dict[int, List[str]]:
    return _current().aliases()


def resolve(query: object) -> Optional[int]:
    return _current().resolve(query)


def resolve_sender(ids: List[str], push_name: str = "") -> Optional[int]:
    return _current().resolve_sender(ids, push_name)


def label(rid: Optional[int]) -> str:
    return _current().label(rid)


def _phones() -> Dict[str, int]:
    return _current().phones()
