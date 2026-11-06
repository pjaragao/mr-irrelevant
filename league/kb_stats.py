"""`estatisticas`: counts / rankings / period comparisons over the league history, computed in SQL.

The model picks an entity, a grouping and a metric; the SQL is assembled from the fixed fragments below (never
from model text), so the answer is exact, small (a table of at most ~2k chars) and cannot be mis-counted by
reading a long list. Example — "trades per week in every season":

    estatisticas entidade=trocas agrupar_por=semana          -> weeks × seasons pivot with totals
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

import kb_db
import kb_queries

LINE = "━━━━━━━━━━━━━━━━━━━━━━"

ENTIDADES = ("trocas", "waivers", "jogos", "draft", "jogadores", "pontuacao")
AGRUPAR = ("temporada", "semana", "mes", "manager", "posicao", "rodada", "tier", "tipo", "adversario", "origem", "time_nfl", "jogador")
METRICAS = ("contagem", "jogadores", "picks", "picks_1a", "faab", "faab_media", "perdidas", "taxa_sucesso", "vitorias",
            "derrotas", "aproveitamento", "pontos", "media", "maximo", "minimo", "margem", "via_troca", "contra",
            "media_contra", "pontos_titular", "jogos")
CHRONOLOGICAL = ("temporada", "semana", "mes", "rodada")

# kind: how to print / whether group values can be summed into a footer total
INT, FLOAT, PCT = "int", "float", "pct"

_TIER_NAMES = {0: "T0 💀", 1: "T1 🗑️", 2: "T2 🩹", 3: "T3 🛡️", 4: "T4 ⭐", 5: "T5 💎"}


class Entity:
    def __init__(self, from_sql: str, base_where: str, filters: Dict[str, str], groupers: Dict[str, str],
                 metrics: Dict[str, Tuple[str, str, bool]], default_metric: str, season_col: str, week_col: str):
        self.from_sql, self.base_where = from_sql, base_where
        self.filters, self.groupers, self.metrics = filters, groupers, metrics
        self.default_metric, self.season_col, self.week_col = default_metric, season_col, week_col


# metric = (sql aggregate, kind, additive-across-groups)
ENT: Dict[str, Entity] = {
    # one row per trade
    "trocas": Entity(
        "trades t", "1=1",
        {"ano": "t.season=?", "semana": "t.week=?",
         "roster": "EXISTS (SELECT 1 FROM trade_sides s WHERE s.trade_seq=t.seq AND s.roster_id=?)",
         "texto": "EXISTS (SELECT 1 FROM trade_assets a WHERE a.trade_seq=t.seq AND lower(a.label) LIKE ?)"},
        {"temporada": "t.season", "semana": "t.week", "mes": "substr(t.date,1,7)"},
        {"contagem": ("COUNT(*)", INT, True), "jogadores": ("SUM(t.n_players)", INT, True),
         "picks": ("SUM(t.n_picks)", INT, True), "faab": ("SUM(t.faab_total)", INT, True)},
        "contagem", "t.season", "t.week"),
    # one row per (trade, side): what each manager received
    "trocas_mgr": Entity(
        "trade_sides s JOIN trades t ON t.seq=s.trade_seq", "1=1",
        {"ano": "t.season=?", "semana": "t.week=?", "roster": "s.roster_id=?",
         "texto": "EXISTS (SELECT 1 FROM trade_assets a WHERE a.trade_seq=s.trade_seq AND a.roster_id=s.roster_id AND lower(a.label) LIKE ?)"},
        {"manager": "s.manager", "temporada": "t.season", "semana": "t.week", "mes": "substr(t.date,1,7)"},
        {"contagem": ("COUNT(*)", INT, False),
         "jogadores": ("SUM((SELECT COUNT(*) FROM trade_assets a WHERE a.trade_seq=s.trade_seq AND a.roster_id=s.roster_id AND a.kind='player'))", INT, True),
         "picks": ("SUM((SELECT COUNT(*) FROM trade_assets a WHERE a.trade_seq=s.trade_seq AND a.roster_id=s.roster_id AND a.kind='pick'))", INT, True),
         "picks_1a": ("SUM((SELECT COUNT(*) FROM trade_assets a WHERE a.trade_seq=s.trade_seq AND a.roster_id=s.roster_id AND a.kind='pick' AND a.pick_round=1))", INT, True),
         "faab": ("SUM((SELECT COALESCE(SUM(a.faab),0) FROM trade_assets a WHERE a.trade_seq=s.trade_seq AND a.roster_id=s.roster_id AND a.kind='faab'))", INT, True)},
        "contagem", "t.season", "t.week"),
    "waivers": Entity(
        "waiver_moves m", "1=1",
        {"ano": "m.season=?", "semana": "m.week=?", "roster": "m.roster_id=?",
         "texto": "(lower(coalesce(m.add_name,'')) LIKE ? OR lower(coalesce(m.drop_name,'')) LIKE ?)"},
        {"temporada": "m.season", "semana": "m.week", "manager": "m.manager", "tier": "m.tier", "tipo": "m.type",
         "posicao": "m.add_pos"},
        {"contagem": ("SUM(m.status='complete')", INT, True),
         "perdidas": ("SUM(m.type='waiver' AND m.status='failed')", INT, True),
         "faab": ("SUM(CASE WHEN m.status='complete' THEN m.bid ELSE 0 END)", INT, True),
         "faab_media": ("AVG(CASE WHEN m.status='complete' AND m.type='waiver' THEN m.bid END)", FLOAT, False),
         "taxa_sucesso": ("100.0*SUM(m.type='waiver' AND m.status='complete')/NULLIF(SUM(m.type='waiver'),0)", PCT, False)},
        "contagem", "m.season", "m.week"),
    "jogos": Entity(
        "games g", "g.result IS NOT NULL",
        {"ano": "g.season=?", "semana": "g.week=?", "roster": "g.roster_id=?", "fase": "g.bracket=?"},
        {"manager": "g.manager", "temporada": "g.season", "semana": "g.week", "adversario": "g.opp_manager"},
        {"contagem": ("COUNT(*)", INT, True), "vitorias": ("SUM(g.result='W')", INT, True),
         "derrotas": ("SUM(g.result='L')", INT, True),
         "aproveitamento": ("100.0*SUM(g.result='W')/COUNT(*)", PCT, False),
         "pontos": ("SUM(g.pf)", FLOAT, True), "media": ("AVG(g.pf)", FLOAT, False),
         "maximo": ("MAX(g.pf)", FLOAT, False), "minimo": ("MIN(g.pf)", FLOAT, False),
         "margem": ("AVG(g.pf-g.pa)", FLOAT, False),
         "contra": ("SUM(g.pa)", FLOAT, True), "media_contra": ("AVG(g.pa)", FLOAT, False)},
        "vitorias", "g.season", "g.week"),
    "draft": Entity(
        "draft_picks d", "1=1",
        {"ano": "d.season=?", "semana": "1=1 OR ?=?", "roster": "d.roster_id=?",
         "texto": "lower(d.player_name) LIKE ?"},
        {"temporada": "d.season", "manager": "d.manager", "posicao": "d.pos", "rodada": "d.round", "time_nfl": "d.nfl_team"},
        {"contagem": ("COUNT(*)", INT, True), "via_troca": ("SUM(d.via_trade)", INT, True)},
        "contagem", "d.season", "1"),
    # one row per (player, week, roster that held him): points in THIS league's scoring; bye weeks are flagged, not zeros
    "pontuacao": Entity(
        "player_weeks pw", "1=1",
        {"ano": "pw.season=?", "semana": "pw.week=?", "roster": "pw.roster_id=?", "texto": "lower(pw.name) LIKE ?",
         "posicao": "pw.pos=?"},
        {"jogador": ("pw.name || ' · ' || COALESCE((SELECT f.current_team FROM player_weeks x JOIN franchises f "
                     "ON f.roster_id=x.roster_id WHERE x.player_id=pw.player_id AND x.season=pw.season "
                     "ORDER BY x.week DESC LIMIT 1), 'sem time')"),
         "manager": "(SELECT f.manager FROM franchises f WHERE f.roster_id=pw.roster_id)", "posicao": "pw.pos",
         "time_nfl": "pw.nfl_team", "temporada": "pw.season", "semana": "pw.week"},
        {"pontos": ("ROUND(SUM(pw.pts),2)", FLOAT, True),
         "pontos_titular": ("ROUND(SUM(CASE WHEN pw.started=1 THEN pw.pts ELSE 0 END),2)", FLOAT, True),
         "media": ("ROUND(AVG(CASE WHEN pw.bye=0 AND pw.pts<>0 THEN pw.pts END),2)", FLOAT, False),
         "maximo": ("MAX(pw.pts)", FLOAT, False),
         "minimo": ("MIN(CASE WHEN pw.bye=0 AND pw.pts<>0 THEN pw.pts END)", FLOAT, False),
         "jogos": ("SUM(CASE WHEN pw.bye=0 AND pw.pts<>0 THEN 1 ELSE 0 END)", INT, True),
         "contagem": ("COUNT(DISTINCT pw.player_id)", INT, False)},
        "pontos", "pw.season", "pw.week"),
    "jogadores": Entity(
        "players p", "1=1",
        {"ano": "p.origin_season=?", "semana": "1=1 OR ?=?", "roster": "p.owner_roster_id=?",
         "texto": "lower(p.name) LIKE ?"},
        {"manager": "(SELECT f.manager FROM franchises f WHERE f.roster_id=p.owner_roster_id)", "posicao": "p.pos",
         "origem": "p.origin_type", "time_nfl": "p.nfl_team", "temporada": "p.origin_season"},
        {"contagem": ("COUNT(*)", INT, True)},
        "contagem", "p.origin_season", "1"),
}


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "-"
    if kind == INT:
        return str(int(round(v)))
    if kind == PCT:
        return f"{v:.1f}%"
    return f"{v:.1f}"


def entidade_has_offseason(ent: "Entity") -> bool:
    """Trades and waivers have a week 0 (offseason); games and points never do."""
    return ent.from_sql.startswith(("trades", "trade_sides", "waiver_moves"))


def _offseason_phase(ent: "Entity", fase: str) -> str:
    """`fase=offseason` / `fase=temporada` on trades and waivers: week 0 (before the NFL kickoff) or everything after it."""
    f = (fase or "").strip().lower()
    if not entidade_has_offseason(ent):
        return ""
    if f.startswith(("off", "pre", "pré")):
        return "off"
    if f.startswith(("temp", "season", "regular")):
        return "on"
    return ""


def _label(group: str, v: Any) -> str:
    if v is None or v == "":
        return "sem time/—"
    if group == "semana":
        return "Offseason" if v == 0 else f"S{v}"
    if group == "tier":
        return _TIER_NAMES.get(int(v), f"T{v}")
    if group == "rodada":
        return f"R{v}"
    return str(v)


def _fase_bracket(fase: str) -> str:
    """`fase` -> games.bracket: playoffs = title bracket only; toilet = consolation bracket; anything else = regular."""
    f = fase.strip().lower()
    if f.startswith("play"):
        return "winners"
    if f.startswith(("toil", "cons")):
        return "losers"
    return "regular"


def _where(ent: Entity, ano, semana, roster, texto, fase, posicao="") -> Tuple[str, List[Any]]:
    clauses, params = [ent.base_where], []
    if ano:
        clauses.append(ent.filters["ano"]); params.append(str(ano))
    if semana and "1=1" not in ent.filters["semana"]:
        clauses.append(ent.filters["semana"]); params.append(int(semana))
    if roster:
        clauses.append(ent.filters["roster"]); params.append(roster)
    if texto:
        clauses.append(ent.filters["texto"])
        params += [f"%{texto.lower()}%"] * ent.filters["texto"].count("?")
    if posicao and "posicao" in ent.filters:
        clauses.append(ent.filters["posicao"]); params.append(posicao.strip().upper())
    if fase and "fase" in ent.filters:
        clauses.append(ent.filters["fase"]); params.append(_fase_bracket(fase))
    phase = _offseason_phase(ent, fase)
    if phase:
        clauses.append(f"{ent.week_col}{'=0' if phase == 'off' else '>0'}")
    return " AND ".join(clauses), params


def fetch(con: sqlite3.Connection, entidade: str = "", agrupar_por: str = "", metrica: str = "", ano: Optional[str] = None,
          semana: Optional[int] = None, alvo: str = "", limite: Optional[int] = None, fase: str = "",
          serie: str = "", metrica2: str = "", posicao: str = "") -> Dict[str, Any]:
    """Run the aggregation and return structured data (used by `query` for text and by chart_service for charts)."""
    entidade = (entidade or "").strip().lower()
    agrupar_por = (agrupar_por or "").strip().lower()
    metrica = (metrica or "").strip().lower()
    if entidade not in ENTIDADES:
        return {"error": f"Informe `entidade`: {' | '.join(ENTIDADES)}. (agrupar_por: {' | '.join(AGRUPAR)}; "
                         f"metrica: {' | '.join(METRICAS)})"}
    serie = (serie or "").strip().lower()
    metrica2 = (metrica2 or "").strip().lower()
    key = "trocas_mgr" if entidade == "trocas" and "manager" in (agrupar_por, serie) else entidade
    ent = ENT[key]
    if agrupar_por and agrupar_por not in ent.groupers:
        return {"error": f"`agrupar_por={agrupar_por}` não existe para {entidade}. Opções: {' | '.join(sorted(ent.groupers))}."}
    if serie and (not agrupar_por or serie not in ent.groupers or serie == agrupar_por):
        return {"error": f"`serie={serie}` precisa de um `agrupar_por` e ser outra dimensão de {entidade}. "
                         f"Opções: {' | '.join(sorted(ent.groupers))}."}
    if metrica2 and metrica2 not in ent.metrics:
        return {"error": f"`metrica2={metrica2}` não existe para {entidade}. Opções: {' | '.join(ent.metrics)}."}
    metrica = metrica or ent.default_metric
    if metrica not in ent.metrics:
        return {"error": f"`metrica={metrica}` não existe para {entidade}. Opções: {' | '.join(ent.metrics)}."}
    agg, kind, additive = ent.metrics[metrica]

    roster, texto = None, ""
    if alvo:
        roster = kb_queries.resolve_roster(con, alvo)
        if roster is None:
            if entidade == "jogos":
                return {"error": f"Manager '{alvo}' não encontrado."}
            texto = alvo
    where, params = _where(ent, ano, semana, roster, texto, fase, posicao)
    if posicao and "posicao" not in ent.filters:
        return {"error": f"`posicao` não se aplica a {entidade} (use entidade=pontuacao)."}

    desc = [f"{'pontuação' if entidade == 'pontuacao' else entidade} · {metrica}"]
    if agrupar_por:
        desc.append(f"por {agrupar_por}")
    if ano:
        desc.append(str(ano))
    if semana:
        desc.append(f"semana {semana}")
    if _offseason_phase(ent, fase):
        desc.append("offseason (antes do kickoff)" if _offseason_phase(ent, fase) == "off" else "temporada (depois do kickoff)")
    if roster:
        desc.append(kb_queries.manager_name(con, roster))
    elif texto:
        desc.append(f"'{texto}'")
    if fase and not _offseason_phase(ent, fase):
        desc.append(fase)
    if posicao:
        desc.append(posicao.upper())
    res: Dict[str, Any] = {"entidade": entidade, "metrica": metrica, "agrupar_por": agrupar_por, "kind": kind,
                           "additive": additive, "desc": " · ".join(desc), "key": key, "ano": ano, "alvo": alvo,
                           "alvo_nome": kb_queries.manager_name(con, roster) if roster else ""}
    res["total"] = con.execute(f"SELECT {agg} FROM {ent.from_sql} WHERE {where}", params).fetchone()[0]
    if entidade == "pontuacao" and agrupar_por == "semana":   # a bye is not a 0-point game: say which weeks it was
        res["bye_weeks"] = [(r[0], r[1]) for r in con.execute(
            f"SELECT DISTINCT pw.season, pw.week FROM {ent.from_sql} WHERE {where} AND pw.bye=1 ORDER BY 1,2", params)]
    # how far each season has been played (so an in-progress season is not drawn as if it were over)
    res["progress"] = {yr: wk for yr, wk in con.execute(
        "SELECT season, MAX(week) FROM games WHERE result IS NOT NULL AND is_playoff=0 GROUP BY season")}
    if not agrupar_por:
        res["mode"] = "total"
        return res

    gexpr = ent.groupers[agrupar_por]
    if serie:  # two dimensions: rows = agrupar_por, columns = serie
        sexpr = ent.groupers[serie]
        raw = con.execute(f"SELECT {gexpr} g, {sexpr} c, {agg} v FROM {ent.from_sql} WHERE {where} GROUP BY 1,2",
                          params).fetchall()
        cell = {(r["g"], r["c"]): r["v"] for r in raw}
        rows_k = {r["g"] for r in raw}
        cols_k = {r["c"] for r in raw}
        row_tot = {g: sum(v or 0 for (gg, _c), v in cell.items() if gg == g) for g in rows_k}
        col_tot = {c: sum(v or 0 for (_g, cc), v in cell.items() if cc == c) for c in cols_k}
        sort_k = lambda k: (k is None, k)  # noqa: E731
        rows_o = sorted(rows_k, key=sort_k) if agrupar_por in CHRONOLOGICAL else sorted(rows_k, key=lambda k: (-row_tot[k], str(k)))
        cols_o = sorted(cols_k, key=sort_k) if serie in CHRONOLOGICAL else sorted(cols_k, key=lambda k: (-col_tot[k], str(k)))
        rows_o = rows_o[: int(limite) if limite else 14]
        cols_o = cols_o[:30 if serie in CHRONOLOGICAL else 12]
        res.update(mode="matrix", serie=serie, rows=rows_o, cols=cols_o, cell=cell,
                   row_chron=agrupar_por in CHRONOLOGICAL, col_chron=serie in CHRONOLOGICAL)
        return res
    if agrupar_por == "semana" and not ano and ent.season_col != "1":
        rows = con.execute(f"SELECT {ent.season_col} s, {gexpr} g, {agg} v FROM {ent.from_sql} WHERE {where} "
                           f"GROUP BY 1,2 ORDER BY 2,1", params).fetchall()
        res.update(mode="pivot", seasons=sorted({r["s"] for r in rows if r["s"] is not None}),
                   weeks=sorted({r["g"] for r in rows}), cell={(r["g"], r["s"]): r["v"] for r in rows})
        for (w, yr), _v in res["cell"].items():  # a trade recorded in a week counts as that week having started
            res["progress"][yr] = max(res["progress"].get(yr, 0), w or 0)
        return res

    chronological = agrupar_por in ("temporada", "semana", "mes", "rodada")
    order = "1 ASC" if chronological else "2 DESC, 1 ASC"
    n = int(limite) if limite else (40 if chronological else 15)
    rows = con.execute(f"SELECT {gexpr} g, {agg} v FROM {ent.from_sql} WHERE {where} GROUP BY 1 ORDER BY {order} LIMIT ?",
                       params + [n]).fetchall()
    res.update(mode="groups", chronological=chronological, rows=[(r["g"], r["v"]) for r in rows])
    if metrica2:  # a second metric over the same groups (scatter plots, side-by-side comparisons)
        agg2, kind2, _a2 = ent.metrics[metrica2]
        v2 = {r["g"]: r["v"] for r in con.execute(f"SELECT {gexpr} g, {agg2} v FROM {ent.from_sql} WHERE {where} GROUP BY 1", params)}
        res.update(metrica2=metrica2, kind2=kind2, values2=v2)
    if key == "trocas_mgr" and metrica == "contagem":
        res["distinct"] = con.execute(f"SELECT COUNT(DISTINCT t.seq) FROM {ent.from_sql} WHERE {where}", params).fetchone()[0]
    return res


def query(con: sqlite3.Connection, entidade: str = "", agrupar_por: str = "", metrica: str = "", ano: Optional[str] = None,
          semana: Optional[int] = None, alvo: str = "", limite: Optional[int] = None, fase: str = "",
          serie: str = "", metrica2: str = "", posicao: str = "") -> str:
    r = fetch(con, entidade, agrupar_por, metrica, ano, semana, alvo, limite, fase, serie, metrica2, posicao)
    if "error" in r:
        return r["error"]
    title = f"📊 *{r['desc'].upper()}*"
    kind, additive, metrica, agrupar_por, entidade = r["kind"], r["additive"], r["metrica"], r["agrupar_por"], r["entidade"]
    if r["mode"] == "total":
        return f"{title}\n{LINE}\n*Total: {_fmt(r['total'], kind)}*"
    if r["mode"] == "matrix":
        cols = r["cols"]
        lines = [title, LINE, f"{agrupar_por} \\ {r['serie']}: " + " | ".join(_label(r["serie"], c) for c in cols)]
        for g in r["rows"]:
            lines.append(f"{_label(agrupar_por, g)}: " + " | ".join(
                _fmt(r["cell"].get((g, c)) if r["cell"].get((g, c)) is not None else 0, kind) for c in cols))
        lines += [LINE, f"Total: {_fmt(r['total'], kind)}"]
        return "\n".join(lines)
    if r["mode"] == "pivot":
        seasons, weeks, cell, total = r["seasons"], r["weeks"], r["cell"], r["total"]
        lines = [title, LINE, "semana: " + " | ".join(f"{s}" for s in seasons) + (" | total" if additive else "")]
        for w in weeks:
            vals = [cell.get((w, s)) for s in seasons]
            line = f"{_label('semana', w)}: " + " | ".join(_fmt(v if v is not None else 0, kind) for v in vals)
            if additive:
                line += f" | {_fmt(sum(v or 0 for v in vals), kind)}"
            lines.append(line)
        if additive:
            per = [sum(v or 0 for (w, s), v in cell.items() if s == yr) for yr in seasons]
            lines += [LINE, "TOTAL: " + " | ".join(_fmt(v, kind) for v in per) + f" | {_fmt(total, kind)}"]
        if entidade == "waivers" and metrica == "contagem":
            lines.append("_contagem = adições concluídas (waivers ganhos + free agents)_")
        if r.get("bye_weeks"):
            lines.append("_bye (sem jogo, NÃO é 0): " + ", ".join(f"S{w}/{y}" for y, w in r["bye_weeks"]) + "_")
        if 0 in weeks:
            lines.append("_Offseason = antes do kickoff da NFL (a Sleeper as marca como semana 1); S1 = só o que rolou "
                         "depois do kickoff. 'Desde o início da temporada' = tudo menos a linha Offseason_")
        return "\n".join(lines)
    rows = r["rows"]
    if not rows:
        return f"{title}\n{LINE}\nSem dados para esse filtro."
    lines = [title, LINE]
    for i, (g, v) in enumerate(rows, 1):
        prefix = "• " if r["chronological"] else f"{i}. "
        lines.append(f"{prefix}{_label(agrupar_por, g)}: *{_fmt(v, kind)}*")
    if r.get("bye_weeks"):
        lines.append("_bye (sem jogo, NÃO é 0): " + ", ".join(f"S{w}/{y}" for y, w in r["bye_weeks"]) + "_")
    lines.append(LINE)
    lines.append(f"Total: *{_fmt(r['total'], kind)}*" if additive or kind == INT else f"Geral: *{_fmt(r['total'], kind)}*")
    if "distinct" in r:
        lines.append("_cada troca conta para todos os lados envolvidos_")
        lines[-2] = f"Trocas distintas: *{r['distinct']}*"
    if entidade == "waivers" and metrica == "contagem":
        lines.append("_contagem = adições concluídas (waivers ganhos + free agents)_")
    return "\n".join(lines)


def run(**kw) -> str:
    con = kb_db.connect()
    try:
        return query(con, **kw)
    finally:
        con.close()
