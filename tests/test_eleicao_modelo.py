"""Offline tests for the 2nd-round projection model (league/eleicao_modelo.py): synthetic countries, no network.

Run:  python3 -m unittest discover -s tests -v
"""
import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

import eleicao_modelo as M  # noqa: E402


def country(n_per_uf=20, ufs=("sc", "rs", "pr", "ba", "pe", "ce"), share=None, aptos=10_000, outros=0):
    """1º turno sintético: A forte no Sul, B forte no Nordeste."""
    rows = []
    for uf in ufs:
        s = share(uf) if share else (0.62 if M.REGIAO[uf] == "S" else 0.38)
        for i in range(n_per_uf):
            v = 8_000
            rows.append({"key": f"{uf}{i:05d}", "uf": uf, "aptos": aptos,
                         "votos": {"22": round(v * s), "13": round(v * (1 - s)), "70": outros}})
    return rows


def second_round(rows, swing, region_swing=None):
    """2º turno 'verdadeiro': base + swing (log-odds), opcionalmente diferente por região; mesmo comparecimento."""
    out = {}
    for r in rows:
        a1, b1 = r["votos"]["22"], r["votos"]["13"]
        sw = swing + (region_swing or {}).get(M.REGIAO[r["uf"]], 0.0)
        p = M.inv_logit(M.logit(a1 / (a1 + b1)) + sw)
        n = a1 + b1
        out[r["key"]] = (n * p, n * (1 - p))
    return out


def observe(rows, truth, counted):
    """counted: {key: fração apurada} → (frac, obs_uf)."""
    obs_uf = {}
    for r in rows:
        f = counted.get(r["key"], 0.0)
        a, b = truth[r["key"]]
        cur = obs_uf.setdefault(r["uf"], [0.0, 0.0])
        cur[0] += f * a
        cur[1] += f * b
    return counted, {u: tuple(v) for u, v in obs_uf.items()}


def final_share(truth):
    a = sum(v[0] for v in truth.values())
    return a / (a + sum(v[1] for v in truth.values()))


class ModeloTest(unittest.TestCase):
    def model(self, rows, transfer=None):
        return M.Modelo(M.build_muns(rows, "22", "13", transfer, turnout=1.0))

    def test_everything_counted_is_exact(self):
        rows = country()
        truth = second_round(rows, 0.1)
        frac, obs = observe(rows, truth, {r["key"]: 1.0 for r in rows})
        r = self.model(rows).projetar(frac, {}, obs)
        self.assertAlmostEqual(r["share_a"], final_share(truth), places=9)
        self.assertLess(r["sd"], 1e-9)
        self.assertAlmostEqual(r["apurado"], 1.0)

    def test_order_bias_is_corrected(self):
        # Só o Sul apurou (onde A é forte): o bruto engana, a projeção não
        rows = country()
        truth = second_round(rows, 0.08)
        south = {r["key"]: 1.0 for r in rows if M.REGIAO[r["uf"]] == "S"}
        frac, obs = observe(rows, truth, south)
        r = self.model(rows).projetar(frac, {}, obs)
        final = final_share(truth)
        self.assertGreater(r["bruto_a"] - final, 0.10)  # bruto 10+ pontos fora
        self.assertLess(abs(r["share_a"] - final), 0.01)  # projeção a menos de 1 ponto
        self.assertTrue(r["faixa90"][0] <= final <= r["faixa90"][1])

    def test_regional_swing_is_learned_where_observed(self):
        rows = country()
        truth = second_round(rows, 0.0, {"S": 0.15, "NE": -0.05})
        half = {r["key"]: 1.0 for i, r in enumerate(rows) if i % 2 == 0}  # metade de cada UF apurada
        frac, obs = observe(rows, truth, half)
        r = self.model(rows).projetar(frac, {}, obs)
        self.assertLess(abs(r["share_a"] - final_share(truth)), 0.004)

    def test_uncertainty_shrinks_as_count_grows(self):
        rows = country()
        truth = second_round(rows, 0.05)
        m = self.model(rows)
        sds = []
        for pct in (0.1, 0.4, 0.8):
            cut = {r["key"]: 1.0 for i, r in enumerate(rows) if (i % 10) < pct * 10}
            sds.append(m.projetar(cut, {}, observe(rows, truth, cut)[1])["sd"])
        self.assertGreater(sds[0], sds[1])
        self.assertGreater(sds[1], sds[2])

    def test_prior_uses_vote_transfers(self):
        rows = country(share=lambda uf: 0.5, outros=2_000)
        neutral = self.model(rows).prior_share()
        to_a = self.model(rows, {"70": (0.8, 0.1)}).prior_share()
        self.assertAlmostEqual(neutral, 0.5, places=6)
        self.assertGreater(to_a, 0.55)

    def test_no_data_is_the_prior(self):
        rows = country()
        m = self.model(rows)
        r = m.projetar({}, {}, {})
        self.assertAlmostEqual(r["share_a"], m.prior_share(), places=9)
        self.assertGreater(r["sd"], 0.02)  # sem voto apurado, incerteza larga

    def test_probability_matches_interval(self):
        rows = country()
        truth = second_round(rows, 0.3)
        cut = {r["key"]: 1.0 for i, r in enumerate(rows) if i % 3 == 0}
        r = self.model(rows).projetar(cut, {}, observe(rows, truth, cut)[1])
        z = (r["share_a"] - 0.5) / r["sd"]
        self.assertAlmostEqual(r["prob_a"], 0.5 * (1 + math.erf(z / math.sqrt(2))), places=9)

    def test_regional_projection_and_interval(self):
        rows = country()
        truth = second_round(rows, 0.0, {"S": 0.15, "NE": -0.05})
        half = {r["key"]: 1.0 for i, r in enumerate(rows) if i % 2 == 0}
        r = self.model(rows).projetar(half, {}, observe(rows, truth, half)[1])
        for reg in ("S", "NE"):
            a = sum(v[0] for k, v in truth.items() if M.REGIAO[k[:2]] == reg)
            b = sum(v[1] for k, v in truth.items() if M.REGIAO[k[:2]] == reg)
            got = r["regioes"][reg]
            self.assertLess(abs(got["share_a"] - a / (a + b)), 0.01)
            self.assertLess(got["faixa90"][0], got["share_a"])
            self.assertGreater(got["faixa90"][1], got["share_a"])
            self.assertAlmostEqual(got["apurado"], 0.5, places=6)
        # regiões somam o Brasil
        self.assertAlmostEqual(sum(v["votos_a"] for v in r["regioes"].values()), r["votos_a"], places=3)

    def test_comparativo_same_places(self):
        import eleicao_tse as E
        m = M.modelo_2026()
        sc = [mu for mu in m.muns if mu.uf == "sc"][:5]
        frac = {mu.key: 1.0 for mu in sc}
        frac[sc[0].key] = 0.5
        c = E.comparativo(m, frac, 0.6)
        a = sum(frac[mu.key] * mu.a1 for mu in sc)
        b = sum(frac[mu.key] * mu.b1 for mu in sc)
        self.assertAlmostEqual(c["t1_2026"], a / (a + b), places=9)
        self.assertEqual(c["agora"], 0.6)
        self.assertTrue(0 < c["t2_2022"] < 1 and 0 < c["t1_2022"] < 1)
        self.assertIsNone(E.comparativo(m, frac, None))
        # abstenção: agora (TSE, seções totalizadas) × 1º turno nos mesmos municípios
        c = E.comparativo(m, frac, 0.6, {"c": 790, "est": 1000})
        self.assertAlmostEqual(c["abst_agora"], 0.21)
        comp = sum(frac[mu.key] * mu.c1 for mu in sc)
        aptos = sum(frac[mu.key] * mu.aptos for mu in sc)
        self.assertAlmostEqual(c["abst_t1"], 1 - comp / aptos, places=9)
        self.assertIsNone(E.comparativo(m, frac, 0.6)["abst_agora"])  # sem dados de comparecimento, sem linha

    def test_g1_parser_skips_other_headings_and_flags_repeated_numbers(self):
        import eleicao_g1 as G1
        page = ("<h2>OCEANIA E ÁSIA</h2>"
                "<h2>Singapura</h2><ul><li>2º turno: Lula, 213 votos (63,77%)</li><li>2º turno: Flávio, 121 votos</li>"
                "<li>1º turno: Lula, 190 votos</li><li>1º turno: Flávio, 92 votos</li></ul>"
                "<h2>Síria</h2><ul><li>2º turno: Lula, 213 (63,77%)</li><li>2º turno: Flávio, 121 (36,23%)</li>"
                "<li>1º turno: Lula, 97 votos</li><li>1º turno: Flávio, 7 votos</li></ul>"
                "<h2>Omã</h2><ul><li>2º turno: ainda não há dados disponíveis</li><li>1º turno: Flávio, 20 votos</li></ul>"
                "<h2>ASSISTA à repercussão na GloboNews</h2><h2>Votação pelo mundo: Lula vence em 41 países</h2>")
        p = G1.parse(page)
        self.assertEqual(list(p), ["Singapura", "Síria", "Omã"])  # chamadas de outras matérias ficam de fora
        self.assertEqual(p["Síria"]["repetido_de"], "Singapura")  # 334 votos contra 104 no 1º turno: é a cópia
        r = G1.resumo(p)
        self.assertEqual((r["paises_com"], r["paises"], r["a"], r["b"]), (1, 3, 121, 213))
        self.assertEqual(r["repetidos"], {"Síria": "Singapura"})

    def test_margin_inflation_tapers_with_regional_count(self):
        rows = country(n_per_uf=50)
        truth = second_round(rows, 0.05)
        m = M.Modelo(M.build_muns(rows, "22", "13", None, turnout=1.0), {"inflacao": 2.5})
        sul = [r["key"] for r in rows if M.REGIAO[r["uf"]] == "S"]
        for pct, esperado in ((0.10, 2.5), (0.40, 1.75), (0.70, 1.0)):
            n = int(round(pct * len(sul)))
            cut = {k: 1.0 for k in sul[:n]}
            r = m.projetar(cut, {}, observe(rows, truth, cut)[1])
            self.assertAlmostEqual(r["inflacao"]["S"], esperado, places=6)
            self.assertAlmostEqual(r["inflacao"]["NE"], 2.5)  # Nordeste sem apurar: margem cheia
            self.assertGreater(r["inflacao"]["BR"], r["inflacao"]["S"] - 1e-9)  # o que falta é quase todo do Nordeste
        # sem inflação (backtests), nada muda
        r1 = M.Modelo(M.build_muns(rows, "22", "13", None, turnout=1.0)).projetar(cut, {}, observe(rows, truth, cut)[1])
        self.assertEqual(set(r1["inflacao"].values()), {1.0})

    def test_real_base_prior_matches_research(self):
        # 1º turno real de 2026 + migração do research de 21/10 → 51,8% (a "projeção da casa", p. 14)
        m = M.modelo_2026()
        self.assertEqual(round(100 * m.prior_share(), 1), 51.8)
        self.assertGreater(len(m.muns), 5700)


if __name__ == "__main__":
    unittest.main()
