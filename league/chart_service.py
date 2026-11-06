#!/usr/bin/env python3
"""League charts: SQL statistics (kb_stats) or a JSON spec -> interactive HTML + PNG (charts.py) -> WhatsApp.

Library:   chart_service.generate(entidade=..., agrupar_por=..., ...)   -> {"png", "html", "title", "caption", ...}
           chart_service.generate_spec(spec_dict, slug="meu-grafico")
Tool:      `liga grafico ...` returns the PNG path as a `MEDIA:` line; the Hermes gateway uploads it to the chat.
CLI:       python3 chart_service.py estatisticas entidade=trocas agrupar_por=semana [serie=.. forma=.. metrica2=..] [--send]
           python3 chart_service.py spec grafico.json [--slug nome] [--send]
`--send` posts the PNG to WHATSAPP_GROUP_JID (or --dest JID) through Evolution; without it nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import league_ctx  # noqa: E402

import charts  # noqa: E402
import kb_db  # noqa: E402
import kb_stats  # noqa: E402

KEEP_DAYS = 7
NOUN = {"trocas": ("Trocas", "trocas"), "waivers": ("Waivers e free agents", "adições"), "jogos": ("Jogos", "jogos"),
        "draft": ("Picks de draft", "picks"), "jogadores": ("Jogadores", "jogadores"),
        "pontuacao": ("Pontuação de jogadores", "jogadores")}
FORMAS = ("", "auto", "barras", "colunas", "empilhado", "linhas", "calor", "pizza", "dispersao")
METRIC_TITLE = {"contra": "pontos sofridos", "media_contra": "média de pontos sofridos", "contagem": "", "faab": "FAAB gasto", "faab_media": "FAAB médio", "perdidas": "lances perdidos",
                "taxa_sucesso": "taxa de sucesso", "vitorias": "vitórias", "derrotas": "derrotas",
                "aproveitamento": "aproveitamento", "pontos": "pontos", "media": "média de pontos",
                "maximo": "maior pontuação", "minimo": "menor pontuação", "margem": "margem média",
                "pontos_titular": "pontos como titular", "jogos": "jogos disputados",
                "jogadores": "jogadores", "picks": "picks", "picks_1a": "picks de 1ª rodada", "via_troca": "picks via troca"}
GROUP_TITLE = {"temporada": "temporada", "semana": "semana", "mes": "mês", "manager": "manager", "posicao": "posição",
               "rodada": "rodada", "tier": "tier", "tipo": "tipo", "adversario": "adversário", "origem": "origem",
               "time_nfl": "time da NFL", "jogador": "jogador"}


def out_dir() -> str:
    """Directory of the generated files. Under HERMES_HOME/image_cache so the gateway may deliver them."""
    import league_ctx
    d = os.environ.get("LIGA_CHARTS_DIR")
    if not d:
        home = league_ctx.current().home or os.environ.get("HERMES_HOME")
        d = os.path.join(home, "image_cache", "liga_graficos") if home else os.path.join(BASE_DIR, "charts_out")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup(d: str) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or "grafico"


# --------------------------------------------------------------------------- stats -> spec
_PTS = ("pontos", "pontos_titular", "media", "maximo", "minimo", "margem", "contra", "media_contra")


def _unit(r: Dict[str, Any]) -> str:
    """Unit that matches the metric (a 'jogos' entity measured in points is points, not games)."""
    m = r["metrica"]
    if m in _PTS:
        return "pts"
    if m in ("faab", "faab_media"):
        return "de FAAB"
    if m in ("aproveitamento", "taxa_sucesso"):
        return ""
    if m in ("vitorias", "derrotas"):
        return "vitórias" if m == "vitorias" else "derrotas"
    return NOUN[r["entidade"]][1]


# What the number *is*, per (entity, metric): used for titles when a single manager is the subject.
SUBJECT = {("jogos", "vitorias"): "Vitórias", ("jogos", "derrotas"): "Derrotas", ("jogos", "pontos"): "Pontos",
           ("trocas", "contagem"): "Trocas", ("waivers", "contagem"): "Adições", ("waivers", "faab"): "FAAB gasto",
           ("waivers", "perdidas"): "Lances perdidos", ("draft", "contagem"): "Picks"}
# Only trades and waivers happen before the season: for them week 0 is the offseason (before the NFL kickoff).
PRESEASON = ("trocas", "waivers")


def _title(r: Dict[str, Any]) -> str:
    subj = SUBJECT.get((r["entidade"], r["metrica"]))
    if r.get("alvo_nome") and subj and r["agrupar_por"]:
        return f"{subj} de {r['alvo_nome']} por {GROUP_TITLE.get(r['agrupar_por'], r['agrupar_por'])}"
    noun = NOUN[r["entidade"]][0]
    metric = METRIC_TITLE.get(r["metrica"], r["metrica"])
    base = f"{noun}: {metric}" if metric else noun
    group = r["agrupar_por"]
    return f"{base} por {GROUP_TITLE.get(group, group)}" if group else base


def _subtitle(r: Dict[str, Any]) -> str:
    import league_ctx
    bits = [league_ctx.current().name]
    if r.get("ano"):
        bits.append(str(r["ano"]))
    if r.get("alvo"):
        bits.append(str(r["alvo"]))
    return " · ".join(bits)


def _footer() -> str:
    import league_ctx
    return f"{league_ctx.current().persona_name} · dados da liga até {datetime.now():%d/%m/%Y}"


def _pivot_spec(r: Dict[str, Any]) -> Dict[str, Any]:
    seasons, cell, progress = r["seasons"], r["cell"], r.get("progress", {})
    import league_ctx
    regular = league_ctx.current().profile.regular_season_weeks or 14   # the league's own regular-season length
    unit = _unit(r)
    pre = r["entidade"] in PRESEASON
    weeks = list(range(0 if pre else 1, max(regular, max(r["weeks"] or [regular])) + 1))   # week 0 = offseason
    cur = seasons[-1]
    last = {s: (progress.get(s, weeks[-1]) if s == cur else weeks[-1]) for s in seasons}
    last[cur] = min(last[cur], weeks[-1])
    values = [[(cell.get((w, s)) or 0) if w <= last[s] else None for w in weeks] for s in seasons]
    cols = ["Pré" if w == 0 else f"S{w}" for w in weeks]
    totals = [sum(v for v in row if v is not None) for row in values]
    total = sum(totals)

    best_i = max(range(len(seasons)), key=lambda i: totals[i])
    week_totals = [sum((values[i][j] or 0) for i in range(len(seasons))) for j in range(len(weeks))]
    lo = 1 if pre and len(weeks) > 1 else 0  # for trades/waivers week 0 is the offseason, so the "busiest week" skips it
    top_j = max(range(lo, len(weeks)), key=lambda j: week_totals[j])
    k = len(seasons)
    tiles = [
        {"value": f"{totals[best_i]}", "label": f"{unit} em {seasons[best_i]}", "sub": f"temporada com mais {unit}"},
        {"value": f"S{weeks[top_j]}", "label": f"semana com mais {unit}",
         "sub": f"{week_totals[top_j]} {unit} somando {k} temporadas" + (" (fora da pré-temporada)" if pre else "")},
    ]
    if pre:
        first_share = week_totals[0] / total * 100 if total else 0
        tiles.append({"value": f"{first_share:.0f}%", "label": f"das {unit} saem na pré-temporada", "sub": f"{week_totals[0]} de {total} · antes do kickoff"})
        first_txt = f" A pré-temporada (antes do kickoff) concentra {first_share:.0f}%."
    else:  # pace: this season so far against the previous one at the same point
        L = last[cur]
        cur_cum = sum(v for v in values[-1][:L] if v is not None)
        if k > 1:
            prev_cum = sum(v for v in values[-2][:L] if v is not None)
            tiles.append({"value": f"{cur_cum}", "label": f"{unit} em {cur} até a S{L}",
                          "sub": f"{prev_cum} em {seasons[-2]} na mesma altura"})
            first_txt = f" Em {cur}, {cur_cum} até a S{L} (contra {prev_cum} em {seasons[-2]})."
        else:
            tiles.append({"value": f"{total / k:.1f}", "label": f"{unit} por temporada", "sub": "média"})
            first_txt = ""
    heat_title = f"{unit.capitalize()} por semana, ano a ano"
    note = ("Pré = offseason, antes do kickoff da NFL. " if pre else "") + "Listrado = semana que ainda não aconteceu."
    blocks: List[Dict[str, Any]] = [
        {"type": "tiles", "items": tiles},
        {"type": "heatmap", "title": heat_title,
         "subtitle": f"Cada linha é uma temporada; cada célula, o total de {unit} naquela semana",
         "rows": seasons, "cols": cols, "values": values, "row_totals": True, "unit": unit,
         "row_notes": {cur: f"até a S{last[cur]}"} if last[cur] < weeks[-1] else {}, "note": note},
    ]
    series = []
    for i, s in enumerate(seasons):
        acc, vals = 0, []
        for j, w in enumerate(weeks):
            if values[i][j] is None:
                vals.append(None)
            else:
                acc += values[i][j]
                vals.append(acc)
        series.append({"name": str(s), "values": vals, "highlight": s == cur})
    blocks.append({"type": "lines", "title": "Ritmo acumulado ao longo da temporada",
                   "subtitle": f"Total de {unit} que cada temporada já somava ao fim de cada semana",
                   "x": cols, "series": series, "unit": unit})
    who = f" de {r['alvo_nome']}" if r.get("alvo_nome") else ""
    caption = (f"{unit.capitalize()}{who} por semana, {seasons[0]}→{seasons[-1]}: {total} no total. "
               f"Pico: {seasons[best_i]} ({totals[best_i]}). A semana com mais {unit} é a S{weeks[top_j]} "
               f"({week_totals[top_j]}{', fora da pré-temporada' if pre else ''}).{first_txt}")
    return {"title": _title(r), "subtitle": _subtitle(r) + f" · {total} {unit} ({seasons[0]}–{seasons[-1]})",
            "blocks": blocks, "footer": _footer(), "caption": caption}


def _fmt_of(kind: str) -> str:
    return {"int": "int", "float": "float", "pct": "pct"}[kind]


def _tiles_groups(r: Dict[str, Any], items: List[Dict[str, Any]], fmt: str, unit: str, group: str):
    top = max(items, key=lambda i: i["value"])
    avg = sum(i["value"] for i in items) / len(items)
    if "distinct" in r:  # a trade has 2+ sides: the sum over managers is not the number of trades
        first = {"value": charts.fmt_value(r["distinct"]), "label": f"{unit} no total", "sub": "cada troca conta p/ todos os lados"}
        total_txt = f"{r['distinct']} {unit} distintas"
    else:
        total_label = "total" if r["additive"] else "geral"
        first = {"value": charts.fmt_value(r["total"], fmt), "label": total_label, "sub": f"{unit} · por {group}"}
        total_txt = f"{total_label} {charts.fmt_value(r['total'], fmt)}"
    tiles = [first,
             {"value": top["label"], "label": "pico" if r["chronological"] else "líder",
              "sub": f"{charts.fmt_value(top['value'], fmt)} {unit}".strip()},
             {"value": charts.fmt_value(avg, "float" if fmt == "int" else fmt), "label": f"média por {group}",
              "sub": f"{len(items)} itens"}]
    return tiles, top, total_txt


def _groups_spec(r: Dict[str, Any], forma: str = "", ) -> Dict[str, Any]:
    from kb_stats import _label
    rows = [(_label(r["agrupar_por"], g), v) for g, v in r["rows"] if v is not None]
    fmt = _fmt_of(r["kind"])
    unit = _unit(r)
    group = GROUP_TITLE.get(r["agrupar_por"], r["agrupar_por"])
    if not rows:
        raise ValueError("Sem dados para esse filtro.")
    items = [{"label": str(g), "value": v} for g, v in rows]
    if r["agrupar_por"] == "temporada":  # the in-progress season is drawn as such, never as a failed one
        cur = max(items, key=lambda i: i["label"])
        prog = r.get("progress", {}).get(cur["label"])
        if prog is not None and prog < (league_ctx.current().profile.regular_season_weeks or 14):
            cur["muted"], cur["note"] = True, f"até a S{prog}"
    tiles, top, total_txt = _tiles_groups(r, items, fmt, unit, group)
    if len(top["label"]) > 7:
        tiles[1]["value"] = top["label"][:7] + "…"
    sub = r["desc"]
    caption = (f"{_title(r)}: {'pico' if r['chronological'] else 'líder'} {top['label']} "
               f"({charts.fmt_value(top['value'], fmt)}); {total_txt}.")

    if r.get("metrica2") or forma == "dispersao":
        if not r.get("metrica2"):
            raise ValueError("A dispersão precisa de duas métricas: informe `metrica` (eixo x) e `metrica2` (eixo y).")
        v2 = r["values2"]
        pts = [{"label": str(_label(r["agrupar_por"], g)), "x": v, "y": v2[g]} for g, v in r["rows"]
               if v is not None and v2.get(g) is not None]
        if len(pts) < 3:
            raise ValueError("Poucos pontos para uma dispersão (preciso de pelo menos 3).")
        ml1, ml2 = METRIC_TITLE.get(r["metrica"], r["metrica"]) or "quantidade", METRIC_TITLE.get(r["metrica2"], r["metrica2"]) or "quantidade"
        quad = {"tl": "poucos pontos, muitos sofridos", "br": "muitos pontos, poucos sofridos"} \
            if {r["metrica"], r["metrica2"]} <= {"media", "media_contra", "pontos", "contra"} else {}
        title = f"{NOUN[r['entidade']][0]}: {ml1} × {ml2}"
        best = max(pts, key=lambda p: p["x"] - p["y"]) if quad else max(pts, key=lambda p: p["x"])
        return {"title": title, "subtitle": f"{_subtitle(r)} · por {group}",
                "blocks": [{"type": "scatter", "title": f"{ml2} (↑) contra {ml1} (→)", "subtitle": "Linhas tracejadas = média da liga",
                            "points": pts, "x_label": ml1, "y_label": ml2, "fmt": fmt, "quadrants": quad}],
                "footer": _footer(), "caption": f"{title}: {len(pts)} pontos; destaque {best['label']}."}

    if forma == "pizza":
        if not r["additive"]:
            raise ValueError("A pizza só faz sentido para quantidades que somam um todo (contagem, FAAB, pontos).")
        blocks = [{"type": "donut", "title": _title(r), "subtitle": sub, "items": items, "fmt": fmt, "unit": unit,
                   "center_label": unit}]
        return {"title": _title(r), "subtitle": _subtitle(r), "blocks": blocks, "footer": _footer(), "caption": caption}

    chron = r["chronological"]
    if forma == "linhas" or (forma in ("", "auto") and chron and len(items) > 8):
        if not chron:
            raise ValueError("Linhas precisam de uma dimensão de tempo (temporada, semana, mês, rodada). Use barras.")
        blocks = [{"type": "tiles", "items": tiles},
                  {"type": "lines", "title": _title(r), "subtitle": sub, "x": [i["label"] for i in items],
                   "series": [{"name": _title(r), "values": [i["value"] for i in items], "highlight": True}],
                   "fmt": fmt, "unit": unit}]
        return {"title": _title(r), "subtitle": _subtitle(r), "blocks": blocks, "footer": _footer(), "caption": caption}
    vertical = forma == "colunas" or (forma in ("", "auto") and chron and len(items) <= 8)
    if forma == "barras":
        vertical = False
    blocks = [{"type": "tiles", "items": tiles},
              {"type": "bars", "title": _title(r), "subtitle": sub, "items": items,
               "orientation": "v" if vertical else "h", "unit": unit, "fmt": fmt}]
    return {"title": _title(r), "subtitle": _subtitle(r), "blocks": blocks, "footer": _footer(), "caption": caption}


def _matrix_spec(r: Dict[str, Any], forma: str = "") -> Dict[str, Any]:
    from kb_stats import _label
    fmt, unit = _fmt_of(r["kind"]), _unit(r)
    g_name, s_name = GROUP_TITLE.get(r["agrupar_por"], r["agrupar_por"]), GROUP_TITLE.get(r["serie"], r["serie"])
    rows, cols, cell = r["rows"], r["cols"], r["cell"]
    rlab, clab = [str(_label(r["agrupar_por"], g)) for g in rows], [str(_label(r["serie"], c)) for c in cols]
    miss = 0 if r["additive"] else None
    grid = [[(cell.get((g, c)) if cell.get((g, c)) is not None else miss) for c in cols] for g in rows]
    title = f"{_title(r)} × {s_name}"
    best = max(((g, c, v) for g, rw in zip(rows, grid) for c, v in zip(cols, rw) if v is not None),
               key=lambda t: t[2], default=None)
    caption = f"{title}: maior valor {charts.fmt_value(best[2], fmt)} ({_label(r['agrupar_por'], best[0])}, {_label(r['serie'], best[1])})." if best else title
    if forma in ("linhas", "colunas", "empilhado"):
        # time (or the dimension that looks like it) goes on x, the other one becomes the series
        if r["col_chron"] or not r["row_chron"]:
            xl, sl, vals_of = clab, rlab, lambda i: grid[i]
        else:
            xl, sl, vals_of = rlab, clab, lambda i: [row[i] for row in grid]
        n_series = len(sl)
        if forma == "linhas" and not (r["col_chron"] or r["row_chron"]):
            raise ValueError("Linhas precisam de uma dimensão de tempo (temporada, semana, mês, rodada). Use calor.")
        if n_series > (8 if forma == "linhas" else 6):
            raise ValueError(f"São {n_series} séries; o máximo é {8 if forma == 'linhas' else 6}. Filtre com `alvo`/`limite` ou use forma=calor.")
        series = [{"name": sl[i], "values": vals_of(i)} for i in range(n_series)]
        block = ({"type": "lines", "x": xl, "series": series} if forma == "linhas"
                 else {"type": "columns", "x": xl, "series": series, "stacked": forma == "empilhado"})
        block.update(title=title, subtitle=r["desc"], fmt=fmt, unit=unit)
        return {"title": _title(r), "subtitle": _subtitle(r), "blocks": [block], "footer": _footer(), "caption": caption}
    block = {"type": "heatmap", "title": title, "subtitle": r["desc"], "rows": rlab, "cols": clab, "values": grid,
             "row_totals": bool(r["additive"]), "fmt": fmt, "unit": unit}
    return {"title": _title(r), "subtitle": _subtitle(r), "blocks": [block], "footer": _footer(), "caption": caption}


def spec_from_stats(con, entidade: str = "trocas", agrupar_por: str = "semana", metrica: str = "", ano=None, semana=None,
                    alvo: str = "", limite=None, fase: str = "", serie: str = "", forma: str = "", metrica2: str = "", posicao: str = "") -> Dict[str, Any]:
    forma = (forma or "").strip().lower()
    if forma not in FORMAS:
        raise ValueError(f"`forma` inválida. Opções: {' | '.join(f for f in FORMAS if f)}.")
    r = kb_stats.fetch(con, entidade, agrupar_por, metrica, ano, semana, alvo, limite, fase, serie, metrica2, posicao)
    if "error" in r:
        raise ValueError(r["error"])
    if r["mode"] == "total":
        raise ValueError("Para o gráfico, informe `agrupar_por` (ex.: semana, temporada, manager).")
    if r["mode"] == "matrix":
        return _matrix_spec(r, forma)
    if r["mode"] == "pivot":
        if r["kind"] != kb_stats.INT:
            raise ValueError("O mapa semana × temporada só existe para contagens (metrica=contagem, faab, ...). "
                             "Use agrupar_por=manager ou temporada para médias e percentuais.")
        if forma in ("barras", "pizza", "dispersao"):
            raise ValueError("Para essa forma, escolha um ano (`ano=2025`) ou outro `agrupar_por`.")
        return _pivot_spec(r)
    return _groups_spec(r, forma)


# --------------------------------------------------------------------------- generate
def _write(spec: Dict[str, Any], slug: str) -> Dict[str, Any]:
    d = out_dir()
    _cleanup(d)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(d, f"{_slug(slug)}-{stamp}")
    res = charts.render_all(spec, base + ".png", base + ".html")
    res.update(title=spec.get("title", ""), caption=spec.get("caption", ""), dir=d)
    return res


def generate_spec(spec: Dict[str, Any], slug: str = "") -> Dict[str, Any]:
    return _write(spec, slug or spec.get("title", "grafico"))


def generate(entidade: str = "trocas", agrupar_por: str = "semana", metrica: str = "", ano=None, semana=None,
             alvo: str = "", limite=None, fase: str = "", serie: str = "", forma: str = "", metrica2: str = "", posicao: str = "") -> Dict[str, Any]:
    con = kb_db.connect()
    try:
        spec = spec_from_stats(con, entidade, agrupar_por, metrica, ano, semana, alvo, limite, fase, serie, forma, metrica2, posicao)
    finally:
        con.close()
    return _write(spec, f"{entidade}-{metrica or 'contagem'}-{agrupar_por}{'-' + serie if serie else ''}{'-' + forma if forma else ''}")


def generate_from_json(dados: str, slug: str = "") -> Dict[str, Any]:
    """Chart of data the caller supplies (a JSON string, e.g. written by the model); validated before drawing."""
    if len(dados) > 20000:
        raise ValueError("`dados` grande demais (máx. 20 mil caracteres).")
    try:
        spec = json.loads(dados)
    except json.JSONDecodeError as exc:
        raise ValueError(f"`dados` não é JSON válido: {exc.msg}") from None
    spec = charts.validate_spec(spec)
    spec.setdefault("footer", "")
    spec["footer"] = spec["footer"] or _footer()
    return generate_spec(spec, slug)


def media_reply(res: Dict[str, Any]) -> str:
    """Text returned to the model by the `grafico` tool: the MEDIA line makes the gateway upload the PNG."""
    return (f"📈 Gráfico pronto: {res['title']}.\n"
            "Para mandar a imagem no chat, escreva na sua resposta a linha abaixo, sozinha (sem aspas nem crases), e "
            "depois UMA ou DUAS frases de comentário no seu estilo. Não repita a tabela: o gráfico já mostra tudo.\n"
            f"MEDIA:{res['png']}\n"
            f"Fatos para o comentário: {res['caption']}")


# --------------------------------------------------------------------------- send
def send_png(path: str, caption: str = "", dest: Optional[str] = None) -> Dict[str, Any]:
    from evolution_client import EvolutionClient, sent_ok
    import league_ctx
    dest = dest or league_ctx.current().group_jid
    if not dest:
        return {"error": "WHATSAPP_GROUP_JID not set (use --dest)"}
    cli = EvolutionClient(base_url=os.environ.get("EVOLUTION_API_URL", "http://evolution-api:8080"),
                          api_key=os.environ.get("EVOLUTION_API_KEY", ""),
                          instance=os.environ.get("EVOLUTION_INSTANCE", "sleeper-bot"))
    res = cli.send_image(dest, path, caption)
    return {"ok": sent_ok(res), "response": res}


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("estatisticas", "spec"):
        print(__doc__)
        return 2
    send = "--send" in argv
    dest = next((argv[i + 1] for i, a in enumerate(argv) if a == "--dest" and i + 1 < len(argv)), None)
    slug = next((argv[i + 1] for i, a in enumerate(argv) if a == "--slug" and i + 1 < len(argv)), "")
    rest = [a for a in argv[2:] if not a.startswith("--") and a not in (dest, slug)]
    try:
        if argv[1] == "spec":
            with open(rest[0], encoding="utf-8") as f:
                res = generate_spec(json.load(f), slug)
        else:
            kv = dict(a.split("=", 1) for a in rest if "=" in a)
            res = generate(kv.get("entidade", "trocas"), kv.get("agrupar_por", "semana"), kv.get("metrica", ""),
                           kv.get("ano"), int(kv["semana"]) if kv.get("semana", "").isdigit() else None,
                           kv.get("alvo", ""), int(kv["limite"]) if kv.get("limite", "").isdigit() else None,
                           kv.get("fase", ""), kv.get("serie", ""), kv.get("forma", ""), kv.get("metrica2", ""))
    except (ValueError, OSError, KeyError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    print(f"PNG:  {res['png']}\nHTML: {res['html']}\nLegenda: {res['caption']}")
    if send:
        out = send_png(res["png"], res["caption"] or res["title"], dest)
        print("enviado:", out.get("ok"), "" if out.get("ok") else out)
        return 0 if out.get("ok") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
