"""Full waiver/FA report: every processed claim, the winning bid, the losing bids and who disputed the player.

Reads the current AND previous Sleeper leg (claims live in the leg they were created in) and keeps the
transactions processed in the last `hours`. Failed claims carry Sleeper's note ("claimed by another owner").
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from msg_format import Msg


def collect(sleeper, hours: float = 30.0) -> Dict[str, Any]:
    done = sleeper.get_recent_transactions(hours=hours, types=("waiver", "free_agent", "commissioner"), only_complete=False)
    complete = [t for t in done if t["status"] == "complete"]
    failed = [t for t in done if t["status"] == "failed" and t["type"] == "waiver"]
    # player_id -> list of (team, bid) of the claims that lost
    losers: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for t in failed:
        bid = int((t.get("settings") or {}).get("waiver_bid") or 0)
        for pid in (t.get("adds") or {}):
            losers[str(pid)].append((", ".join(t.get("teams") or []) or "?", bid))
    return {"complete": complete, "failed": failed, "losers": losers}


def build(sleeper, player_label, faab_budget: int, hours: float = 30.0, title: str = "Plantão do waiver",
          subtitle: str = "", footer: str = "") -> Optional[str]:
    data = collect(sleeper, hours)
    complete, losers = data["complete"], data["losers"]
    # A team's own failed claim on the player it won (same add, other drop) is not a rival: only claims from OTHER
    # teams make a player "disputado" (even a $0 rival bid, and even when just one bid carries value).
    winners: Dict[str, str] = {str(pid): ", ".join(t.get("teams") or []) or "?"
                               for t in complete for pid in (t.get("adds") or {})}
    losers = {pid: [(tm, b) for tm, b in r if winners.get(pid) != tm] for pid, r in losers.items()}
    losers = {pid: r for pid, r in losers.items() if r}
    lost_count = sum(len(r) for r in losers.values())
    msg = Msg(title, subtitle or f"Tudo o que foi processado nas últimas {int(hours)}h", emoji="💰")
    if not complete and not lost_count:
        msg.text("😴 Nenhuma movimentação de waiver/FA no período.")
        return msg.footer(footer).render() if footer else msg.render()

    by_team: Dict[str, List[str]] = defaultdict(list)
    spent: Dict[str, int] = defaultdict(int)
    disputed = []
    for t in complete:
        team = ", ".join(t.get("teams") or []) or "?"
        bid = int((t.get("settings") or {}).get("waiver_bid") or 0)
        kind = "FA" if t["type"] == "free_agent" else ("comissário" if t["type"] == "commissioner" else f"${bid}")
        spent[team] += bid
        for pid in (t.get("adds") or {}):
            line = f"• ➕ {player_label(pid)} — {kind}"
            rivals = losers.get(str(pid), [])
            if rivals:
                line += f" (venceu {len(rivals)} lance{'s' if len(rivals) > 1 else ''})"
                disputed.append((player_label(pid, False), team, bid, rivals))
            by_team[team].append(line)
        for pid in (t.get("drops") or {}):
            by_team[team].append(f"• ➖ {player_label(pid, False)}")

    total_bids = sum(spent.values())
    msg.text(f"{len(complete)} movimentações concluídas · {lost_count} lances perdidos · "
             f"${total_bids} de FAAB gasto (orçamento ${faab_budget})")
    for team in sorted(by_team):
        head = team + (f" — gastou ${spent[team]}" if spent[team] else "")
        msg.section(head, by_team[team])
    if disputed:
        lines = []
        for name, winner, bid, rivals in sorted(disputed, key=lambda d: -d[2]):
            others = ", ".join(f"{t} (${b})" for t, b in sorted(rivals, key=lambda r: -r[1]))
            lines.append(f"• {name}: *{winner}* levou com ${bid}")
            lines.append(f"   ↳ perderam: {others}")
        msg.section("Disputas", lines, emoji="⚔️")
    orphan = [(pid, r) for pid, r in losers.items() if not any(str(pid) in (t.get("adds") or {}) for t in complete)]
    if orphan:
        msg.section("Lances que não levaram ninguém", [
            f"• {player_label(pid, False)}: " + ", ".join(f"{t} (${b})" for t, b in r) for pid, r in orphan])
    if footer:
        msg.footer(footer)
    return msg.render()
