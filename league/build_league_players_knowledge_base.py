#!/usr/bin/env python3
"""League Players Relational Knowledge Base Builder (2022–2026).

Compiles and interconnects all 976 historical and active athletes in the High Stakes Fantasy:
- Tracks exact origin (Startup Draft 2022, Rookie Drafts 2023-2026, Waiver Wire with FAAB, or Free Agency)
- Constructs chronological career timeline (Draft -> Trades -> Waivers -> Drops -> Bidding wars -> Current Roster)
- Identifies 2026 Current Status (Starter, Bench, Taxi, IR, Unowned)
- Flags Franchise Lifers (drafted in 2022 Startup and never left the team in 4 years)
- Flags League Nomads (players with the highest number of trades/waiver moves)
- Computes Roster Origin DNA for all 12 franchises (Draft % vs Trade % vs Waiver %)

Generates:
- knowledge_base/players_registry.json
- knowledge_base/PLAYERS_DOSSIER.md
"""

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_DIR = os.path.join(BASE_DIR, "knowledge_base")


def load_canonical_data():
    """Load all necessary historical and current databases."""
    with open(os.path.join(BASE_DIR, "draft_history.json"), "r", encoding="utf-8") as f:
        drafts = json.load(f)

    with open(os.path.join(BASE_DIR, "all_sleeper_trades_raw.json"), "r", encoding="utf-8") as f:
        trades_raw = json.load(f)

    waivers_path = os.path.join(KB_DIR, "waivers_market_classified.json")
    if not os.path.exists(waivers_path):
        waivers_path = os.path.join(BASE_DIR, "waivers_market_classified.json")
    with open(waivers_path, "r", encoding="utf-8") as f:
        waivers_data = json.load(f)

    import players_db as _pdb
    master_players = _pdb.load()

    with open(os.path.join(KB_DIR, "franchises_history.json"), "r", encoding="utf-8") as f:
        raw_f = json.load(f)
        franchises = {int(k): v for k, v in raw_f.items()}

    rosters_path = os.path.join(KB_DIR, "teams_rosters_analysis.json")
    if not os.path.exists(rosters_path):
        rosters_path = os.path.join(BASE_DIR, "teams_rosters_analysis.json")
    with open(rosters_path, "r", encoding="utf-8") as f:
        rosters_data = json.load(f)

    return drafts, trades_raw, waivers_data, master_players, franchises, rosters_data


def build_players_knowledge_base():
    """Build the relational knowledge base for all league players."""
    drafts, trades_raw, waivers_data, master_players, franchises, rosters_data = load_canonical_data()
    waivers_txs = waivers_data.get("transactions", [])

    # -------------------------------------------------------------------------
    # 1. Index All Draft Picks by Player ID
    # -------------------------------------------------------------------------
    # Format: player_id -> list of draft pick occurrences (sorted by season)
    drafts_by_player: Dict[str, List[Dict[str, Any]]] = {}
    for yr, dinfo in sorted(drafts.items(), key=lambda x: x[0]):
        dname = dinfo.get("draft_name", f"Draft {yr}")
        for p in dinfo.get("picks", []):
            pid = str(p.get("player_id"))
            if not pid or pid == "None":
                continue
            rid = p.get("roster_id")
            f_info = franchises.get(rid, {})
            mgr = f_info.get("canonical_manager") or f"Manager {rid}"
            team_name = f_info.get("current_team") or f_info.get("current_team_name") or f"Team {rid}"
            entry = {
                "season": yr,
                "event_name": dname,
                "round": p.get("round"),
                "slot": p.get("draft_slot"),
                "pick_no": p.get("pick_no"),
                "roster_id": rid,
                "manager": mgr,
                "team_name": team_name,
            }
            drafts_by_player.setdefault(pid, []).append(entry)

    # -------------------------------------------------------------------------
    # 2. Index All Trades by Player ID
    # -------------------------------------------------------------------------
    # Format: player_id -> list of trade transactions (chronological)
    trades_by_player: Dict[str, List[Dict[str, Any]]] = {}
    flat_trades = []
    for yr, tlist in trades_raw.items():
        for t in tlist:
            t["trade_season"] = yr
            flat_trades.append(t)
    flat_trades.sort(key=lambda x: x.get("created", 0))

    for t in flat_trades:
        tx_id = str(t.get("transaction_id"))
        created_ts = t.get("created", 0)
        dt_str = datetime.fromtimestamp(created_ts / 1000, tz=BRT).strftime("%d/%m/%Y") if created_ts else t.get("trade_season")
        yr = t.get("trade_season", "2026")
        adds = t.get("adds") or {}
        drops = t.get("drops") or {}
        rids_in_trade = t.get("roster_ids") or []

        for pid, to_rid in adds.items():
            pid = str(pid)
            from_rid = drops.get(pid)
            if not from_rid:
                other_rids = [r for r in rids_in_trade if r != to_rid]
                from_rid = other_rids[0] if len(other_rids) == 1 else None

            to_f = franchises.get(to_rid, {})
            from_f = franchises.get(from_rid, {}) if from_rid else {}
            to_mgr = to_f.get("canonical_manager") or f"Team {to_rid}"
            from_mgr = from_f.get("canonical_manager") if from_rid else "Outro time"
            to_team = to_f.get("current_team") or to_f.get("current_team_name") or f"Team {to_rid}"
            from_team = from_f.get("current_team") or from_f.get("current_team_name") if from_rid else ""

            entry = {
                "transaction_id": tx_id,
                "season": yr,
                "date": dt_str,
                "timestamp": created_ts,
                "from_roster_id": from_rid,
                "from_manager": from_mgr,
                "from_team": from_team,
                "to_roster_id": to_rid,
                "to_manager": to_mgr,
                "to_team": to_team,
            }
            trades_by_player.setdefault(pid, []).append(entry)

    # -------------------------------------------------------------------------
    # 3. Index All Waivers and Free Agency Moves by Player ID
    # -------------------------------------------------------------------------
    # We maintain both successful moves (ADD/DROP) and contested outbids
    waivers_by_player: Dict[str, List[Dict[str, Any]]] = {}
    outbids_by_player: Dict[str, List[Dict[str, Any]]] = {}

    # Sort waivers chronologically (oldest to newest)
    sorted_waivers = sorted(waivers_txs, key=lambda x: x.get("created_ts", 0))

    for w in sorted_waivers:
        tx_id = str(w.get("transaction_id"))
        season = str(w.get("season", ""))
        week = w.get("week")
        date_str = w.get("date", "")
        created_ts = w.get("created_ts", 0)
        w_type = w.get("type", "waiver")
        status = w.get("status", "complete")
        rid = w.get("roster_id")
        f_info = franchises.get(rid, {})
        mgr = w.get("manager") or f_info.get("canonical_manager") or f"Team {rid}"
        team_name = w.get("team_name") or f_info.get("current_team_name") or f"Team {rid}"
        bid = w.get("bid", 0)
        tier = w.get("quality_tier", 2)
        tier_badge = w.get("tier_badge", "")

        if status == "complete":
            # Adds
            for a in w.get("adds", []):
                pid = str(a.get("player_id"))
                entry = {
                    "transaction_id": tx_id,
                    "season": season,
                    "week": week,
                    "date": date_str,
                    "timestamp": created_ts,
                    "action": "ADD",
                    "type": w_type,
                    "bid": bid,
                    "roster_id": rid,
                    "manager": mgr,
                    "team_name": team_name,
                    "quality_tier": tier,
                    "tier_badge": tier_badge,
                }
                waivers_by_player.setdefault(pid, []).append(entry)

            # Drops
            for d in w.get("drops", []):
                pid = str(d.get("player_id"))
                entry = {
                    "transaction_id": tx_id,
                    "season": season,
                    "week": week,
                    "date": date_str,
                    "timestamp": created_ts,
                    "action": "DROP",
                    "type": w_type,
                    "bid": bid,
                    "roster_id": rid,
                    "manager": mgr,
                    "team_name": team_name,
                    "quality_tier": tier,
                    "tier_badge": tier_badge,
                }
                waivers_by_player.setdefault(pid, []).append(entry)
        else:
            # Failed / Outbid attempt
            for a in w.get("adds", []):
                pid = str(a.get("player_id"))
                entry = {
                    "transaction_id": tx_id,
                    "season": season,
                    "week": week,
                    "date": date_str,
                    "timestamp": created_ts,
                    "bid": bid,
                    "roster_id": rid,
                    "manager": mgr,
                    "team_name": team_name,
                }
                outbids_by_player.setdefault(pid, []).append(entry)

    # -------------------------------------------------------------------------
    # 4. Index 2026 Rosters & Starter Roles
    # -------------------------------------------------------------------------
    roster_player_map: Dict[str, Dict[str, Any]] = {}
    for rid_str, t in rosters_data.items():
        rid = int(rid_str)
        mgr = t.get("canonical_manager")
        real_name = t.get("real_name", "")
        team_name = t.get("current_team")

        for s in t.get("starters", []):
            pid = str(s.get("player_id"))
            roster_player_map[pid] = {
                "roster_id": rid,
                "manager": mgr,
                "real_name": real_name,
                "team_name": team_name,
                "role": "starter",
                "slot": s.get("position"),
                "season_pts": s.get("season_pts", 0.0),
            }
        for pos, blist in t.get("bench_by_position", {}).items():
            for b in blist:
                pid = str(b.get("player_id"))
                roster_player_map[pid] = {
                    "roster_id": rid,
                    "manager": mgr,
                    "real_name": real_name,
                    "team_name": team_name,
                    "role": "bench",
                    "slot": pos,
                    "season_pts": b.get("season_pts", 0.0),
                }
        for taxi in t.get("taxi_squad", []):
            pid = str(taxi.get("player_id"))
            roster_player_map[pid] = {
                "roster_id": rid,
                "manager": mgr,
                "real_name": real_name,
                "team_name": team_name,
                "role": "taxi_squad",
                "slot": taxi.get("position"),
                "season_pts": taxi.get("season_pts", 0.0),
            }
        for ir in t.get("reserve_ir", []):
            pid = str(ir.get("player_id"))
            roster_player_map[pid] = {
                "roster_id": rid,
                "manager": mgr,
                "real_name": real_name,
                "team_name": team_name,
                "role": "reserve_ir",
                "slot": ir.get("position"),
                "season_pts": ir.get("season_pts", 0.0),
            }

    # -------------------------------------------------------------------------
    # 5. Collect All Unique Players Across the League
    # -------------------------------------------------------------------------
    all_player_ids = set()
    all_player_ids.update(drafts_by_player.keys())
    all_player_ids.update(trades_by_player.keys())
    all_player_ids.update(waivers_by_player.keys())
    all_player_ids.update(roster_player_map.keys())

    # Build Master Players Registry
    players_registry: Dict[str, Dict[str, Any]] = {}

    franchise_lifers: List[Dict[str, Any]] = []
    nomads_list: List[Dict[str, Any]] = []

    # Team DNA Stats tracking
    team_dna: Dict[int, Dict[str, Any]] = {
        rid: {
            "roster_id": rid,
            "manager": franchises[rid].get("canonical_manager", f"Manager {rid}"),
            "real_name": franchises[rid].get("real_name", ""),
            "team_name": franchises[rid].get("current_team") or franchises[rid].get("current_team_name", f"Team {rid}"),
            "total_players": 0,
            "drafted_count": 0,
            "trades_count": 0,
            "waivers_count": 0,
            "free_agents_count": 0,
            "lifers": [],
            "key_trade_assets": [],
            "key_waiver_assets": [],
        }
        for rid in franchises.keys()
    }

    for pid in sorted(all_player_ids):
        p_master = master_players.get(pid, {})
        p_name = p_master.get("name", f"Player #{pid}")
        pos = p_master.get("pos", "FLEX")
        team_nfl = p_master.get("team", "FA")
        status_nfl = _pdb.injury(p_master) or "Active"

        p_drafts = drafts_by_player.get(pid, [])
        p_trades = trades_by_player.get(pid, [])
        p_waivers_all = waivers_by_player.get(pid, [])
        p_outbids = outbids_by_player.get(pid, [])
        cur_roster_info = roster_player_map.get(pid)

        # ---------------------------------------------------------------------
        # A. Resolve Origin
        # ---------------------------------------------------------------------
        # If drafted, earliest draft pick is origin
        origin: Dict[str, Any] = {}
        if p_drafts:
            first_draft = p_drafts[0]
            origin = {
                "type": "draft",
                "season": first_draft["season"],
                "event_name": first_draft["event_name"],
                "round": first_draft["round"],
                "slot": first_draft["slot"],
                "pick_no": first_draft["pick_no"],
                "badge": f"[Draft {first_draft['season']} R{first_draft['round']}.P{first_draft['pick_no']}]",
                "original_roster_id": first_draft["roster_id"],
                "original_manager": first_draft["manager"],
                "original_team_name": first_draft["team_name"],
            }
        else:
            # Undrafted -> Earliest successful ADD in waivers/FA
            adds = [w for w in p_waivers_all if w["action"] == "ADD"]
            if adds:
                first_add = adds[0]
                is_fa = first_add["type"] == "free_agent" or first_add["bid"] == 0
                origin_type = "free_agent" if is_fa else "waiver"
                prefix = "FA" if is_fa else "Waiver"
                bid_str = f" ${first_add['bid']}" if not is_fa else ""
                origin = {
                    "type": origin_type,
                    "season": first_add["season"],
                    "week": first_add["week"],
                    "date": first_add["date"],
                    "bid": first_add["bid"],
                    "badge": f"[{prefix} {first_add['season']} W{first_add['week']}{bid_str}]",
                    "original_roster_id": first_add["roster_id"],
                    "original_manager": first_add["manager"],
                    "original_team_name": first_add["team_name"],
                }
            else:
                # Should not occur, but failsafe
                origin = {
                    "type": "unknown",
                    "badge": "[Origem Desconhecida]",
                    "original_manager": "Desconhecido",
                }

        # ---------------------------------------------------------------------
        # B. Construct Chronological Career Timeline
        # ---------------------------------------------------------------------
        timeline: List[Dict[str, Any]] = []

        # Add Drafts
        for d in p_drafts:
            timeline.append({
                "timestamp": int(datetime(int(d["season"]), 5, 1, tzinfo=BRT).timestamp()),
                "date": f"01/05/{d['season']}",
                "season": d["season"],
                "week": 0,
                "event_type": "draft_pick",
                "summary": f"Selecionado no {d['event_name']} (Rodada {d['round']}, Escolha #{d['pick_no']}) por *{d['manager']}* ({d['team_name']}).",
                "manager": d["manager"],
                "roster_id": d["roster_id"],
            })

        # Add Trades
        for t in p_trades:
            timeline.append({
                "timestamp": t.get("timestamp") or 0,
                "date": t.get("date", ""),
                "season": t.get("season", "2026"),
                "event_type": "trade",
                "summary": f"Trocado de *{t['from_manager']}* ({t['from_team']}) para *{t['to_manager']}* ({t['to_team']}).",
                "manager": t["to_manager"],
                "from_manager": t["from_manager"],
                "to_manager": t["to_manager"],
                "transaction_id": t["transaction_id"],
            })

        # Add Waivers & FA Moves
        for w in p_waivers_all:
            if w["action"] == "ADD":
                is_waiver = w["type"] == "waiver" and w["bid"] > 0
                summary = (
                    f"Reivindicado via Waiver por ${w['bid']} FAAB por *{w['manager']}* ({w['team_name']}) na Semana {w['week']} de {w['season']}."
                    if is_waiver
                    else f"Adicionado como Free Agent ($0 FAAB) por *{w['manager']}* ({w['team_name']}) na Semana {w['week']} de {w['season']}."
                )
            else:
                summary = f"Cortado / Dropado para a Waiver Wire por *{w['manager']}* ({w['team_name']}) na Semana {w['week']} de {w['season']}."

            timeline.append({
                "timestamp": w.get("timestamp") or 0,
                "date": w.get("date", ""),
                "season": w.get("season", ""),
                "week": w.get("week"),
                "event_type": f"waiver_{w['action'].lower()}",
                "summary": summary,
                "manager": w["manager"],
                "roster_id": w["roster_id"],
                "bid": w.get("bid", 0),
            })

        # Sort timeline chronologically
        timeline.sort(key=lambda x: (x.get("timestamp", 0), str(x.get("season", "")), x.get("week", 0)))

        # ---------------------------------------------------------------------
        # C. Current Status in 2026
        # ---------------------------------------------------------------------
        is_owned = cur_roster_info is not None
        current_status: Dict[str, Any] = {}

        if is_owned:
            cur_rid = cur_roster_info["roster_id"]
            cur_mgr = cur_roster_info["manager"]
            cur_team = cur_roster_info["team_name"]
            role = cur_roster_info["role"]
            slot = cur_roster_info["slot"]
            pts = cur_roster_info["season_pts"]

            # Determine acquisition type to CURRENT team
            # 1. Did current team draft him and he NEVER left?
            is_drafted_by_cur = p_drafts and p_drafts[0]["roster_id"] == cur_rid
            traded_away = any(t["from_roster_id"] == cur_rid for t in p_trades)
            dropped = any(w["action"] == "DROP" and w["roster_id"] == cur_rid for w in p_waivers_all)

            is_lifer = False
            acq_type = "draft"
            acq_detail = ""
            acq_badge = ""

            if is_drafted_by_cur and not traded_away and not dropped:
                acq_type = "draft"
                draft_yr = p_drafts[0]["season"]
                acq_detail = f"Draftado no {p_drafts[0]['event_name']} (R{p_drafts[0]['round']}.P{p_drafts[0]['pick_no']})"
                acq_badge = f"[Draft {draft_yr} R{p_drafts[0]['round']}.P{p_drafts[0]['pick_no']}]"
                if draft_yr == "2022":
                    is_lifer = True
                    franchise_lifers.append({
                        "player_id": pid,
                        "name": p_name,
                        "position": pos,
                        "team_nfl": team_nfl,
                        "manager": cur_mgr,
                        "team_name": cur_team,
                        "pick_no": p_drafts[0]["pick_no"],
                        "round": p_drafts[0]["round"],
                        "role": role,
                    })
            else:
                # Acquired via trade or waiver
                last_trade = None
                for t in p_trades:
                    if t["to_roster_id"] == cur_rid:
                        if not last_trade or (t.get("timestamp") or 0) > (last_trade.get("timestamp") or 0):
                            last_trade = t

                last_waiver_add = None
                for w in [w for w in p_waivers_all if w["action"] == "ADD"]:
                    if w["roster_id"] == cur_rid:
                        if not last_waiver_add or (w.get("timestamp") or 0) > (last_waiver_add.get("timestamp") or 0):
                            last_waiver_add = w

                t_time = last_trade.get("timestamp") if last_trade else -1
                w_time = last_waiver_add.get("timestamp") if last_waiver_add else -1

                if t_time > w_time and last_trade:
                    acq_type = "trade"
                    acq_detail = f"Adquirido via Troca de {last_trade['from_manager']} em {last_trade['date']}"
                    acq_badge = f"[Trade {last_trade['season']} via {last_trade['from_manager']}]"
                elif last_waiver_add:
                    if last_waiver_add["type"] == "waiver" and last_waiver_add["bid"] > 0:
                        acq_type = "waiver"
                        acq_detail = f"Adquirido na Waiver por ${last_waiver_add['bid']} FAAB em {last_waiver_add['date']}"
                        acq_badge = f"[Waiver {last_waiver_add['season']} W{last_waiver_add['week']} ${last_waiver_add['bid']}]"
                    else:
                        acq_type = "free_agent"
                        acq_detail = f"Adicionado como Free Agent ($0) em {last_waiver_add['date']}"
                        acq_badge = f"[FA {last_waiver_add['season']} W{last_waiver_add['week']}]"
                elif is_drafted_by_cur:
                    acq_type = "draft"
                    acq_detail = f"Draftado no {p_drafts[0]['event_name']}"
                    acq_badge = f"[Draft {p_drafts[0]['season']}]"
                else:
                    acq_type = "free_agent"
                    acq_detail = "Adicionado como Free Agent"
                    acq_badge = "[FA]"

            current_status = {
                "is_owned": True,
                "roster_id": cur_rid,
                "manager": cur_mgr,
                "real_name": cur_roster_info["real_name"],
                "team_name": cur_team,
                "role": role,
                "slot": slot,
                "season_pts": pts,
                "acquisition_type": acq_type,
                "acquisition_detail": acq_detail,
                "acquisition_badge": acq_badge,
                "is_franchise_lifer": is_lifer,
            }

            # Update team DNA
            t_dna = team_dna[cur_rid]
            t_dna["total_players"] += 1
            if acq_type == "draft":
                t_dna["drafted_count"] += 1
            elif acq_type == "trade":
                t_dna["trades_count"] += 1
                if role in ["starter", "bench"] and len(t_dna["key_trade_assets"]) < 6:
                    t_dna["key_trade_assets"].append(f"{p_name} ({pos} - {team_nfl})")
            elif acq_type == "waiver":
                t_dna["waivers_count"] += 1
                if len(t_dna["key_waiver_assets"]) < 6:
                    t_dna["key_waiver_assets"].append(f"{p_name} ({pos} - {team_nfl})")
            elif acq_type == "free_agent":
                t_dna["free_agents_count"] += 1
                if len(t_dna["key_waiver_assets"]) < 6:
                    t_dna["key_waiver_assets"].append(f"{p_name} ({pos} - {team_nfl})")

            if is_lifer:
                t_dna["lifers"].append(f"{p_name} ({pos}) [Pick #{p_drafts[0]['pick_no']}]")

        else:
            current_status = {
                "is_owned": False,
                "roster_id": None,
                "manager": None,
                "team_name": "Free Agent / Sem Time",
                "role": "unowned",
                "slot": None,
                "season_pts": 0.0,
                "acquisition_type": None,
                "acquisition_detail": "Atualmente livre no mercado ou fora da liga.",
                "acquisition_badge": "[Livre / FA]",
                "is_franchise_lifer": False,
            }

        # ---------------------------------------------------------------------
        # D. Market Dynamics Summary
        # ---------------------------------------------------------------------
        teams_held = list(dict.fromkeys(
            [d["manager"] for d in p_drafts] +
            [t["to_manager"] for t in p_trades] +
            [w["manager"] for w in p_waivers_all if w["action"] == "ADD"]
        ))
        total_faab_spent = sum(w.get("bid", 0) for w in p_waivers_all if w["action"] == "ADD")

        market_summary = {
            "total_drafts": len(p_drafts),
            "total_trades": len(p_trades),
            "total_waiver_adds": len([w for w in p_waivers_all if w["action"] == "ADD" and w["type"] == "waiver" and w["bid"] > 0]),
            "total_fa_adds": len([w for w in p_waivers_all if w["action"] == "ADD" and (w["type"] == "free_agent" or w["bid"] == 0)]),
            "total_drops": len([w for w in p_waivers_all if w["action"] == "DROP"]),
            "total_outbid_attempts": len(p_outbids),
            "total_faab_invested_league": total_faab_spent,
            "unique_franchises_held": len(teams_held),
            "teams_held": teams_held,
        }

        # Check if Nomad (held by 3+ teams or traded 2+ times)
        if len(p_trades) >= 2 or len(teams_held) >= 3:
            nomads_list.append({
                "player_id": pid,
                "name": p_name,
                "position": pos,
                "team_nfl": team_nfl,
                "total_trades": len(p_trades),
                "unique_teams": len(teams_held),
                "teams_held": teams_held,
                "current_team": current_status.get("manager") or "Sem time",
            })

        # Assemble full player record
        players_registry[pid] = {
            "player_id": pid,
            "name": p_name,
            "position": pos,
            "team_nfl": team_nfl,
            "status_nfl": status_nfl,
            "origin": origin,
            "current_status_2026": current_status,
            "market_summary": market_summary,
            "career_timeline": timeline,
            "contested_bids": p_outbids,
        }

    # -------------------------------------------------------------------------
    # 6. Finalize Team DNA Percentages
    # -------------------------------------------------------------------------
    for rid, d in team_dna.items():
        tot = d["total_players"] or 1
        d["drafted_pct"] = round((d["drafted_count"] / tot) * 100, 1)
        d["trades_pct"] = round((d["trades_count"] / tot) * 100, 1)
        d["waivers_fa_pct"] = round(((d["waivers_count"] + d["free_agents_count"]) / tot) * 100, 1)

    # Sort Nomads by number of trades and teams
    nomads_list.sort(key=lambda x: (x["total_trades"], x["unique_teams"]), reverse=True)
    franchise_lifers.sort(key=lambda x: x["pick_no"])

    # -------------------------------------------------------------------------
    # 7. Save Structured JSON Database
    # -------------------------------------------------------------------------
    output_registry = {
        "summary": {
            "total_unique_players": len(players_registry),
            "active_on_rosters_2026": len(roster_player_map),
            "unowned_or_historical": len(players_registry) - len(roster_player_map),
            "total_drafted_players": len(drafts_by_player),
            "total_traded_players": len(trades_by_player),
            "total_waiver_fa_players": len(waivers_by_player),
            "total_franchise_lifers": len(franchise_lifers),
            "last_updated": datetime.now(BRT).isoformat(),
        },
        "team_dna": team_dna,
        "franchise_lifers": franchise_lifers,
        "nomads": nomads_list,
        "players": players_registry,
    }

    out_json_path = os.path.join(KB_DIR, "players_registry.json")
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(output_registry, f, indent=2)


    # -------------------------------------------------------------------------
    # 8. Generate PLAYERS_DOSSIER.md
    # -------------------------------------------------------------------------
    generate_players_dossier_md(output_registry)

    # -------------------------------------------------------------------------
    # 9. Enrich and Update teams_rosters_analysis.json with Badges & DNA
    # -------------------------------------------------------------------------
    enrich_rosters_analysis_with_player_graph(players_registry, team_dna)

    print(f"✅ Master Players Knowledge Base compilada com sucesso!")
    print(f"   • Total de Atletas Catalogados: {len(players_registry)}")
    print(f"   • Ativos em 2026: {len(roster_player_map)} | Lifers: {len(franchise_lifers)} | Nômades: {len(nomads_list)}")
    return output_registry


def enrich_rosters_analysis_with_player_graph(players_registry: Dict[str, Any], team_dna: Dict[int, Any]):
    """Inject origin badges, lifers flags, and DNA summaries into teams_rosters_analysis."""
    rosters_path = os.path.join(KB_DIR, "teams_rosters_analysis.json")
    if not os.path.exists(rosters_path):
        rosters_path = os.path.join(BASE_DIR, "teams_rosters_analysis.json")

    with open(rosters_path, "r", encoding="utf-8") as f:
        rosters = json.load(f)

    for rid_str, team in rosters.items():
        rid = int(rid_str)
        dna = team_dna.get(rid, {})

        team["roster_dna"] = {
            "total_roster": dna.get("total_players", 0),
            "drafted_count": dna.get("drafted_count", 0),
            "drafted_pct": dna.get("drafted_pct", 0.0),
            "trades_count": dna.get("trades_count", 0),
            "trades_pct": dna.get("trades_pct", 0.0),
            "waivers_fa_count": dna.get("waivers_count", 0) + dna.get("free_agents_count", 0),
            "waivers_fa_pct": dna.get("waivers_fa_pct", 0.0),
            "lifers_count": len(dna.get("lifers", [])),
            "lifers": dna.get("lifers", []),
            "key_trade_assets": dna.get("key_trade_assets", []),
            "key_waiver_assets": dna.get("key_waiver_assets", []),
        }

        # Tag every player in starters, bench, taxi, ir
        def enrich_p(p):
            pid = str(p.get("player_id"))
            p_reg = players_registry.get(pid, {})
            cur_stat = p_reg.get("current_status_2026", {})
            orig = p_reg.get("origin", {})
            p["origin_badge"] = cur_stat.get("acquisition_badge") or orig.get("badge") or "[Desconhecido]"
            p["acquisition_type"] = cur_stat.get("acquisition_type", "draft")
            p["is_franchise_lifer"] = cur_stat.get("is_franchise_lifer", False)
            p["total_trades"] = p_reg.get("market_summary", {}).get("total_trades", 0)
            return p

        team["starters"] = [enrich_p(p) for p in team.get("starters", [])]
        for pos, blist in team.get("bench_by_position", {}).items():
            team["bench_by_position"][pos] = [enrich_p(p) for p in blist]
        team["taxi_squad"] = [enrich_p(p) for p in team.get("taxi_squad", [])]
        team["reserve_ir"] = [enrich_p(p) for p in team.get("reserve_ir", [])]

    # Save back
    with open(os.path.join(KB_DIR, "teams_rosters_analysis.json"), "w", encoding="utf-8") as f:
        json.dump(rosters, f, indent=2)

    # Re-render TEAMS_AND_ROSTERS_ANALYSIS.md with DNA and Badges
    render_teams_and_rosters_md(rosters)


def render_teams_and_rosters_md(teams_analysis: Dict[Any, Any]):
    """Render TEAMS_AND_ROSTERS_ANALYSIS.md directly from enriched teams_analysis."""
    md_lines = [
        "# 🛡️ Dossiê Completo de Elencos e Equipes: High Stakes Fantasy (2026)",
        "",
        "> [!NOTE]",
        "> Este arquivo mapeia a composição técnica dos 12 elencos da liga:",
        "> - Escalação titular ideal/provável (11 posições: 1 QB, 2 RB, 3 WR, 1 TE, 3 FLEX, 1 SUPER_FLEX)",
        "> - **Origem e Linhagem de Cada Atleta** (Draft, Troca ou Waiver/FA) com selo de 👑 *Franchise Lifer*",
        "> - **Roster DNA**: perfil estatístico de montagem de elenco de cada treinador",
        "> - Contexto obrigatório para análise de trocas e movimentações de waiver.",
        "",
        "---",
        "",
    ]

    for rid_key in sorted(teams_analysis.keys(), key=lambda x: int(x)):
        t = teams_analysis[rid_key]
        dna = t.get("roster_dna", {})
        md_lines.extend([
            f"## 🏈 {t['current_team']} — Gestor: *{t['canonical_manager']}* ({t.get('real_name', '')})",
            f"- **Divisão**: {t.get('division', '')} | **Campanha**: {t.get('record', '0-0-0')} ({t.get('points_for', 0.0)} PF) | **FAAB**: ${t.get('faab_remaining', 0)}",
            f"- **Status Competitivo**: *{t.get('competitive_status', '')}*",
            f"- **Total de Atletas**: {t.get('total_players', len(t.get('starters', [])))} (Elenco Principal) + {len(t.get('taxi_squad', []))} Taxi + {len(t.get('reserve_ir', []))} IR",
            "",
            "### 🧬 Roster DNA & Linhagem do Elenco:",
            f"- **Origem dos Atletas**: **{dna.get('drafted_count', 0)}** via Draft Próprio ({dna.get('drafted_pct', 0.0)}%) | **{dna.get('trades_count', 0)}** via Trocas ({dna.get('trades_pct', 0.0)}%) | **{dna.get('waivers_fa_count', 0)}** via Waivers/FA ({dna.get('waivers_fa_pct', 0.0)}%)",
        ])

        if dna.get("lifers"):
            md_lines.append(f"- **👑 Franchise Lifers (Fiéis desde 2022)**: {', '.join(dna['lifers'])}")
        if dna.get("key_trade_assets"):
            md_lines.append(f"- **🤝 Principais Ativos via Troca**: {', '.join(dna['key_trade_assets'][:4])}")
        if dna.get("key_waiver_assets"):
            md_lines.append(f"- **🗑️ Garimpados na Waiver/FA**: {', '.join(dna['key_waiver_assets'][:4])}")

        md_lines.extend([
            "",
            "### 🌟 Escalação Titular Provável / Ideal (11 Titulares):",
        ])

        for idx, s in enumerate(t.get("starters", []), 1):
            lifer_tag = " 👑 *Lifer*" if s.get("is_franchise_lifer") else ""
            badge = s.get("origin_badge", "")
            badge_str = f" ➔ *{badge}*{lifer_tag}" if badge else ""
            inj = s.get("injury_status") or "Active"
            md_lines.append(f"{idx}. **{s['name']}** ({s.get('position', '')} - {s.get('team', '')}) — _{inj}_ [{s.get('season_pts', 0.0):.2f} pts]{badge_str}")

        md_lines.extend([
            "",
            "### 📦 Profundidade de Banco e Reservas Imediatos:",
        ])
        for pos in ["QB", "RB", "WR", "TE"]:
            b_list = t.get("bench_by_position", {}).get(pos, [])
            if b_list:
                items_str = ", ".join([f"{b['name']} ({b.get('team', '')}) [{b.get('origin_badge', '')}]" for b in b_list])
                md_lines.append(f"- **{pos}s ({len(b_list)})**: {items_str}")
            else:
                md_lines.append(f"- **{pos}s**: _Sem reservas no banco_")

        if t.get("taxi_squad"):
            t_str = ", ".join([f"{p['name']} ({p.get('position', '')} - {p.get('team', '')}) [{p.get('origin_badge', '')}]" for p in t["taxi_squad"]])
            md_lines.append(f"- **Taxi Squad (Calouros/Stashes)**: {t_str}")

        if t.get("reserve_ir"):
            r_str = ", ".join([f"{p['name']} ({p.get('position', '')} - {p.get('team', '')}) [{p.get('origin_badge', '')}]" for p in t["reserve_ir"]])
            md_lines.append(f"- **Reserva por Lesão (IR)**: {r_str}")

        md_lines.extend([
            "",
            "### 🎯 Diagnóstico Técnico & Dinâmica de Mercado:",
            f"- **Pilares / Estrelas**: {', '.join([p['name'] + ' (' + p.get('position', '') + ')' for p in t.get('core_stars', [])])}",
            f"- **Pontos Fortes**: {'; '.join(t.get('strengths', [])) if t.get('strengths') else 'Elenco equilibrado'}",
            f"- **Carências Críticas**: {'; '.join(t.get('weaknesses', [])) if t.get('weaknesses') else 'Sem carências graves imediatas'}",
            f"- **Ativos Negociáveis / Excedentes de Banco**: {', '.join(t.get('surplus_assets', [])) if t.get('surplus_assets') else 'Sem excedentes óbvios (elenco enxuto)'}",
            "",
            "---",
            "",
        ])

    md_content = "\n".join(md_lines)
    with open(os.path.join(KB_DIR, "TEAMS_AND_ROSTERS_ANALYSIS.md"), "w", encoding="utf-8") as f:
        f.write(md_content)


def generate_players_dossier_md(data: Dict[str, Any]):
    """Generate PLAYERS_DOSSIER.md with high-density insights."""
    summary = data["summary"]
    team_dna = data["team_dna"]
    lifers = data["franchise_lifers"]
    nomads = data["nomads"]

    lines = [
        "# 🏈 Dossiê de Atletas & Árvore Genealógica da High Stakes Fantasy (2022–2026)",
        "",
        "> **Base de Conhecimento Relacional**: Registro canônico de linhagem, origem, histórico de mercado e status dos **976 atletas** que já pisaram na liga.",
        "",
        "---",
        "",
        "## 📊 1. Censo Geral de Atletas da Liga",
        "",
        f"- **Universo Total de Atletas**: **{summary['total_unique_players']} jogadores catalogados**",
        f"- **Ativos nos Elencos de 2026**: **{summary['active_on_rosters_2026']} atletas** (12 franquias)",
        f"- **Ex-Jogadores / Cortados / Free Agents**: **{summary['unowned_or_historical']} atletas**",
        f"- **Atletas Selecionados em Drafts**: **{summary['total_drafted_players']} atletas** (Startup 2022 + 4 Rookie Drafts)",
        f"- **Atletas que já Passaram por Trocas**: **{summary['total_traded_players']} atletas**",
        f"- **Atletas Envolvidos em Waivers/FA**: **{summary['total_waiver_fa_players']} atletas**",
        f"- **Franchise Lifers (Patrimônio Histórico)**: **{summary['total_franchise_lifers']} atletas** fiéis desde o Draft Inaugural de 2022",
        "",
        "---",
        "",
        "## 🧬 2. Roster DNA: Como Cada Franquia Constrói seu Elenco (2026)",
        "",
        "| Franquia | Treinador | Elenco Total | 🎓 Draft Próprio | 🤝 Via Trocas | 🗑️ Waivers / FA | 👑 Lifers 2022 |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :---: |",
    ]

    for rid in sorted(team_dna.keys()):
        d = team_dna[rid]
        lines.append(
            f"| **{d['team_name']}** | {d['manager']} | {d['total_players']} | "
            f"**{d['drafted_count']}** ({d['drafted_pct']}%) | "
            f"**{d['trades_count']}** ({d['trades_pct']}%) | "
            f"**{d['waivers_count'] + d['free_agents_count']}** ({d['waivers_fa_pct']}%) | "
            f"{len(d['lifers'])} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 👑 3. Franchise Lifers: Os Guardiões das Franquias",
        "",
        "Atletas selecionados no **Startup Draft Inaugural de 2022** que **NUNCA** vestiram outra camisa nem foram dropados em 4 anos:",
        "",
    ])

    current_mgr = None
    for l in lifers:
        if l["manager"] != current_mgr:
            current_mgr = l["manager"]
            lines.append(f"### • *{current_mgr}* ({l['team_name']})")
        lines.append(f"- **{l['name']}** ({l['position']} - {l['team_nfl']}) — Escolha Geral #{l['pick_no']} (Rodada {l['round']}) | Função Atual: *{l['role'].title()}*")

    lines.extend([
        "",
        "---",
        "",
        "## 🌪️ 4. Nômades da Liga: Os Jogadores Mais Negociados da História",
        "",
        "Atletas que mais rodaram de time através de trocas oficiais e reinvindicações de mercado:",
        "",
        "| Atleta | Posição | NFL | Trocas Oficiais | Times Diferentes | Trajetória na Liga | Time Atual (2026) |",
        "| :--- | :---: | :---: | :---: | :---: | :--- | :--- |",
    ])

    for n in nomads[:20]:
        journey = " ➔ ".join(n["teams_held"][:4])
        if len(n["teams_held"]) > 4:
            journey += f" (+{len(n['teams_held'])-4})"
        lines.append(
            f"| **{n['name']}** | {n['position']} | {n['team_nfl']} | {n['total_trades']} | {n['unique_teams']} | {journey} | *{n['current_team']}* |"
        )

    lines.extend([
        "",
        "---",
        "",
        "## 🔍 5. Como Consultar Qualquer Atleta no Bot",
        "",
        "Você pode consultar o raio-x histórico completo de qualquer jogador via CLI ou perguntando ao Mr. Irrelevant:",
        "```bash",
        "# Consultar um atleta específico pelo nome",
        "python3 sleeper_cli.py player \"Brock Purdy\"",
        "python3 sleeper_cli.py player \"Philip Rivers\"",
        "python3 sleeper_cli.py player \"Josh Allen\"",
        "",
        "# Consultar a certidão de DNA de um time específico",
        "python3 sleeper_cli.py roster-dna panda",
        "python3 sleeper_cli.py roster-dna moc",
        "```",
        "",
        "---",
        f"*Dossiê compilado e auditado automaticamente em {datetime.now(BRT).strftime('%d/%m/%Y às %H:%M')} (Horário de Brasília).*",
    ])

    md_content = "\n".join(lines)
    with open(os.path.join(KB_DIR, "PLAYERS_DOSSIER.md"), "w", encoding="utf-8") as f:
        f.write(md_content)


if __name__ == "__main__":
    build_players_knowledge_base()
