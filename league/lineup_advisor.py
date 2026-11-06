"""Lineup advice for the admin's own team, by DM, on a dynamic schedule and always INCREMENTAL.

When (league timezone, derived from the ESPN schedule of the week — never fixed hours):
* every day with games, except Sunday/Monday (Thursday, Thanksgiving, Christmas, Friday/Saturday games…):
  08:00 and 1 h before each kickoff window that matters to the roster (the first window of the day always);
* Sunday: 08:00 and 1 h before the first kickoff of the day;
* Monday: same as Sunday, but only sent when there is something to change (otherwise stays silent).

What: the engine computes the facts (starters/bench, injury status, game state and kickoff, projections, bye, the
deterministic swaps and the autosub candidates) and one model call writes the verdict. The previous analysis of the
week and what changed since it go into the prompt, so each message says "mantém / mude X" instead of starting over.
Sleeper AutoSub (league setting `max_subs`) is part of every analysis: up to N bench players, in priority order.
Without a model (or on a bad answer) the deterministic text goes out, so the alert never depends on the LLM.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import league_ctx
import league_profile
import league_time
import watch_prefs
from msg_format import Msg

logger = logging.getLogger("lineup_advisor")

FILE = "lineup_state.json"
INJURED = ("Out", "IR", "Doubtful", "Sus", "PUP", "NFI", "DNR")
SWAP_MARGIN = float(os.environ.get("LINEUP_SWAP_MARGIN", "2.0"))
WINDOW_MIN = 25          # a slot is valid for this long after its time (cron ticks every 5 min)
MORNING_HOUR = 8


# ============================================================================ schedule
def plan_slots(games: List[Dict[str, Any]], tz: ZoneInfo) -> List[Dict[str, Any]]:
    """[{key, at, kind, kickoff, conditional}] for the games of one NFL week, sorted by time.

    kind: "morning" | "pre". Sunday: first kickoff only. Monday: conditional. Other days: every window."""
    by_day: Dict[Any, List[datetime]] = {}
    for g in games:
        ko = g.get("kickoff")
        if ko:
            by_day.setdefault(ko.astimezone(tz).date(), []).append(ko.astimezone(tz))
    out: List[Dict[str, Any]] = []
    for day, kicks in sorted(by_day.items()):
        kicks = sorted(set(kicks))
        wd = day.weekday()                      # Mon=0 … Sun=6
        conditional = wd == 0
        morning = datetime(day.year, day.month, day.day, MORNING_HOUR, 0, tzinfo=tz)
        windows = kicks[:1] if wd in (6, 0) else kicks
        if windows and windows[0] - timedelta(minutes=60) > morning + timedelta(minutes=30):
            out.append({"key": f"{day.isoformat()}_morning", "at": morning, "kind": "morning",
                        "kickoff": kicks[0], "conditional": conditional})
        for i, ko in enumerate(windows):
            out.append({"key": f"{day.isoformat()}_pre_{ko.strftime('%H%M')}", "at": ko - timedelta(minutes=60),
                        "kind": "pre", "kickoff": ko, "conditional": conditional, "first": i == 0})
    out.sort(key=lambda s: s["at"])
    return out


def due(slots: List[Dict[str, Any]], dispatched: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    res = []
    for s in slots:
        end = min(s["at"] + timedelta(minutes=WINDOW_MIN), s["kickoff"])
        if s["key"] not in dispatched and s["at"] <= now < end:
            res.append(s)
    return res


# ============================================================================ facts
def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build_facts(profile, roster: Dict[str, Any], db: Dict[str, Any], games: Dict[str, Dict[str, Any]],
                proj: Dict[str, Any], injury_fn, max_subs: int = 3,
                live_pts: Optional[Dict[str, Any]] = None, same_time: bool = False) -> Dict[str, Any]:
    """Everything the advice needs about ONE roster, deterministic."""
    espn_ok = bool(games)
    slots = list(profile.starter_slots)
    starters_ids = list(roster.get("starters") or [])

    def view(pid: str, slot: Optional[str] = None) -> Dict[str, Any]:
        p = db.get(pid, {}) or {}
        g = games.get(p.get("team") or "")
        status = injury_fn(p) or ""
        return {
            "vs": ("" if not g else f"vs {g['away']}" if g.get("home") == p.get("team") else f"@ {g['home']}"),
            "pts": round(_num((live_pts or {}).get(pid)), 1),
            "pid": pid, "slot": slot, "name": p.get("name") or pid, "pos": p.get("pos") or "?",
            "team": p.get("team") or "FA", "status": status,
            "out": status in INJURED, "q": status == "Questionable",
            "bye": bool(espn_ok and not g), "started": bool(g and g.get("state") != "pre"),
            "game": league_time.game_label(g) if espn_ok else "?",
            "kickoff": g["kickoff"].isoformat() if g and g.get("kickoff") else None,
            "proj": round(_num((proj.get(pid) or {}).get("pts_ppr") or (proj.get(pid) or {}).get("pts_half_ppr")), 1),
        }

    starters = [view(pid, slots[i] if i < len(slots) else "?") if pid and pid != "0"
                else {"pid": "0", "slot": slots[i] if i < len(slots) else "?", "name": "(vaga vazia)", "pos": "?",
                      "team": "", "status": "", "out": True, "q": False, "bye": False, "started": False,
                      "game": "", "kickoff": None, "proj": 0.0, "vs": "", "pts": 0.0}
                for i, pid in enumerate(starters_ids)]
    bench = [view(pid) for pid in (roster.get("players") or []) if pid not in starters_ids and pid]

    def usable(b: Dict[str, Any]) -> bool:
        return not (b["out"] or b["bye"] or b["started"])

    def eligible(slot: str) -> frozenset:
        try:
            return profile.slot_positions(slot)
        except Exception:
            return frozenset()

    # deterministic swaps: only starters whose game has not started, each bench player used once
    swaps: List[Dict[str, Any]] = []
    used: set = set()
    cands = []
    for s in starters:
        if s["started"]:
            continue
        broken = s["out"] or s["bye"]
        for b in bench:
            if not usable(b) or league_profile.normalize_pos(b["pos"]) not in eligible(s["slot"]):
                continue
            base = s["proj"] * (0.0 if broken else 0.75 if s["q"] else 1.0)
            gain = b["proj"] - base
            if broken or gain >= SWAP_MARGIN:
                cands.append((gain + (100 if broken else 0), s, b))
    for _, s, b in sorted(cands, key=lambda c: -c[0]):
        if s["pid"] in {x["out_pid"] for x in swaps} or b["pid"] in used:
            continue
        used.add(b["pid"])
        why = ("fora (" + (s["status"] or "sem status") + ")" if s["out"] else "de bye" if s["bye"]
               else "Questionable" if s["q"] else f"+{b['proj'] - s['proj']:.1f} proj.")
        swaps.append({"out_pid": s["pid"], "out": s["name"], "in_pid": b["pid"], "in": b["name"], "slot": s["slot"],
                      "why": why, "gain": round(b["proj"] - s["proj"], 1)})

    # Slot moves (nobody leaves the lineup) and AutoSubs, on the lineup AFTER the swaps above
    plan = list(starters)
    for x in swaps:
        k = next(i for i, s in enumerate(plan) if s["pid"] == x["out_pid"])
        plan[k] = {**next(b for b in bench if b["pid"] == x["in_pid"]), "slot": plan[k]["slot"]}
    moves = realign(plan, eligible)
    autosubs = pair_autosubs(plan, [b for b in bench if usable(b) and b["pid"] not in used], eligible, max_subs,
                             same_time)
    issues = [s for s in starters if not s["started"] and (s["out"] or s["bye"] or s["q"])]
    return {"starters": starters, "bench": bench, "swaps": swaps, "moves": moves, "plan": plan, "same_time": same_time,
            "autosubs": autosubs, "issues_n": len(issues),
            "needs_action": bool(swaps) or any(s["out"] or s["bye"] for s in starters if not s["started"]),
            "projected": round(sum(s["proj"] for s in starters), 1)}


def _ts(kickoff: Optional[str]) -> float:
    try:
        return datetime.fromisoformat(kickoff).timestamp() if kickoff else 0.0
    except ValueError:
        return 0.0


def _risk(s: Dict[str, Any]) -> float:
    if s["started"] or s["pid"] == "0":
        return 0.0
    return 4.0 if s["out"] or s["bye"] else 3.0 if s["q"] else 1.0


def realign(plan: List[Dict[str, Any]], eligible) -> List[Dict[str, Any]]:
    """Slot exchanges between starters, applied IN PLACE on `plan`; returns the moves.

    A Sleeper AutoSub is paired with ONE starter and must fit that starter's SLOT, and a manual swap obeys the same
    rule. So the wide slots (FLEX, SUPER_FLEX) belong to whoever may still need replacing: first a Questionable
    starter, then whoever plays LATER (news can still come; by then only late-game players are swappable, and in
    FLEX any RB/WR/TE of those fits). An exchange happens only if the teammate fits the narrow slot."""
    def pos(x):
        return league_profile.normalize_pos(x["pos"])

    def free(x):
        return x["pid"] != "0" and not x["started"] and not x["out"] and not x["bye"]

    def prio(x):
        return (1 if x["q"] else 0, _ts(x["kickoff"]))

    moves: List[Dict[str, Any]] = []
    for _ in range(len(plan) ** 2):
        cands = []
        for i, x in enumerate(plan):
            if not free(x):
                continue
            for j, y in enumerate(plan):
                if j == i or not free(y) or prio(x) <= prio(y):
                    continue
                wa, wb = eligible(x["slot"]), eligible(y["slot"])
                if len(wb) <= len(wa) or pos(x) not in wb or pos(y) not in wa:
                    continue
                cands.append(((x["q"] and not y["q"], len(wb) - len(wa), prio(x), -_ts(y["kickoff"])), i, j))
        if not cands:
            break
        _, i, j = max(cands, key=lambda c: c[0])
        x, y = plan[i], plan[j]
        why = (f"{x['status'] or 'Questionable'} · {x['game']}" if x["q"] and not y["q"]
               else f"joga depois ({x['game']}; {y['name']} joga {y['game']})")
        moves.append({"pid": x["pid"], "name": x["name"], "from": x["slot"], "to": y["slot"],
                      "with_pid": y["pid"], "with": y["name"], "game": x["game"], "why": why})
        plan[i], plan[j] = {**y, "slot": x["slot"]}, {**x, "slot": y["slot"]}
    return moves


def pair_autosubs(plan: List[Dict[str, Any]], pool: List[Dict[str, Any]], eligible, max_subs: int,
                  same_time: bool = False) -> List[Dict[str, Any]]:
    """Sleeper AutoSub pairs: ≤ max_subs × (bench player → ONE starter), sub eligible for the starter's slot, each
    sub used once (league setting `sub_start_time_eligibility` → the sub must not play before the starter).
    Riskiest starters first; among them the most constrained slot first, so a TE-only slot does not lose the
    only TE to a FLEX that anyone could cover. The pair locks when either player kicks off: `deadline`."""
    def opts(s, left):
        return [b for b in left if league_profile.normalize_pos(b["pos"]) in eligible(s["slot"])
                and (not same_time or _ts(b["kickoff"]) >= _ts(s["kickoff"]))]

    left = list(pool)
    targets = [s for s in plan if _risk(s) > 0]
    out: List[Dict[str, Any]] = []
    while targets and left and len(out) < max_subs:
        targets.sort(key=lambda s: (-_risk(s), len(opts(s, left)) or 999, -_ts(s["kickoff"])))
        s = targets.pop(0)
        o = opts(s, left)
        if not o:
            continue
        b = max(o, key=lambda b: (b["proj"], _ts(b["kickoff"]) >= _ts(s["kickoff"])))
        left.remove(b)
        dl = min((k for k in (s["kickoff"], b["kickoff"]) if k), key=_ts, default=None)
        out.append({"pid": b["pid"], "name": b["name"], "pos": b["pos"], "proj": b["proj"], "game": b["game"],
                    "covers": [s["name"]], "covers_pid": s["pid"], "slot": s["slot"], "deadline": dl})
    return out


def snapshot(facts: Dict[str, Any]) -> Dict[str, Any]:
    return {"starters": [s["pid"] for s in facts["starters"]],
            "status": {x["pid"]: x["status"] for x in facts["starters"] + facts["bench"] if x["status"]},
            "bench": [b["pid"] for b in facts["bench"]],
            "started": [x["pid"] for x in facts["starters"] + facts["bench"] if x["started"]]}


def changes_since(prev: Optional[Dict[str, Any]], facts: Dict[str, Any]) -> List[str]:
    """Plain list of what moved since the last analysis (the model must not rediscover it)."""
    if not prev:
        return []
    names = {x["pid"]: x["name"] for x in facts["starters"] + facts["bench"]}
    cur = snapshot(facts)
    out = []
    for pid in cur["starters"]:
        if pid not in prev["starters"]:
            out.append(f"{names.get(pid, pid)} entrou no time titular")
    for pid in prev["starters"]:
        if pid not in cur["starters"]:
            out.append(f"{names.get(pid, pid)} saiu do time titular")
    for pid in set(cur["status"]) | set(prev.get("status", {})):
        a, b = prev.get("status", {}).get(pid, ""), cur["status"].get(pid, "")
        if a != b and pid in names:
            out.append(f"{names[pid]}: status {a or 'saudável'} → {b or 'saudável'}")
    for pid in cur["started"]:
        if pid not in prev.get("started", []) and pid in names:
            out.append(f"jogo de {names[pid]} começou")
    return out


# ============================================================================ text
def _line(p: Dict[str, Any]) -> str:
    tag = f" — {p['status']}" if p["status"] else ""
    pts = f" · já fez {p['pts']}" if p.get("pts") else ""
    return (f"{p['slot']}: {p['name']} ({p['pos']}·{p['team']} {p.get('vs', '')}) proj {p['proj']} · "
            f"{p['game']}{tag}{pts}")


def pending_swaps(prev: Optional[Dict[str, Any]], facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Swaps suggested before that were NOT applied (starter still in, bench player still out, games not started)."""
    if not prev:
        return []
    cur = {x["pid"]: x for x in facts["starters"]}
    ben = {x["pid"]: x for x in facts["bench"]}
    out = []
    for s in prev.get("swaps_struct") or []:
        a, b = cur.get(s.get("sai_pid")), ben.get(s.get("entra_pid"))
        if a and b and not a["started"] and not b["started"] and not b["out"]:
            out.append(s)
    return out


def prompt(facts: Dict[str, Any], label: str, prev: Optional[Dict[str, Any]], changes: List[str],
           opp: str, max_subs: int, full: bool = False, extra: Optional[Dict[str, Any]] = None) -> str:
    extra = extra or {}
    pend = extra.get("pending") or []
    head = [f"Momento: {label}. Adversário da semana: {opp or 'bye'}."]
    if extra.get("my_pts") is not None:
        head.append(f"PLACAR ATUAL: meu time {extra['my_pts']} x {extra.get('opp_pts', 0)} adversário "
                    f"(projeção do que falta: meu {extra.get('my_left', '?')} · adversário {extra.get('opp_left', '?')}).")
    body = ["TITULARES (slot: jogador, jogo, projeção Sleeper):", *[_line(s) for s in facts["starters"]],
            "BANCO:", *[_line({**b, "slot": "BN"}) for b in facts["bench"]]]
    if extra.get("opp_starters"):
        body += ["TITULARES DO ADVERSÁRIO:", *[_line(s) for s in extra["opp_starters"]]]
    body += [
        "TROCAS CALCULADAS (determinístico, só por projeção): " + ("; ".join(
            f"{x['out']} → {x['in']} ({x['why']})" for x in facts["swaps"]) or "nenhuma"),
        "AJUSTES DE POSIÇÃO CALCULADOS (ninguém sai do time; obrigatórios, já entram na mensagem): " + ("; ".join(
            f"{m['name']} {m['from']} → {m['to']} (troca de vaga com {m['with']}; {m['why']})"
            for m in facts.get("moves") or []) or "nenhum"),
        "ESCALAÇÃO APÓS TROCAS E AJUSTES (é sobre ela que o AutoSub vale):",
        *[_line(x) for x in facts.get("plan") or facts["starters"]],
        f"PARES DE AUTOSUB CALCULADOS (máx {max_subs}): " + ("; ".join(
            f"{a['name']} ({a['pos']}) → {', '.join(a['covers'])} [{a.get('slot', '?')}]"
            for a in facts["autosubs"]) or "nenhum"),
    ]
    rules = ["Regras: use SÓ os jogadores acima. Jogo que já começou/terminou não se troca. "
             "AutoSub do Sleeper é um PAR: cada reserva cobre UM titular só, precisa caber na VAGA desse titular "
             "na escalação após ajustes (WR só aceita WR; FLEX aceita RB/WR/TE; SUPER_FLEX também QB), cada "
             "reserva vale uma vez e o par trava quando qualquer um dos dois entra em campo. Indique até "
             f"{max_subs} pares (reserva → titular), em ordem de prioridade. "
             + ("Nesta liga o reserva precisa jogar no mesmo horário ou depois do titular. " if facts.get("same_time")
                else "Nesta liga o reserva PODE jogar antes do titular: o par só fica travado (sem edição) quando o "
                     "primeiro joga, mas CONTINUA VALENDO — se o titular for declarado inativo depois, os pontos do "
                     "reserva entram; o prazo para montar o par é o primeiro jogo dos dois. ")
             + 
             "Os AJUSTES DE POSIÇÃO existem para isso: as vagas FLEX/SUPER_FLEX ficam com quem ainda pode precisar "
             "de substituto — primeiro titular Questionable, depois quem joga MAIS TARDE — porque ali qualquer "
             "RB/WR/TE serve de AutoSub ou de troca manual até o jogo dele; explique o ganho. "
             "Escolha pela projeção, matchup e VOLATILIDADE: se o time está na frente, prefira piso alto e jogo "
             "já definido; se está atrás, prefira teto alto. Considere lesão/status e horário do jogo."]
    if full:
        body.append("ANÁLISE ANTERIOR: nenhuma — esta é a análise COMPLETA da semana.")
        rules.append("Faça a análise COMPLETA: raio-x por posição (QB, RB, WR, TE e demais) citando matchup "
                     "(vs/@), projeção, status e quem deve ser titular; leitura curta do adversário; escalação "
                     "ideal completa (um jogador por slot). Responda APENAS JSON: "
                     '{"veredito":"mantem"|"mudar","resumo":"2-3 frases","adversario":"1-2 frases",'
                     '"raio_x":[{"pos":"QB","texto":"..."}],"escalacao":[{"slot":"","jogador":"","motivo":""}],'
                     '"trocas":[{"sai":"","entra":"","motivo":""}],'
                     '"autosubs":[{"jogador":"","cobre":"um titular","motivo":""}],"alerta":"opcional"}')
    else:
        prev_txt = f"({prev.get('at', '')[:16]}) {prev.get('summary', '')}" if prev else "nenhuma"
        if prev and prev.get("swaps"):
            prev_txt += " | trocas sugeridas: " + "; ".join(prev["swaps"])
        if prev and prev.get("moves"):
            prev_txt += " | ajustes de posição sugeridos: " + "; ".join(prev["moves"])
        if prev and prev.get("autosubs"):
            prev_txt += " | autosubs sugeridos: " + ", ".join(prev["autosubs"])
        body += [f"ANÁLISE ANTERIOR: {prev_txt}", "O QUE MUDOU DESDE ENTÃO: " + ("; ".join(changes) or "nada relevante")]
        if pend:
            body.append("SUGESTÕES ANTERIORES AINDA NÃO APLICADAS: " + "; ".join(
                f"{p['sai']} → {p['entra']} ({p.get('motivo', '')})" for p in pend))
            rules.append("CONSISTÊNCIA: cada sugestão anterior ainda não aplicada deve ser REAFIRMADA ou "
                         'RETIRADA com motivo concreto (lista "anteriores"); nunca ignore em silêncio.')
        rules.append("Seja incremental: diga se a análise anterior continua valendo e o que mudou. Responda "
                     'APENAS JSON: {"veredito":"mantem"|"mudar","resumo":"1-3 frases","trocas":[{"sai":"","entra":"",'
                     '"motivo":""}],"anteriores":[{"sai":"","status":"mantida"|"retirada","motivo":""}],'
                     '"autosubs":[{"jogador":"","cobre":"um titular","motivo":""}],"alerta":"opcional"}')
    return "\n".join(head + body + [""] + rules)


def _parse(raw: str, facts: Dict[str, Any], max_subs: int) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, TypeError):
        return None
    known = {x["name"].lower(): x for x in facts["starters"] + facts["bench"]}
    bench_names = {b["name"].lower() for b in facts["bench"]}
    trocas = [t for t in (data.get("trocas") or []) if isinstance(t, dict)
              and str(t.get("sai", "")).lower() in known and str(t.get("entra", "")).lower() in bench_names]
    subs = _valid_pairs(data.get("autosubs") or [], facts, max_subs)
    raio = [r for r in (data.get("raio_x") or []) if isinstance(r, dict) and r.get("texto")]
    esc = [e for e in (data.get("escalacao") or []) if isinstance(e, dict)
           and str(e.get("jogador", "")).lower() in known]
    ant = [a for a in (data.get("anteriores") or []) if isinstance(a, dict)]
    return {"veredito": "mudar" if trocas else "mantem",
            "resumo": str(data.get("resumo") or "").strip()[:500], "trocas": trocas, "autosubs": subs,
            "raio_x": raio[:8], "escalacao": esc, "anteriores": ant,
            "adversario": str(data.get("adversario") or "").strip()[:400],
            "alerta": str(data.get("alerta") or "").strip()[:300]}


def _valid_pairs(raw: List[Any], facts: Dict[str, Any], max_subs: int) -> List[Dict[str, Any]]:
    """The model's AutoSubs that Sleeper would accept: bench player usable, ONE starter of the plan whose slot
    takes the sub's position, nobody used twice. `cobre` is rewritten to that starter's name."""
    bench = {b["name"].lower(): b for b in facts["bench"] if not (b["out"] or b["bye"] or b["started"])}
    plan = [x for x in facts.get("plan") or facts["starters"] if x["pid"] != "0" and not x["started"]]
    used_b, used_s, out = set(), set(), []
    for a in raw:
        if not isinstance(a, dict) or len(out) >= max_subs:
            continue
        b = bench.get(str(a.get("jogador", "")).strip().lower())
        cob = str(a.get("cobre", "")).lower()
        s = next((x for x in plan if x["name"].lower() in cob and x["pid"] not in used_s), None)
        if not b or not s or b["pid"] in used_b or \
                league_profile.normalize_pos(b["pos"]) not in league_profile.SLOT_ELIGIBILITY.get(
                    s["slot"], frozenset({league_profile.normalize_pos(s["slot"])})):
            continue
        used_b.add(b["pid"])
        used_s.add(s["pid"])
        dl = min((k for k in (s["kickoff"], b["kickoff"]) if k), key=_ts, default=None)
        out.append({**a, "jogador": b["name"], "cobre": s["name"], "slot": s["slot"], "deadline": dl})
    return out


def decide(facts: Dict[str, Any], raw: str, max_subs: int,
           pending: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Model answer (validated against the roster) with the deterministic advice as the floor.
    Earlier suggestions still unapplied stay in the list unless the model explicitly withdrew them."""
    out = _parse(raw, facts, max_subs)
    swaps = [{"sai": x["out"], "entra": x["in"], "motivo": x["why"]} for x in facts["swaps"]]
    subs = [{"jogador": a["name"], "cobre": ", ".join(a["covers"]), "motivo": f"proj {a['proj']} · {a['game']}",
             "slot": a.get("slot"), "deadline": a.get("deadline")} for a in facts["autosubs"]]
    if not out:
        out = {"veredito": "", "trocas": swaps, "autosubs": subs, "alerta": "", "raio_x": [], "escalacao": [],
               "anteriores": [], "adversario": "",
               "resumo": "" if swaps or facts.get("moves") else "Time titular sem pendências pelo status atual.",
               "ai": False}
    else:
        have = {t["sai"].lower() for t in out["trocas"]}
        out["trocas"] += [s for s in swaps if s["sai"].lower() not in have
                          and (s["motivo"].startswith("fora") or s["motivo"] == "de bye")]
        if len(out["autosubs"]) < max_subs:
            have_s = {a["jogador"].lower() for a in out["autosubs"]}
            have_c = {a["cobre"].lower() for a in out["autosubs"]}
            out["autosubs"] += [s for s in subs if s["jogador"].lower() not in have_s
                                and s["cobre"].lower() not in have_c][:max_subs - len(out["autosubs"])]
        out["ai"] = True
    retired = {str(a.get("sai", "")).lower() for a in out.get("anteriores", [])
               if str(a.get("status", "")).lower().startswith("retir")}
    have = {t["sai"].lower() for t in out["trocas"]}
    for p in pending or []:
        k = p["sai"].lower()
        if k in retired or k in have:
            continue
        out["trocas"].append({"sai": p["sai"], "entra": p["entra"],
                              "motivo": (p.get("motivo") or "") + " (sugerida antes, segue valendo)"})
        have.add(k)
    out["ajustes"] = list(facts.get("moves") or [])
    out["veredito"] = "mudar" if out["trocas"] or out["ajustes"] else "mantem"
    return out


def render(decision: Dict[str, Any], facts: Dict[str, Any], week: int, label: str, opp: str,
           prev: Optional[Dict[str, Any]], changes: List[str], full: bool = False) -> str:
    sub = f"Semana {week} · {label}" + (f" · vs {opp}" if opp else "")
    msg = Msg("Escalação completa" if full else "Escalação", sub, emoji="📋")
    n = len(decision["trocas"]) + len(decision.get("ajustes") or [])
    ver = f"🔁 *Mudar {n}*" if n else "✅ *Manter como está*"
    head = [ver]
    if decision.get("resumo"):
        head.append(decision["resumo"])
    head.append(f"Projeção do time: {facts['projected']}")
    msg.text(*head)
    if full and decision.get("adversario"):
        msg.section(f"Adversário ({opp})" if opp else "Adversário", [decision["adversario"]], emoji="🆚")
    if full and decision.get("raio_x"):
        msg.section("Raio-X por posição", [f"*{r.get('pos', '')}*: {r['texto']}" for r in decision["raio_x"]], emoji="🔍")
    if full and decision.get("escalacao"):
        msg.section("Escalação ideal", [f"• {e.get('slot', '')}: *{e['jogador']}*"
                                        + (f" — {e['motivo']}" if e.get("motivo") else "")
                                        for e in decision["escalacao"]], emoji="🏈")
    if not full and prev and changes:
        msg.section(f"O que mudou desde {prev.get('at', '')[11:16]}", [f"• {c}" for c in changes[:8]], emoji="🔎")
    elif not full and prev:
        msg.text(f"_Sem mudanças desde a análise das {prev.get('at', '')[11:16]}._")
    if decision["trocas"]:
        msg.section("Trocas", [f"• *{t['sai']}* → *{t['entra']}*" + (f" — {t['motivo']}" if t.get("motivo") else "")
                               for t in decision["trocas"]], emoji="🔁")
    if decision.get("ajustes"):
        msg.section("Ajustes de posição (ninguém sai do time)",
                    [f"• *{m['name']}* {m['from']} → {m['to']}, *{m['with']}* {m['to']} → {m['from']} — "
                     f"{m['why']}: no {m['to']} qualquer {_positions(m['to'])} do banco pode ser o AutoSub dele, "
                     f"e dá pra trocá-lo à mão até o jogo"
                     for m in decision["ajustes"]], emoji="🔀")
    if decision["autosubs"]:
        msg.section(f"AutoSub (até {len(decision['autosubs'])})",
                    [f"{i}. *{a['jogador']}*" + (f" cobre *{a['cobre']}*" if a.get("cobre") else "")
                     + (f" ({a['slot']})" if a.get("slot") else "")
                     + (f" — {a['motivo']}" if a.get("motivo") else "")
                     + (f" · definir até {_when(a['deadline'])}" if a.get("deadline") else "")
                     for i, a in enumerate(decision["autosubs"], 1)], emoji="🤖")
    if decision.get("alerta"):
        msg.text(f"⚠️ {decision['alerta']}")
    # ONE WhatsApp message, never split into parts
    return msg.footer(watch_prefs.instructions("escalacao")).render(max_lines=10_000)


def _positions(slot: str) -> str:
    ok = league_profile.SLOT_ELIGIBILITY.get(slot, frozenset({slot}))
    return "/".join([p for p in league_profile.POSITION_ORDER if p in ok] + sorted(ok - set(league_profile.POSITION_ORDER)))


def _when(kickoff: str) -> str:
    try:
        d = datetime.fromisoformat(kickoff)
    except (TypeError, ValueError):
        return str(kickoff)
    return f"{['seg', 'ter', 'qua', 'qui', 'sex', 'sáb', 'dom'][d.weekday()]} {d.strftime('%H:%M')}"


def _week_lineup(roster: Optional[Dict[str, Any]], entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The roster with the starters saved for THIS week (its /matchups entry); /rosters can lag behind."""
    if not roster or not (entry or {}).get("starters"):
        return roster
    return {**roster, "starters": [str(p) for p in entry["starters"]]}


# ============================================================================ runner
class LineupAdvisor:
    def __init__(self, ctx=None, sleeper=None, llm_fn=None, send=None, state_path: str = "", targets=None,
                 games_fn=None, db=None, dry: bool = False):
        self.ctx = ctx or league_ctx.current()
        self._sleeper = sleeper
        self._llm = llm_fn
        self._send_fn = send
        self.state_path = state_path or self.ctx.state_path(FILE)
        self._targets = targets
        self._games_fn = games_fn
        self._db = db
        self.dry = dry
        self.out: List[str] = []

    @property
    def sleeper(self):
        if self._sleeper is None:
            from sleeper_client import SleeperClient
            self._sleeper = SleeperClient()
        return self._sleeper

    def load(self) -> Dict[str, Any]:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, st: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    def _call_llm(self, text: str, full: bool = False) -> str:
        if self._llm:
            return self._llm(text)
        import llm
        # thinking "high" (agy shim honours it per request): this analysis is worth the extra seconds
        return llm.chat(text, max_tokens=3000 if full else 1800, timeout=170, json_mode=True, effort="high",
                        system="Você é o analista de escalação de fantasy football do dono do time. PT-BR, direto, "
                               "sem inventar jogadores.")

    def _label(self, slot: Dict[str, Any], tz: ZoneInfo) -> str:
        day = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][slot["kickoff"].astimezone(tz).weekday()]
        hh = slot["kickoff"].astimezone(tz).strftime("%H:%M")
        return f"{day} de manhã (1º jogo {hh})" if slot["kind"] == "morning" else f"{day}, 1h antes do jogo das {hh}"

    def tick(self, season: str, week: int, now: Optional[datetime] = None) -> List[str]:
        self.out = []
        if watch_prefs.is_paused("escalacao"):
            return []
        tz = ZoneInfo(self.ctx.timezone)
        now = (now or datetime.now(tz)).astimezone(tz)
        games = self._games_fn(week, season) if self._games_fn else league_time.week_games(week, season)
        if not games:
            return []
        st = self.load()
        disp = st.setdefault("dispatched", {})
        todo = due(plan_slots(games, tz), {k.split("::", 1)[-1]: v for k, v in disp.items()
                                           if k.startswith(f"{season}-{week}::")}, now)
        if not todo:
            return []
        targets = self._targets if self._targets is not None else watch_prefs.targets(self.ctx)
        by_team = league_time.games_by_team(week, season) if not self._games_fn else {
            t: g for g in games for t in (g["home"], g["away"])}
        proj = self.sleeper._get(f"projections/nfl/regular/{season}/{week}") or {}
        db = self._db if self._db is not None else __import__("players_db").load()
        import players_db
        lset = self.sleeper.get_league().get("settings") or {}
        max_subs = int(lset.get("max_subs") or 3)
        same_time = bool(lset.get("sub_start_time_eligibility"))   # sub may not play before the starter
        rosters = {int(r["roster_id"]): r for r in self.sleeper.get_rosters()}
        for slot in todo[:1]:                    # one slot per tick: the next one (if any) goes on the next tick
            sid = f"{season}-{week}::{slot['key']}"
            delivered = True
            for tg in targets:
                roster = rosters.get(int(tg["rid"]))
                if not roster:
                    continue
                mine, opp_info = self._matchup(week, int(tg["rid"]))
                roster = _week_lineup(roster, mine)
                facts = build_facts(self.ctx.profile, roster, db, by_team, proj, players_db.injury, max_subs,
                                    live_pts=(mine or {}).get("players_points"), same_time=same_time)
                relevant = any((x["kickoff"] or "") and datetime.fromisoformat(x["kickoff"]).astimezone(tz)
                               == slot["kickoff"] for x in facts["starters"] + facts["bench"])
                if slot["kind"] == "pre" and not relevant and not slot.get("first"):
                    continue
                label = self._label(slot, tz)
                opp = self._opponent(season, week, int(tg["rid"]))
                hist = st.setdefault("analyses", {}).setdefault(f"{season}-{week}-{tg['rid']}", [])
                prev = hist[-1] if hist else None
                # the first message of the week (Thursday morning) is always the COMPLETE analysis
                full = prev is None or (slot["kind"] == "morning" and slot["kickoff"].astimezone(tz).weekday() == 3
                                        and not any(h.get("full") for h in hist))
                changes = changes_since(prev.get("snapshot") if prev else None, facts)
                pend = pending_swaps(prev, facts)
                extra = {"pending": pend}
                if opp_info is not None:
                    orost = _week_lineup(rosters.get(int(opp_info["roster_id"])), opp_info)
                    ofacts = build_facts(self.ctx.profile, orost, db, by_team, proj, players_db.injury, max_subs,
                                         live_pts=opp_info.get("players_points")) if orost else None
                    if ofacts:
                        extra["opp_starters"] = ofacts["starters"]
                        extra["opp_left"] = round(sum(s["proj"] for s in ofacts["starters"] if not s["started"]), 1)
                    extra.update(my_pts=round(_num((mine or {}).get("points")), 2),
                                 opp_pts=round(_num(opp_info.get("points")), 2),
                                 my_left=round(sum(s["proj"] for s in facts["starters"] if not s["started"]), 1))
                raw = self._call_llm(prompt(facts, label, prev, changes, opp, max_subs, full, extra), full)
                decision = decide(facts, raw, max_subs, pend)
                by_name = {x["name"].lower(): x["pid"] for x in facts["starters"] + facts["bench"]}
                hist.append({"at": now.isoformat(timespec="seconds"), "slot": slot["key"], "full": full,
                             "summary": decision.get("resumo") or ("Manter." if not decision["trocas"] else "Mudar."),
                             "swaps": [f"{t['sai']} → {t['entra']}" for t in decision["trocas"]],
                             "swaps_struct": [{"sai": t["sai"], "entra": t["entra"], "motivo": t.get("motivo", ""),
                                               "sai_pid": by_name.get(t["sai"].lower()),
                                               "entra_pid": by_name.get(t["entra"].lower())}
                                              for t in decision["trocas"]],
                             "moves": [f"{m['name']} {m['from']} → {m['to']} (com {m['with']})"
                                       for m in decision.get("ajustes") or []],
                             "autosubs": [f"{a['jogador']} → {a.get('cobre', '')}" for a in decision["autosubs"]],
                             "snapshot": snapshot(facts)})
                del hist[:-4]
                if slot["conditional"] and not (decision["trocas"] or decision["ajustes"] or facts["needs_action"]):
                    continue                     # Monday with nothing to move: silence
                text = render(decision, facts, week, label, opp, prev, changes, full)
                if self.dry:
                    self.out.append(text)
                    continue
                if self._send(tg, text):
                    self.out.append(text)
                else:
                    delivered = False            # retry on the next tick, still inside the slot window
            if delivered and not self.dry:
                disp[sid] = now.isoformat(timespec="seconds")
        # keep the state small: only the current week
        st["analyses"] = {k: v for k, v in st.get("analyses", {}).items() if k.startswith(f"{season}-{week}-")}
        st["dispatched"] = {k: v for k, v in disp.items() if k.startswith(f"{season}-")}
        if not self.dry:
            self.save(st)
        return self.out

    def _opponent(self, season: str, week: int, rid: int) -> str:
        try:
            for m in self.sleeper.get_matchups(week):
                t1, t2 = m.get("team1"), m.get("team2")
                if t1 and t2 and rid in (t1["roster_id"], t2["roster_id"]):
                    return (t2 if t1["roster_id"] == rid else t1).get("team_name", "")
        except Exception:
            pass
        return ""

    def _matchup(self, week: int, rid: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """(my raw matchup entry, opponent's) from Sleeper — players_points and points included."""
        try:
            raw = self.sleeper._get(f"league/{self.ctx.league_id}/matchups/{week}") or []
        except Exception:
            return None, None
        mine = next((m for m in raw if int(m.get("roster_id", -1)) == rid), None)
        if not mine or mine.get("matchup_id") is None:
            return mine, None
        opp = next((m for m in raw if m.get("matchup_id") == mine["matchup_id"]
                    and int(m.get("roster_id", -1)) != rid), None)
        return mine, opp

    def _send(self, tg: Dict[str, Any], text: str) -> bool:
        """ONE message to ONE number (the first that Evolution confirms)."""
        if self._send_fn:
            return bool(self._send_fn(text))
        from evolution_client import EvolutionClient
        return watch_prefs.dm(EvolutionClient(), tg["dms"], text)
