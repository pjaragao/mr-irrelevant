"""Projeção do 2º turno presidencial durante a apuração — estatística sobre dados oficiais do TSE, sem IA.

O percentual bruto do TSE no começo da noite é uma amostra enviesada pela ordem de chegada (Sul/Centro-Oeste
apuram rápido, Nordeste devagar). O modelo não "adivinha" o resultado: compara o que saiu da urna com o que o
1º turno + a migração de votos prevê para OS MESMOS LUGARES e aplica o desvio medido ao que falta.

Para cada município m (base = 1º turno de 2026, `eleicao_base_2026.json`):
  s_m  = parcela esperada de A entre A+B no 2º turno: (A1 + Σ αk·Ok) / (A1 + B1 + Σ (αk+βk)·Ok)
         (αk, βk = fração dos votos do eliminado k que vai para A / B — a "migração"; prior do research de 21/10)
  q_m  = votos A+B esperados por eleitor apto (o resto dos eliminados vira branco/nulo/abstenção)
  o_m  = parcela dos eliminados nos válidos do 1º turno (onde há mais voto "solto", o erro da migração pesa mais)
  z_m  = porte do município (log do eleitorado, padronizado)

Modelo do desvio, em log-odds:  logit(y_m) = logit(s_m) + δ_BR + δ_região + δ_UF + γ·(o_m − ō) + β·z_m + u_m
  priors normais (δ_BR ~ N(0, τN²), δ_reg ~ N(0, τR²), δ_UF ~ N(0, τU²), γ, β ~ N(0, ·)) → posterior gaussiana
  fechada (mínimos quadrados ponderados com regularização). Com pouco voto apurado tudo vira δ_BR (modelo
  nacional); conforme os votos entram, o desvio fica regional e estadual — o encolhimento hierárquico.
  u_m = efeito próprio do município (variância σu²): municípios parcialmente apurados puxam o próprio resto.

Observações: cada município baixado (votos de A e B na parte apurada) + um "resíduo" por UF (total da UF menos os
municípios baixados, com a composição esperada das partes apuradas e não baixadas) — a conta nacional fecha
exatamente com os totais do TSE mesmo baixando poucos municípios.

Comparecimento: κ_UF = votos A+B observados / esperados nas partes apuradas (encolhido para o nacional e para 1).

Incerteza (analítica): parâmetros (gᵀΣg, linearizado) + efeito próprio dos municípios + escala de comparecimento
+ um piso de erro de especificação proporcional ao que falta (σ_piso), calibrado no backtest de 2018/2022
(`eleicao_backtest.py`, ordem real de chegada de cada 2º turno, zona a zona). P(vitória) = Φ((S − ½)/sd).
"""
from __future__ import annotations

import json
import math
import os
from typing import Dict, List, Optional, Tuple

REGIAO = {
    "ac": "N", "am": "N", "ap": "N", "pa": "N", "ro": "N", "rr": "N", "to": "N",
    "al": "NE", "ba": "NE", "ce": "NE", "ma": "NE", "pb": "NE", "pe": "NE", "pi": "NE", "rn": "NE", "se": "NE",
    "df": "CO", "go": "CO", "ms": "CO", "mt": "CO",
    "es": "SE", "mg": "SE", "rj": "SE", "sp": "SE",
    "pr": "S", "rs": "S", "sc": "S",
    "zz": "EX",
}
REGIOES = ["N", "NE", "CO", "SE", "S", "EX"]
UFS = sorted(REGIAO)

# Migração do 1º para o 2º turno (fração para A=Flávio, fração para B=Lula; o resto = branco/nulo/abstenção).
# Caiado, Cury e Renan: "projeção da casa" do research de 21/10/2026 (Talk 2º turno, p. 13–14). Os demais são
# palpites conservadores; com < 1% dos válidos, quase não mexem. O modelo reestima o desvio durante a apuração.
TRANSFER_2026 = {
    "55": (0.60, 0.25),  # Ronaldo Caiado (PSD) — apoiou Flávio em 06/10
    "70": (0.40, 0.40),  # Augusto Cury (Avante) — apoiou Flávio em 08/10
    "14": (0.65, 0.25),  # Renan Santos (Missão)
    "30": (0.65, 0.15),  # Zema (Novo)
    "27": (0.50, 0.20),  # Clariana Barão (DC)
    "35": (0.50, 0.20),  # Wilson Grassi (Democrata)
    "80": (0.05, 0.55),  # Samara (UP)
    "16": (0.05, 0.50),  # Hertz Dias (PSTU)
    "21": (0.05, 0.55),  # Edmilson Costa (PCB)
    "29": (0.05, 0.60),  # Rui Costa Pimenta (PCO)
}
TRANSFER_NEUTRO = (0.40, 0.40)  # backtests: sem opinião sobre a migração
COMPARECIMENTO_PRIOR = 0.98  # 2º/1º turno: abstenção sobe ~1–2 p.p. em média (2006–2022: +2,2 +3,4 +1,7 +1,0 −0,4)

PARAMS = {  # calibrados em eleicao_backtest.py (2014, 2018, 2022 — só totais por UF + andamento por município)
    "tauN": 0.15,     # desvio nacional a priori (log-odds) ≈ ±3,7 p.p.
    "tauR": 0.12,     # quanto uma região pode diferir do país
    "tauU": 0.04,     # quanto uma UF pode diferir da região
    "tauG": 0.3,      # coeficiente do perfil (voto em eliminados)
    "tauB": 0.0,      # porte do município — DESLIGADO: os pequenos apuram antes e contaminam o coeficiente
    "tauL": 0.0,      # nível do lugar — DESLIGADO: ajuda 2022, piora 2018 (o mesmo achado da literatura aberta)
    "sigmaU": 0.05,   # efeito próprio de cada município (log-odds)
    "sigmaPiso": 0.025,  # piso de erro comum sobre o que falta: faixa de 90% cobriu 84% nos backtests (n=90)
    "sigmaPisoReg": 0.04,  # idem por região (o erro de especificação não se compensa): cobertura 93% nas 5 regiões
    "kN": 300_000.0,  # votos que valem o prior de comparecimento (nacional)
    "kU": 60_000.0,   # idem por UF (puxa para o nacional)
    "sdKappa": 0.02,  # incerteza do comparecimento do que falta
    "inflacao": 1.0,  # multiplica o desvio (margem) — 1 nos backtests; ver INFLACAO_2026
    # a inflação da margem cai por região conforme ela apura: inteira até infl_ini, linear até 1× em infl_fim
    "infl_ini": 0.20,
    "infl_fim": 0.60,
}
# 2026: apuração atípica (decisão judicial prorrogou a votação até as 19h e o TSE parou às 17h06) — ritmo e ordem de
# chegada diferentes dos anos em que o modelo foi calibrado. A pedido, a margem fica 2,5× mais larga no ao vivo e
# volta ao normal por região conforme ela apura (cheia até 20%, 1× a partir de 60%; o Brasil pondera pelo que falta).
INFLACAO_2026 = 2.5


def logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def inv_logit(x: float) -> float:
    if x > 35:
        return 1.0
    if x < -35:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ----------------------------------------------------------------------------- álgebra (36×36, Python puro)
def _cholesky(a: List[List[float]]) -> List[List[float]]:
    n = len(a)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                L[i][j] = math.sqrt(max(s, 1e-12))
            else:
                L[i][j] = s / L[j][j]
    return L


def _chol_solve(L: List[List[float]], b: List[float]) -> List[float]:
    n = len(L)
    y = [0.0] * n
    for i in range(n):
        y[i] = (b[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    x = [0.0] * n
    for i in reversed(range(n)):
        x[i] = (y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))) / L[i][i]
    return x


def _quad_inv(L: List[List[float]], g: List[float]) -> float:
    """gᵀ A⁻¹ g com A = L Lᵀ: ‖L⁻¹ g‖²."""
    n = len(L)
    y = [0.0] * n
    for i in range(n):
        y[i] = (g[i] - sum(L[i][k] * y[k] for k in range(i))) / L[i][i]
    return sum(v * v for v in y)


# ----------------------------------------------------------------------------- base
class Mun:
    __slots__ = ("key", "uf", "reg", "aptos", "s", "ls", "q", "o", "z", "a1", "b1", "c1")

    def __init__(self, key: str, uf: str, aptos: int, s: float, q: float, o: float, z: float,
                 a1: float = 0.0, b1: float = 0.0, c1: float = 0.0):
        self.key, self.uf, self.reg = key, uf, REGIAO.get(uf, "EX")
        self.aptos, self.s, self.ls, self.q, self.o, self.z = aptos, s, logit(s), q, o, z
        self.a1, self.b1, self.c1 = a1, b1, c1  # 1º turno: votos de A e B, comparecimento (comparativos)


def build_muns(rows: List[dict], cand_a: str, cand_b: str, transfer: Optional[Dict[str, Tuple[float, float]]] = None,
               turnout: float = COMPARECIMENTO_PRIOR) -> List[Mun]:
    """rows: {key, uf, aptos, comp, votos: {nº: votos}} do 1º turno → municípios com prior do 2º turno."""
    transfer = transfer or {}
    prelim = []
    for r in rows:
        v = r.get("votos") or {}
        a1, b1 = float(v.get(cand_a, 0)), float(v.get(cand_b, 0))
        if r.get("aptos", 0) <= 0 or a1 + b1 <= 0:
            continue
        ea, eb, outros = a1, b1, 0.0
        for nr, votos in v.items():
            if nr in (cand_a, cand_b):
                continue
            ta, tb = transfer.get(nr, TRANSFER_NEUTRO)
            ea += ta * float(votos)
            eb += tb * float(votos)
            outros += float(votos)
        validos1 = a1 + b1 + outros
        share = min(max(ea / (ea + eb), 0.005), 0.995)  # cidade com um só candidato (ex.: exterior) não vira 0/1
        prelim.append((r, share, (ea + eb) * turnout / r["aptos"], outros / validos1 if validos1 else 0.0))
    # porte padronizado (log do eleitorado)
    logs = [math.log(max(r["aptos"], 1)) for r, *_ in prelim]
    mu = sum(logs) / len(logs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in logs) / len(logs)) or 1.0
    return [Mun(r["key"], r["uf"], r["aptos"], s, q, o, (math.log(max(r["aptos"], 1)) - mu) / sd,
                float((r.get("votos") or {}).get(cand_a, 0)), float((r.get("votos") or {}).get(cand_b, 0)),
                float(r.get("comp", 0)))
            for (r, s, q, o) in prelim]


def load_base(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ----------------------------------------------------------------------------- modelo
class Modelo:
    def __init__(self, muns: List[Mun], params: Optional[dict] = None):
        self.P = {**PARAMS, **(params or {})}
        self.muns = muns
        self.by_key = {m.key: m for m in muns}
        self.by_uf: Dict[str, List[Mun]] = {}
        for m in muns:
            self.by_uf.setdefault(m.uf, []).append(m)
        w = sum(m.aptos * m.q for m in muns) or 1.0
        self.o_bar = sum(m.o * m.aptos * m.q for m in muns) / w
        # índices dos parâmetros: [δ_BR, δ_reg×6, δ_UF×28, γ, β]
        self.ix_reg = {r: 1 + i for i, r in enumerate(REGIOES)}
        self.ix_uf = {u: 1 + len(REGIOES) + i for i, u in enumerate(UFS)}
        self.ix_g = 1 + len(REGIOES) + len(UFS)
        self.ix_b = self.ix_g + 1
        self.ix_l = self.ix_b + 1
        self.np = self.ix_l + 1
        self.l_bar = sum(m.ls * m.aptos * m.q for m in muns) / w

    def prior_share(self) -> float:
        a = sum(m.aptos * m.q * m.s for m in self.muns)
        t = sum(m.aptos * m.q for m in self.muns)
        return a / t

    def _row(self, uf: str, reg: str, o: float, z: float, ls: float) -> List[Tuple[int, float]]:
        return [(0, 1.0), (self.ix_reg[reg], 1.0), (self.ix_uf[uf], 1.0), (self.ix_g, o - self.o_bar), (self.ix_b, z),
                (self.ix_l, ls - self.l_bar)]

    def projetar(self, frac: Dict[str, float], obs_mun: Dict[str, Tuple[float, float, float]],
                 obs_uf: Dict[str, Tuple[float, float]]) -> dict:
        """
        frac:    {município: fração do eleitorado já totalizada} (arquivo de andamento do TSE)
        obs_mun: {município: (votos A, votos B, fração apurada nesse retrato)} — municípios baixados
        obs_uf:  {uf: (votos A, votos B)} — totais apurados da UF (arquivo da UF)
        """
        P = self.P
        units = []      # (n, a, prior_logit, row, var, key|None)
        kap_obs: Dict[str, float] = {}
        kap_exp: Dict[str, float] = {}
        f_done: Dict[str, float] = {}   # fração que já não é projetada
        known: Dict[str, Tuple[float, float, float]] = {}
        for uf, muns in self.by_uf.items():
            A_u, B_u = obs_uf.get(uf, (0.0, 0.0))
            ra = rb = 0.0
            r_w = r_ws = r_wo = r_wz = r_wl = r_w2 = 0.0
            exp_u = 0.0
            for m in muns:
                f_ab = min(max(frac.get(m.key, 0.0), 0.0), 1.0)
                om = obs_mun.get(m.key)
                f_u = min(max(om[2], 0.0), 1.0) if om else 0.0
                f = max(f_ab, f_u)
                f_done[m.key] = f
                exp_u += f * m.aptos * m.q
                if om and om[0] + om[1] > 0 and f_u > 0:
                    a, b = om[0], om[1]
                    n = a + b
                    ra += a
                    rb += b
                    known[m.key] = (a, b, f_u)
                    s = m.s
                    var = P["sigmaU"] ** 2 + 1.0 / (n * s * (1 - s))
                    units.append((n, a, m.ls, self._row(m.uf, m.reg, m.o, m.z, m.ls), var, m.key))
                resid_f = f_ab - f_u if om else f_ab
                if resid_f > 1e-9:
                    w = resid_f * m.aptos * m.q
                    r_w += w
                    r_ws += w * m.s
                    r_wo += w * m.o
                    r_wz += w * m.z
                    r_wl += w * m.ls
                    r_w2 += w * w
            if A_u + B_u > 0:
                kap_obs[uf] = A_u + B_u
                kap_exp[uf] = exp_u
            res_a, res_b = A_u - ra, B_u - rb
            if r_w > 0 and res_a > 0 and res_b > 0:
                n = res_a + res_b
                s = r_ws / r_w
                h = r_w2 / (r_w * r_w)
                var = P["sigmaU"] ** 2 * h + 1.0 / (n * s * (1 - s))
                muns0 = muns[0]
                units.append((n, res_a, logit(s), self._row(uf, muns0.reg, r_wo / r_w, r_wz / r_w, r_wl / r_w), var, None))

        # --- posterior gaussiana dos parâmetros ---------------------------------
        npar = self.np
        prec = [[0.0] * npar for _ in range(npar)]
        rhs = [0.0] * npar
        prior_var = [P["tauN"] ** 2] + [P["tauR"] ** 2] * len(REGIOES) + [P["tauU"] ** 2] * len(UFS) \
            + [P["tauG"] ** 2, P["tauB"] ** 2, P["tauL"] ** 2]
        for i, v in enumerate(prior_var):
            prec[i][i] += 1.0 / max(v, 1e-12)  # τ = 0 → parâmetro preso em 0
        for n, a, ls, row, var, _ in units:
            y = logit(a / n) - ls
            w = 1.0 / var
            for i, xi in row:
                rhs[i] += w * xi * y
                for j, xj in row:
                    prec[i][j] += w * xi * xj
        L = _cholesky(prec)
        theta = _chol_solve(L, rhs)

        def fitted(row):
            return sum(theta[i] * x for i, x in row)

        # efeito próprio (BLUP) dos municípios baixados
        own: Dict[str, float] = {}
        for n, a, ls, row, var, key in units:
            if key is None:
                continue
            m = self.by_key[key]
            resid = logit(a / n) - ls - fitted(row)
            samp = 1.0 / (n * m.s * (1 - m.s))
            own[key] = resid * P["sigmaU"] ** 2 / (P["sigmaU"] ** 2 + samp)

        # --- comparecimento ------------------------------------------------------
        tot_obs = sum(kap_obs.values())
        tot_exp = sum(kap_exp.values())
        kN = (tot_obs + P["kN"]) / (tot_exp + P["kN"])
        kap = {}
        for uf in self.by_uf:
            o, e = kap_obs.get(uf, 0.0), kap_exp.get(uf, 0.0)
            kap[uf] = min(max((o + P["kU"] * kN) / (e + P["kU"]), 0.8), 1.25) if e > 0 else kN

        # --- projeção (Brasil e cada região, com a mesma conta de incerteza) -----
        scopes = ["BR"] + REGIOES
        acc = {sc: {"A": 0.0, "B": 0.0, "grad": [0.0] * npar, "own": 0.0, "floor": 0.0, "rest": []} for sc in scopes}
        for uf, (a, b) in obs_uf.items():
            for sc in ("BR", REGIAO.get(uf, "EX")):
                acc[sc]["A"] += a
                acc[sc]["B"] += b
        rem_tot = 0.0
        por_uf = {uf: {"A": obs_uf.get(uf, (0.0, 0.0))[0], "B": obs_uf.get(uf, (0.0, 0.0))[1]} for uf in self.by_uf}
        for uf, muns in self.by_uf.items():
            for m in muns:
                rem_f = 1.0 - f_done.get(m.key, 0.0)
                if rem_f <= 1e-9:
                    continue
                V = rem_f * m.aptos * m.q * kap[uf]
                row = self._row(m.uf, m.reg, m.o, m.z, m.ls)
                p = inv_logit(m.ls + fitted(row) + own.get(m.key, 0.0))
                d = V * p * (1 - p)
                n_m = sum(known.get(m.key, (0, 0, 0))[:2])
                shrink = 1.0 if not n_m else 1.0 - P["sigmaU"] ** 2 / (P["sigmaU"] ** 2 + 1.0 / (n_m * m.s * (1 - m.s)))
                por_uf[uf]["A"] += V * p
                por_uf[uf]["B"] += V * (1 - p)
                rem_tot += V
                for sc in ("BR", m.reg):
                    c = acc[sc]
                    c["A"] += V * p
                    c["B"] += V * (1 - p)
                    for i, x in row:
                        c["grad"][i] += d * x
                    c["own"] += (d ** 2) * P["sigmaU"] ** 2 * shrink
                    c["floor"] += d
                    c["rest"].append((uf, V, p))

        apurado_reg = {}
        for r in REGIOES:
            ms = [m for m in self.muns if m.reg == r]
            tot = sum(m.aptos for m in ms)
            apurado_reg[r] = sum(f_done.get(m.key, 0.0) * m.aptos for m in ms) / tot if tot else 0.0

        def fator_reg(a: float) -> float:
            """2,5× (a inflação) até infl_ini apurado na região; cai linear até 1× em infl_fim."""
            ini, fim = P["infl_ini"], P["infl_fim"]
            g = 1.0 if a <= ini else 0.0 if a >= fim else (fim - a) / (fim - ini)
            return 1.0 + (P["inflacao"] - 1.0) * g

        rem_reg = {r: sum(V for _, V, _ in acc[r]["rest"]) for r in REGIOES}
        rem_tot_reg = sum(rem_reg.values())
        fator = {r: fator_reg(apurado_reg[r]) for r in REGIOES}
        # Brasil: média dos fatores ponderada pelo que cada região ainda tem para apurar
        fator["BR"] = (sum(rem_reg[r] * fator[r] for r in REGIOES) / rem_tot_reg) if rem_tot_reg else 1.0

        def resumo(c: dict, piso: float, infl: float = 1.0) -> Optional[dict]:
            T = c["A"] + c["B"]
            if T <= 0:
                return None
            S = c["A"] / T
            kap_terms: Dict[str, float] = {}
            for uf, V, p in c["rest"]:  # escala de comparecimento por UF: mexe no peso do que falta
                kap_terms[uf] = kap_terms.get(uf, 0.0) + V * (p - S)
            var = (_quad_inv(L, [x / T for x in c["grad"]]) + c["own"] / (T * T)
                   + sum((t / T * P["sdKappa"]) ** 2 for t in kap_terms.values())
                   + (piso * c["floor"] / T) ** 2)
            sd = math.sqrt(max(var, 0.0)) * infl
            prob = norm_cdf((S - 0.5) / sd) if sd > 0 else (1.0 if S > 0.5 else 0.0)
            return {"share_a": S, "sd": sd, "faixa90": (S - 1.645 * sd, S + 1.645 * sd), "prob_a": prob,
                    "votos_a": c["A"], "votos_b": c["B"]}

        br = resumo(acc["BR"], P["sigmaPiso"], fator["BR"]) or {"share_a": 0.5, "sd": 0.0, "faixa90": (0.5, 0.5), "prob_a": 0.5,
                                   "votos_a": 0.0, "votos_b": 0.0}
        aptos_tot = sum(m.aptos for m in self.muns)
        apurado = sum(f_done.get(m.key, 0.0) * m.aptos for m in self.muns) / aptos_tot if aptos_tot else 0.0
        bruto_t = sum(a + b for a, b in obs_uf.values())
        regioes = {}
        for r in REGIOES:
            res = resumo(acc[r], P["sigmaPisoReg"], fator[r])
            if res:
                obs_a = sum(a for uf, (a, _) in obs_uf.items() if REGIAO.get(uf) == r)
                obs_b = sum(b for uf, (_, b) in obs_uf.items() if REGIAO.get(uf) == r)
                res.update(apurado=apurado_reg[r], obs_a=obs_a, obs_b=obs_b)
                regioes[r] = res
        return {
            **br, "apurado": apurado, "bruto_a": (sum(a for a, _ in obs_uf.values()) / bruto_t) if bruto_t else None,
            "restante": rem_tot, "delta_br": theta[0], "gamma": theta[self.ix_g], "beta": theta[self.ix_b],
            "kappa_br": kN, "unidades": len(units), "municipios_baixados": len(own), "regioes": regioes,
            "inflacao": fator,
            "por_uf": {uf: {"share_a": v["A"] / (v["A"] + v["B"]) if v["A"] + v["B"] else None} for uf, v in por_uf.items()},
        }


def modelo_2026(base_path: Optional[str] = None, params: Optional[dict] = None) -> Modelo:
    base_path = base_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "eleicao_base_2026.json")
    base = load_base(base_path)
    return Modelo(build_muns(base["municipios"], base["cand_a"], base["cand_b"], TRANSFER_2026),
                  {"inflacao": INFLACAO_2026, **(params or {})})
