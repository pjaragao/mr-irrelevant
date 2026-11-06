"""On-demand trade evaluator (`liga avaliar_troca alvo="Josh Allen + 2027 1st por Bijan Robinson"`).

Values: FantasyCalc dynasty market (Superflex, 12 teams, PPR — the league's format), cached 12h.
Players are matched by Sleeper id (via the engine's fuzzy player search); picks by year + round
("2027 1st", "pick de 1ª 2028", "2028 2nd round", optional Early/Mid/Late).
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import league_ctx

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
URL = "https://api.fantasycalc.com/values/current"


def market_params() -> Dict[str, Any]:
    """FantasyCalc request for THIS league's format (dynasty/redraft, 1 or 2 QB slots, teams, PPR)."""
    p = league_ctx.current().profile
    return {"isDynasty": "true" if p.dynasty or p.league_type == "keeper" else "false", "numQbs": p.n_qb_slots,
            "numTeams": p.total_rosters or 12, "ppr": f"{p.ppr:g}"}


def _cache_path(params: Dict[str, Any]) -> str:
    """NFL-level market data, shared by every league of the same format (one file per format)."""
    tag = "_".join(str(params[k]) for k in ("isDynasty", "numQbs", "numTeams", "ppr"))
    return os.path.join(BASE_DIR, "nfl_data", f"fantasycalc_values_{tag}.json")

SIDES_RX = re.compile(r"\s+(?:por|pelo|pela|x|vs\.?|versus|<->|<=>|⇄|=)\s+", re.I)
ITEMS_RX = re.compile(r"\s*(?:\+|,|;|\s+e\s+|\s+and\s+)\s*", re.I)
ROUND_WORDS = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "primeira": "1st", "segunda": "2nd", "terceira": "3rd",
               "1st": "1st", "2nd": "2nd", "3rd": "3rd", "4th": "4th"}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()


def values() -> List[Dict[str, Any]]:
    params = market_params()
    cache = _cache_path(params)
    try:
        if time.time() - os.path.getmtime(cache) < 12 * 3600:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
    except OSError:
        pass
    import urllib.parse
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "MrIrrelevant/2.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=20).read())
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return data


def _pick_name(text: str) -> Optional[str]:
    t = _norm(text)
    year = re.search(r"\b(20\d\d)\b", t)
    if not year or not re.search(r"pick|escolha|round|rodada|1st|2nd|3rd|4th|\d\s*[ao]\b|primeira|segunda|terceira", t):
        return None
    rnd = None
    m = re.search(r"\b(1st|2nd|3rd|4th)\b", t) or re.search(r"\b([1-4])\s*(?:a|o|st|nd|rd|th)?\s*(?:round|rodada)?\b(?!\d)", t.replace(year.group(1), " "))
    if m:
        rnd = ROUND_WORDS.get(m.group(1))
    for w in ("primeira", "segunda", "terceira"):
        if w in t:
            rnd = ROUND_WORDS[w]
    if not rnd:
        return None
    tier = next((x for x in ("early", "mid", "late") if x in t), None)
    return f"{year.group(1)} {rnd}" + (f" ({tier.capitalize()})" if tier else "")


def _value_of(item: str, vals: List[Dict[str, Any]]) -> Tuple[str, Optional[int]]:
    pick = _pick_name(item)
    if pick:
        by_name = {v["player"]["name"]: v["value"] for v in vals}
        if pick in by_name:
            return pick, by_name[pick]
        base = pick.split(" (")[0]
        return base, by_name.get(base)
    from sleeper_client import SleeperClient
    p = SleeperClient().find_player(item)
    if not p:
        return item, None
    sid = str(p.get("player_id"))
    for v in vals:
        if str(v["player"].get("sleeperId")) == sid:
            return v["player"]["name"], v["value"]
    return p.get("name") or item, 0  # known player, no dynasty value (deep bench / FA)


def evaluate(text: str) -> str:
    sides = [s for s in SIDES_RX.split(text.strip()) if s.strip()]
    if len(sides) != 2:
        return ("❌ Formato: `jogador + pick por jogador` (ex.: 'Josh Allen + 2027 1st por Bijan Robinson'). "
                "Separe os lados com 'por', 'x' ou 'vs' e os itens com '+' ou vírgula.")
    try:
        vals = values()
    except Exception as exc:
        return f"❌ FantasyCalc indisponível agora ({exc}). Não invente valores."
    report, totals, unknown = [], [], []
    for label, side in zip(("Lado A", "Lado B"), sides):
        items = [i for i in ITEMS_RX.split(side) if i.strip()]
        lines, total = [], 0
        for it in items:
            name, v = _value_of(it, vals)
            if v is None:
                unknown.append(it.strip())
                lines.append(f"   • {it.strip()}: não encontrado")
                continue
            total += v
            lines.append(f"   • {name}: {v:,}".replace(",", "."))
        totals.append(total)
        report.append((label, side.strip(), lines, total))
    a, b = totals
    diff = abs(a - b)
    pct = 100.0 * diff / max(a, b) if max(a, b) else 0.0
    if pct < 8:
        verdict = "troca equilibrada (diferença dentro da margem do mercado)"
    else:
        winner = "Lado A" if a > b else "Lado B"
        verdict = f"*{winner}* leva vantagem de {pct:.0f}% no valor de mercado"
    fmt = league_ctx.current().profile
    out = [f"📊 *AVALIAÇÃO DE TROCA — VALOR {'DYNASTY' if fmt.dynasty else 'DE MERCADO'} ({fmt.format_label()})*", ""]
    for label, side, lines, total in report:
        out.append(f"*{label}* ({side}) — total {total:,}".replace(",", "."))
        out.extend(lines)
        out.append("")
    out.append(f"⚖️ Veredito do mercado: {verdict}.")
    if unknown:
        out.append(f"⚠️ Sem valor para: {', '.join(unknown)} (confira o nome/ano da pick).")
    out.append("_Fonte: FantasyCalc (mercado de trocas reais). Valor de mercado ≠ encaixe no elenco: "
               "considere as carências de cada time._")
    return "\n".join(out)
