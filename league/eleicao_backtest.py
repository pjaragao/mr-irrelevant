"""Dados e backtest do modelo de projeção (eleicao_modelo.py), a partir dos dados abertos do TSE.

  python3 eleicao_backtest.py base                 # gera eleicao_base_2026.json (1º turno 2026 por município)
  python3 eleicao_backtest.py ref2022              # gera eleicao_ref_2022.json (2022 por município, p/ o comparativo)
  python3 eleicao_backtest.py backtest [2014 2018 2022]  # reencena cada 2º turno na ordem REAL de chegada (zona a zona)
  python3 eleicao_backtest.py demo 2022             # o bloco 🔮 que teria saído no grupo em cada momento da noite
  python3 eleicao_backtest.py demo 2022 --quadro    # o quadro completo do 2º turno (layout do WhatsApp)

Fonte: cdn.tse.jus.br/estatistica/sead/odsele — votacao_candidato_munzona_{ano}.zip e
detalhe_votacao_munzona_{ano}.zip. Só o membro *_BR.csv (Presidente) é lido, via HTTP Range (~6 MB em vez de ~1,4 GB).
O horário de totalização de cada zona no 2º turno (HH_ULTIMA_TOTALIZACAO) dá a ordem de chegada daquela noite.
"""
import csv
import functools
import io
import json
import math
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eleicao_modelo as M  # noqa: E402

ODS = "https://cdn.tse.jus.br/estatistica/sead/odsele"
CACHE = os.environ.get("TSE_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "nfl_data", "tse"))
PARES = {2014: ("45", "13"), 2018: ("17", "13"), 2022: ("22", "13"), 2026: ("22", "13")}  # (A, B): A = "da direita"
# Anos sem horário de totalização nos dados abertos (2014: "00:00:00") usam a ordem real de outro 2º turno,
# casada por município+zona (zona que não existia lá vai para o horário mediano do município, ou para o fim).
ORDEM_EMPRESTADA = {2014: 2022}


class _RangeFile(io.RawIOBase):
    def __init__(self, url):
        self.url, self.pos = url, 0
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=60) as r:
            self.size = int(r.headers["Content-Length"])

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else (self.pos + off if whence == 1 else self.size + off)
        return self.pos

    def readinto(self, b):
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(b), self.size) - 1
        req = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def baixar_br(tabela: str, ano: int) -> str:
    """Extrai {tabela}_{ano}_BR.csv do zip remoto do TSE (cache local)."""
    os.makedirs(CACHE, exist_ok=True)
    nome = f"{tabela}_{ano}_BR.csv"
    dest = os.path.join(CACHE, nome)
    if not os.path.exists(dest):
        z = zipfile.ZipFile(io.BufferedReader(_RangeFile(f"{ODS}/{tabela}/{tabela}_{ano}.zip"), 1 << 20))
        with z.open(nome) as src, open(dest + ".tmp", "wb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
        os.replace(dest + ".tmp", dest)
    return dest


def _ler(path):
    with open(path, encoding="latin-1", newline="") as f:
        yield from csv.DictReader(f, delimiter=";")


def _ordem(ano: int):
    """{(município, zona): horário} e {município: horário mediano} do 2º turno de `ano`."""
    zonas, por_mun = {}, defaultdict(list)
    for x in _ler(baixar_br("detalhe_votacao_munzona", ano)):
        if x["CD_CARGO"] == "1" and x["NR_TURNO"] == "2":
            key = x["SG_UF"].lower() + x["CD_MUNICIPIO"].zfill(5)
            h = f'{x["DT_ULTIMA_TOTALIZACAO"][6:]}{x["DT_ULTIMA_TOTALIZACAO"][3:5]}{x["DT_ULTIMA_TOTALIZACAO"][:2]} ' \
                f'{x["HH_ULTIMA_TOTALIZACAO"]}'
            zonas[(key, x["NR_ZONA"])] = max(zonas.get((key, x["NR_ZONA"]), ""), h)
            por_mun[key].append(h)
    return zonas, {k: sorted(v)[len(v) // 2] for k, v in por_mun.items()}


@functools.lru_cache(maxsize=None)
def carregar(ano: int):
    """→ (municípios do 1º turno, zonas do 2º turno) do Presidente."""
    t1 = defaultdict(lambda: {"aptos": 0, "comp": 0, "votos": defaultdict(int)})
    zonas2 = defaultdict(lambda: {"aptos": 0, "a": 0, "b": 0, "hora": ""})
    a_nr, b_nr = PARES[ano]
    for x in _ler(baixar_br("detalhe_votacao_munzona", ano)):
        if x["CD_CARGO"] != "1":
            continue
        uf = x["SG_UF"].lower()
        key = uf + x["CD_MUNICIPIO"].zfill(5)
        if x["NR_TURNO"] == "1":
            t1[key]["uf"] = uf
            t1[key]["aptos"] += int(x["QT_APTOS"] or 0)
            t1[key]["comp"] += int(x["QT_COMPARECIMENTO"] or 0)
        else:
            z = zonas2[(key, x["NR_ZONA"], x.get("ST_VOTO_EM_TRANSITO", "N"))]
            z["uf"], z["key"] = uf, key
            z["aptos"] += int(x["QT_APTOS"] or 0)
            z["hora"] = max(z["hora"], f'{x["DT_ULTIMA_TOTALIZACAO"][6:]}{x["DT_ULTIMA_TOTALIZACAO"][3:5]}'
                                       f'{x["DT_ULTIMA_TOTALIZACAO"][:2]} {x["HH_ULTIMA_TOTALIZACAO"]}')
    for x in _ler(baixar_br("votacao_candidato_munzona", ano)):
        if x["CD_CARGO"] != "1":
            continue
        uf = x["SG_UF"].lower()
        key = uf + x["CD_MUNICIPIO"].zfill(5)
        v = int(x["QT_VOTOS_NOMINAIS"] or 0)
        if x["NR_TURNO"] == "1":
            t1[key]["uf"] = uf
            t1[key]["votos"][x["NR_CANDIDATO"]] += v
        else:
            z = zonas2[(key, x["NR_ZONA"], x.get("ST_VOTO_EM_TRANSITO", "N"))]
            z["uf"], z["key"] = uf, key
            if x["NR_CANDIDATO"] == a_nr:
                z["a"] += v
            elif x["NR_CANDIDATO"] == b_nr:
                z["b"] += v
    if ano in ORDEM_EMPRESTADA:
        oz, om = _ordem(ORDEM_EMPRESTADA[ano])
        for (key, zona, _), z in zonas2.items():
            z["hora"] = oz.get((key, zona)) or om.get(key) or "99999999 99:99:99"
    muns = [{"key": k, "uf": d["uf"], "aptos": d["aptos"], "comp": d["comp"], "votos": dict(d["votos"])}
            for k, d in t1.items() if d.get("uf")]
    return muns, list(zonas2.values())


def gerar_base_2026(dest=None):
    dest = dest or os.path.join(os.path.dirname(os.path.abspath(__file__)), "eleicao_base_2026.json")
    muns, _ = carregar(2026)
    out = {"fonte": f"{ODS}/votacao_candidato_munzona + detalhe_votacao_munzona (2026, Presidente, 1º turno)",
           "cand_a": "22", "cand_b": "13", "municipios": sorted(muns, key=lambda m: m["key"])}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    tot = defaultdict(int)
    for m in muns:
        for nr, v in m["votos"].items():
            tot[nr] += v
    val = sum(tot.values())
    print(f"{dest}: {len(muns)} municípios; Flávio {100 * tot['22'] / val:.2f}% Lula {100 * tot['13'] / val:.2f}%")


CORTES = [0.5, 1, 2, 3, 5, 10, 15, 20, 30, 40, 50, 60, 70, 80, 90]


def reencenar(ano: int, params=None, transfer=None, baixa_municipios=True, cortes=CORTES, verbose=True,
              congela_em=None):
    """Reencena o 2º turno de `ano`: a cada corte (% do eleitorado apurado), projeta e compara com o final.
    baixa_municipios: usa o resultado de cada município apurado (senão só totais por UF + andamento).
    congela_em: para de "baixar" municípios depois desse % apurado (o retrato fica; o resto vem dos totais da UF)."""
    muns_rows, zonas = carregar(ano)
    a_nr, b_nr = PARES[ano]
    modelo = M.Modelo(M.build_muns(muns_rows, a_nr, b_nr, transfer), params)
    aptos_mun = defaultdict(int)
    for z in zonas:
        aptos_mun[z["key"]] += z["aptos"]
    final_a = sum(z["a"] for z in zonas)
    final = final_a / sum(z["a"] + z["b"] for z in zonas)
    zonas.sort(key=lambda z: z["hora"])
    aptos_tot = sum(z["aptos"] for z in zonas)
    res = []
    acc = 0
    i = 0
    cnt_mun = defaultdict(lambda: [0, 0, 0])  # a, b, aptos apurados
    for c in cortes:
        alvo = c / 100 * aptos_tot
        while i < len(zonas) and acc < alvo:
            z = zonas[i]
            cm = cnt_mun[z["key"]]
            cm[0] += z["a"]
            cm[1] += z["b"]
            cm[2] += z["aptos"]
            acc += z["aptos"]
            i += 1
        frac, obs_uf = {}, defaultdict(lambda: [0.0, 0.0])
        congelado = congela_em is not None and c > congela_em
        if not congelado:
            obs_mun = {}
        for k, (a, b, ap) in cnt_mun.items():
            f = ap / aptos_mun[k] if aptos_mun[k] else 1.0
            frac[k] = f
            uf = k[:2]
            obs_uf[uf][0] += a
            obs_uf[uf][1] += b
            if baixa_municipios and not congelado:
                obs_mun[k] = (a, b, f)
        r = modelo.projetar(frac, obs_mun, {u: tuple(v) for u, v in obs_uf.items()})
        err = 100 * (r["share_a"] - final)
        cobre = r["faixa90"][0] <= final <= r["faixa90"][1]
        sd = max(r["sd"], 1e-6)
        ll = -math.log(sd * math.sqrt(2 * math.pi)) - 0.5 * ((final - r["share_a"]) / sd) ** 2
        res.append({"corte": c, "apurado": 100 * acc / aptos_tot, "proj": 100 * r["share_a"], "bruto": 100 * r["bruto_a"],
                    "erro": err, "sd": 100 * r["sd"], "cobre90": cobre, "prob_a": r["prob_a"], "final": 100 * final,
                    "ll": ll, "r": r})
        if verbose:
            print(f"{ano} {c:>4}% | bruto {100 * r['bruto_a']:6.2f} | proj {100 * r['share_a']:6.2f} ±{164.5 * r['sd']:.2f} "
                  f"| final {100 * final:6.2f} | erro {err:+.2f} | P(A) {r['prob_a']:.3f} {'✓' if cobre else '✗'}")
    return res


def resumo(resultados):
    erros = [abs(r["erro"]) for rs in resultados for r in rs if r["corte"] >= 5]
    cedo = [abs(r["erro"]) for rs in resultados for r in rs if r["corte"] < 5]
    todos = [r for rs in resultados for r in rs]
    cob = sum(r["cobre90"] for r in todos) / len(todos)
    errado = sum(1 for r in todos if (r["prob_a"] > 0.95 and r["final"] < 50) or (r["prob_a"] < 0.05 and r["final"] > 50))
    return {"erro_medio_5pct+": sum(erros) / len(erros), "pior_5pct+": max(erros),
            "erro_medio_cedo": sum(cedo) / len(cedo) if cedo else None, "cobertura90": cob,
            "confiante_errado": errado, "loglik": sum(r["ll"] for r in todos) / len(todos), "n": len(todos)}


def gerar_ref_2022(dest=None):
    """eleicao_ref_2022.json: Bolsonaro (22) e Lula (13) por município no 1º e no 2º turno de 2022 — para o
    comparativo "nos lugares já apurados" do quadro do 2º turno de 2026. {município: [22 1T, 13 1T, 22 2T, 13 2T]}."""
    dest = dest or os.path.join(os.path.dirname(os.path.abspath(__file__)), "eleicao_ref_2022.json")
    muns, zonas = carregar(2022)
    ref = {m["key"]: [m["votos"].get("22", 0), m["votos"].get("13", 0), 0, 0] for m in muns}
    for z in zonas:
        r = ref.setdefault(z["key"], [0, 0, 0, 0])
        r[2] += z["a"]
        r[3] += z["b"]
    with open(dest, "w", encoding="utf-8") as f:
        json.dump({"fonte": "dados abertos do TSE, Presidente 2022 (Bolsonaro 22 × Lula 13), por município",
                   "municipios": ref}, f, separators=(",", ":"))
    return dest


NOMES = {2014: ("Aécio", "Dilma"), 2018: ("Bolsonaro", "Haddad"), 2022: ("Bolsonaro", "Lula")}


def demo(ano: int):
    import eleicao_tse
    for row in reencenar(ano, baixa_municipios=False, verbose=False, cortes=[0.5, 1, 2, 5, 10, 20, 40, 60, 80, 95]):
        r = row["r"]
        proj = {"share_a": r["share_a"], "sd": r["sd"], "lo": r["faixa90"][0], "hi": r["faixa90"][1],
                "prob_a": r["prob_a"], "apurado": r["apurado"], "bruto_a": r["bruto_a"]}
        print(f"\n——— {ano}, {row['apurado']:.1f}% apurado — bruto do TSE: {NOMES[ano][0]} {row['bruto']:.2f}% "
              f"(resultado final: {row['final']:.2f}%)")
        print(eleicao_tse.format_projection(proj, NOMES[ano]))


def demo_quadro(ano: int, cortes=(1, 10, 40, 80)):
    """Quadro completo do 2º turno (format_board_t2) montado a partir da reencenação."""
    import eleicao_tse
    from datetime import datetime
    muns_rows, zonas = carregar(ano)
    a_nr, b_nr = PARES[ano]
    aptos_reg = defaultdict(int)
    for z in zonas:
        aptos_reg[M.REGIAO.get(z["uf"], "EX")] += z["aptos"]
    nomes = {"22": NOMES[ano][0], "13": NOMES[ano][1]} if ano != 2026 else {}
    for row in reencenar(ano, baixa_municipios=False, verbose=False, cortes=list(cortes)):
        r = row["r"]
        a = sum(v["obs_a"] for v in r["regioes"].values())
        b = sum(v["obs_b"] for v in r["regioes"].values())
        pct = lambda x: f"{100 * x:.2f}".replace(".", ",")  # noqa: E731
        summ = {"pst": pct(row["apurado"] / 100), "tf": False, "gen": "", "tv": 0,
                "cands": [{"n": "22", "nm": NOMES[ano][0], "v": int(a), "pct": pct(a / (a + b))},
                          {"n": "13", "nm": NOMES[ano][1], "v": int(b), "pct": pct(b / (a + b))}]}
        regions = {"reg": [{"nm": nome, "st": round(1000 * r["regioes"].get(code, {}).get("apurado", 0)), "ts": 1000}
                           for code, nome, _ in eleicao_tse.REG_TAB]}
        proj = {"share_a": r["share_a"], "sd": r["sd"], "lo": r["faixa90"][0], "hi": r["faixa90"][1],
                "prob_a": r["prob_a"], "apurado": r["apurado"], "bruto_a": r["bruto_a"],
                "regioes": {k: {"share_a": v["share_a"], "lo": v["faixa90"][0], "hi": v["faixa90"][1],
                                "apurado": v["apurado"], "obs_a": v["obs_a"], "obs_b": v["obs_b"]}
                            for k, v in r["regioes"].items()}}
        print(f"\n════════ {ano} · {row['apurado']:.0f}% apurado · resultado final real: {NOMES[ano][0]} {row['final']:.2f}%\n")
        print(eleicao_tse.format_board_t2(summ, regions, proj, datetime(2026, 10, 25, 19, 0),
                                          names={"22": (NOMES[ano][0], NOMES[ano][0], "🔵"),
                                                 "13": (NOMES[ano][1], NOMES[ano][1], "🔴")}))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backtest"
    if cmd == "base":
        gerar_base_2026()
    elif cmd == "ref2022":
        print(gerar_ref_2022())
    elif cmd == "demo":
        ano = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 2022
        (demo_quadro if "--quadro" in sys.argv else demo)(ano)
    else:
        anos = [int(a) for a in sys.argv[2:]] or [2014, 2018, 2022]
        out = []
        for ano in anos:
            for baixa in (True, False):
                print(f"\n=== {ano} — {'com' if baixa else 'sem'} municípios baixados ===")
                out.append(reencenar(ano, baixa_municipios=baixa))
        print("\n", resumo(out))
