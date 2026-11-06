"""Eleições 2026 — apuração oficial do TSE no DM do admin (sem IA: o texto sai direto dos arquivos do TSE).

Fonte: o CDN oficial que o app "Resultados" do TSE consome (resultados.tse.jus.br). Um arquivo "unificado" por
cargo/abrangência, assinado em JWS (Ed25519); a assinatura é conferida com a chave pública publicada no próprio app:
    {BASE}/{ciclo}/{eleição}/dados/{uf}/{uf}-c{cargo:04d}-e{eleição:06d}-u.jws    (fallback: mesmo nome .json)
Campos usados: s.pst (% de seções totalizadas = "% apurado"), tf (totalização final s/n), dg/hg (geração),
carg[0].nv (vagas), carg[0].agr[].par[].cand[] (nmu, vap, pvap, st, e), v.tv (total), v.vv (válidos),
v.vb/pvb (brancos), v.tvn/ptvn (nulos, incl. técnicos), v.van/vansj (anulados), e.te (eleitorado apto) — rótulos do
app oficial. "Total" = votos apurados / votos possíveis (eleitorado × votos por eleitor).
Parcial por região (só Presidente): arquivo de acompanhamento {BASE}/{ciclo}/{eleição}/dados/br/br-e{eleição:06d}-ab.jws,
uma entrada por UF (+ zz = exterior) com s.st/s.ts (seções), e.c (comparecimento = votos para Presidente) e e.te.
Códigos (comum/config/ele-c.json, pleito 3220): federal 1ºT 6257 / 2ºT 6258 · estadual 1ºT 6259 / 2ºT 6260.

Tempo real: só Presidente (+ parcial por região), a cada mudança do % apurado. No 2º turno, junto, a projeção do
resultado final (eleicao_modelo.py): andamento por município + total por UF de cada um dos 28 arquivos, sem IA. No 1º turno, a pedido, cada fluxo
(Presidente, governadores, senadores) manda no máximo 1 mensagem a cada THROTTLE_T1 (30 min); no 2º turno, tempo real.
Governadores e senadores eleitos nos 27 estados (c0003/c0005, eleição 6259; não há arquivo nacional): a cada 30 min,
e na hora (checagem a cada minuto, ETag) sempre que surgir um eleito novo ou um 2º turno definido — a lista completa
com o 🆕 no início. Governadores e senadores em mensagens separadas; a de senadores traz a composição do Senado a
partir de 2027 (eleitos + projetados + os 27 com mandato até 2031, de league/senado_2031.json). Quando todos os
governos (eleito ou 2º turno) e as 54 vagas estiverem definidos: consolidado final e para.
"Eleito" = marca oficial do TSE (st "Eleito…" / e="s") ou matematicamente definido: os votos que ainda podem
entrar (e.esnt = eleitorado das seções não totalizadas) não alteram o resultado.
Deputados federais/estaduais de SC (RS encerrado a pedido): uma única mensagem quando chegar a 100% apurado — a lista
oficial se o TSE já tiver marcado os eleitos, senão o cálculo pelas regras de legenda (project_seats, conferido com
a distribuição de vagas do próprio TSE em agr.vag) — e para. Agrupados por legenda com votos nominais + de legenda.
Ciclo de vida: 1º turno (04/10) → se Presidente for ao 2º turno, volta em 25/10 só com ele →
encerra (state["finished"]) e o script vira no-op. Limite duro: ELEICAO_ATE (default 2026-10-27).
Limite da API: o CDN anuncia 2000 req/s (x-ratelimit-limit "2000;w=1") e cache de ~60 s; usamos ≤6 req/min com
ETag (If-None-Match → 304) e recuamos em 429/5xx ou quando x-ratelimit-remaining fica baixo.
"""
import base64
import gzip
import json
import logging
import math
import os
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger("eleicao")
BRT = ZoneInfo("America/Sao_Paulo")
BASE = os.environ.get("TSE_BASE_URL", "https://resultados.tse.jus.br/oficial").rstrip("/")
CICLO = "ele2026"
STATE = os.path.join(os.environ.get("LEAGUE_DIR", "/opt/league"), "eleicao_state.json")
SENADO_2031 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "senado_2031.json")
# chave pública "prod" embutida no app oficial (assets/assinatura-jws/prod.jwk.json)
JWK_KID = "sNbt9Q_fLS65zE1_ZLNV-XRRwPY"
JWK_X = "kWlpNHjuws1csyQZwzn3Fhzbi3RD435RbpThtSr4hMc"
UA = "mr-irrelevant-eleicao/1.0 (+aviso pessoal, <=6 req/min)"
SUMM_VER = 6  # muda quando summarize() ganha campos: invalida o cache (sem If-None-Match) na próxima rodada


@dataclass(frozen=True)
class Race:
    key: str
    label: str
    emoji: str
    uf: str
    cargo: int
    ele_t1: int
    ele_t2: Optional[int]
    top: int = 0  # quantos candidatos mostrar (0 = todos)


RACES = [
    Race("pres", "Presidente", "🇧🇷", "br", 1, 6257, 6258, top=6),
]
BY_KEY = {r.key: r for r in RACES}
UFS = "ac al am ap ba ce df es go ma mg ms mt pa pb pe pi pr rj rn ro rr rs sc se sp to".split()
GOV = [Race(f"gov_{uf}", f"Governo {uf.upper()}", "", uf, 3, 6259, None) for uf in UFS]
SENADO = [Race(f"sen_{uf}", f"Senado {uf.upper()}", "", uf, 5, 6259, None) for uf in UFS]
DEPUTADOS = [
    Race("depf_sc", "Deputados Federais eleitos — SC", "", "sc", 6, 6259, None),
    Race("depe_sc", "Deputados Estaduais eleitos — SC", "", "sc", 7, 6259, None),
]
ELEITOS_EVERY = timedelta(minutes=30)
ELEITOS_CHECK_EVERY = timedelta(minutes=1)
THROTTLE_T1 = timedelta(minutes=30)
# Exterior (2º turno): no máximo 1 mensagem a cada 30 min e só quando muda. O TSE só publica qualquer resultado
# (inclusive do exterior) depois das 17h de Brasília: antes disso, a parcial por país compilada pelo g1 a partir dos
# boletins de urna (eleicao_g1.py), sem projeção; a partir das 17h, só a API oficial, com a projeção do exterior.
EXT_EVERY = timedelta(minutes=30)
EXT_CHECK = timedelta(minutes=5)
EXT_DESDE = datetime(2026, 10, 25, 8, 0, tzinfo=BRT)
EXT_OFICIAL = datetime(2026, 10, 25, 17, 0, tzinfo=BRT)
# Convite curto com o card do link do grupo, a pedido: 15h30 e de 15 em 15 min entre 16h e 17h30 do 2º turno.
GRUPO_LINK = "https://chat.whatsapp.com/B5aT3oUDdyHG7CBNFqjdT9"
PROJ_MIN_PST = 5.0  # a pedido: projeção (texto, gráfico e exterior) só com mais de 5% das urnas apuradas
# Pausa: decisão judicial prorrogou a votação até as 19h e o TSE parou de atualizar (17h06). Até a apuração voltar,
# a última parcial (sem projeção) vai de 30 em 30 min; a partir das 19h, volta o tempo real com projeção.
PAUSA_DESDE = datetime(2026, 10, 25, 17, 30, tzinfo=BRT)
PAUSA_ATE = datetime(2026, 10, 25, 19, 0, tzinfo=BRT)
PAUSA_EVERY = timedelta(minutes=30)
CONVITE_HORARIOS: List[str] = []  # encerrado a pedido (eram 15:30 e de 15 em 15 min entre 16h e 17h30)
CONVITE_DIA = datetime(2026, 10, 25, tzinfo=BRT)
CONVITE_TOLERANCIA = timedelta(minutes=10)  # passou disso (job parado, etc.), pula: não manda convite atrasado  # 1º turno: no máximo 1 mensagem por fluxo a cada 30 min
DEP_CHECK_EVERY = timedelta(minutes=1)
CARGO_NOME = {6: "Deputado Federal", 7: "Deputado Estadual"}
REGIOES = [
    ("Norte", {"ac", "ap", "am", "pa", "ro", "rr", "to"}),
    ("Nordeste", {"al", "ba", "ce", "ma", "pb", "pe", "pi", "rn", "se"}),
    ("Centro-Oeste", {"df", "go", "mt", "ms"}),
    ("Sudeste", {"es", "mg", "rj", "sp"}),
    ("Sul", {"pr", "rs", "sc"}),
    ("Exterior", {"zz"}),
]
TURNS = {1: datetime(2026, 10, 4, 16, 50, tzinfo=BRT), 2: datetime(2026, 10, 25, 16, 50, tzinfo=BRT)}
TURN_CAP = timedelta(days=2, hours=7)  # sem totalização final até ~D+2 23:59 → encerra o turno com o que houver
HARD_STOP = datetime.fromisoformat(os.environ.get("ELEICAO_ATE", "2026-10-27T23:59")).replace(tzinfo=BRT)


class ApiError(Exception):
    pass


def url_for(race: Race, turn: int, ext: str = "jws") -> str:
    ele = race.ele_t1 if turn == 1 else race.ele_t2
    return f"{BASE}/{CICLO}/{ele}/dados/{race.uf}/{race.uf}-c{race.cargo:04d}-e{ele:06d}-u.{ext}"


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def decode_jws(token: str, verify: bool = True) -> dict:
    """Payload JSON de um JWS compacto do TSE; confere a assinatura Ed25519 quando `cryptography` existe."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ApiError("JWS malformado")
    header = json.loads(_b64(parts[0]))
    if verify:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        except ImportError:
            log.warning("cryptography ausente: assinatura do TSE não conferida")
        else:
            if header.get("alg") != "EdDSA" or header.get("kid") != JWK_KID:
                raise ApiError(f"JWS com chave inesperada: {header}")
            try:
                Ed25519PublicKey.from_public_bytes(_b64(JWK_X)).verify(_b64(parts[2]), f"{parts[0]}.{parts[1]}".encode())
            except Exception as e:  # InvalidSignature
                raise ApiError(f"assinatura do TSE inválida: {e!r}")
    return json.loads(_b64(parts[1]))


def _num(s) -> float:
    try:
        return float(str(s).replace(".", "").replace(",", ".")) if "," in str(s) else float(s)
    except (TypeError, ValueError):
        return 0.0


def summarize(payload: dict) -> dict:
    """Só o que a mensagem precisa (é isso que fica salvo no estado entre execuções)."""
    carg = (payload.get("carg") or [{}])[0]
    cands, legendas = [], []
    for i, agr in enumerate(carg.get("agr") or []):
        pars = agr.get("par") or []
        # federação: votos na agremiação; partido isolado: no próprio partido
        src = agr if agr.get("tp") == "f" else (pars[0] if len(pars) == 1 else {})
        legendas.append({"nm": agr.get("com") or agr.get("nm") or "", "fed": agr.get("tp") == "f",
                         "full": agr.get("nm") or "", "vag": int(agr.get("vag") or 0),
                         "tvtn": int(src.get("tvtn") or sum(int(p.get("tvtn") or 0) for p in pars)),
                         "tvtl": int(src.get("tvtl") or sum(int(p.get("tvtl") or 0) for p in pars))})
        for par in pars:
            for c in par.get("cand") or []:
                cands.append({"n": str(c.get("n") or ""), "nm": c.get("nmu") or c.get("nm") or "?", "p": par.get("sg") or "",
                              "v": int(c.get("vap") or 0),
                              "pct": c.get("pvap") or "0,00", "st": (c.get("st") or "").strip(), "e": c.get("e") == "s",
                              "seq": int(c.get("seq") or 99), "agr": i})
    cands.sort(key=lambda c: (-c["v"], c["seq"]))
    s, v = payload.get("s") or {}, payload.get("v") or {}
    n = lambda k: int(v.get(k) or 0)  # noqa: E731
    return {"pst": s.get("pst") or "0,00", "st": int(s.get("st") or 0), "ts": int(s.get("ts") or 0),
            "tf": payload.get("tf") == "s", "gen": f"{payload.get('dg', '')} {payload.get('hg', '')}".strip(),
            "nv": int(carg.get("nv") or 1), "cands": cands,
            "tv": n("tv"), "vv": n("vv"), "vb": n("vb"), "pvb": v.get("pvb") or "0,00", "tvn": n("tvn"),
            "ptvn": v.get("ptvn") or "0,00", "van": n("van") + n("vansj"),
            "te": int((payload.get("e") or {}).get("te") or 0), "esnt": int((payload.get("e") or {}).get("esnt") or 0),
            "c": int((payload.get("e") or {}).get("c") or 0), "est": int((payload.get("e") or {}).get("est") or 0),
            "leg": legendas, "qe": int(carg.get("qe") or 0)}


def summarize_regions(payload: dict) -> dict:
    """Seções totalizadas e votos (comparecimento) somados por região, a partir das UFs do arquivo -ab."""
    ufs = {a.get("cdabr"): a for a in payload.get("abr") or [] if a.get("tpabr") == "uf"}
    reg = []
    for nome, siglas in REGIOES:
        rows = [ufs[u] for u in siglas if u in ufs]
        reg.append({"nm": nome, "st": sum(int((a.get("s") or {}).get("st") or 0) for a in rows),
                    "ts": sum(int((a.get("s") or {}).get("ts") or 0) for a in rows),
                    "c": sum(int((a.get("e") or {}).get("c") or 0) for a in rows),
                    "te": sum(int((a.get("e") or {}).get("te") or 0) for a in rows)})
    return {"gen": f"{payload.get('dg', '')} {payload.get('hg', '')}".strip(), "reg": reg}


def needs_second_round(summ: dict) -> bool:
    if any("turno" in c["st"].lower() for c in summ["cands"]):
        return True
    if any(c["e"] or c["st"].lower().startswith("eleit") for c in summ["cands"]):
        return False
    # sem situação oficial (limite de tempo): líder abaixo de 50% dos válidos
    return bool(summ["cands"]) and _num(summ["cands"][0]["pct"]) <= 50.0 and summ["cands"][0]["v"] > 0


def _name(s: str) -> str:
    small = {"de", "da", "do", "das", "dos", "e"}
    return " ".join(w.lower() if w.lower() in small and i else w.capitalize() for i, w in enumerate(s.split()))


def _int_br(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def _mark(c: dict) -> str:
    st = c["st"].lower()
    if "turno" in st:
        return " 🔁 2º turno"
    if c["e"] or st.startswith("eleit"):
        return " ✅ eleito"
    return ""


def _pct(part: int, total: int) -> str:
    return f"{100 * part / total:.2f}".replace(".", ",") if total else "0,00"


def format_race(race: Race, summ: Optional[dict]) -> str:
    title = f"{race.emoji} *{race.label}*" + (" (Brasil)" if race.uf == "br" else "") \
        + (f" ({summ['nv']} vagas)" if summ and summ["nv"] > 1 else "")
    if not summ or _num(summ["pst"]) <= 0:
        return f"{title}\n_aguardando os primeiros boletins_"
    head = f"{title} · *{summ['pst']}%* apurado" + (" · ✔️ final" if summ["tf"] else "")
    lines = [head]
    cands = summ["cands"]
    for i, c in enumerate(cands[: race.top or None], 1):
        lines.append(f"{i}. {_name(c['nm'])} ({c['p']}) {c['pct']}% · {_int_br(c['v'])}{_mark(c)}")
    k = summ["nv"]  # disputa pela última vaga: k-ésimo × (k+1)-ésimo
    if len(cands) > k and cands[k - 1]["v"] > 0:
        a, b = cands[k - 1], cands[k]
        gap_pp = f"{_num(a['pct']) - _num(b['pct']):.2f}".replace(".", ",")
        who = "1º → 2º" if k == 1 else f"{k}º → {k + 1}º"
        lines.append(f"↔️ {who}: {gap_pp} p.p. ({_int_br(a['v'] - b['v'])} votos)")
    tv = summ.get("tv", 0)
    if tv:
        lines.append(f"▫️ Válidos: {_int_br(summ.get('vv', 0))} ({_pct(summ.get('vv', 0), tv)}%)")
        lines.append(f"⚪ Brancos: {_int_br(summ.get('vb', 0))} ({summ.get('pvb', '0,00')}%)")
        lines.append(f"⚫ Nulos: {_int_br(summ.get('tvn', 0))} ({summ.get('ptvn', '0,00')}%)")
        if summ.get("van"):
            lines.append(f"⚖️ Anulados/sub judice: {_int_br(summ['van'])}")
        possible = summ.get("te", 0) * summ["nv"]  # majoritária: 1 voto por vaga (Senado com 2 vagas = 2 votos)
        lines.append(f"🧮 *Total: {_int_br(tv)}{f' / {_int_br(possible)}' if possible else ''} votos*"
                     + (f" ({summ['nv']} por eleitor)" if summ["nv"] > 1 else ""))
    return "\n".join(lines)


def is_elected(c: dict) -> bool:
    """Situação "Eleito", "Eleito por QP", "Eleito por média"…; a flag e="s" só vale sem situação escrita — o TSE
    também marca e="s" em quem vai ao 2º turno (situação "2º turno")."""
    st = c["st"].lower()
    return st.startswith("eleit") if st else c["e"]


def majoritarian_result(summ: dict, second_round: bool) -> Tuple[List[dict], List[dict]]:
    """(eleitos, 2º turno) — oficial do TSE quando houver; senão só o que já está matematicamente definido.

    R = eleitores das seções não totalizadas: no pior caso todos votam em um único candidato (+R para ele) ou
    nenhum vota nele (+R no total de válidos). Senado (nv vagas): i-ésimo eleito se v_i − v_(nv+1) > R.
    Governo (maioria absoluta): eleito se 2·v1 > V + R; 2º turno definido se 2·v1 + R ≤ V e v2 − v3 > R."""
    cands, nv = summ["cands"], summ["nv"]
    official = [c for c in cands if is_elected(c)]
    runoff = [c for c in cands if "turno" in c["st"].lower()]
    if official or runoff or summ["tf"]:
        return official, runoff
    if not cands or not cands[0]["v"]:
        return [], []
    R = summ.get("esnt", 0)
    v = [c["v"] for c in cands] + [0, 0, 0]
    if not second_round:
        return [c for c in cands[:nv] if c["v"] - v[nv] > R], []
    V = sum(c["v"] for c in cands)
    if 2 * v[0] > V + R:
        return [cands[0]], []
    if 2 * v[0] + R <= V and v[1] - v[2] > R:
        return [], cands[:2]
    return [], []


def _who(c: dict) -> str:
    return f"{_name(c['nm'])} ({c['p']})"


def majoritarian_events(gov: Dict[str, dict], sen: Dict[str, dict]) -> List[Tuple[str, str]]:
    """Definições já conhecidas, como (id estável, linha p/ o aviso 🆕): eleito ao governo/senado ou 2º turno definido."""
    ev: List[Tuple[str, str]] = []
    for r in GOV:
        summ = gov.get(r.key)
        if summ and _num(summ["pst"]) > 0:
            el, rt = majoritarian_result(summ, second_round=True)
            if el:
                ev.append((f"{r.key}:el:{el[0]['nm']}", f"✅ Governo {r.uf.upper()}: {_who(el[0])} {el[0]['pct']}%"))
            elif rt:
                ev.append((f"{r.key}:2t:{rt[0]['nm']}|{rt[1]['nm']}",
                           f"🔁 Governo {r.uf.upper()}: 2º turno — {_who(rt[0])} × {_who(rt[1])}"))
    for r in SENADO:
        summ = sen.get(r.key)
        if summ and _num(summ["pst"]) > 0:
            for c in majoritarian_result(summ, second_round=False)[0]:
                ev.append((f"{r.key}:el:{c['nm']}", f"✅ Senado {r.uf.upper()}: {_who(c)} {c['pct']}%"))
    return ev


def _by_party(cs: List[dict]) -> str:
    n: Dict[str, int] = {}
    for c in cs:
        n[c["p"]] = n.get(c["p"], 0) + 1
    return " · ".join(f"{p} {k}" for p, k in sorted(n.items(), key=lambda kv: (-kv[1], kv[0]))) or "—"


def _new_block(new: Optional[List[Tuple[str, str]]]) -> List[str]:
    return ["🆕 *Acabou de ser definido*"] + [line for _, line in new] + [""] if new else []


def governors_status(gov: Dict[str, dict]) -> Tuple[List[Tuple[str, dict]], List[Tuple[str, List[dict]]], List[str]]:
    """(eleitos [(uf, cand)], 2º turno [(uf, [c1, c2])], sem definição ["UF 12,34%"])."""
    el_, rt_, open_ = [], [], []
    for r in GOV:
        summ = gov.get(r.key)
        if not summ or _num(summ["pst"]) <= 0:
            open_.append(f"{r.uf.upper()} –")
            continue
        el, rt = majoritarian_result(summ, second_round=True)
        if el:
            el_.append((r.uf, el[0]))
        elif rt:
            rt_.append((r.uf, rt))
        else:
            open_.append(f"{r.uf.upper()} {summ['pst']}%")
    return el_, rt_, open_


def senators_status(sen: Dict[str, dict]) -> Tuple[List[Tuple[str, List[dict], int, str]], List[dict], List[dict]]:
    """Por UF: (uf, eleitos, vagas em aberto, % apurado); + lista de eleitos; + projetados (lideram as vagas abertas)."""
    rows, elected, projected = [], [], []
    for r in SENADO:
        summ = sen.get(r.key)
        if not summ or _num(summ["pst"]) <= 0:
            rows.append((r.uf, [], 2, "–"))
            continue
        el, _ = majoritarian_result(summ, second_round=False)
        elected += el
        projected += [c for c in summ["cands"][: summ["nv"]] if c not in el]
        rows.append((r.uf, el, summ["nv"] - len(el), summ["pst"]))
    return rows, elected, projected


def _senado_snapshot(path: str = SENADO_2031) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        log.warning("sem %s: composição do Senado sem os mandatos até 2031 / composição atual", path)
        return {}


def load_continuing(path: str = SENADO_2031) -> List[dict]:
    return _senado_snapshot(path).get("senadores") or []


def load_current_senate(path: str = SENADO_2031) -> Dict[str, int]:
    """Os 81 em exercício hoje, por partido (dados abertos do Senado)."""
    return _senado_snapshot(path).get("atual_por_partido") or {}


def format_senate_composition(elected: List[dict], projected: List[dict], continuing: List[dict],
                              current: Optional[Dict[str, int]] = None) -> List[str]:
    """2027 por partido (eleitos + projetados + continuam), ordenado pelo total de 2027, com quanto era hoje."""
    current = load_current_senate() if current is None else current
    parts: Dict[str, List[int]] = {}
    for i, group in enumerate((elected, projected, [{"p": c["partido"]} for c in continuing])):
        for c in group:
            parts.setdefault(c["p"], [0, 0, 0])[i] += 1
    for p in current:  # quem tem cadeira hoje e pode zerar em 2027
        parts.setdefault(p, [0, 0, 0])
    total = len(elected) + len(projected) + len(continuing)
    out = [f"🏛️ *Senado a partir de 2027 — {total} cadeiras por partido*" + (" (era → vai)" if current else "")]
    for p, (e, pr, ct) in sorted(parts.items(), key=lambda kv: (-sum(kv[1]), -kv[1][0], -current.get(kv[0], 0), kv[0])):
        now_ = e + pr + ct
        bits = " + ".join(b for b in (f"{e} ✅" if e else "", f"{pr} ⏳" if pr else "", f"{ct} ↪️" if ct else "") if b)
        if current:
            was = current.get(p, 0)
            delta = f"+{now_ - was}" if now_ > was else (f"−{was - now_}" if now_ < was else "=")
            out.append(f"{p}: {was} → *{now_}* ({delta})" + (f" · {bits}" if bits else ""))
        else:
            out.append(f"{p} *{now_}* = {bits}")
    out.append("_era = os 81 em exercício hoje · ✅ eleitos em 2026 · ⏳ projetados (lideram as vagas ainda abertas) · "
               "↪️ continuam até 2031 (eleitos em 2022; quem está no cargo hoje e o partido atual, dados do Senado)_")
    return out


def _gov_lines(gov: Dict[str, dict], tag: Callable[[str], str]) -> Tuple[List[str], List[dict]]:
    el_, rt_, open_ = governors_status(gov)
    lines = [f"✅ {len(el_)} eleitos · 🔁 {len(rt_)} no 2º turno · ⏳ {len(open_)} sem definição"]
    for uf in UFS:
        e = next((c for u, c in el_ if u == uf), None)
        rt = next((c for u, c in rt_ if u == uf), None)
        if e:
            lines.append(f"✅ {uf.upper()}: {_who(e)} {e['pct']}%{tag('gov_' + uf)}")
        elif rt:
            lines.append(f"🔁 {uf.upper()}: 2º turno — {_who(rt[0])} × {_who(rt[1])}{tag('gov_' + uf)}")
    if open_:
        lines.append(f"⏳ Sem definição: {', '.join(open_)}")
    return lines, [c for _, c in el_]


def _sen_lines(sen: Dict[str, dict], tag: Callable[[str], str]) -> Tuple[List[str], List[dict], List[dict]]:
    rows, elected, projected = senators_status(sen)
    lines = [f"*{len(elected)} de {2 * len(SENADO)} vagas definidas*"]
    pending = []
    for uf, el, rest, pst in rows:
        if el:
            lines.append(f"✅ {uf.upper()}: " + ", ".join(_who(c) for c in el)
                         + (f" · {rest} vaga em aberto ({pst}%)" if rest else "") + tag("sen_" + uf))
        else:
            pending.append(f"{uf.upper()} {pst}%")
    if pending:
        lines.append(f"⏳ Sem definição: {', '.join(pending)}")
    return lines, elected, projected


FOOT = "_✅ = eleito oficialmente ou matematicamente definido (os votos que faltam não mudam o resultado). % dos válidos. TSE._"


def _tagger(new: Optional[List[Tuple[str, str]]]) -> Callable[[str], str]:
    fresh = {eid.split(":")[0] for eid, _ in new or []}
    return lambda key: " 🆕" if key in fresh else ""


def format_governors(gov: Dict[str, dict], now: datetime, new: Optional[List[Tuple[str, str]]] = None) -> str:
    lines, el = _gov_lines(gov, _tagger(new))
    out = [f"🏛️ *Governadores eleitos* — {now:%H:%M}", ""] + _new_block(new)
    out += [f"📊 *Eleitos por partido:* {_by_party(el)}", ""] + lines + ["", FOOT]
    return "\n".join(out)


def format_senators(sen: Dict[str, dict], now: datetime, new: Optional[List[Tuple[str, str]]] = None,
                    continuing: Optional[List[dict]] = None) -> str:
    lines, el, proj = _sen_lines(sen, _tagger(new))
    out = [f"🏛️ *Senadores eleitos* — {now:%H:%M}", ""] + _new_block(new)
    out += [f"📊 *Eleitos por partido:* {_by_party(el)}", ""] + lines + [""]
    out += format_senate_composition(el, proj, continuing if continuing is not None else load_continuing())
    return "\n".join(out + ["", FOOT])


def format_final_majoritarian(gov: Dict[str, dict], sen: Dict[str, dict], now: datetime,
                              new: Optional[List[Tuple[str, str]]] = None, continuing: Optional[List[dict]] = None) -> str:
    tag = _tagger(new)
    g_lines, g_el = _gov_lines(gov, tag)
    s_lines, s_el, s_proj = _sen_lines(sen, tag)
    out = [f"🏁 *Consolidado final — governadores e senadores* — {now:%H:%M}", ""] + _new_block(new)
    out += ["🏛️ *Governadores*", f"📊 {_by_party(g_el)}"] + g_lines[1:] + [""]
    out += ["🏛️ *Senadores*", f"📊 {_by_party(s_el)}"] + s_lines[1:] + [""]
    out += format_senate_composition(s_el, s_proj, continuing if continuing is not None else load_continuing())
    out += ["", FOOT, "_Todos os governos e as 54 vagas do Senado estão definidos — este acompanhamento termina aqui._"]
    return "\n".join(out)


def deputies_known(summ: Optional[dict]) -> bool:
    """O TSE já marcou os eleitos de todas as vagas (ou fechou a totalização com eleitos marcados)."""
    n = sum(is_elected(c) for c in summ["cands"]) if summ else 0
    return n >= summ["nv"] or (summ["tf"] and n > 0) if summ else False


def _format_groups(groups: Dict[int, List[Tuple[dict, str]]], legs: List[dict]) -> List[str]:
    """Eleitos agrupados por legenda: vagas desc, votos da legenda desc."""
    votes = lambda i: legs[i]["tvtn"] + legs[i]["tvtl"] if 0 <= i < len(legs) else 0  # noqa: E731
    out: List[str] = []
    for i in sorted(groups, key=lambda i: (-len(groups[i]), -votes(i))):
        leg = legs[i] if 0 <= i < len(legs) else {"nm": groups[i][0][0]["p"], "fed": False, "full": "", "tvtn": 0, "tvtl": 0}
        name = f"{leg['nm']}" + (f" ({_name(leg['full'])})" if leg["fed"] and leg["full"] else "")
        n = len(groups[i])
        out += ["", f"*{name}* — {n} eleito{'s' if n > 1 else ''}"
                    + (f" · legenda: {_int_br(votes(i))} votos "
                       f"({_int_br(leg['tvtn'])} nominais + {_int_br(leg['tvtl'])} de legenda)" if leg["tvtn"] else "")]
        for c, how in sorted(groups[i], key=lambda ch: -ch[0]["v"]):
            out.append(f"• {_name(c['nm'])} ({c['p']}) — {_int_br(c['v'])}{f' ({how})' if how else ''}")
    return out


def format_elected_deputies(race: Race, summ: dict, now: datetime) -> str:
    groups: Dict[int, List[Tuple[dict, str]]] = {}
    for c in summ["cands"]:
        if is_elected(c):
            how = {"eleito por qp": "QP", "eleito por média": "média"}.get(c["st"].lower(), "")
            groups.setdefault(c.get("agr", -1), []).append((c, how))
    out = [f"✅ *{race.label}* ({summ['nv']} vagas) — {now:%H:%M}" + (" · ✔️ totalização final" if summ["tf"] else "")]
    out += _format_groups(groups, summ.get("leg") or [])
    out += ["", "_Lista oficial do TSE. QP = quociente partidário · média = sobras._"]
    return "\n".join(out)


def _round_half_down(x: float) -> int:
    f = math.floor(x)
    return f + 1 if x - f > 0.5 else f  # CE art. 106: despreza a fração ≤ meio


def project_seats(summ: dict) -> Tuple[Dict[int, int], List[Tuple[dict, str, int]], int]:
    """Quem estaria eleito agora pelas regras de legenda (CE arts. 106–109, Lei 14.211/2021, STF ADIs 7228/7263/7325).

    QE = válidos / vagas (fração ≤ ½ desprezada; usa o qe do TSE quando vier no arquivo).
    1) QP = votos da legenda // QE; vagas para os mais votados com ≥ 10% do QE.
    2) Sobras pela maior média (votos / (vagas + 1)): legendas com ≥ 80% do QE e candidato com ≥ 20% do QE.
    3) Restando vagas, todas as legendas pela maior média, sem as exigências (STF, 2024). Empate: mais votos.
    Devolve (vagas por legenda, [(candidato, "QP"|"média"|"média 3ª fase", legenda)], QE)."""
    nv, legs = summ["nv"], summ.get("leg") or []
    votes = [lg["tvtn"] + lg["tvtl"] for lg in legs]
    qe = summ.get("qe") or (_round_half_down(sum(votes) / nv) if nv else 0)
    rem = {i: sorted((c for c in summ["cands"] if c.get("agr") == i), key=lambda c: -c["v"]) for i in range(len(legs))}
    seats = {i: 0 for i in range(len(legs))}
    elected: List[Tuple[dict, str, int]] = []
    if not qe:
        return seats, elected, qe
    for i, v in enumerate(votes):
        while seats[i] < v // qe and rem[i] and rem[i][0]["v"] >= 0.1 * qe:
            elected.append((rem[i].pop(0), "QP", i))
            seats[i] += 1
    phases = (("média", lambda i, v: v >= 0.8 * qe and rem[i][0]["v"] >= 0.2 * qe), ("média 3ª fase", lambda i, v: True))
    for how, ok in phases:
        while len(elected) < nv:
            best = max(((v / (seats[i] + 1), v, i) for i, v in enumerate(votes) if rem[i] and ok(i, v)), default=None)
            if not best:
                break
            i = best[2]
            elected.append((rem[i].pop(0), how, i))
            seats[i] += 1
    return seats, elected, qe


def format_projected_deputies(uf: str, summaries: Dict[str, dict], races: List[Race], now: datetime,
                              final: bool = False) -> str:
    rules = ("Regras de legenda: QE; QP com candidato ≥ 10% do QE; sobras pela maior média (legenda ≥ 80% e "
             "candidato ≥ 20% do QE; 3ª fase com todas).")
    out = ([f"🏁 *Deputados {uf.upper()} eleitos — 100% apurado* — {now:%H:%M}",
            f"_{rules} Cálculo sobre 100% das seções; a lista oficial (diplomação) é a do TSE._"] if final else
           [f"🧮 *Deputados {uf.upper()} — eleitos se a apuração acabasse agora* — {now:%H:%M}",
            f"_{rules} Projeção, não é o resultado oficial._"])
    for r in races:
        summ = summaries.get(r.key)
        if not summ or _num(summ["pst"]) <= 0:
            continue
        seats, elected, qe = project_seats(summ)
        legs = summ.get("leg") or []
        groups: Dict[int, List[Tuple[dict, str]]] = {}
        for c, how, i in elected:
            groups.setdefault(i, []).append((c, how))
        out += ["", f"📜 *{CARGO_NOME.get(r.cargo, r.label)}* · {summ['pst']}% apurado · {summ['nv']} vagas · QE {_int_br(qe)}"]
        out += _format_groups(groups, legs)
        tse = {i: lg["vag"] for i, lg in enumerate(legs)}
        if sum(tse.values()) == summ["nv"]:
            diff = [f"{legs[i]['nm']} {seats[i]}×{tse[i]}" for i in tse if seats[i] != tse[i]]
            out.append("")
            out.append("✔️ _Vagas por legenda batem com a distribuição do TSE._" if not diff
                       else f"⚠️ _Difere do TSE (nosso×TSE): {', '.join(diff)}_")
    return "\n".join(out)


def format_regions(regions: Optional[dict]) -> str:
    if not regions or not any(r["c"] for r in regions.get("reg", [])):
        return ""
    lines = ["📍 *Presidente por região* — % apurado · votos apurados / total"]
    for r in regions["reg"]:
        total = f" / {_int_br(r['te'])}" if r.get("te") else ""
        lines.append(f"{r['nm']}: {_pct(r['st'], r['ts'])}% · {_int_br(r['c'])}{total}")
    return "\n".join(lines)


def format_update(turn: int, races: List[Race], summaries: Dict[str, dict], now: datetime,
                  regions: Optional[dict] = None, proj: Optional[dict] = None) -> str:
    gens = []
    for s in summaries.values():
        try:
            gens.append(datetime.strptime(s["gen"], "%d/%m/%Y %H:%M:%S"))
        except (KeyError, TypeError, ValueError):
            pass
    blocks = [format_race(r, summaries.get(r.key)) for r in races]
    if proj and any(r.key == "pres" for r in races):
        blocks.append(format_projection(proj, projection_names(summaries.get("pres"))))
    if any(r.key == "pres" for r in races) and format_regions(regions):
        blocks.append(format_regions(regions))
    tail = f"TSE · arquivo gerado {max(gens):%H:%M:%S}" if gens else "TSE"
    return f"🗳️ *Apuração {turn}º turno* — {now:%H:%M}\n\n" + "\n\n".join(blocks) + f"\n\n_{tail}_"


# ----------------------------------------------------------------------------- projeção (2º turno)
_MODELO = None


def projection_model():
    """Modelo carregado uma vez por processo (base do 1º turno: eleicao_base_2026.json); None se faltar a base."""
    global _MODELO
    if _MODELO is None:
        try:
            import eleicao_modelo
            _MODELO = eleicao_modelo.modelo_2026()
        except Exception as e:  # sem base / base corrompida: o quadro segue sem projeção
            log.warning("modelo de projeção indisponível: %s", e)
            _MODELO = False
    return _MODELO or None


def load_projection_inputs(turn: int, state: dict, fetcher: Callable = None) -> Tuple[Dict[str, float], Dict[str, tuple]]:
    """Andamento por município ({uf}-e{ele}-ab) e votos A/B por UF ({uf}-c0001-e{ele}-u), 28 abrangências.

    ≤ 56 requisições por rodada (gzip + ETag: o que não mudou volta 304)."""
    import eleicao_modelo
    fetcher = fetcher or fetch
    pres = BY_KEY["pres"]
    ele = pres.ele_t1 if turn == 1 else pres.ele_t2
    a_nr, b_nr = "22", "13"

    def andamento(uf: str):
        def parse(p: dict) -> Dict[str, float]:
            out = {}
            for m in p.get("abr") or []:
                if m.get("tpabr") == "mun":
                    e = m.get("e") or {}
                    te = int(e.get("te") or 0)
                    out[uf + str(m.get("cdabr"))] = int(e.get("est") or 0) / te if te else 0.0
            return out
        urls = [f"{BASE}/{CICLO}/{ele}/dados/{uf}/{uf}-e{ele:06d}-ab.{ext}" for ext in ("jws", "json")]
        return _load_cached(urls, parse, state, fetcher) or {}

    def votos(uf: str):
        race = Race(f"proj_{uf}", uf, "", uf, 1, ele, ele)
        summ = load_race(race, 1, state, fetcher) or {}
        v = {c.get("n"): c["v"] for c in summ.get("cands") or []}
        return float(v.get(a_nr, 0)), float(v.get(b_nr, 0))

    def one(uf: str):
        try:
            return uf, andamento(uf), votos(uf)
        except Exception as e:  # uma UF falhando não derruba a projeção: fica com o último retrato em cache
            log.warning("projeção %s: %s", uf, e)
            return uf, None, None

    frac: Dict[str, float] = {}
    obs_uf: Dict[str, tuple] = {}
    prev = state.get("proj_inputs") or {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for uf, fr, vo in ex.map(one, eleicao_modelo.UFS):
            if fr is None or vo is None:
                fr = {k: v for k, v in (prev.get("frac") or {}).items() if k.startswith(uf)}
                vo = tuple((prev.get("obs_uf") or {}).get(uf, (0.0, 0.0)))
            frac.update(fr)
            obs_uf[uf] = vo
    state["proj_inputs"] = {"frac": frac, "obs_uf": obs_uf}
    return frac, obs_uf


_REF2022 = None


def comparativo(model, frac: Dict[str, float], bruto_a: Optional[float], summ: Optional[dict] = None) -> Optional[dict]:
    """Nos lugares já apurados (fração de cada município), quanto A teve entre A e B no 1º turno de 2026 e no 1º e
    no 2º turno de 2022 — mesmos lugares, então dá para comparar com o bruto de agora sem viés de ordem de chegada.
    Idem para a abstenção: agora (TSE, seções totalizadas) × 1º turno nos mesmos municípios."""
    global _REF2022
    if bruto_a is None:
        return None
    if _REF2022 is None:
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eleicao_ref_2022.json")
            with open(path, encoding="utf-8") as f:
                _REF2022 = json.load(f)["municipios"]
        except (OSError, ValueError, KeyError):
            _REF2022 = {}
    acc = [0.0] * 8  # 2026 1T (a, b), 2022 1T (a, b), 2022 2T (a, b), 2026 1T (comparecimento, aptos)
    for m in model.muns:
        f = frac.get(m.key, 0.0)
        if f <= 0:
            continue
        acc[0] += f * m.a1
        acc[1] += f * m.b1
        acc[6] += f * m.c1
        acc[7] += f * m.aptos
        r = _REF2022.get(m.key)
        if r:
            for i in range(4):
                acc[2 + i] += f * r[i]
    share = lambda a, b: a / (a + b) if a + b else None  # noqa: E731
    est, comp = (summ or {}).get("est", 0), (summ or {}).get("c", 0)
    return {"agora": bruto_a, "t1_2026": share(acc[0], acc[1]), "t1_2022": share(acc[2], acc[3]),
            "t2_2022": share(acc[4], acc[5]),
            "abst_agora": 1 - comp / est if est and comp else None,
            "abst_t1": 1 - acc[6] / acc[7] if acc[7] else None}


def compute_projection(turn: int, state: dict, fetcher: Callable = None, summ: Optional[dict] = None) -> Optional[dict]:
    model = projection_model()
    if not model:
        return None
    frac, obs_uf = load_projection_inputs(turn, state, fetcher)
    r = model.projetar(frac, {}, obs_uf)
    out = {"comparativo": comparativo(model, frac, r["bruto_a"], summ), "kappa": r.get("kappa_br"),
           "share_a": r["share_a"], "sd": r["sd"], "lo": r["faixa90"][0], "hi": r["faixa90"][1], "prob_a": r["prob_a"],
           "apurado": r["apurado"], "bruto_a": r["bruto_a"],
           "regioes": {k: {"share_a": v["share_a"], "lo": v["faixa90"][0], "hi": v["faixa90"][1], "apurado": v["apurado"],
                           "obs_a": v["obs_a"], "obs_b": v["obs_b"]} for k, v in (r.get("regioes") or {}).items()}}
    hist = state.setdefault("proj_hist", [])
    if not hist or abs(hist[-1][1] - out["apurado"]) > 1e-6:  # série para conferir o modelo depois
        hist.append([round(time.time()), round(100 * out["apurado"], 3), round(100 * out["share_a"], 3),
                     round(100 * out["sd"], 3), None if out["bruto_a"] is None else round(100 * out["bruto_a"], 3)])
    log.info("projeção: A %.2f%% ±%.2f (P=%.3f) com %.2f%% apurado", 100 * out["share_a"], 164.5 * out["sd"],
             out["prob_a"], 100 * out["apurado"])
    return out


def armed_projection_note() -> str:
    """Ponto de partida do modelo (antes do 1º voto) + referências do research de 21/10 (Talk 2º turno)."""
    model = projection_model()
    if not model:
        return ""
    r = model.projetar({}, {}, {})
    return ("🔮 Junto com cada quadro vai a *projeção do modelo* para o resultado final — corrige o viés da ordem de "
            "chegada das urnas (Sul e Centro-Oeste apuram antes, Nordeste depois).\n"
            f"Ponto de partida, antes do 1º voto: Flávio Bolsonaro {_br(100 * r['share_a'])}% × Lula "
            f"{_br(100 * (1 - r['share_a']))}% — 1º turno por município + migração dos eliminados "
            "(Caiado 60/25, Cury 40/40, Renan 65/25; research de 21/10). Média das pesquisas até 21/10: 50,9 × 49,1.\n"
            "_Testado reencenando os 2º turnos de 2014, 2018 e 2022 na ordem real de chegada: erro médio de 0,23 p.p. "
            "a partir de 5% apurado; a faixa de 90% acertou 89% das vezes._")


def _br(x: float, nd: int = 1) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


def convite_curto(now: datetime) -> str:
    quando = "agora" if now >= EXT_OFICIAL else "hoje, a partir das 17h"
    return (f"🗳️ *2º turno ao vivo {quando}*, com *previsão de quem vai ganhar*.\n"
            "No começo o número engana: os votos do *Nordeste* são contados por último, e a previsão já leva isso "
            f"em conta.\n👉 {GRUPO_LINK}")


def _invite_tick(state: dict, send: Callable[[str], bool], now: datetime, sent: List[str]) -> None:
    feitos = state.setdefault("convites", [])
    for hhmm in CONVITE_HORARIOS:
        h, m = map(int, hhmm.split(":"))
        slot = CONVITE_DIA.replace(hour=h, minute=m)
        if hhmm in feitos or not (slot <= now < slot + CONVITE_TOLERANCIA):
            continue
        text = convite_curto(now)
        if send(text):
            sent.append(text)
            feitos.append(hhmm)
        return  # no máximo um por rodada


def format_exterior_g1(r: dict, now: datetime) -> str:
    """Parcial do exterior antes das 17h (g1, por país) — 2º × 1º turno nos mesmos países, sem projeção."""
    a, b, a1, b1 = r["a"], r["b"], r["a1"], r["b1"]
    out = [f"🌎 *Exterior · 2º turno* — {now:%H:%M}",
           "_Parcial do g1 (boletins de urna), antes de o TSE divulgar às 17h_", "",
           f"Já tem resultado em *{r['paises_com']} de {r['paises']} países*. Neles:", ""]
    for n, v, v1 in sorted((("22", a, a1), ("13", b, b1)), key=lambda x: -x[1]):
        _, short, dot = NOMES_T2[n]
        out.append(f"{dot} *{short}*: {_int_br(v)} votos ({_br(100 * v / (a + b))}%)")
        if a1 + b1:
            out.append(f"      no 1º turno: {_int_br(v1)} votos ({_br(100 * v1 / (a1 + b1))}%)")
    if a1 + b1:
        da, db = a - a1, b - b1
        sinal = lambda d: f"+{_int_br(d)}" if d >= 0 else f"−{_int_br(-d)}"  # noqa: E731
        out += ["", f"📈 Do 1º para o 2º turno: Flávio {sinal(da)} votos · Lula {sinal(db)} votos"]
    out += ["", "*Onde mais se votou até agora*"]
    for pais, va, vb in r["maiores"]:
        out.append(f"• {pais}: Flávio {_int_br(va)} × {_int_br(vb)} Lula")
    out += ["", "_O exterior é menos de 1% do eleitorado. Sem projeção: às 17h entram os números oficiais do TSE._"]
    return "\n".join(out)


def format_exterior_g1_completo(paises: Dict[str, dict], r: dict, now: datetime) -> str:
    """17h: a lista completa da parcial do g1, país a país (por continente), antes de passar só para o TSE."""
    a, b, a1, b1 = r["a"], r["b"], r["a1"], r["b1"]
    out = [f"🌎 *Exterior · lista completa até as 17h* — {now:%H:%M}",
           "_Parcial do g1 (boletins de urna), país a país — a última antes dos dados oficiais do TSE_", "",
           f"*{r['paises_com']} de {r['paises']} países* com resultado. Neles:",
           f"🔵 *Flávio*: {_int_br(a)} votos ({_br(100 * a / (a + b))}%)"
           + (f" · 1º turno: {_int_br(a1)}" if a1 + b1 else ""),
           f"🟠 *Lula*: {_int_br(b)} votos ({_br(100 * b / (a + b))}%)"
           + (f" · 1º turno: {_int_br(b1)}" if a1 + b1 else "")]
    por_cont: Dict[str, list] = {}
    sem = []
    for pais, d in paises.items():
        if d.get("repetido_de"):
            continue
        if d["t2"] and sum(d["t2"].values()) > 0:
            por_cont.setdefault(d["continente"] or "Outros", []).append((pais, d["t2"].get("22", 0), d["t2"].get("13", 0)))
        else:
            sem.append(pais)
    for cont, rows in por_cont.items():
        ta, tb = sum(x[1] for x in rows), sum(x[2] for x in rows)
        out += ["", f"*{cont}* — Flávio {_int_br(ta)} × {_int_br(tb)} Lula"]
        for pais, va, vb in sorted(rows, key=lambda x: -(x[1] + x[2])):
            out.append(f"• {pais}: Flávio {_int_br(va)} × {_int_br(vb)} Lula")
    if sem:
        out += ["", f"_Ainda sem resultado no g1 ({len(sem)}): {', '.join(sem)}_"]
    rep_ = r.get("repetidos") or {}
    if rep_:
        out.append("_Fora da soma — o g1 repetiu os números de outro país: "
                   + ", ".join(f"{p} (= {q})" for p, q in rep_.items()) + "_")
    return "\n".join(out)


AVISO_SO_TSE = ("📢 *A partir de agora, só dados oficiais do TSE.*\n"
                "A parcial do exterior pelo g1 (boletins de urna) acabou. Daqui em diante, o exterior e o Brasil vêm "
                "direto da API do TSE, com a projeção do resultado.")


def _obs_regioes(state: dict) -> Dict[str, tuple]:
    import eleicao_modelo
    out: Dict[str, list] = {}
    for uf, (a, b) in ((state.get("proj_inputs") or {}).get("obs_uf") or {}).items():
        r = out.setdefault(eleicao_modelo.REGIAO.get(uf, "EX"), [0.0, 0.0])
        r[0] += a
        r[1] += b
    return {k: tuple(v) for k, v in out.items()}


def format_pausa(summ: Optional[dict], regions: Optional[dict], now: datetime, primeira: bool,
                 obs_reg: Optional[Dict[str, tuple]] = None) -> str:
    gen = ((summ or {}).get("gen") or "").split(" ")[-1][:5]
    if primeira:
        head = ["⚖️ *Decisão judicial: votação prorrogada até as 19h*",
                f"O TSE parou de atualizar a apuração{' às ' + gen if gen else ''}. Até a apuração voltar, mandamos "
                "aqui a última parcial *de 30 em 30 minutos*, sem projeção."]
    else:
        head = [f"⏸️ *Apuração em pausa* — votação prorrogada até as 19h. Última parcial do TSE"
                f"{' (' + gen + ')' if gen else ''}, sem projeção; próxima atualização em 30 min."]
    return "\n".join(head) + "\n\n" + format_board_t2(summ, regions, None, now, obs_reg=obs_reg)


def format_exterior(summ: dict, now: datetime, proj: Optional[dict] = None) -> str:
    """Só os votos de fora do país (API oficial do TSE), com a projeção do resultado final do exterior."""
    cands = sorted(summ.get("cands") or [], key=lambda c: -c["v"])
    out = [f"🌎 *Exterior · 2º turno* — {now:%H:%M}",
           f"📊 *{summ['pst']}% das seções do exterior apuradas* ({_int_br(summ['st'])} de {_int_br(summ['ts'])})"
           + (" · ✔️ final" if summ["tf"] else "")]
    for c in cands:
        full, _, dot = NOMES_T2.get(c.get("n"), (_name(c["nm"]), "", "▫️"))
        out.append(f"{dot} {full} — *{c['pct']}%* · {_int_br(c['v'])} votos")
    if len(cands) >= 2 and cands[0]["v"] > cands[1]["v"]:
        lead = NOMES_T2.get(cands[0].get("n"), ("", _name(cands[0]["nm"]), ""))[1]
        out.append(f"↔️ {lead} à frente por {_int_br(cands[0]['v'] - cands[1]['v'])} votos no exterior")
    if summ.get("tv"):
        out.append(f"Brancos {summ.get('pvb', '0,00')}% · Nulos {summ.get('ptvn', '0,00')}% · "
                   f"🧮 {_int_br(summ['tv'])} votos de {_int_br(summ.get('te', 0))} eleitores no exterior")
    ex = ((proj or {}).get("regioes") or {}).get("EX")
    if ex:
        sa = ex["share_a"]
        out.append(f"🔮 Projeção do exterior no fim da apuração: Flávio *{_br(100 * sa)}%* "
                   f"({_br(100 * ex['lo'])} a {_br(100 * ex['hi'])}) × Lula *{_br(100 * (1 - sa))}%*")
    out.append("_Só o exterior (menos de 1% do eleitorado). Fonte: TSE; projeção estatística, não é resultado oficial._")
    return "\n".join(out)


def _exterior_tick(state: dict, send: Callable[[str], bool], now: datetime, fetcher: Callable, sent: List[str],
                   proj: Optional[dict] = None, g1_fetch: Optional[Callable[[], str]] = None) -> None:
    """2º turno: exterior a cada EXT_EVERY, só quando mudou desde o último envio; para no final.
    Antes das 17h: parcial do g1 (sem projeção). Depois: API oficial + projeção do exterior."""
    ts = now.timestamp()
    if now >= EXT_OFICIAL and not state.get("g1_final_sent") and not state.get("zz_final"):
        # 17h: uma vez só, a lista completa do g1 e o aviso de que daqui em diante é só a API oficial
        try:
            import eleicao_g1
            paises = eleicao_g1.parse((g1_fetch or eleicao_g1.fetch)())
            r = eleicao_g1.resumo(paises)
        except Exception as e:
            log.warning("exterior (g1, lista final): %s", e)
            paises, r = {}, None
        if r:
            text = format_exterior_g1_completo(paises, r, now)
            if not send(text):
                return  # tenta de novo no próximo minuto
            sent.append(text)
        if send(AVISO_SO_TSE):
            sent.append(AVISO_SO_TSE)
            state["g1_final_sent"] = True
            state["zz_at"] = ts  # o próximo do exterior (TSE) respeita os 30 min a partir daqui
        return
    if state.get("zz_final") or now < EXT_DESDE or ts - state.get("zz_check_at", 0) < EXT_CHECK.total_seconds() \
            or time.time() < state.get("backoff_until", 0):
        return
    state["zz_check_at"] = ts
    if now < EXT_OFICIAL:
        try:
            import eleicao_g1
            r = eleicao_g1.resumo(eleicao_g1.parse((g1_fetch or eleicao_g1.fetch)()))
        except Exception as e:  # página fora do ar / mudou de formato: só espera as 17h
            log.warning("exterior (g1): %s", e)
            return
        if not r:
            return
        sig = f"g1|{r['paises_com']}|{r['a']}|{r['b']}"
        if sig == state.get("zz_sig") or ts - state.get("zz_at", 0) < EXT_EVERY.total_seconds():
            return
        text = format_exterior_g1(r, now)
        if send(text):
            sent.append(text)
            state.update(zz_sig=sig, zz_at=ts)
        return
    ele = BY_KEY["pres"].ele_t2
    try:
        summ = load_race(Race("zz_pres", "Exterior", "", "zz", 1, ele, ele), 1, state, fetcher)
    except Exception as e:
        log.warning("exterior: %s", e)
        return
    if not summ or _num(summ["pst"]) <= 0:
        return
    sig = f"{summ['pst']}|" + "|".join(f"{c.get('n')}:{c['v']}" for c in summ["cands"])
    if sig == state.get("zz_sig") or ts - state.get("zz_at", 0) < EXT_EVERY.total_seconds():
        return
    text = format_exterior(summ, now, proj)
    if send(text):
        sent.append(text)
        state.update(zz_sig=sig, zz_at=ts, zz_final=bool(summ["tf"]))


def render_chart(summ: Optional[dict], proj: Optional[dict], hist: Optional[list], now: datetime,
                 path: Optional[str] = None) -> str:
    """PNG da apuração + projeção (eleicao_grafico.py) para mandar logo depois do quadro de texto."""
    import eleicao_grafico
    cands = {c.get("n"): c for c in (summ or {}).get("cands") or []}
    va, vb = cands.get("22", {}).get("v", 0), cands.get("13", {}).get("v", 0)
    path = path or os.path.join(os.environ.get("LEAGUE_DIR", os.path.dirname(os.path.abspath(__file__))),
                                "nfl_data", "eleicao_grafico.png")
    return eleicao_grafico.render(path, now, _num((summ or {}).get("pst", "0")), va / (va + vb) if va + vb else None,
                                  proj, hist, names=(NOMES_T2["22"][1], NOMES_T2["13"][1]))


def format_projection(proj: Optional[dict], names: Tuple[str, str]) -> str:
    if not proj:
        return ""
    a, b = names
    sa, pa = proj["share_a"], proj["prob_a"]
    lead, lead_p = (a, pa) if pa >= 0.5 else (b, 1 - pa)
    lines = ["🔮 *Projeção do modelo* — resultado final estimado",
             f"{a} *{_br(100 * sa, 2)}%* × {b} *{_br(100 * (1 - sa), 2)}%*",
             f"Faixa de 90%: {a} entre {_br(100 * proj['lo'])}% e {_br(100 * proj['hi'])}%"]
    if lead_p >= 0.995:
        lines.append(f"🏁 Indica vitória de *{lead}* (chance acima de 99,5%)")
    else:
        lines.append(f"Chance de vitória: {lead} *{_br(100 * lead_p, 0)}%* · "
                     f"{b if lead == a else a} {_br(100 * (1 - lead_p), 0)}%")
    if proj["apurado"] < 0.005:
        lines.append("_Muito cedo: com menos de 0,5% apurado o modelo ainda é quase só o 1º turno + migração estimada._")
    lines.append("_Corrige o viés da ordem de chegada das urnas (1º turno por município + desvio medido em cada UF). "
                 "Estimativa estatística, não é resultado oficial._")
    return "\n".join(lines)


def projection_names(summ: Optional[dict]) -> Tuple[str, str]:
    names = {c.get("n"): _name(c["nm"]) for c in (summ or {}).get("cands") or []}
    return names.get("22", "Flávio Bolsonaro"), names.get("13", "Lula")


# ----------------------------------------------------------------------------- quadro do 2º turno (WhatsApp)
NOMES_T2 = {"22": ("Flávio Bolsonaro", "Flávio", "🔵"), "13": ("Lula", "Lula", "🟠")}  # mesmas cores do gráfico
REG_TAB = [("N", "Norte", "Norte"), ("NE", "Nordeste", "Nordeste"), ("CO", "Centro-Oeste", "C.-Oeste"),
           ("SE", "Sudeste", "Sudeste"), ("S", "Sul", "Sul"), ("EX", "Exterior", "Exterior")]


def _pct_cell(x: Optional[float], nd: int = 1) -> str:
    return "—" if x is None else _br(100 * x, nd)


def _apur_cell(f: Optional[float]) -> str:
    if f is None:
        return "—"
    p = 100 * f
    return f"{_br(p, 0 if p >= 9.95 else 1)}%"


def format_board_t2(summ: Optional[dict], regions: Optional[dict], proj: Optional[dict], now: datetime,
                    num_a: str = "22", num_b: str = "13", names: Optional[dict] = None,
                    obs_reg: Optional[Dict[str, tuple]] = None) -> str:
    """Quadro do 2º turno pensado para o celular: apurado → por região → projeção → projeção por região.

    As tabelas vão em bloco monoespaçado (```), que o WhatsApp alinha; ≤ 27 colunas para caber em pé."""
    cands = {c.get("n"): c for c in (summ or {}).get("cands") or []}
    nomes = {**NOMES_T2, **(names or {})}
    full_a, short_a, dot_a = nomes.get(num_a, (_name(cands.get(num_a, {}).get("nm", "A")),) * 2 + ("🔵",))
    full_b, short_b, dot_b = nomes.get(num_b, (_name(cands.get(num_b, {}).get("nm", "B")),) * 2 + ("🔴",))
    out = [f"🗳️ *2º TURNO · PRESIDENTE* — {now:%H:%M}"]
    if summ and _num(summ["pst"]) > 0:
        out.append(f"📊 *{summ['pst']}% das urnas apuradas*" + (" · ✔️ final" if summ["tf"] else ""))
    else:
        out.append("📊 _aguardando os primeiros boletins_")

    # 1) apurado até agora
    va, vb = cands.get(num_a, {}).get("v", 0), cands.get(num_b, {}).get("v", 0)
    if va + vb > 0:
        lead = (short_a, va - vb) if va >= vb else (short_b, vb - va)
        out += ["", "*✅ APURADO ATÉ AGORA*",
                f"{dot_a} {full_a} — *{cands[num_a]['pct']}%*", f"      {_int_br(va)} votos",
                f"{dot_b} {full_b} — *{cands[num_b]['pct']}%*", f"      {_int_br(vb)} votos",
                f"↔️ {lead[0]} à frente por {_int_br(lead[1])} votos "
                f"({_br(abs(_num(cands[num_a]['pct']) - _num(cands[num_b]['pct'])), 2)} p.p.)"]
        tv = summ.get("tv", 0)
        if tv:
            out.append(f"Válidos {_pct(summ.get('vv', 0), tv)}% · Brancos {summ.get('pvb', '0,00')}% · "
                       f"Nulos {summ.get('ptvn', '0,00')}%")
            out.append(f"🧮 {_int_br(tv)} votos de {_int_br(summ.get('te', 0))} eleitores" if summ.get("te")
                       else f"🧮 {_int_br(tv)} votos")

    # 1b) comparativo nos lugares já apurados (mesmos municípios, outras eleições)
    cmp_ = (proj or {}).get("comparativo")
    if cmp_ and cmp_.get("agora") is not None and cmp_.get("t1_2026") is not None:
        def ref(nome, v):
            if v is None:
                return None
            d = 100 * (cmp_["agora"] - v)
            return f"{nome} {_br(100 * v)}% ({'+' if d >= 0 else '−'}{_br(abs(d))})"
        linha22 = " · ".join(x for x in (ref("1º turno", cmp_.get("t1_2022")), ref("2º turno", cmp_.get("t2_2022"))) if x)
        out += ["", f"*📊 Nos lugares já apurados* (% de {short_a} entre os dois · diferença em pontos)",
                f"Agora *{_br(100 * cmp_['agora'])}%* · no 1º turno: {ref('', cmp_['t1_2026']).strip()}"]
        if linha22:
            out.append(f"Bolsonaro em 2022: {linha22}")
        if cmp_.get("abst_agora") is not None and cmp_.get("abst_t1") is not None:
            d = 100 * (cmp_["abst_agora"] - cmp_["abst_t1"])
            out.append(f"🚶 Abstenção: *{_br(100 * cmp_['abst_agora'])}%* agora · {_br(100 * cmp_['abst_t1'])}% no "
                       f"1º turno ({'+' if d >= 0 else '−'}{_br(abs(d))})")

    # 2) por região — apurado (seções totalizadas do TSE + votos de cada um somados das UFs)
    secoes = {r["nm"]: r for r in (regions or {}).get("reg", [])}
    pr = (proj or {}).get("regioes") or {}
    rows = []
    for code, nome, tab in REG_TAB:
        sec = secoes.get(nome)
        apur = (sec["st"] / sec["ts"]) if sec and sec.get("ts") else (pr.get(code) or {}).get("apurado")
        reg = pr.get(code) or {}
        a, b = reg.get("obs_a", 0.0), reg.get("obs_b", 0.0)
        if not a + b and obs_reg:  # sem projeção (pausa): votos somados direto dos totais das UFs
            a, b = obs_reg.get(code, (0.0, 0.0))
        sa = a / (a + b) if a + b else None
        rows.append(f"{tab:<9}{_apur_cell(apur):>5} {_pct_cell(sa):>6} {_pct_cell(None if sa is None else 1 - sa):>6}")
    if any(r.split()[-1] != "—" for r in rows) or secoes:
        out += ["", "*📍 POR REGIÃO — APURADO*", "```",
                f"{'Região':<9}{'Apur.':>5} {short_a[:6]:>6} {short_b[:6]:>6}", *rows, "```"]

    # 3) projeção do resultado final (só acima de PROJ_MIN_PST)
    if proj and summ and _num(summ.get("pst", "0")) <= PROJ_MIN_PST:
        proj = None
        out += ["", f"🔮 _A projeção do resultado final aparece a partir de {_br(PROJ_MIN_PST, 0)}% das urnas apuradas._"]
    if proj:
        sa, pa = proj["share_a"], proj["prob_a"]
        out += ["", "*🔮 PROJEÇÃO DO RESULTADO FINAL*",
                f"{dot_a} {short_a} — *{_br(100 * sa)}%* ({_br(100 * proj['lo'])} a {_br(100 * proj['hi'])})",
                f"{dot_b} {short_b} — *{_br(100 * (1 - sa))}%* ({_br(100 * (1 - proj['hi']))} a {_br(100 * (1 - proj['lo']))})"]
        lead, lead_p, other = (short_a, pa, short_b) if pa >= 0.5 else (short_b, 1 - pa, short_a)
        if lead_p >= 0.995:
            out.append(f"🏁 *Projeção indica vitória de {lead}* (chance acima de 99,5%)")
        else:
            out.append(f"🎯 Chance de vitória: *{lead} {_br(100 * lead_p, 0)}%* · {other} {_br(100 * (1 - lead_p), 0)}%")
        if proj["apurado"] < 0.005:
            out.append("_Muito cedo: com menos de 0,5% apurado a projeção ainda é quase só o 1º turno + migração._")

        # 4) projeção por região
        if pr:
            out += ["", "*🗺️ PROJEÇÃO POR REGIÃO*", "```", f"{'Região':<9}{short_a[:6]:>6} {short_b[:6]:>6} {'±':>4}"]
            for code, _, tab in REG_TAB:
                r = pr.get(code)
                if r:
                    out.append(f"{tab:<9}{_br(100 * r['share_a']):>6} {_br(100 * (1 - r['share_a'])):>6} "
                               f"{_br(50 * (r['hi'] - r['lo'])):>4}")
            out.append("```")
        out += ["", "_Intervalos de 90% (± = margem). Projeção estatística: 1º turno por município + desvio medido em "
                    "cada UF; não é resultado oficial._"]
    gen = (summ or {}).get("gen", "")
    out.append(f"_TSE · arquivo gerado {gen.split(' ')[-1]}_" if gen else "_TSE_")
    return "\n".join(out)


# ----------------------------------------------------------------------------- rede
def fetch(url: str, etag: str = "", timeout: int = 10) -> Tuple[int, Optional[str], str, dict]:
    """(status, corpo|None, etag, headers). 304 → corpo None."""
    headers = {"User-Agent": UA, "Accept": "application/jose, application/json, */*", "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = {k.lower(): v for k, v in r.headers.items()}
            raw = r.read()
            if h.get("content-encoding", "").lower() == "gzip":  # andamento de SP: 717 KB → 64 KB
                raw = gzip.decompress(raw)
            return r.status, raw.decode("utf-8"), h.get("etag", ""), h
    except urllib.error.HTTPError as e:
        h = {k.lower(): v for k, v in (e.headers or {}).items()}
        return e.code, None, etag if e.code == 304 else "", h


def _backoff_from(status: int, h: dict) -> int:
    """Segundos de recuo pedidos pelo CDN (0 = ok)."""
    if status == 429 or status >= 500:
        try:
            return max(60, int(h.get("retry-after", "120")))
        except ValueError:
            return 120
    try:
        remaining = int(h.get("x-ratelimit-remaining", "999").split(",")[0])
        if remaining < 20:
            return max(60, int(h.get("x-ratelimit-reset", "60").split(",")[0]))
    except ValueError:
        pass
    return 0


def load_race(race: Race, turn: int, state: dict, fetcher: Callable = fetch) -> Optional[dict]:
    """Resumo atual do cargo (do cache em 304). Levanta ApiError em falha / pedido de recuo."""
    return _load_cached([url_for(race, turn, ext) for ext in ("jws", "json")], summarize, state, fetcher)


def load_many(races: List[Race], state: dict, fetcher: Callable = fetch) -> Dict[str, dict]:
    """Vários arquivos (6 em paralelo); o que falhar usa o último resumo em cache."""
    def one(r: Race) -> Optional[dict]:
        try:
            return load_race(r, 1, state, fetcher)
        except Exception as e:
            log.warning("%s: %s", r.key, e)
            return (state.get("cache", {}).get(url_for(r, 1)) or {}).get("summ")
    with ThreadPoolExecutor(max_workers=6) as ex:
        return {r.key: s for r, s in zip(races, ex.map(one, races)) if s}


def load_regions(turn: int, state: dict, fetcher: Callable = fetch) -> Optional[dict]:
    ele = BY_KEY["pres"].ele_t1 if turn == 1 else BY_KEY["pres"].ele_t2
    urls = [f"{BASE}/{CICLO}/{ele}/dados/br/br-e{ele:06d}-ab.{ext}" for ext in ("jws", "json")]
    return _load_cached(urls, summarize_regions, state, fetcher)


def _load_cached(urls: List[str], parse: Callable[[dict], dict], state: dict, fetcher: Callable) -> Optional[dict]:
    """Primeira URL publicada (jws → json), com ETag/304 e recuo do CDN."""
    cache = state.setdefault("cache", {})
    for url in urls:
        prev = cache.get(url) or {}
        fresh = prev.get("summ") and prev.get("ver") == SUMM_VER
        status, body, etag, h = fetcher(url, prev.get("etag", "") if fresh else "")
        wait = _backoff_from(status, h)
        if wait:
            state["backoff_until"] = time.time() + wait
            raise ApiError(f"HTTP {status} / rate limit em {url}: recuando {wait}s")
        if status == 304 and fresh:
            return prev["summ"]
        if status == 404:
            continue  # ainda não publicado (ou só existe a outra extensão)
        if status != 200 or body is None:
            raise ApiError(f"HTTP {status} em {url}")
        payload = decode_jws(body) if url.endswith(".jws") else json.loads(body)
        summ = parse(payload)
        cache[url] = {"etag": etag, "summ": summ, "ver": SUMM_VER}
        return summ
    return None


# ----------------------------------------------------------------------------- ciclo
def new_state() -> dict:
    return {"turn": 1, "t2": None, "finished": False, "armed": False, "sent_pst": {}, "final_sent": {},
            "summ": {}, "reg": None, "cache": {}, "backoff_until": 0, "msgs": 0, "pres_at": 0,
            "zz_at": 0, "zz_check_at": 0, "zz_sig": "", "zz_final": False, "g1_final_sent": False,
            "gov_at": 0, "sen_at": 0, "eleitos_final": False, "eleitos_check_at": 0, "eleitos_known": None, "dep_check_at": 0, "dep_sent": {}}


def load_state(path: str = STATE) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return {**new_state(), **json.load(f)}
    except (OSError, ValueError):
        return new_state()


def save_state(state: dict, path: str = STATE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


def races_for(state: dict) -> List[Race]:
    if state["turn"] == 1:
        return list(RACES)
    return [BY_KEY[k] for k in state.get("t2") or []]


def tick(state: dict, send: Callable[[str], bool], now: Optional[datetime] = None, fetcher: Callable = fetch,
         force: bool = False, send_image: Optional[Callable[[str], bool]] = None,
         send_link: Optional[Callable[[str], bool]] = None) -> List[str]:
    """Uma execução do cron. Muta `state`; devolve os textos enviados (com sucesso).
    send_image(path): no 2º turno, o gráfico da apuração + projeção vai logo depois de cada quadro."""
    now = now or datetime.now(BRT)
    sent: List[str] = []
    if state.get("finished") or now > HARD_STOP:
        return sent
    turn = state["turn"]
    start = TURNS[turn]
    if turn == 2:
        _invite_tick(state, send_link or send, now, sent)
    if now < start and not force:
        if turn == 2 and "pres" in (state.get("t2") or []):
            _exterior_tick(state, send, now, fetcher, sent)
        return sent
    if time.time() < state.get("backoff_until", 0):
        log.info("em recuo até %s", datetime.fromtimestamp(state["backoff_until"], BRT).strftime("%H:%M:%S"))
        return sent
    races = races_for(state)

    if not state.get("armed"):
        names = ", ".join(r.label for r in races)
        msg = (f"🗳️ Monitor da apuração ligado ({turn}º turno): {names}.\n"
               f"Te mando o quadro a cada atualização do % apurado no TSE (consulta a cada 1 min).")
        if turn == 2 and any(r.key == "pres" for r in races):
            msg += "\n\n" + armed_projection_note()
        if send(msg):
            state["armed"] = True
            sent.append(msg)

    summaries: Dict[str, dict] = {k: v for k, v in (state.get("summ") or {}).items() if k in {r.key for r in races}}
    for r in races:
        try:
            s = load_race(r, turn, state, fetcher)
        except ApiError as e:
            log.warning("%s: %s", r.key, e)
            if state.get("backoff_until", 0) > time.time():
                break
            continue
        except Exception as e:  # payload inesperado: loga e segue com os outros cargos
            log.exception("%s: falha lendo o TSE: %s", r.key, e)
            continue
        if s:
            summaries[r.key] = s
    state["summ"] = summaries
    if any(r.key == "pres" for r in races) and time.time() >= state.get("backoff_until", 0):
        try:
            state["reg"] = load_regions(turn, state, fetcher) or state.get("reg")
        except Exception as e:  # região é complemento: falha não segura o quadro
            log.warning("regiões: %s", e)
    proj = None
    if turn == 2 and any(r.key == "pres" for r in races) and time.time() >= state.get("backoff_until", 0):
        try:
            proj = compute_projection(turn, state, fetcher, summaries.get("pres"))
        except Exception as e:  # projeção é complemento: falha não segura o quadro
            log.exception("projeção: %s", e)
    if turn == 2 and "pres" in (state.get("t2") or []):
        pres_pst = _num((summaries.get("pres") or {}).get("pst", "0"))
        _exterior_tick(state, send, now, fetcher, sent, proj if pres_pst > PROJ_MIN_PST else None)

    changed = [r.key for r in races if summaries.get(r.key) and _num(summaries[r.key]["pst"]) > 0
               and (summaries[r.key]["pst"] != state["sent_pst"].get(r.key)
                    or (summaries[r.key]["tf"] and not state["final_sent"].get(r.key)))]
    if turn == 2 and PAUSA_DESDE <= now < PAUSA_ATE and "pres" in summaries:
        # pausa: a última parcial de 30 em 30 min (mesmo sem mudança), sem projeção nem gráfico
        if now.timestamp() - state.get("pausa_at", 0) >= PAUSA_EVERY.total_seconds():
            text = format_pausa(summaries["pres"], state.get("reg"), now, primeira=not state.get("pausa_at"),
                                obs_reg=_obs_regioes(state))
            if send(text):
                sent.append(text)
                state.update(pausa_at=now.timestamp(), pres_at=now.timestamp())
                state["sent_pst"]["pres"] = summaries["pres"]["pst"]
        changed = []
    if changed and not _throttled(state, "pres", turn, now):
        if turn == 2 and [r.key for r in races] == ["pres"]:
            text = format_board_t2(summaries.get("pres"), state.get("reg"), proj, now)
        else:
            text = format_update(turn, races, summaries, now, state.get("reg"), proj)
        if send(text):
            sent.append(text)
            state["msgs"] = state.get("msgs", 0) + 1
            state["pres_at"] = now.timestamp()
            for r in races:
                if summaries.get(r.key):
                    state["sent_pst"][r.key] = summaries[r.key]["pst"]
                    if summaries[r.key]["tf"]:
                        state["final_sent"][r.key] = True
            if turn == 2 and proj and send_image and "pres" in summaries \
                    and _num(summaries["pres"].get("pst", "0")) > PROJ_MIN_PST:
                try:  # o gráfico é complemento: falha não segura o quadro
                    path = render_chart(summaries["pres"], proj, state.get("proj_hist"), now)
                    if send_image(path):
                        sent.append(path)
                except Exception as e:
                    log.exception("gráfico: %s", e)
        else:
            return sent  # tenta de novo no próximo minuto, sem avançar o turno

    all_final = all(summaries.get(r.key, {}).get("tf") and state["final_sent"].get(r.key) for r in races)
    if turn == 1:
        _eleitos_tick(state, send, now, fetcher, sent)
        _deputies_tick(state, send, now, fetcher, sent)
        all_final = all_final and state.get("eleitos_final") and all(state["dep_sent"].get(r.key) for r in DEPUTADOS)
    if all_final or now > start + TURN_CAP:
        msg = _close_turn(state, turn, summaries)
        if send(msg):
            sent.append(msg)
        else:
            log.warning("aviso de fim de turno não entregue (o turno já foi encerrado no estado)")
    return sent


def _throttled(state: dict, key: str, turn: int, now: datetime) -> bool:
    """1º turno: o fluxo `key` mandou há menos de THROTTLE_T1? (no 2º turno, nunca)."""
    return turn == 1 and now.timestamp() - state.get(f"{key}_at", 0) < THROTTLE_T1.total_seconds()


def _eleitos_tick(state: dict, send: Callable[[str], bool], now: datetime, fetcher: Callable, sent: List[str]) -> None:
    """Governadores e senadores (27 UFs), em mensagens separadas: lista completa a cada ELEITOS_EVERY e na hora quando
    surgir uma definição nova da categoria (🆕 no início). Categoria já toda definida não repete a cada 20 min. Quando
    tudo estiver definido: consolidado final e para (eleitos_final)."""
    if "gov_at" not in state:  # estado de antes da separação
        state["gov_at"] = state["sen_at"] = state.get("eleitos_at", 0)
    ts = now.timestamp()
    periodic = {k: ts - state.get(f"{k}_at", 0) >= ELEITOS_EVERY.total_seconds() for k in ("gov", "sen")}
    check = ts - state.get("eleitos_check_at", 0) >= ELEITOS_CHECK_EVERY.total_seconds()
    if state.get("eleitos_final") or time.time() < state.get("backoff_until", 0) or not (check or any(periodic.values())):
        return
    state["eleitos_check_at"] = ts
    allsum = load_many(GOV + SENADO, state, fetcher)
    gov = {r.key: allsum[r.key] for r in GOV if r.key in allsum}
    sen = {r.key: allsum[r.key] for r in SENADO if r.key in allsum}
    if not any(_num(x["pst"]) > 0 for x in allsum.values()):
        return
    events = majoritarian_events(gov, sen)
    if state.get("eleitos_known") is None:  # primeira rodada: o que já está definido é a base, sem 🆕
        state["eleitos_known"] = [eid for eid, _ in events]
    known = set(state["eleitos_known"])
    new = [(eid, line) for eid, line in events if eid not in known]
    g_el, g_rt, g_open = governors_status(gov)
    gov_done = len(gov) == len(GOV) and not g_open
    sen_done = len(senators_status(sen)[1]) == 2 * len(SENADO)

    def mark_sent(ids: List[str], text: str) -> None:
        sent.append(text)
        state["eleitos_known"] = sorted(set(state["eleitos_known"]) | set(ids))

    if gov_done and sen_done:
        if _throttled(state, "gov", 1, now) or _throttled(state, "sen", 1, now):
            return  # novidades seguem pendentes (não entram em eleitos_known) até passar o intervalo
        text = format_final_majoritarian(gov, sen, now, new)
        if send(text):
            mark_sent([eid for eid, _ in events], text)
            state["eleitos_final"] = True
        return
    for k, done, fmt in (("gov", gov_done, lambda n: format_governors(gov, now, n)),
                         ("sen", sen_done, lambda n: format_senators(sen, now, n))):
        cat_new = [(eid, line) for eid, line in new if eid.startswith(k + "_")]
        if not cat_new and (done or not periodic[k]):
            continue
        if _throttled(state, k, 1, now):
            continue  # novidade fica pendente e sai quando completar o intervalo
        text = fmt(cat_new)
        if send(text):
            mark_sent([eid for eid, _ in events if eid.startswith(k + "_")], text)
            state[f"{k}_at"] = ts


def _deputies_tick(state: dict, send: Callable[[str], bool], now: datetime, fetcher: Callable, sent: List[str]) -> None:
    """Deputados (SC): confere a cada DEP_CHECK_EVERY e manda uma única vez, quando a UF chegar a 100% apurado (ou a
    totalização fechar) — a lista oficial do TSE se os eleitos já estiverem marcados, senão o cálculo por legenda."""
    pending = [r for r in DEPUTADOS if not state["dep_sent"].get(r.key)]
    if not pending or time.time() < state.get("backoff_until", 0) \
            or now.timestamp() - state.get("dep_check_at", 0) < DEP_CHECK_EVERY.total_seconds():
        return
    state["dep_check_at"] = now.timestamp()
    summ = load_many(pending, state, fetcher)
    for uf in dict.fromkeys(r.uf for r in pending):
        races = [r for r in pending if r.uf == uf]
        if not all(summ.get(r.key) and (summ[r.key]["tf"] or _num(summ[r.key]["pst"]) >= 100) for r in races):
            continue
        if all(deputies_known(summ[r.key]) for r in races):
            texts = [format_elected_deputies(r, summ[r.key], now) for r in races]
        else:
            texts = [format_projected_deputies(uf, summ, races, now, final=True)]
        ok = True
        for text in texts:
            if send(text):
                sent.append(text)
            else:
                ok = False
        if ok:
            for r in races:
                state["dep_sent"][r.key] = True


def _close_turn(state: dict, turn: int, summaries: Dict[str, dict]) -> str:
    if turn == 1:
        t2 = [r.key for r in RACES if r.ele_t2 and summaries.get(r.key) and needs_second_round(summaries[r.key])]
        if t2:
            state.update(turn=2, t2=t2, armed=False, sent_pst={}, final_sent={}, summ={}, reg=None, cache={})
            labels = " e ".join(BY_KEY[k].label for k in t2)
            return (f"🏁 1º turno totalizado. 2º turno em 25/10 para {labels} — "
                    f"a apuração volta *em tempo real* a partir das 17h desse dia.")
    state["finished"] = True
    return f"🏁 Apuração {'do 2º turno ' if turn == 2 else ''}encerrada. Monitor da eleição desligado. ✅"
