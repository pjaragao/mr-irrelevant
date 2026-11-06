"""SQL-backed readers for the league history (WhatsApp-formatted text). Used by KnowledgeBaseService.

Everything here reads `league.db` (see kb_db.py): filters (ano/semana/manager/jogador) are applied in SQL and the
header always says "N de TOTAL", so the model never has to count or guess what was left out.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import kb_db
import league_ctx

week_label = kb_db.week_label

LINE = "━━━━━━━━━━━━━━━━━━━━━━"
BLOCKBUSTER_WORDS = {"blockbuster", "blockbusters", "impact", "polemicas", "top", "maiores"}
PICK_WORDS = {"picks", "escolhas", "draft"}
USED_WORDS = {"convertidas", "convertida", "virou"}


# --------------------------------------------------------------------------- helpers
def _seasons(con: sqlite3.Connection) -> List[str]:
    """Every season on record (games, trades, drafts, waivers), oldest first — never a hard-coded list."""
    rows = con.execute("SELECT season FROM games UNION SELECT season FROM trades UNION SELECT season FROM draft_picks "
                       "UNION SELECT season FROM waiver_moves").fetchall()
    return sorted({str(r[0]) for r in rows if r[0]})


def _first_season(con: sqlite3.Connection) -> str:
    s = _seasons(con)
    return s[0] if s else ""


def _league_name() -> str:
    import league_ctx
    return league_ctx.current().name


def _persona() -> str:
    import league_ctx
    return league_ctx.current().persona_name


def resolve_roster(con: sqlite3.Connection, query: Any) -> Optional[int]:
    """Any name/nickname/team/username -> roster_id (people registry first, then every known username)."""
    q = str(query or "").strip()
    if not q:
        return None
    try:
        import people
        rid = people.resolve(q)
        if rid is not None:
            return int(rid)
    except Exception:
        pass
    row = con.execute("SELECT roster_id FROM manager_names WHERE name_lc=?", (q.lower().lstrip("@"),)).fetchone()
    return int(row[0]) if row else None


def manager_name(con: sqlite3.Connection, rid: Optional[int]) -> str:
    row = con.execute("SELECT manager FROM franchises WHERE roster_id=?", (rid,)).fetchone() if rid else None
    return row[0] if row else (f"Roster {rid}" if rid else "?")


def _dmy(iso: str) -> str:
    y, m, d = (iso or "1970-01-01").split("-")
    return f"{d}/{m}/{y}"


def _in(values: List[Any]) -> Tuple[str, List[Any]]:
    return ",".join("?" * len(values)), list(values)


# --------------------------------------------------------------------------- trades
def _asset_text(a: sqlite3.Row) -> str:
    if a["kind"] == "pick":
        t = f"Pick R{a['pick_round']} {a['pick_year']}" + (f" via {a['pick_via']}" if a["pick_via"] else "")
        if a["pick_status"] == "used" and a["used_player"]:
            t += f" 🎯 virou #{a['used_no']} {a['used_player']}"
        elif a["pick_status"] == "future":
            t += " ⏳"
        return t
    return a["label"]


def trade_lines(con: sqlite3.Connection, seq: int, detail: bool = False) -> List[str]:
    sides = con.execute("SELECT * FROM trade_sides WHERE trade_seq=? ORDER BY rowid", (seq,)).fetchall()
    out = []
    for s in sides:
        assets = con.execute("SELECT * FROM trade_assets WHERE trade_seq=? AND roster_id IS ? ORDER BY id",
                             (seq, s["roster_id"])).fetchall()
        items = [_asset_text(a) for a in assets] or ["(nada registrado)"]
        if detail:
            out.append(f"  • *{s['manager']}* ({s['team']}) recebeu:")
            out += [f"      - {i}" for i in items]
        else:
            out.append(f"*{s['manager']}* ← " + " + ".join(items))
    return out


def format_trade(con: sqlite3.Connection, t: sqlite3.Row, detail: bool = False) -> str:
    head = f"#{t['seq']} · {_dmy(t['date'])} ({t['season']} {week_label(t['week'])})"
    if detail:
        return "\n".join([f"🤝 *Troca {head}*"] + trade_lines(con, t["seq"], True))
    return f"• *{head}*: " + " | ".join(trade_lines(con, t["seq"]))


def trades_report(con: sqlite3.Connection, query: Optional[str] = None, limit: int = 12, ano: Optional[str] = None,
                  semana: Optional[int] = None, detalhe: bool = False, contra: Optional[str] = None) -> str:
    """`contra` = a second manager the trade must also involve (query=A, contra=B -> trades between A and B)."""
    total = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    if not total:
        return "❌ Base histórica de trocas não encontrada."
    contra_rid = None
    if str(contra or "").strip():
        contra_rid = resolve_roster(con, contra)
        if not contra_rid:
            return f"❌ Treinador '{contra}' não encontrado."
    where, params, order, titles = [], [], "t.seq DESC", []
    main_rid = None
    q = (query or "").strip()
    ql = q.lower()
    if ql.isdigit() and len(ql) == 4 and not ano:
        ano, q, ql = ql, "", ""
    if ano:
        where.append("t.season=?"); params.append(str(ano)); titles.append(f"temporada {ano}")
    if semana not in (None, ""):
        where.append("t.week=?"); params.append(int(semana)); titles.append("offseason" if int(semana) == 0 else f"semana {semana}")
    if ql in BLOCKBUSTER_WORDS:
        where.append("t.blockbuster=1"); order = "t.impact DESC, t.seq DESC"; titles.append("maior impacto (blockbusters)")
    elif ql in PICK_WORDS:
        where.append("t.n_picks>0"); titles.append("com picks de draft")
    elif ql in USED_WORDS:
        where.append("EXISTS (SELECT 1 FROM trade_assets a WHERE a.trade_seq=t.seq AND a.pick_status='used')")
        titles.append("com picks já usadas no draft")
    elif q:
        rid = resolve_roster(con, q)
        if rid:
            main_rid = rid
            where.append("EXISTS (SELECT 1 FROM trade_sides s WHERE s.trade_seq=t.seq AND s.roster_id=?)")
            params.append(rid); titles.append(f"envolvendo {manager_name(con, rid)}")
        else:
            like = f"%{ql}%"
            where.append("(EXISTS (SELECT 1 FROM trade_assets a WHERE a.trade_seq=t.seq AND (lower(a.label) LIKE ? "
                         "OR lower(coalesce(a.used_player,'')) LIKE ?)) OR EXISTS (SELECT 1 FROM trade_sides s "
                         "WHERE s.trade_seq=t.seq AND (lower(s.manager) LIKE ? OR lower(s.team) LIKE ?)))")
            params += [like] * 4; titles.append(f"busca '{q}'")
    if contra_rid:
        where.append("EXISTS (SELECT 1 FROM trade_sides s WHERE s.trade_seq=t.seq AND s.roster_id=?)")
        params.append(contra_rid)
        if main_rid:  # replaces the "envolvendo A" title added just above
            titles[-1] = f"entre {manager_name(con, main_rid)} e {manager_name(con, contra_rid)}"
        else:
            titles.append(f"envolvendo {manager_name(con, contra_rid)}")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    matched = con.execute(f"SELECT COUNT(*) FROM trades t {sql_where}", params).fetchone()[0]
    title = " · ".join(titles) or "todas as trocas"
    if not matched:
        return f"ℹ️ Nenhuma troca encontrada ({title}) entre as {total} da história da liga."
    n = limit if limit and limit > 0 else matched
    rows = con.execute(f"SELECT t.* FROM trades t {sql_where} ORDER BY {order} LIMIT ?", params + [n]).fetchall()
    lines = [f"🔄 *TROCAS — {title.upper()}*",
             f"{matched} de {total} trocas na história da liga ({_first_season(con)}–hoje); mostrando {len(rows)}"
             + (" (mais recentes primeiro)" if order.startswith("t.seq") else "") + ":", LINE]
    sep = "\n──────────────────────\n" if detalhe else "\n"
    lines.append(sep.join(format_trade(con, t, detalhe) for t in rows))
    if matched > len(rows):
        lines.append(f"… faltam {matched - len(rows)}: filtre por `ano`/`semana`/manager ou aumente `limite`. "
                     "Para contar/comparar períodos use `estatisticas` (não conte linhas).")
    return "\n".join(lines)


def trade_picks_report(con: sqlite3.Connection, manager_query: str) -> str:
    rid = resolve_roster(con, manager_query)
    if not rid:
        return f"❌ Treinador '{manager_query}' não encontrado."
    name = manager_name(con, rid)
    base = ("FROM trades t WHERE t.n_picks>0 AND EXISTS (SELECT 1 FROM trade_sides s "
            "WHERE s.trade_seq=t.seq AND s.roster_id=?)")
    total = con.execute(f"SELECT COUNT(*) {base}", (rid,)).fetchone()[0]
    if not total:
        return f"ℹ️ Nenhuma troca envolvendo escolhas de draft encontrada para '{manager_query}'."
    rows = con.execute(f"SELECT t.* {base} ORDER BY t.seq DESC LIMIT 10", (rid,)).fetchall()
    lines = [f"🔄 *TROCAS DE PICKS: {name.upper()}*",
             f"{total} trocas com escolhas de draft (mostrando as {len(rows)} mais recentes):", LINE]
    lines += [format_trade(con, t) for t in rows]
    return "\n".join(lines)


# --------------------------------------------------------------------------- players
def player_report(con: sqlite3.Connection, query: str, current_owner: Optional[str] = None) -> str:
    """Career of an athlete inside THIS league from the index alone (no curated registry needed): where he came from
    (draft pick), every trade and waiver move he was part of, and who owns him today (`current_owner`, live)."""
    q = (query or "").strip().lower()
    if not q:
        return "❌ Informe o nome do atleta."
    like = f"%{q}%"
    names: Dict[str, int] = {}
    for sql in ("SELECT player_name FROM draft_picks WHERE lower(player_name) LIKE ?",
                "SELECT player_name FROM trade_assets WHERE kind='player' AND lower(player_name) LIKE ?",
                "SELECT add_name FROM waiver_moves WHERE lower(add_name) LIKE ?",
                "SELECT drop_name FROM waiver_moves WHERE lower(drop_name) LIKE ?"):
        for (n,) in con.execute(sql, (like,)):
            if n:
                names[n] = names.get(n, 0) + 1
    if not names:
        return (f"❌ Nenhum registro de '{query}' no histórico desta liga (draft, trocas ou waivers). "
                "Confira a grafia ou tente o sobrenome.")
    name = max(names, key=lambda n: (n.lower() == q, names[n]))
    others = [n for n in sorted(names, key=names.get, reverse=True) if n != name][:3]
    lines = [f"🧬 *DOSSIÊ NA LIGA: {name.upper()}*", LINE]
    for d in con.execute("SELECT * FROM draft_picks WHERE player_name=? ORDER BY season", (name,)):
        via = f" (pick de {manager_name(con, d['orig_roster_id'])})" if d["via_trade"] else ""
        lines.append(f"📥 *Draft {d['season']}*: R{d['round']} #{d['pick_no']} — escolhido por *{d['manager']}*{via}")
    trades = con.execute("""SELECT t.seq, t.date, t.season, t.week, a.roster_id FROM trade_assets a
                            JOIN trades t ON t.seq = a.trade_seq WHERE a.kind='player' AND a.player_name=?
                            ORDER BY t.seq""", (name,)).fetchall()
    if trades:
        lines.append("\n🔄 *Trocas:*")
        for t in trades:
            others_s = [r["manager"] for r in con.execute(
                "SELECT manager FROM trade_sides WHERE trade_seq=? AND roster_id IS NOT ?", (t["seq"], t["roster_id"]))]
            lines.append(f"• {_dmy(t['date'])} ({t['season']} {week_label(t['week'])}): *{manager_name(con, t['roster_id'])}* recebeu"
                         + (f" (de {', '.join(others_s)})" if others_s else ""))
    moves = con.execute("""SELECT * FROM waiver_moves WHERE (add_name=? OR drop_name=?) AND status='complete'
                           ORDER BY ts""", (name, name)).fetchall()
    if moves:
        lines.append("\n💰 *Waivers / free agency:*")
        for m in moves[-8:]:
            what = "contratado" if m["add_name"] == name else "dispensado"
            bid = f" por ${m['bid']}" if m["add_name"] == name and m["bid"] else ""
            lines.append(f"• {m['date']} ({m['season']} {week_label(m['week'])}): {what} por *{m['manager']}*{bid}")
    if current_owner:
        lines.append(f"\n📍 *Hoje:* no elenco de *{current_owner}*")
    if others:
        lines.append(f"\n_(Também encontrei: {', '.join(others)})_")
    return "\n".join(lines)


# --------------------------------------------------------------------------- draft
def draft_picks_report(con: sqlite3.Connection, year: str) -> str:
    y = str(year).strip()
    n = con.execute("SELECT COUNT(*) FROM draft_picks WHERE season=?", (y,)).fetchone()[0]
    if not n:
        return f"❌ Dados do Draft {year} não encontrados."
    name = con.execute("SELECT draft_name FROM draft_picks WHERE season=? LIMIT 1", (y,)).fetchone()[0]
    # A league's first draft is a startup draft (many rounds); later ones are rookie drafts of `draft_rounds`.
    last_round = con.execute("SELECT MAX(round) FROM draft_picks WHERE season=?", (y,)).fetchone()[0] or 1
    import league_ctx
    rookie_rounds = league_ctx.current().profile.draft_rounds or 5
    startup = y == _first_season(con) and last_round > rookie_rounds
    rounds = [1] if startup else list(range(1, last_round + 1))
    lines = [f"🎯 *RAIO-X DO DRAFT {y} — {name or _league_name().upper()}* ({n} escolhas)", LINE]
    for rnd in rounds:
        rows = con.execute("SELECT * FROM draft_picks WHERE season=? AND round=? ORDER BY pick_no", (y, rnd)).fetchall()
        if not rows:
            continue
        lines.append(f"\n*RODADA {rnd}:*")
        for p in rows:
            via = ""
            if p["via_trade"]:
                via = f" [pick de {manager_name(con, p['orig_roster_id'])}]"
            lines.append(f"• #{p['pick_no']}: *{p['manager']}* ➡️ *{p['player_name']}* "
                         f"({p['pos']} - {p['nfl_team']}){via}")
    if startup:
        lines.append(f"\n_Startup de {last_round} rodadas: mostrando a 1ª; use `draft_historico alvo` para as picks de um manager._")
    return "\n".join(lines)


def manager_draft_report(con: sqlite3.Connection, manager_query: str) -> str:
    rid = resolve_roster(con, manager_query)
    if not rid:
        return f"❌ Nenhuma escolha encontrada para o manager '{manager_query}'."
    rows = con.execute("SELECT * FROM draft_picks WHERE roster_id=? ORDER BY season, pick_no", (rid,)).fetchall()
    if not rows:
        return f"❌ Nenhuma escolha encontrada para o manager '{manager_query}'."
    lines = [f"📋 *TODAS AS ESCOLHAS DE DRAFT: {manager_name(con, rid).upper()}* ({len(rows)} picks)", LINE]
    cur = None
    for p in rows:
        if p["season"] != cur:
            cur = p["season"]
            lines.append(f"\n🏈 *{p['draft_name'] or cur}* ({cur}):")
        via = f" — pick de {manager_name(con, p['orig_roster_id'])}" if p["via_trade"] else ""
        lines.append(f"  • #{p['pick_no']} (R{p['round']}): *{p['player_name']}* ({p['pos']} - {p['nfl_team']}){via}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- records
def records_report(con: sqlite3.Connection) -> str:
    if not con.execute("SELECT COUNT(*) FROM games").fetchone()[0]:
        return "❌ Base de recordes históricos não encontrada."
    lines = [f"🏆 *LIVRO DOS RECORDES HISTÓRICOS — {_league_name().upper()}*", LINE, "\n*MAIORES PONTUAÇÕES EM UMA RODADA (TOP 20)*"]
    for i, g in enumerate(con.execute("SELECT * FROM games WHERE pf>0 ORDER BY pf DESC LIMIT 20"), 1):
        lines.append(f"• #{i} *{g['manager']}*: *{g['pf']:.2f} pts* [{g['season']} S{g['week']}]")
    lines.append("\n*MENORES PONTUAÇÕES (TOP 10 FIASCOS)*")
    for i, g in enumerate(con.execute("SELECT * FROM games WHERE pf>0 ORDER BY pf ASC LIMIT 10"), 1):
        lines.append(f"• #{i} *{g['manager']}*: *{g['pf']:.2f} pts* [{g['season']} S{g['week']}]")
    lines.append("\n*MAIORES GOLEADAS (TOP 10)*")
    for i, g in enumerate(con.execute("SELECT * FROM games WHERE result='W' AND pa>0 ORDER BY pf-pa DESC LIMIT 10"), 1):
        lines.append(f"• #{i} *{g['manager']}* esmagou {g['opp_manager']}: {g['pa']:.2f} x {g['pf']:.2f} "
                     f"(*+{g['pf'] - g['pa']:.2f}*) [{g['season']} S{g['week']}]")
    lines += ["", LINE, f"🎙️ _{_persona()}: Recordes servem pra exaltar os gênios e humilhar eternamente os micos!_"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- waivers / free agents
STEAL_WORDS = {"steals", "top", "tier5", "ouro", "achados", "melhores", "best"}
FIASCO_WORDS = {"fiascos", "queimas", "tier0", "bagres", "piores", "desperdicios", "lixo", "worst"}
BID_WORDS = {"bids", "maiores_lances", "gastos", "faab", "maiores_bids"}
OVERVIEW_WORDS = {"", "geral", "resumo", "overview", "mercado", "all"}


def _wlabel(m: sqlite3.Row) -> str:
    return m["add_label"] or m["add_name"] or "Atleta"


def _season_clause(season: Optional[str]) -> Tuple[str, List[Any]]:
    return ("AND season=?", [season]) if season else ("", [])


def _top_waivers(con, where: str, order: str, limit: int, season: Optional[str]):
    sc, sp = _season_clause(season)
    return con.execute(f"SELECT * FROM waiver_moves WHERE {where} {sc} ORDER BY {order} LIMIT ?", sp + [limit]).fetchall()


def waivers_report(con: sqlite3.Connection, query: str = "", limit: int = 12, ano: Optional[str] = None) -> str:
    if not con.execute("SELECT COUNT(*) FROM waiver_moves").fetchone()[0]:
        return "❌ Base de waivers não encontrada."
    q = (query or "").strip().lower()
    season = str(ano) if ano else None
    if q in set(_seasons(con)) and not season:
        season, q = q, ""
    suffix = f" — TEMPORADA {season}" if season else " DA HISTÓRIA"

    if q in STEAL_WORDS:
        rows = _top_waivers(con, "status='complete' AND tier=5", "bid ASC, ts DESC", limit, season)
        lines = [f"💎 *TOP {len(rows)} STEALS{suffix} (TIER 5 — OURO NO LIXO)*", LINE]
        for i, m in enumerate(rows, 1):
            lines.append(f"{i}. *{_wlabel(m)}* ➔ *{m['manager']}* ({m['team']}) | *${m['bid']} FAAB* "
                         f"({m['season']} {week_label(m['week'])})\n   _{m['reason']}_")
        return "\n".join(lines + [LINE])
    if q in FIASCO_WORDS:
        rows = _top_waivers(con, "status='complete' AND tier=0", "bid DESC", limit, season)
        lines = [f"🗑️ *TOP {len(rows)} PIORES QUEIMAS DE FAAB{suffix} (TIER 0)*", LINE]
        for i, m in enumerate(rows, 1):
            drop = f" (dropou {m['drop_name']})" if m["drop_name"] else ""
            lines.append(f"{i}. *{_wlabel(m)}* ➔ *{m['manager']}* ({m['team']}) | *${m['bid']} FAAB* "
                         f"({m['season']} {week_label(m['week'])}){drop}\n   _{m['reason']}_")
        return "\n".join(lines + [LINE])
    if q in BID_WORDS:
        rows = _top_waivers(con, "status='complete' AND bid>0", "bid DESC", limit, season)
        lines = [f"💰 *TOP {len(rows)} MAIORES LANCES DE FAAB{suffix}*", LINE]
        for i, m in enumerate(rows, 1):
            lines.append(f"{i}. *${m['bid']} FAAB*: *{_wlabel(m)}* ➔ *{m['manager']}* ({m['team']}) — "
                         f"_{m['season']} {week_label(m['week'])}_ [{m['tier_badge']}]")
        return "\n".join(lines + [LINE])
    if q in OVERVIEW_WORDS:
        return _waivers_overview(con, season)

    rid = resolve_roster(con, q)
    if rid:
        return _waivers_manager(con, rid, season, limit)

    like = f"%{q}%"
    sc, sp = _season_clause(season)
    rows = con.execute(f"SELECT * FROM waiver_moves WHERE (lower(coalesce(add_name,'')) LIKE ? OR "
                       f"lower(coalesce(drop_name,'')) LIKE ?) {sc} ORDER BY ts DESC", [like, like] + sp).fetchall()
    if not rows:
        return (f"❌ Nenhuma movimentação ou treinador encontrado para '{query}'. Tente 'steals', 'fiascos', 'bids', "
                "uma temporada ('2025'), o nome do manager ou de um jogador.")
    lines = [f"🔍 *WAIVERS/FA: '{query.upper()}'*", f"{len(rows)} movimentações na história da liga (mostrando {min(limit, len(rows))}):", LINE]
    for m in rows[:limit]:
        drop = f" (dropou {m['drop_name']})" if m["drop_name"] else ""
        bid = f"${m['bid']} FAAB" if m["bid"] else "Free"
        status = "✅ Venceu" if m["status"] == "complete" else "❌ Falhou/Superado"
        lines.append(f"• {m['date']} ({m['season']} {week_label(m['week'])}) [{m['type'].upper()} - {status}]\n"
                     f"  *{m['manager']}* ({m['team']}) levou *{_wlabel(m)}* por *{bid}*{drop} [{m['tier_badge']}]")
    return "\n".join(lines + [LINE])


def _waivers_overview(con, season: Optional[str]) -> str:
    sc, sp = _season_clause(season)
    one = lambda sql, p=(): con.execute(sql, list(p) + sp).fetchone()[0]  # noqa: E731
    total = one(f"SELECT COUNT(*) FROM waiver_moves WHERE 1=1 {sc}")
    w_total = one(f"SELECT COUNT(*) FROM waiver_moves WHERE type='waiver' {sc}")
    w_ok = one(f"SELECT COUNT(*) FROM waiver_moves WHERE type='waiver' AND status='complete' {sc}")
    fa = one(f"SELECT COUNT(*) FROM waiver_moves WHERE type='free_agent' AND status='complete' {sc}")
    faab = one(f"SELECT COALESCE(SUM(bid),0) FROM waiver_moves WHERE status='complete' {sc}")
    title = f"TEMPORADA {season}" if season else f"{_first_season(con)}–HOJE"
    lines = [f"📡 *RAIO-X DO MERCADO DE WAIVERS & FA ({title})*", LINE,
             f"• *Volume*: {total:,} movimentações registradas",
             f"• *Waivers processadas*: {w_total:,} ({w_ok:,} concluídas | {w_total - w_ok:,} lances perdidos)",
             f"• *Free agents adicionados*: {fa:,}", f"• *FAAB queimado*: ${faab:,}", "",
             "🏆 *MAIS VICIADOS EM WAIVER (adições concluídas):*"]
    rows = con.execute(f"""SELECT manager, team, SUM(status='complete') adds, SUM(type='waiver' AND status='failed') lost,
                           SUM(CASE WHEN status='complete' THEN bid ELSE 0 END) faab FROM waiver_moves
                           WHERE 1=1 {sc} GROUP BY roster_id ORDER BY adds DESC LIMIT 3""", sp).fetchall()
    lines += [f"{i}. *{r['manager']}* ({r['team']}): {r['adds']} adições | {r['lost']} lances perdidos (${r['faab']} FAAB)"
              for i, r in enumerate(rows, 1)]
    for title_, where, order, icon in (("TOP 3 STEALS (Tier 5)", "status='complete' AND tier=5", "bid ASC, ts DESC", "💎"),
                                       ("TOP 3 QUEIMAS DE FAAB (Tier 0)", "status='complete' AND tier=0", "bid DESC", "🗑️")):
        lines += ["", f"{icon} *{title_}:*"]
        lines += [f"{i}. *{_wlabel(m)}* ➔ {m['manager']} (${m['bid']} FAAB, {m['season']} {week_label(m['week'])})"
                  for i, m in enumerate(_top_waivers(con, where, order, 3, season), 1)]
    lines += ["", LINE, "💡 Filtros: `steals` · `fiascos` · `bids` · nome do manager · nome do jogador · ano"]
    return "\n".join(lines)


def _waivers_manager(con, rid: int, season: Optional[str], limit: int) -> str:
    sc, sp = _season_clause(season)
    f = con.execute("SELECT * FROM franchises WHERE roster_id=?", (rid,)).fetchone()
    r = con.execute(f"""SELECT SUM(type='waiver' AND status='complete') won, SUM(type='waiver' AND status='failed') lost,
                        SUM(type='free_agent' AND status='complete') fa,
                        SUM(CASE WHEN status='complete' THEN bid ELSE 0 END) faab FROM waiver_moves
                        WHERE roster_id=? {sc}""", [rid] + sp).fetchone()
    won, lost, fa, faab = (r[0] or 0), (r[1] or 0), (r[2] or 0), (r[3] or 0)
    tiers = {t: n for t, n in con.execute(f"SELECT tier, COUNT(*) FROM waiver_moves WHERE roster_id=? AND status='complete' "
                                          f"{sc} GROUP BY tier", [rid] + sp)}
    rate = f"{won / (won + lost) * 100:.1f}" if won + lost else "0.0"
    avg = f"{faab / won:.1f}" if won else "0.0"
    where_season = f" ({season})" if season else ""
    lines = [f"🏈 *PERFIL DE WAIVER WIRE: {f['manager']} ({f['real_name']}) — {f['current_team']}{where_season}*", LINE,
             f"• *Volume*: {won + fa} adições ({won} waivers + {fa} free agents) | {lost} lances perdidos",
             f"• *Eficiência em disputas*: {rate}% de sucesso",
             f"• *FAAB*: ${faab} queimados (média ${avg}/waiver)",
             f"• *Tiers*: 💎 T5: {tiers.get(5, 0)} | ⭐ T4: {tiers.get(4, 0)} | 🛡️ T3: {tiers.get(3, 0)} | "
             f"🩹 T2: {tiers.get(2, 0)} | 🗑️ T1: {tiers.get(1, 0)} | 💀 T0: {tiers.get(0, 0)}"]
    for title_, where, order in (("💎 *Maiores steals (Tier 5):*", "tier=5", "bid ASC, ts DESC"),
                                 ("🗑️ *Piores queimas de FAAB (Tier 0):*", "tier=0", "bid DESC")):
        rows = con.execute(f"SELECT * FROM waiver_moves WHERE roster_id=? AND status='complete' AND {where} {sc} "
                           f"ORDER BY {order} LIMIT 4", [rid] + sp).fetchall()
        lines += ["", title_] + ([f"• {_wlabel(m)} (${m['bid']} FAAB, {m['season']} {week_label(m['week'])})" for m in rows] or ["• nenhum registrado"])
    rows = con.execute(f"SELECT * FROM waiver_moves WHERE roster_id=? AND status='complete' {sc} ORDER BY ts DESC LIMIT 5",
                       [rid] + sp).fetchall()
    lines += ["", "📦 *Últimas 5 adições:*"]
    lines += [f"• {m['date']} ({m['season']} {week_label(m['week'])}): *{_wlabel(m)}* (${m['bid']} FAAB) [{m['tier_badge']}]" for m in rows]
    return "\n".join(lines + [LINE])


# --------------------------------------------------------------------------- profiles
def profile_report(con: sqlite3.Connection, query: str) -> str:
    rid = resolve_roster(con, query)
    if not rid:
        return (f"❌ Treinador ou franquia '{query}' não identificado no perfil psicológico. "
                f"Tente pelo nome do manager ou do time ({league_ctx.current().people.hint()}).")
    row = con.execute("SELECT body FROM profiles WHERE roster_id=?", (rid,)).fetchone()
    if not row:
        return f"❌ Seção do treinador '{query}' não encontrada no documento de perfis."
    return row[0]
