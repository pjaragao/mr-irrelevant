"""League rules and roster structure, derived from the Sleeper league object (pure: no I/O, no network).

Every bot serves ONE league; what makes two leagues different (starting slots, bench/IR/taxi, scoring format, FAAB,
playoff layout, dynasty or redraft...) lives here as data read from Sleeper, never as constants in the engine.
Modules ask the profile ("can a K start here?", "how many matchup cards?", "which positions fill a FLEX?") instead of
hard-coding the answer of the league they were first written for.

    profile = LeagueProfile.from_sleeper(league_json)       # league_json = GET /league/{id}
    profile.can_start("K")        -> False when no starting slot accepts a kicker
    profile.flex_positions        -> frozenset({"RB", "WR", "TE"}) (union of every FLEX-type slot)
    profile.describe_pt()         -> human lines (persona prompt, `ajuda`)

`to_dict()` / `from_dict()` round-trip through JSON (the context caches it on disk).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Sleeper slot name -> player positions that may fill it.
SLOT_ELIGIBILITY: Dict[str, frozenset] = {
    "QB": frozenset({"QB"}), "RB": frozenset({"RB"}), "WR": frozenset({"WR"}), "TE": frozenset({"TE"}),
    "K": frozenset({"K"}), "DEF": frozenset({"DEF"}),
    "DL": frozenset({"DL"}), "LB": frozenset({"LB"}), "DB": frozenset({"DB"}),
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"WR", "RB"}), "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "IDP_FLEX": frozenset({"DL", "LB", "DB"}),
}
NON_STARTING_SLOTS = frozenset({"BN", "IR", "TAXI"})
FLEX_SLOTS = frozenset({"FLEX", "WRRB_FLEX", "REC_FLEX", "SUPER_FLEX", "IDP_FLEX"})
IDP_POSITIONS = frozenset({"DL", "LB", "DB"})
POSITION_ORDER = ("QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB")

# Player positions that Sleeper reports finer than the slot names.
_POS_ALIASES = {"DE": "DL", "DT": "DL", "EDGE": "DL", "NT": "DL", "OLB": "LB", "ILB": "LB", "MLB": "LB",
                "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB", "PK": "K", "DST": "DEF", "D/ST": "DEF", "HB": "RB",
                "FB": "RB"}

_TYPE_NAMES = {0: "redraft", 1: "keeper", 2: "dynasty"}
_WAIVER_FAAB = 2


def normalize_pos(pos: Any) -> str:
    """Player position as the slot table spells it (DE -> DL, CB -> DB, DST -> DEF...)."""
    p = str(pos or "").strip().upper()
    return _POS_ALIASES.get(p, p)


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LeagueProfile:
    league_id: str
    name: str = ""
    season: str = ""
    status: str = ""
    sport: str = "nfl"
    total_rosters: int = 0
    roster_positions: Tuple[str, ...] = ()
    league_type: str = "redraft"          # redraft | keeper | dynasty
    best_ball: bool = False
    divisions: int = 0
    division_names: Tuple[str, ...] = ()
    playoff_teams: int = 0
    playoff_week_start: int = 0
    playoff_round_type: int = 0            # 0 one week per round, 1 two weeks per round
    trade_deadline: int = 0
    waiver_type: int = 0
    waiver_budget: int = 0                 # > 0 means FAAB
    waiver_clear_days: int = 0
    draft_rounds: int = 0
    taxi_slots: int = 0
    reserve_slots: int = 0
    previous_league_id: str = ""
    draft_id: str = ""
    scoring: Dict[str, float] = field(default_factory=dict)
    estimated: bool = False                # True when built from defaults because Sleeper could not be reached

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_sleeper(cls, league: Dict[str, Any]) -> "LeagueProfile":
        s = league.get("settings") or {}
        meta = league.get("metadata") or {}
        n_div = _int(s.get("divisions"))
        names = tuple(str(meta.get(f"division_{i}") or f"Divisão {i}") for i in range(1, n_div + 1))
        prev = str(league.get("previous_league_id") or "")
        return cls(
            league_id=str(league.get("league_id") or ""),
            name=str(league.get("name") or ""),
            season=str(league.get("season") or ""),
            status=str(league.get("status") or ""),
            sport=str(league.get("sport") or "nfl"),
            total_rosters=_int(league.get("total_rosters") or s.get("num_teams")),
            roster_positions=tuple(str(p) for p in (league.get("roster_positions") or ())),
            league_type=_TYPE_NAMES.get(_int(s.get("type")), "redraft"),
            best_ball=bool(_int(s.get("best_ball"))),
            divisions=n_div, division_names=names,
            playoff_teams=_int(s.get("playoff_teams")),
            playoff_week_start=_int(s.get("playoff_week_start")),
            playoff_round_type=_int(s.get("playoff_round_type")),
            trade_deadline=_int(s.get("trade_deadline")),
            waiver_type=_int(s.get("waiver_type")),
            waiver_budget=_int(s.get("waiver_budget")),
            waiver_clear_days=_int(s.get("waiver_clear_days")),
            draft_rounds=_int(s.get("draft_rounds")),
            taxi_slots=_int(s.get("taxi_slots")),
            reserve_slots=_int(s.get("reserve_slots")),
            previous_league_id="" if prev in ("", "0") else prev,   # Sleeper sends "0" for a league's first season
            draft_id=str(league.get("draft_id") or ""),
            scoring={k: float(v) for k, v in (league.get("scoring_settings") or {}).items()
                     if isinstance(v, (int, float))},
        )

    def to_dict(self) -> Dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["roster_positions"] = list(self.roster_positions)
        d["division_names"] = list(self.division_names)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LeagueProfile":
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        known["roster_positions"] = tuple(known.get("roster_positions") or ())
        known["division_names"] = tuple(known.get("division_names") or ())
        known["scoring"] = dict(known.get("scoring") or {})
        return cls(**known)

    # ------------------------------------------------------------------ roster structure
    @property
    def starter_slots(self) -> Tuple[str, ...]:
        """Slots that score, in Sleeper's order (QB RB RB WR ... FLEX SUPER_FLEX)."""
        return tuple(p for p in self.roster_positions if p not in NON_STARTING_SLOTS)

    @property
    def bench_slots(self) -> int:
        return sum(1 for p in self.roster_positions if p == "BN")

    @property
    def n_starters(self) -> int:
        return len(self.starter_slots)

    @property
    def roster_size(self) -> int:
        """Active roster: starters + bench (IR/taxi are extra)."""
        return len(self.roster_positions)

    def slot_positions(self, slot: str) -> frozenset:
        return SLOT_ELIGIBILITY.get(slot, frozenset({normalize_pos(slot)}) if slot else frozenset())

    @property
    def startable(self) -> frozenset:
        """Positions that can fill at least one starting slot."""
        out: set = set()
        for slot in self.starter_slots:
            out |= self.slot_positions(slot)
        return frozenset(out)

    @property
    def startable_ordered(self) -> Tuple[str, ...]:
        have = self.startable
        return tuple(p for p in POSITION_ORDER if p in have) + tuple(sorted(have - set(POSITION_ORDER)))

    def can_start(self, pos: Any) -> bool:
        return normalize_pos(pos) in self.startable

    @property
    def flex_positions(self) -> frozenset:
        """Positions that can land in any FLEX-type slot (RB/WR/TE for a plain FLEX, plus QB for a SUPER_FLEX...)."""
        out: set = set()
        for slot in self.starter_slots:
            if slot in FLEX_SLOTS:
                out |= self.slot_positions(slot)
        return frozenset(out)

    @property
    def has_kicker(self) -> bool:
        return "K" in self.startable

    @property
    def has_defense(self) -> bool:
        return "DEF" in self.startable

    @property
    def has_idp(self) -> bool:
        return bool(self.startable & IDP_POSITIONS)

    @property
    def superflex(self) -> bool:
        return "SUPER_FLEX" in self.starter_slots or self.starter_slots.count("QB") > 1

    @property
    def n_qb_slots(self) -> int:
        """1 normally, 2 when a QB may fill a second slot (SUPER_FLEX / 2QB): what trade-value markets call numQbs."""
        return 2 if self.superflex else 1

    # ------------------------------------------------------------------ league rules
    @property
    def faab(self) -> bool:
        return self.waiver_budget > 0

    @property
    def dynasty(self) -> bool:
        return self.league_type == "dynasty"

    @property
    def regular_season_weeks(self) -> int:
        return max(0, self.playoff_week_start - 1) if self.playoff_week_start else 0

    @property
    def matchups_per_week(self) -> int:
        return max(1, self.total_rosters // 2)

    @property
    def playoff_rounds(self) -> int:
        """Rounds of the title bracket (byes included): 4 teams -> 2, 6 or 8 -> 3, 12 -> 4."""
        n = self.playoff_teams
        return 0 if n < 2 else (n - 1).bit_length()

    @property
    def playoff_week_end(self) -> int:
        """Last week of the title bracket (0 when the league has no playoff schedule)."""
        if not (self.playoff_week_start and self.playoff_rounds):
            return 0
        return self.playoff_week_start + self.playoff_rounds * (2 if self.playoff_round_type == 1 else 1) - 1

    @property
    def ppr(self) -> float:
        return float(self.scoring.get("rec", 0.0))

    @property
    def te_premium(self) -> float:
        return float(self.scoring.get("bonus_rec_te", 0.0))

    def optimal_lineup(self, players: Iterable[Dict[str, Any]]) -> Tuple[float, List[Tuple[str, Dict[str, Any]]]]:
        """Best possible lineup for this league's starting slots from `players` (dicts with `pos` and `pts`):
        the "Max PF" of a week. Slots are filled most restrictive first (QB before FLEX before SUPER_FLEX), each with
        the highest scorer still free who is eligible for it. Returns (total points, [(slot, player), ...])."""
        pool = sorted(players, key=lambda p: float(p.get("pts") or 0.0), reverse=True)
        used: set = set()
        lineup: List[Tuple[str, Dict[str, Any]]] = []
        for slot in sorted(self.starter_slots, key=lambda sl: (len(self.slot_positions(sl)), sl)):
            eligible = self.slot_positions(slot)
            for i, p in enumerate(pool):
                if i not in used and normalize_pos(p.get("pos")) in eligible:
                    used.add(i)
                    lineup.append((slot, p))
                    break
        return round(sum(float(p.get("pts") or 0.0) for _, p in lineup), 2), lineup

    def lineup_requirements(self) -> Counter:
        """How many of each starting slot (e.g. Counter(RB=2, WR=3, FLEX=3, ...))."""
        return Counter(self.starter_slots)

    # ------------------------------------------------------------------ text
    def format_label(self) -> str:
        """Short format tag for prompts and headers: 'dynasty, PPR, Superflex, 12 times'."""
        ppr = self.ppr
        scoring = "PPR" if ppr == 1 else ("half PPR" if ppr == 0.5 else ("standard" if ppr == 0 else f"{ppr:g} PPR"))
        bits = [self.league_type, scoring]
        if self.superflex:
            bits.append("Superflex")
        if self.te_premium:
            bits.append("TE premium")
        if self.best_ball:
            bits.append("best ball")
        if self.total_rosters:
            bits.append(f"{self.total_rosters} times")
        return ", ".join(bits)

    def describe_pt(self) -> List[str]:
        """Rules in Portuguese, one fact per line — fed to the persona prompt and the `ajuda` query."""
        if not self.roster_positions:
            return ["Estrutura de elenco ainda não carregada da Sleeper."]
        req = self.lineup_requirements()
        order = [s for s in list(POSITION_ORDER) + sorted(FLEX_SLOTS) if s in req]
        order += [s for s in req if s not in order]

        def slot_text(s: str) -> str:
            n = req[s]
            extra = ""
            if s in FLEX_SLOTS:
                extra = " (" + "/".join(p for p in POSITION_ORDER if p in self.slot_positions(s)) + ")"
            return f"{n} {s}{extra}"
        lines = [f"{self.total_rosters} times, liga {self.league_type}"
                 + (f", FAAB de ${self.waiver_budget}" if self.faab else ", waiver por prioridade")
                 + (f", {self.divisions} divisões" if self.divisions else "") + "."]
        lines.append("Titulares: " + ", ".join(slot_text(s) for s in order) + f". Banco: {self.bench_slots}"
                     + (f"; IR: {self.reserve_slots}" if self.reserve_slots else "")
                     + (f"; taxi: {self.taxi_slots}" if self.taxi_slots else "") + ".")
        missing = [p for p, ok in (("kicker (K)", self.has_kicker), ("defesa (DEF)", self.has_defense),
                                   ("IDP (DL/LB/DB)", self.has_idp)) if not ok]
        if missing:
            lines.append("NÃO há vaga para: " + ", ".join(missing) + " — nunca sugira esses jogadores.")
        if self.best_ball:
            lines.append("Best ball: a escalação é automática, a melhor possível a cada rodada.")
        else:
            lines.append("Só os titulares pontuam; reserva não conta.")
        pts = []
        if self.ppr:
            pts.append(f"{self.ppr:g} por recepção" + (" (PPR)" if self.ppr == 1 else ""))
        if self.scoring.get("pass_td"):
            pts.append(f"TD de passe {self.scoring['pass_td']:g}")
        if self.te_premium:
            pts.append(f"TE premium +{self.te_premium:g}")
        if pts:
            lines.append("Pontuação: " + ", ".join(pts) + ".")
        if self.playoff_week_start:
            rnd = "2 semanas por rodada" if self.playoff_round_type == 1 else "1 semana por rodada"
            lines.append(f"Playoffs: {self.playoff_teams} times a partir da semana {self.playoff_week_start} ({rnd})"
                         + (f"; prazo de trocas: semana {self.trade_deadline}" if self.trade_deadline else "") + ".")
        return lines


def default_profile(league_id: str = "") -> LeagueProfile:
    """Fallback when Sleeper cannot be reached and nothing is cached: a plain 12-team, 1-QB standard league."""
    slots = ("QB", "RB", "RB", "WR", "WR", "TE", "FLEX") + ("BN",) * 6
    return LeagueProfile(league_id=league_id, total_rosters=12, roster_positions=slots, playoff_teams=6,
                         playoff_week_start=15, estimated=True)


def positions_of(slots: Iterable[str]) -> frozenset:
    """Union of positions that the given slot names accept."""
    out: set = set()
    for s in slots:
        out |= SLOT_ELIGIBILITY.get(s, frozenset())
    return frozenset(out)
