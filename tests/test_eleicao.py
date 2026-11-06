"""Offline tests for the election watcher (league/eleicao_tse.py): no network, nothing is sent.

Run:  python3 -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "league"))

import eleicao_tse as E  # noqa: E402

T1_NIGHT = datetime(2026, 10, 4, 18, 0, tzinfo=E.BRT)


def payload(pst="0,00", tf="n", nv="1", cands=(), hg="18:00:00", v=None, te=0, esnt=0):
    """Minimal unified file with the TSE layout (carg → agr → par → cand, v = vote totals)."""
    par = [{"sg": sg, "cand": [{"nmu": nm, "vap": str(v), "pvap": pct, "st": st, "e": "s" if st in ("Eleito", "2º turno") else "n",
                                "seq": str(i)}]} for i, (nm, sg, v, pct, st) in enumerate(cands, 1)]
    return {"tf": tf, "dg": "04/10/2026", "hg": hg, "s": {"pst": pst, "st": "1", "ts": "10"}, "v": v or {},
            "e": {"te": str(te), "esnt": str(esnt)},
            "carg": [{"cd": "1", "nv": nv, "agr": [{"par": par}]}]}


def dep_payload(tf="n", nv="3", agrs=(), qe=0):
    """Proportional file: agrs = [(sigla, tp, tvtn, tvtl, vag, [(nome, votos, situação), ...]), ...]."""
    out = []
    for sg, tp, tvtn, tvtl, vag, cands in agrs:
        cs = [{"nmu": nm, "vap": str(v), "pvap": "1,00", "st": st, "e": "n", "seq": "1"} for nm, v, st in cands]
        agr = {"nm": "FEDERAÇÃO BRASIL DA ESPERANÇA" if tp == "f" else sg, "tp": tp, "com": sg, "vag": str(vag)}
        if tp == "f":  # federação: votos na agremiação, um partido por sigla
            agr.update(tvtn=str(tvtn), tvtl=str(tvtl))
            agr["par"] = [{"sg": sg.split("/")[0], "cand": cs}]
        else:
            agr["par"] = [{"sg": sg, "tvtn": str(tvtn), "tvtl": str(tvtl), "cand": cs}]
        out.append(agr)
    return {"tf": tf, "dg": "04/10/2026", "hg": "22:00:00", "s": {"pst": "100,00"}, "v": {}, "e": {},
            "carg": [{"cd": "6", "nv": nv, "agr": out, **({"qe": str(qe)} if qe else {})}]}


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class FakeTSE:
    """Serves per-race payloads as plain JSON (404 on .jws), with ETag / 304 support."""

    def __init__(self):
        self.data, self.calls, self.default = {}, [], payload()

    def set(self, key, p):
        self.data[key] = p

    def __call__(self, url, etag=""):
        self.calls.append(url)
        if url.endswith(".jws"):
            return 404, None, "", {}
        if "-ab." in url:
            uf = url.rsplit("/", 2)[-2]
            key = "ab" if uf == "br" else f"ab_{uf}"  # andamento nacional (regiões) ou da UF (projeção)
            return (200, json.dumps(self.data[key]), "", {}) if key in self.data else (404, None, "", {})
        if "-c0001-" in url and "/br/" not in url:  # Presidente por UF (projeção)
            uf = url.rsplit("/", 2)[-2]
            body = json.dumps(self.data.get(f"pres_{uf}", payload()))
            return 200, body, str(hash(body)), {}
        key = next(r.key for r in E.RACES + E.DEPUTADOS + E.SENADO + E.GOV
                   if f"-c{r.cargo:04d}-" in url and f"/{r.uf}/" in url)
        body = json.dumps(self.data.get(key, self.default))
        tag = str(hash(body))
        if etag == tag:
            return 304, None, etag, {}
        return 200, body, tag, {"x-ratelimit-remaining": "1999"}


SEN_SC = E.Race("sen", "Senado SC", "🏛️", "sc", 5, 6259, None, top=6)
GOV_SC = E.Race("gov", "Governo SC", "🌲", "sc", 3, 6259, None, top=3)


class ParseTest(unittest.TestCase):
    def test_summary_sorted_by_votes(self):
        s = E.summarize(payload("12,34", cands=[("B", "X", 10, "10,00", ""), ("A", "Y", 90, "90,00", "")]))
        self.assertEqual([c["nm"] for c in s["cands"]], ["A", "B"])
        self.assertEqual(s["pst"], "12,34")
        self.assertFalse(s["tf"])

    def test_second_round_detection(self):
        two = E.summarize(payload("100,00", "s", cands=[("A", "X", 48, "48,00", "2º turno"), ("B", "Y", 40, "40,00", "2º turno")]))
        won = E.summarize(payload("100,00", "s", cands=[("A", "X", 60, "60,00", "Eleito"), ("B", "Y", 40, "40,00", "Não eleito")]))
        no_status = E.summarize(payload("99,00", cands=[("A", "X", 45, "45,00", ""), ("B", "Y", 40, "40,00", "")]))
        self.assertTrue(E.needs_second_round(two))
        self.assertFalse(E.needs_second_round(won))
        self.assertTrue(E.needs_second_round(no_status))

    def test_format(self):
        cands = [("JOAO DA SILVA", "PL", 3000, "30,00", ""), ("MARIA", "PT", 2500, "25,00", ""),
                 ("ZE", "PSD", 2000, "20,00", ""), ("A", "X", 1500, "15,00", ""), ("B", "Y", 1000, "10,00", "")]
        v = {"tv": "11000", "vv": "9870", "vvc": "10000", "vb": "600", "pvb": "5,45", "tvn": "400", "ptvn": "3,64",
             "van": "0", "vansj": "130"}
        txt = E.format_race(SEN_SC, E.summarize(payload("51,20", nv="2", cands=cands, v=v, te=50000)))
        self.assertIn("*51,20%* apurado", txt)
        self.assertIn("(2 vagas)", txt)
        self.assertIn("1. Joao da Silva (PL) 30,00% · 3.000", txt)
        self.assertIn("5. B (Y) 10,00% · 1.000", txt)
        self.assertIn("2º → 3º: 5,00 p.p. (500 votos)", txt)
        self.assertIn("Válidos: 9.870 (89,73%)", txt)
        self.assertIn("Brancos: 600 (5,45%)", txt)
        self.assertIn("Nulos: 400 (3,64%)", txt)
        self.assertIn("Anulados/sub judice: 130", txt)
        self.assertIn("*Total: 11.000 / 100.000 votos* (2 por eleitor)", txt)  # apurados / eleitorado × 2
        many = [(f"C{i}", "X", 100 - i, "1,00", "") for i in range(10)]
        pres = E.format_race(E.BY_KEY["pres"], E.summarize(payload("1,00", cands=many, v={"tv": "10"})))
        self.assertIn("*Presidente* (Brasil)", pres)
        self.assertIn("6. C5", pres)
        self.assertNotIn("7. C6", pres)  # top 6
        self.assertNotIn("Anulados", pres)

    def test_only_president_in_real_time(self):
        self.assertEqual([r.key for r in E.RACES], ["pres"])

    def test_regions(self):
        uf = lambda cd, st, ts, c: {"tpabr": "uf", "cdabr": cd, "s": {"st": str(st), "ts": str(ts)},  # noqa: E731
                                    "e": {"c": str(c), "te": str(c * 10)}}
        ab = {"dg": "04/10/2026", "hg": "18:00:00", "abr": [{"tpabr": "br", "cdabr": "br", "s": {"st": "99", "ts": "99"}},
                                                             uf("sp", 50, 100, 1000000), uf("rj", 25, 100, 400000),
                                                             uf("sc", 10, 40, 123456), uf("zz", 0, 10, 0)]}
        txt = E.format_regions(E.summarize_regions(ab))
        self.assertIn("Sudeste: 37,50% · 1.400.000 / 14.000.000", txt)
        self.assertIn("Sul: 25,00% · 123.456 / 1.234.560", txt)
        self.assertIn("Norte: 0,00% · 0", txt)
        self.assertIn("Exterior: 0,00% · 0", txt)
        self.assertEqual(E.format_regions(E.summarize_regions({"abr": []})), "")

    def test_old_cache_entry_is_refetched(self):
        state = {"cache": {E.url_for(E.BY_KEY["pres"], 1): {"etag": "x", "summ": {"pst": "1,00"}}}}
        seen = []
        fetcher = lambda url, etag="": (seen.append(etag), (404, None, "", {}))[1]  # noqa: E731
        E.load_race(E.BY_KEY["pres"], 1, state, fetcher)
        self.assertEqual(seen[0], "")  # no If-None-Match for a summary from an older format

    def test_jws_signature(self):
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError:
            self.skipTest("cryptography not installed")
        key = Ed25519PrivateKey.generate()
        x = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        head = b64(json.dumps({"alg": "EdDSA", "kid": "k1"}).encode())
        body = b64(json.dumps({"ok": 1}).encode())
        tok = f"{head}.{body}.{b64(key.sign(f'{head}.{body}'.encode()))}"
        with mock.patch.multiple(E, JWK_X=x, JWK_KID="k1"):
            self.assertEqual(E.decode_jws(tok), {"ok": 1})
            with self.assertRaises(E.ApiError):
                E.decode_jws(tok[:-4] + "AAAA")


class MathElectedTest(unittest.TestCase):
    """Matematicamente eleito: os votos que faltam (R = eleitores das seções não totalizadas) não mudam o resultado."""

    def res(self, cands, esnt, second_round, nv="1", tf="n"):
        el, rt = E.majoritarian_result(E.summarize(payload("90,00", tf, nv=nv, cands=cands, esnt=esnt)), second_round)
        return [c["nm"] for c in el], [c["nm"] for c in rt]

    def test_senate_seat_locked_only_beyond_reach(self):
        cands = [("A", "PL", 1000, "", ""), ("B", "PT", 700, "", ""), ("C", "PSD", 600, "", "")]
        self.assertEqual(self.res(cands, 300, False, nv="2"), (["A"], []))  # A−C = 400 > 300; B−C = 100 ≤ 300
        self.assertEqual(self.res(cands, 99, False, nv="2"), (["A", "B"], []))
        self.assertEqual(self.res(cands, 400, False, nv="2"), ([], []))  # 400 is not > 400

    def test_governor_wins_outright(self):
        cands = [("A", "PL", 600, "", ""), ("B", "PT", 300, "", ""), ("C", "PSD", 100, "", "")]
        self.assertEqual(self.res(cands, 199, True), (["A"], []))  # 1200 > 1000 + 199
        self.assertEqual(self.res(cands, 200, True), ([], []))  # 1200 is not > 1200

    def test_governor_runoff_defined(self):
        cands = [("A", "PL", 400, "", ""), ("B", "PT", 350, "", ""), ("C", "PSD", 250, "", "")]
        self.assertEqual(self.res(cands, 99, True), ([], ["A", "B"]))  # 2·400+99 ≤ 1000 and 350−250 > 99
        self.assertEqual(self.res(cands, 100, True), ([], []))  # C could still pass B

    def test_official_status_wins_over_math(self):
        cands = [("A", "PL", 10, "", "Eleito"), ("B", "PT", 9, "", "")]
        self.assertEqual(self.res(cands, 10 ** 6, True), (["A"], []))

    def test_runoff_flag_is_not_elected(self):
        # bug real de 04/10: o TSE marca e="s" nos dois que vão ao 2º turno
        summ = E.summarize(payload("100,00", "s", cands=[("MAILZA", "PP", 49, "49,76", "2º turno"),
                                                         ("ALAN", "REP", 32, "32,27", "2º turno"), ("T", "PSDB", 10, "10,22", "Não eleito")]))
        self.assertTrue(all(c["e"] for c in summ["cands"][:2]))
        self.assertEqual(E.majoritarian_result(summ, second_round=True), ([], summ["cands"][:2]))
        self.assertFalse(E.is_elected({"st": "Suplente", "e": True}))
        self.assertTrue(E.is_elected({"st": "", "e": True}))

    def boards(self):
        gov = {"gov_sc": E.summarize(payload("99,00", cands=[("JORGINHO", "PL", 700, "70,00", ""), ("X", "PT", 300, "30,00", "")])),
               "gov_df": E.summarize(payload("100,00", cands=[("A", "PP", 45, "45,00", ""), ("B", "PT", 35, "35,00", ""),
                                                              ("C", "X", 20, "20,00", "")])),
               "gov_sp": E.summarize(payload("50,00", cands=[("D", "R", 51, "51,00", ""), ("E", "PT", 49, "49,00", "")],
                                             esnt=10 ** 6))}
        sen = {"sen_sc": E.summarize(payload("99,00", nv="2", cands=[("C1", "PL", 900, "", ""), ("C2", "PL", 800, "", ""),
                                                                     ("C3", "PP", 100, "", "")])),
               "sen_rs": E.summarize(payload("99,00", nv="2", cands=[("R1", "PL", 900, "", ""), ("R2", "NOVO", 300, "", ""),
                                                                     ("R3", "PT", 250, "", "")], esnt=100))}
        return gov, sen

    def test_governors_message(self):
        gov, _ = self.boards()
        txt = E.format_governors(gov, T1_NIGHT)
        self.assertTrue(txt.startswith("🏛️ *Governadores eleitos*"))
        self.assertIn("📊 *Eleitos por partido:* PL 1", txt)
        self.assertIn("✅ SC: Jorginho (PL) 70,00%", txt)
        self.assertIn("🔁 DF: 2º turno — A (PP) × B (PT)", txt)
        self.assertIn("SP 50,00%", txt)
        self.assertNotIn("Senado", txt)

    def test_senators_message_with_composition(self):
        _, sen = self.boards()
        cont = [{"uf": "sc", "partido": "PL"}, {"uf": "rs", "partido": "PT"}, {"uf": "sp", "partido": "PL"}]
        with mock.patch.object(E, "load_current_senate", return_value={"PL": 2, "PT": 3, "PSDB": 1}):
            txt = E.format_senators(sen, T1_NIGHT, continuing=cont)
        self.assertTrue(txt.startswith("🏛️ *Senadores eleitos*"))
        self.assertIn("📊 *Eleitos por partido:* PL 3", txt)  # C1, C2 (SC) + R1 (RS)
        self.assertIn("✅ RS: R1 (PL) · 1 vaga em aberto (99,00%)", txt)
        self.assertIn("*3 de 54 vagas definidas*", txt)
        comp = txt[txt.index("🏛️ *Senado a partir de 2027"):]
        self.assertIn("— 7 cadeiras por partido*", comp)  # 3 eleitos + 1 projetado (R2) + 3 continuam
        self.assertIn("(era → vai)", comp)
        self.assertIn("PL: 2 → *5* (+3) · 3 ✅ + 2 ↪️", comp)
        self.assertIn("PT: 3 → *1* (−2) · 1 ↪️", comp)
        self.assertIn("NOVO: 0 → *1* (+1) · 1 ⏳", comp)
        self.assertIn("PSDB: 1 → *0* (−1)", comp)  # tinha cadeira e zera
        lines = [ln.split(":")[0] for ln in comp.splitlines() if " → *" in ln]
        self.assertEqual(lines, ["PL", "PT", "NOVO", "PSDB"])  # total de 2027; empate: mais eleitos, depois mais cadeiras hoje
        self.assertNotIn("Governadores", txt)

    def test_continuing_snapshot(self):
        cont = E.load_continuing()
        self.assertEqual(len(cont), 27)
        self.assertEqual(sum(E.load_current_senate().values()), 81)
        self.assertEqual(sorted(c["uf"] for c in cont), sorted(E.UFS))


class DeputiesElectedTest(unittest.TestCase):
    def test_grouped_by_legenda_with_votes(self):
        summ = E.summarize(dep_payload("s", nv="3", agrs=[
            ("PL", "i", 5000, 300, 2, [("JULIA", 3000, "Eleito por QP"), ("ZE", 1500, "Eleito por média"), ("NAO", 400, "Suplente")]),
            ("PT/PC do B/PV", "f", 2500, 200, 1, [("ANA", 2000, "Eleito por QP"), ("BETO", 100, "Suplente")]),
            ("UP", "i", 50, 5, 0, [("X", 50, "Não eleito")])]))
        self.assertTrue(E.deputies_known(summ))
        txt = E.format_elected_deputies(E.DEPUTADOS[0], summ, T1_NIGHT)
        self.assertIn("*PL* — 2 eleitos · legenda: 5.300 votos (5.000 nominais + 300 de legenda)", txt)
        self.assertIn("• Julia (PL) — 3.000 (QP)", txt)
        self.assertIn("• Ze (PL) — 1.500 (média)", txt)
        self.assertIn("*PT/PC do B/PV (Federação Brasil da Esperança)* — 1 eleito · legenda: 2.700 votos", txt)
        self.assertNotIn("Nao", txt)
        self.assertNotIn("UP", txt)
        self.assertLess(txt.index("*PL*"), txt.index("*PT/PC"))

    def test_not_known_until_tse_marks_the_seats(self):
        partial = E.summarize(dep_payload("n", nv="3", agrs=[("PL", "i", 10, 0, 2, [("A", 9, "Eleito por QP")])]))
        self.assertFalse(E.deputies_known(partial))
        closed_unmarked = E.summarize(dep_payload("s", nv="3", agrs=[("PL", "i", 10, 0, 2, [("A", 9, "")])]))
        self.assertFalse(E.deputies_known(closed_unmarked))  # nunca manda lista vazia


class ProjectionTest(unittest.TestCase):
    """Regras de legenda: QE, QP (≥10% QE), sobras 80/20 pela maior média, 3ª fase com todas."""

    def project(self, nv, agrs, qe=0):
        seats, el, q = E.project_seats(E.summarize(dep_payload(nv=str(nv), agrs=agrs, qe=qe)))
        return seats, [(c["nm"], how) for c, how, _ in el], q

    def test_quotient_and_party_quotient(self):
        # 1000 válidos / 4 vagas → QE 250. A: 600 → QP 2; B: 300 → QP 1; C: 100 (< 80% QE)
        seats, el, qe = self.project(4, [("A", "i", 600, 0, 0, [("A1", 400, ""), ("A2", 150, ""), ("A3", 50, "")]),
                                         ("B", "i", 300, 0, 0, [("B1", 280, ""), ("B2", 20, "")]),
                                         ("C", "i", 100, 0, 0, [("C1", 100, "")])])
        self.assertEqual(qe, 250)
        self.assertEqual(el[:3], [("A1", "QP"), ("A2", "QP"), ("B1", "QP")])
        # sobra: médias A 600/3=200, B 300/2=150; C fora (<80% QE). A3 tem 50 = 20% do QE → elegível
        self.assertEqual(el[3], ("A3", "média"))
        self.assertEqual(seats, {0: 3, 1: 1, 2: 0})

    def test_individual_thresholds(self):
        # QE 250: A tem QP 2, mas A2 (20) < 10% do QE → a vaga vai para as sobras
        seats, el, _ = self.project(4, [("A", "i", 600, 0, 0, [("A1", 580, ""), ("A2", 20, "")]),
                                        ("B", "i", 400, 0, 0, [("B1", 300, ""), ("B2", 60, ""), ("B3", 40, "")])])
        self.assertIn(("A1", "QP"), el)
        self.assertNotIn("A2", [n for n, _ in el[:3]])
        self.assertEqual([n for n, h in el if h == "QP"], ["A1", "B1"])
        # sobras (≥ 20% QE = 50): A sem candidato elegível → B2 pela média; depois 3ª fase (A2 ou B3)
        self.assertIn(("B2", "média"), el)
        self.assertEqual(el[-1][1], "média 3ª fase")
        self.assertEqual(len(el), 4)

    def test_uses_tse_quotient_when_present(self):
        _, _, qe = self.project(2, [("A", "i", 100, 0, 0, [("A1", 100, "")])], qe=77)
        self.assertEqual(qe, 77)

    def test_round_half_down(self):
        self.assertEqual(E._round_half_down(10.5), 10)
        self.assertEqual(E._round_half_down(10.51), 11)

    def test_message_flags_agreement_with_tse(self):
        agrs = [("PL", "i", 600, 0, 3, [("A1", 400, ""), ("A2", 150, ""), ("A3", 50, "")]),
                ("PT/PC do B/PV", "f", 300, 0, 1, [("B1", 280, "")]), ("UP", "i", 100, 0, 0, [("C1", 100, "")])]
        summ = {"depf_sc": E.summarize(dep_payload(nv="4", agrs=agrs))}
        summ["depf_sc"]["pst"] = "90,00"
        txt = E.format_projected_deputies("sc", summ, [E.DEPUTADOS[0]], T1_NIGHT)
        self.assertIn("eleitos se a apuração acabasse agora", txt)
        self.assertIn("*Deputado Federal* · 90,00% apurado · 4 vagas · QE 250", txt)
        self.assertIn("• A3 (PL) — 50 (média)", txt)
        self.assertIn("✔️ _Vagas por legenda batem com a distribuição do TSE._", txt)
        agrs[0] = ("PL", "i", 600, 0, 2, agrs[0][5])
        agrs[2] = ("UP", "i", 100, 0, 1, agrs[2][5])
        summ = {"depf_sc": E.summarize(dep_payload(nv="4", agrs=agrs))}
        summ["depf_sc"]["pst"] = "90,00"
        self.assertIn("⚠️ _Difere do TSE (nosso×TSE): PL 3×2, UP 0×1_",
                      E.format_projected_deputies("sc", summ, [E.DEPUTADOS[0]], T1_NIGHT))


class TickTest(unittest.TestCase):
    def setUp(self):
        self.tse, self.state, self.out = FakeTSE(), E.new_state(), []
        # convites agendados, lista final do g1 e pausa da apuração têm testes próprios; aqui ficam desligados
        self.state.update(convites=list(E.CONVITE_HORARIOS), g1_final_sent=True)
        pausa = mock.patch.multiple(E, PAUSA_DESDE=E.PAUSA_ATE)
        pausa.start()
        self.addCleanup(pausa.stop)
        import eleicao_g1
        g1 = mock.patch.object(eleicao_g1, "fetch", side_effect=RuntimeError("sem rede nos testes"))
        g1.start()
        self.addCleanup(g1.stop)
        # cadências fixas nos testes (a configuração de produção é conferida em test_production_cadence)
        for name, minutes in (("THROTTLE_T1", 5), ("ELEITOS_EVERY", 20)):
            patcher = mock.patch.object(E, name, E.timedelta(minutes=minutes))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_production_cadence(self):
        mock.patch.stopall()
        self.assertEqual((E.THROTTLE_T1, E.ELEITOS_EVERY), (E.timedelta(minutes=30), E.timedelta(minutes=30)))

    def run_tick(self, now=T1_NIGHT, ok=True):
        def send(t):
            if ok:
                self.out.append(t)
            return ok
        return E.tick(self.state, send, now=now, fetcher=self.tse)

    def quiet_periodic(self, now=T1_NIGHT):
        self.state.update(gov_at=now.timestamp(), sen_at=now.timestamp(), eleitos_check_at=now.timestamp(),
                          dep_check_at=now.timestamp())

    def test_before_window_no_network(self):
        self.run_tick(now=datetime(2026, 10, 4, 15, 0, tzinfo=E.BRT))
        self.assertEqual(self.tse.calls, [])
        self.assertEqual(self.out, [])

    def test_arms_then_only_sends_on_pst_change(self):
        self.quiet_periodic()
        self.run_tick()
        self.assertEqual(len(self.out), 1)  # only the "monitor ligado" note at 0%
        self.assertIn("Monitor da apuração ligado", self.out[0])
        self.tse.set("pres", payload("10,00", cands=[("LULA", "PT", 100, "50,00", "")]))
        self.tse.set("ab", {"abr": [{"tpabr": "uf", "cdabr": "ba", "s": {"st": "1", "ts": "4"}, "e": {"c": "100"}}]})
        self.run_tick()
        self.assertEqual(len(self.out), 2)
        self.assertIn("*10,00%* apurado", self.out[1])
        self.assertTrue(self.out[1].split("\n\n")[-2].startswith("📍 *Presidente por região*"))  # last block
        self.assertIn("Nordeste: 25,00% · 100", self.out[1])
        calls = len(self.tse.calls)
        self.run_tick()  # nothing changed (304s) → silent
        self.assertEqual(len(self.out), 2)
        self.assertLessEqual(len(self.tse.calls) - calls, 4)  # pres + regiões, ≤ 2 tries each (jws → json)

    def test_president_at_most_every_5_min_in_first_round(self):
        self.state["armed"] = True
        self.quiet_periodic()
        pres = lambda: [t for t in self.out if t.startswith("🗳️ *Apuração")]  # noqa: E731
        for minute, pst in ((0, "10,00"), (1, "11,00"), (4, "12,00"), (5, "13,00")):
            self.tse.set("pres", payload(pst, cands=[("A", "PL", 10, "100,00", "")]))
            self.quiet_periodic(T1_NIGHT.replace(minute=minute))
            self.run_tick(now=T1_NIGHT.replace(minute=minute))
        self.assertEqual(len(pres()), 2)  # :00 e :05
        self.assertIn("*13,00%* apurado", pres()[1])

    def test_second_round_is_real_time(self):
        self.state.update(turn=2, t2=["pres"], armed=True)
        pres = lambda: [t for t in self.out if t.startswith("🗳️ *2º TURNO · PRESIDENTE*")]  # noqa: E731
        night = datetime(2026, 10, 25, 18, 0, tzinfo=E.BRT)
        for minute, pst in ((0, "10,00"), (1, "11,00"), (2, "12,00")):
            self.tse.set("pres", payload(pst, cands=[("A", "PL", 10, "100,00", "")]))
            self.run_tick(now=night.replace(minute=minute))
        self.assertEqual(len(pres()), 3)

    def test_failed_send_is_retried(self):
        self.state["armed"] = True
        self.quiet_periodic()
        self.tse.set("pres", payload("5,00", cands=[("A", "PL", 10, "100,00", "")]))
        self.run_tick(ok=False)
        self.assertNotIn("pres", self.state["sent_pst"])
        self.run_tick()
        self.assertEqual(len(self.out), 1)

    def test_gov_and_sen_separate_every_20_min(self):
        self.state["armed"] = True
        self.state.update(dep_check_at=T1_NIGHT.timestamp())
        self.tse.set("gov_sc", payload("90,00", cands=[("J", "PL", 700, "70,00", ""), ("X", "PT", 300, "30,00", "")]))
        self.run_tick()
        gov = lambda: [t for t in self.out if t.startswith("🏛️ *Governadores eleitos*")]  # noqa: E731
        sen = lambda: [t for t in self.out if t.startswith("🏛️ *Senadores eleitos*")]  # noqa: E731
        self.assertEqual((len(gov()), len(sen())), (1, 1))
        self.assertIn("✅ SC: J (PL) 70,00%", gov()[0])
        self.assertIn("🏛️ *Senado a partir de 2027 — 27 cadeiras por partido*", sen()[0])  # só os que continuam
        self.run_tick(now=T1_NIGHT.replace(minute=19))
        self.assertEqual((len(gov()), len(sen())), (1, 1))
        self.run_tick(now=T1_NIGHT.replace(minute=20))
        self.assertEqual((len(gov()), len(sen())), (2, 2))

    def test_new_definition_sends_full_list_after_5_min_gap(self):
        self.state["armed"] = True
        self.state.update(dep_check_at=T1_NIGHT.timestamp())
        boards = lambda: [t for t in self.out if t.startswith("🏛️ *Senadores eleitos*")]  # noqa: E731
        gov = lambda: [t for t in self.out if t.startswith("🏛️ *Governadores eleitos*")]  # noqa: E731
        self.tse.set("gov_sc", payload("90,00", cands=[("J", "PL", 700, "70,00", ""), ("X", "PT", 300, "30,00", "")]))
        self.run_tick()  # 1ª rodada: o que já estava definido vira a base, sem 🆕
        self.assertEqual(len(boards()), 1)
        self.assertNotIn("🆕", boards()[0] + gov()[0])
        self.run_tick(now=T1_NIGHT.replace(minute=1))  # nada novo e < 20 min → silêncio
        self.assertEqual(len(boards()), 1)
        self.tse.set("sen_rs", payload("95,00", nv="2", cands=[("R1", "PL", 900, "60,00", ""), ("R2", "NOVO", 500, "33,00", ""),
                                                               ("R3", "PT", 100, "7,00", "")], esnt=50))
        self.run_tick(now=T1_NIGHT.replace(minute=2))  # novidade, mas o Senado mandou às :00 → espera
        self.assertEqual(len(boards()), 1)
        self.run_tick(now=T1_NIGHT.replace(minute=5))  # 5 min depois sai, com o 🆕 que ficou pendente
        self.assertEqual(len(boards()), 2)
        top = boards()[1].split("📊")[0]
        self.assertIn("🆕 *Acabou de ser definido*", top)
        self.assertIn("✅ Senado RS: R1 (PL) 60,00%", top)
        self.assertIn("✅ Senado RS: R2 (NOVO) 33,00%", top)
        self.assertIn("✅ RS: R1 (PL), R2 (NOVO) 🆕", boards()[1])
        self.assertEqual(len(gov()), 1)  # novidade só no Senado → só a mensagem do Senado
        self.run_tick(now=T1_NIGHT.replace(minute=24))  # relógio do Senado recomeçou às :05
        self.assertEqual(len(boards()), 2)
        self.assertEqual(len(gov()), 2)  # governadores seguem no relógio deles (:00 → :20)
        self.run_tick(now=T1_NIGHT.replace(minute=25))
        self.assertEqual(len(boards()), 3)
        self.assertNotIn("🆕", boards()[2])

    def test_all_defined_sends_final_and_stops(self):
        self.state["armed"] = True
        self.state.update(dep_check_at=T1_NIGHT.timestamp())
        # tudo matematicamente definido (sem totalização final): governos eleitos/2º turno, 2 senadores por UF
        self.tse.default = payload("99,00", nv="2", cands=[("S1", "PL", 900, "60,00", ""), ("S2", "PT", 500, "30,00", ""),
                                                           ("S3", "PSD", 100, "10,00", "")])
        self.run_tick()
        finals = [t for t in self.out if t.startswith("🏁 *Consolidado final")]
        self.assertEqual(len(finals), 1)
        self.assertFalse(any(t.startswith("🏛️ *Governadores eleitos*") or t.startswith("🏛️ *Senadores eleitos*")
                             for t in self.out))
        self.assertIn("🏛️ *Senado a partir de 2027 — 81 cadeiras por partido*", finals[0])
        self.assertIn("termina aqui", finals[0])
        self.assertTrue(self.state["eleitos_final"])
        n, calls = len(self.out), len(self.tse.calls)
        self.run_tick(now=T1_NIGHT.replace(minute=30))
        self.assertFalse(any(t.startswith("🏁 *Consolidado") or "eleitos*" in t.splitlines()[0] for t in self.out[n:]))
        self.assertFalse(any("-c0003-" in u or "-c0005-" in u for u in self.tse.calls[calls:]))  # parou de consultar

    def test_only_sc_deputies(self):
        self.assertEqual({r.uf for r in E.DEPUTADOS}, {"sc"})

    def test_sc_deputies_only_at_100_then_stop(self):
        self.state["armed"] = True
        self.state.update(gov_at=T1_NIGHT.timestamp(), sen_at=T1_NIGHT.timestamp(), eleitos_check_at=T1_NIGHT.timestamp())
        deps = lambda: [t for t in self.out if "Deputados" in t.splitlines()[0]]  # noqa: E731
        agrs = [("PL", "i", 600, 0, 3, [("A1", 400, ""), ("A2", 150, ""), ("A3", 50, "")]),
                ("PT/PC do B/PV", "f", 300, 0, 1, [("B1", 280, "")])]
        for k in ("depf_sc", "depe_sc"):
            p = dep_payload("n", nv="4", agrs=agrs)
            p["s"]["pst"] = "99,93"
            self.tse.set(k, p)
        self.run_tick()
        self.assertEqual(deps(), [])  # 99,93% → nada
        for k in ("depf_sc", "depe_sc"):
            self.tse.set(k, dep_payload("n", nv="4", agrs=agrs))  # 100%, TSE sem marcar eleitos → cálculo por legenda
        self.run_tick(now=T1_NIGHT.replace(minute=1))
        self.assertEqual(len(deps()), 1)
        self.assertTrue(deps()[0].startswith("🏁 *Deputados SC eleitos — 100% apurado*"))
        self.assertIn("• A3 (PL) — 50 (média)", deps()[0])
        self.assertEqual(self.state["dep_sent"], {"depf_sc": True, "depe_sc": True})
        calls = len(self.tse.calls)
        self.run_tick(now=T1_NIGHT.replace(minute=30))
        self.assertEqual(len(deps()), 1)  # parou
        self.assertFalse(any("-c0006-" in u or "-c0007-" in u for u in self.tse.calls[calls:]))

    def test_sc_deputies_official_list_when_marked(self):
        self.state["armed"] = True
        self.state.update(gov_at=T1_NIGHT.timestamp(), sen_at=T1_NIGHT.timestamp(), eleitos_check_at=T1_NIGHT.timestamp())
        for r in E.DEPUTADOS:
            self.tse.set(r.key, dep_payload("s", nv="1", agrs=[("PL", "i", 10, 1, 1, [("A", 9, "Eleito por QP")])]))
        self.run_tick()
        self.assertEqual(sum(t.startswith("✅ *Deputados") for t in self.out), 2)  # federal + estadual oficiais

    def test_full_lifecycle(self):
        self.state["armed"] = True
        self.tse.set("pres", payload("100,00", "s", cands=[("A", "PT", 48, "48,00", "2º turno"), ("B", "PL", 45, "45,00", "2º turno")]))
        self.tse.default = payload("100,00", "s", nv="2", cands=[("S1", "PL", 9, "60,00", "Eleito"), ("S2", "PT", 6, "40,00", "Eleito")])
        for r in E.DEPUTADOS:
            self.tse.set(r.key, dep_payload("s", nv="1", agrs=[("PL", "i", 10, 1, 1, [("G", 9, "Eleito por QP")])]))
        self.run_tick()
        self.assertIn("🔁 2º turno", self.out[0])
        self.assertTrue(self.out[1].startswith("🏁 *Consolidado final — governadores e senadores*"))
        self.assertTrue(self.state["eleitos_final"])
        self.assertEqual(sum(t.startswith("✅ *Deputados") for t in self.out), 2)  # SC: federal + estadual
        self.assertIn("2º turno em 25/10 para Presidente", self.out[-1])
        self.assertEqual(self.state["turn"], 2)
        self.assertEqual(self.state["t2"], ["pres"])
        # between rounds: silent, no requests
        calls = len(self.tse.calls)
        self.run_tick(now=datetime(2026, 10, 10, 12, 0, tzinfo=E.BRT))
        self.assertEqual(len(self.tse.calls), calls)
        # 2nd round night: only the presidential files, then shutdown
        self.tse.set("pres", payload("100,00", "s", cands=[("A", "PT", 52, "52,00", "Eleito"), ("B", "PL", 48, "48,00", "Não eleito")]))
        self.run_tick(now=datetime(2026, 10, 25, 19, 0, tzinfo=E.BRT))
        self.assertTrue(all("006258" in u for u in self.tse.calls[calls:]))
        self.assertIn("encerrada", self.out[-1])
        self.assertTrue(self.state["finished"])
        n = len(self.out)
        self.run_tick(now=datetime(2026, 10, 25, 19, 5, tzinfo=E.BRT))
        self.assertEqual(len(self.out), n)

    def test_second_round_projection_in_board(self):
        self.state.update(turn=2, t2=["pres"], armed=False)
        night = datetime(2026, 10, 25, 18, 0, tzinfo=E.BRT)
        # só SC apurado, inteiro: Flávio 70% lá — o bruto dá 70%, a projeção não pode acreditar nisso
        sc = [k for k in ("sc80004", "sc80012", "sc80020") ]
        self.tse.set("ab_sc", {"abr": [{"tpabr": "mun", "cdabr": k[2:], "e": {"te": "100", "est": "100"}} for k in sc]})
        self.tse.set("pres_sc", payload("100,00", cands=[("FLAVIO BOLSONARO", "PL", 7000, "70,00", ""),
                                                         ("LULA", "PT", 3000, "30,00", "")]))
        for c in self.tse.data["pres_sc"]["carg"][0]["agr"][0]["par"]:
            c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
        pres = payload("8,00", cands=[("FLAVIO BOLSONARO", "PL", 7000, "70,00", ""), ("LULA", "PT", 3000, "30,00", "")])
        pres["e"].update(c="8000", est="10000")
        for c in pres["carg"][0]["agr"][0]["par"]:
            c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
        self.tse.set("pres", pres)
        self.tse.set("ab", {"abr": [{"tpabr": "uf", "cdabr": "sc", "s": {"st": "3", "ts": "3"}, "e": {"c": "10000", "te": "10000"}},
                                    {"tpabr": "uf", "cdabr": "pr", "s": {"st": "0", "ts": "3"}, "e": {"c": "0", "te": "1"}},
                                    {"tpabr": "uf", "cdabr": "rs", "s": {"st": "0", "ts": "4"}, "e": {"c": "0", "te": "1"}}]})
        self.run_tick(now=night)
        self.assertIn("projeção do modelo", self.out[0])  # aviso de abertura do 2º turno
        self.assertIn("Ponto de partida", self.out[0])
        board = self.out[1]
        self.assertTrue(board.startswith("🗳️ *2º TURNO · PRESIDENTE*"))
        order = ["*✅ APURADO ATÉ AGORA*", "*📍 POR REGIÃO — APURADO*", "*🔮 PROJEÇÃO DO RESULTADO FINAL*",
                 "*🗺️ PROJEÇÃO POR REGIÃO*"]
        self.assertEqual([board.index(x) for x in order], sorted(board.index(x) for x in order))
        self.assertIn("🔵 Flávio Bolsonaro — *70,00%*", board)  # apurado: só SC entrou
        share = float(board.split("🔵 Flávio — *")[1].split("%")[0].replace(",", "."))
        self.assertLess(share, 60.0)  # projeção nada perto dos 70% do bruto de SC
        self.assertGreater(share, 45.0)
        self.assertIn("Sul        30%   70,0   30,0", board)  # tabela do apurado: 3 de 10 seções do Sul (só SC)
        self.assertEqual(board.count("```"), 4)  # duas tabelas em bloco monoespaçado
        self.assertIn("*📊 Nos lugares já apurados*", board)  # comparativo com 1º turno e 2022 nos mesmos municípios
        self.assertIn("Agora *70,0%* · no 1º turno:", board)
        self.assertIn("🚶 Abstenção: *20,0%* agora", board)  # 8.000 de 10.000 eleitores das seções apuradas
        self.assertLess(board.index("✅ APURADO"), board.index("📊 Nos lugares"))
        self.assertLess(board.index("📊 Nos lugares"), board.index("📍 POR REGIÃO"))
        self.assertTrue(self.state["proj_hist"])

    def test_second_round_chart_follows_the_board(self):
        self.state.update(turn=2, t2=["pres"], armed=True)
        pres = payload("10,00", cands=[("FLAVIO BOLSONARO", "PL", 600, "60,00", ""), ("LULA", "PT", 400, "40,00", "")])
        for c in pres["carg"][0]["agr"][0]["par"]:
            c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
        self.tse.set("pres", pres)
        imgs, order = [], []
        night = datetime(2026, 10, 25, 18, 0, tzinfo=E.BRT)
        with mock.patch.object(E, "render_chart", return_value="/tmp/x.png") as rc:
            E.tick(self.state, lambda t: (order.append("texto"), True)[1], now=night, fetcher=self.tse,
                   send_image=lambda p: (imgs.append(p), order.append("imagem"), True)[2])
        self.assertEqual(imgs, ["/tmp/x.png"])
        self.assertEqual(order, ["texto", "imagem"])  # gráfico logo depois do quadro
        summ, proj, hist, _ = rc.call_args[0]
        self.assertEqual(summ["pst"], "10,00")
        self.assertIn("share_a", proj)
        self.assertEqual(len(hist[-1]), 5)  # [ts, %apurado, %projeção, %sd, %bruto]
        # gráfico falhando não derruba nada
        self.tse.set("pres", payload("11,00", cands=[("FLAVIO BOLSONARO", "PL", 600, "60,00", "")]))
        with mock.patch.object(E, "render_chart", side_effect=RuntimeError("sem Pillow")):
            E.tick(self.state, lambda t: True, now=night.replace(minute=1), fetcher=self.tse, send_image=lambda p: True)
        self.assertEqual(self.state["sent_pst"]["pres"], "11,00")

    def test_exterior_every_30_min_only_when_it_changes(self):
        self.state.update(turn=2, t2=["pres"], armed=True, g1_final_sent=False)
        ext = lambda: [t for t in self.out if t.startswith("🌎 *Exterior")]  # noqa: E731
        morning = datetime(2026, 10, 25, 10, 0, tzinfo=E.BRT)

        def zz(pst, a, b, tf="n"):
            p = payload(pst, tf, cands=[("FLAVIO BOLSONARO", "PL", a, f"{100 * a / (a + b):.2f}".replace(".", ","), ""),
                                        ("LULA", "PT", b, f"{100 * b / (a + b):.2f}".replace(".", ","), "")],
                        v={"tv": str(a + b + 100), "pvb": "1,00", "ptvn": "2,00"}, te=700_000)
            for c in p["carg"][0]["agr"][0]["par"]:
                c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
            p["s"].update(st="100", ts="1351")
            return p

        g1 = ("<h2><strong>OCEANIA E ÁSIA</strong></h2>"
              "<h2><strong>Japão</strong></h2><ul><li><strong>2º turno:</strong> Flávio, 28.538 votos (80,08%)</li>"
              "<li>2º turno: Lula, 7.098 votos (19,92%)</li><li>1º turno: Flávio, 24.601 votos</li>"
              "<li>1º turno: Lula, 5.234 votos</li></ul>"
              "<h2><strong>Catar</strong></h2><ul><li>2º turno: ainda não há dados disponíveis</li>"
              "<li>1º turno: Flávio, 173 votos</li></ul>")
        import eleicao_g1
        with mock.patch.object(eleicao_g1, "fetch", return_value=g1):
            self.run_tick(now=morning)  # antes das 17h: parcial do g1, sem projeção
        self.assertEqual(len(ext()), 1)
        self.assertIn("Já tem resultado em *1 de 2 países*", ext()[0])
        self.assertIn("🔵 *Flávio*: 28.538 votos (80,1%)\n      no 1º turno: 24.601 votos (82,5%)", ext()[0])
        self.assertIn("📈 Do 1º para o 2º turno: Flávio +3.937 votos · Lula +1.864 votos", ext()[0])
        self.assertIn("• Japão: Flávio 28.538 × 7.098 Lula", ext()[0])
        self.assertNotIn("Projeção", ext()[0])
        self.assertFalse(any("/zz/zz-c0001-" in u for u in self.tse.calls))  # ainda não consulta o TSE
        pres = payload("12,00", cands=[("FLAVIO BOLSONARO", "PL", 600, "60,00", ""), ("LULA", "PT", 400, "40,00", "")])
        for c in pres["carg"][0]["agr"][0]["par"]:
            c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
        self.tse.set("pres", pres)  # Brasil acima de 5%: o exterior (TSE) vem com a projeção
        # 17h: uma vez só, a lista completa do g1 + o aviso de que daqui em diante é só a API oficial
        self.tse.set("pres_zz", zz("7,40", 4000, 6000))
        with mock.patch.object(eleicao_g1, "fetch", return_value=g1):
            self.run_tick(now=morning.replace(hour=17, minute=1))
        lista = [t for t in self.out if t.startswith("🌎 *Exterior · lista completa até as 17h*")]
        self.assertEqual(len(lista), 1)
        self.assertIn("• Japão: Flávio 28.538 × 7.098 Lula", lista[0])
        self.assertIn("_Ainda sem resultado no g1 (1): Catar_", lista[0])
        self.assertEqual(self.out.index(E.AVISO_SO_TSE), self.out.index(lista[0]) + 1)  # aviso logo depois da lista
        self.assertTrue(self.state["g1_final_sent"])
        n = len(self.out)
        self.run_tick(now=morning.replace(hour=17, minute=20))  # < 30 min do envio das 17h
        self.assertEqual(len(self.out), n)
        self.run_tick(now=morning.replace(hour=17, minute=31))  # daqui em diante: só a API oficial, com projeção
        self.assertEqual(len(ext()), 3)
        msg = ext()[-1]
        self.assertIn("📊 *7,40% das seções do exterior apuradas* (100 de 1.351)", msg)
        self.assertIn("🟠 Lula — *60,00%* · 6.000 votos", msg)
        self.assertLess(msg.index("Lula"), msg.index("Flávio"))  # quem tem mais votos primeiro
        self.assertIn("🔮 Projeção do exterior no fim da apuração", msg)
        self.assertNotIn("Chance", msg)  # sem previsão de vencedor da eleição
        self.tse.set("pres_zz", zz("9,00", 5000, 7000))
        self.run_tick(now=morning.replace(hour=17, minute=50))  # mudou, mas < 30 min
        self.assertEqual(len(ext()), 3)
        self.run_tick(now=morning.replace(hour=18, minute=2))  # 30 min e mudou → manda
        self.assertEqual(len(ext()), 4)
        self.run_tick(now=morning.replace(hour=18, minute=40))  # 30 min, mas igual → silêncio
        self.assertEqual(len(ext()), 4)
        self.tse.set("pres_zz", zz("100,00", 9000, 9900, tf="s"))
        self.run_tick(now=morning.replace(hour=18, minute=45))
        self.assertEqual(len(ext()), 5)
        self.assertIn("✔️ final", ext()[-1])
        self.assertTrue(self.state["zz_final"])
        check = self.state["zz_check_at"]
        self.run_tick(now=morning.replace(hour=19, minute=30))
        self.assertEqual(len(ext()), 5)  # parou no final
        self.assertEqual(self.state["zz_check_at"], check)  # o fluxo do exterior nem consulta mais

    def test_no_projection_until_5_percent(self):
        self.state.update(turn=2, t2=["pres"], armed=True)
        imgs = []
        night = datetime(2026, 10, 25, 17, 8, tzinfo=E.BRT)
        for minute, pst in ((0, "0,25"), (5, "5,00"), (9, "5,10")):
            pres = payload(pst, cands=[("FLAVIO BOLSONARO", "PL", 420, "42,00", ""), ("LULA", "PT", 580, "58,00", "")])
            for c in pres["carg"][0]["agr"][0]["par"]:
                c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
            self.tse.set("pres", pres)
            with mock.patch.object(E, "render_chart", return_value="/tmp/x.png"):
                self.run_tick_img(night.replace(minute=minute), imgs)
        boards = [t for t in self.out if t.startswith("🗳️ *2º TURNO")]
        self.assertEqual(len(boards), 3)
        for b in boards[:2]:  # 0,25% e exatamente 5%: só o apurado
            self.assertNotIn("PROJEÇÃO", b)
            self.assertIn("A projeção do resultado final aparece a partir de 5%", b)
        self.assertIn("*🔮 PROJEÇÃO DO RESULTADO FINAL*", boards[2])  # 5,10%: projeção
        self.assertEqual(len(imgs), 1)  # gráfico só com projeção

    def run_tick_img(self, now, imgs):
        E.tick(self.state, lambda t: (self.out.append(t), True)[1], now=now, fetcher=self.tse,
               send_image=lambda p: (imgs.append(p), True)[1])

    def test_pause_sends_last_partial_every_30_min_without_projection(self):
        self.state.update(turn=2, t2=["pres"], armed=True)
        dia = datetime(2026, 10, 25, tzinfo=E.BRT)
        pres = payload("0,25", cands=[("FLAVIO BOLSONARO", "PL", 142169, "42,34", ""), ("LULA", "PT", 193574, "57,66", "")])
        for c in pres["carg"][0]["agr"][0]["par"]:
            c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
        self.tse.set("pres", pres)
        imgs = []
        with mock.patch.multiple(E, PAUSA_DESDE=dia.replace(hour=17, minute=30), PAUSA_ATE=dia.replace(hour=19)):
            for h, m in ((17, 33), (17, 50), (18, 3), (18, 30)):
                self.run_tick_img(dia.replace(hour=h, minute=m), imgs)
        boards = [t for t in self.out if "2º TURNO · PRESIDENTE" in t]
        self.assertEqual(len(boards), 2)  # 17:33 e 18:03 (de 30 em 30, mesmo sem mudança); 18:30 < 30 min de 18:03
        self.assertTrue(boards[0].startswith("⚖️ *Decisão judicial: votação prorrogada até as 19h*"))
        self.assertIn("de 30 em 30 minutos", boards[0])
        self.assertTrue(boards[1].startswith("⏸️ *Apuração em pausa*"))
        for b in boards:
            self.assertIn("🔵 Flávio Bolsonaro — *42,34%*", b)
            self.assertNotIn("PROJEÇÃO", b)
        self.assertEqual(imgs, [])  # sem gráfico na pausa
        # 19h: volta o tempo real (com projeção acima de 5%)
        pres = payload("6,00", cands=[("FLAVIO BOLSONARO", "PL", 600, "60,00", ""), ("LULA", "PT", 400, "40,00", "")])
        for c in pres["carg"][0]["agr"][0]["par"]:
            c["cand"][0]["n"] = "22" if c["sg"] == "PL" else "13"
        self.tse.set("pres", pres)
        with mock.patch.multiple(E, PAUSA_DESDE=dia.replace(hour=17, minute=30), PAUSA_ATE=dia.replace(hour=19)):
            with mock.patch.object(E, "render_chart", return_value="/tmp/x.png"):
                self.run_tick_img(dia.replace(hour=19, minute=1), imgs)
        self.assertTrue(self.out[-1].startswith("🗳️ *2º TURNO · PRESIDENTE*"))
        self.assertIn("*🔮 PROJEÇÃO DO RESULTADO FINAL*", self.out[-1])
        self.assertEqual(imgs, ["/tmp/x.png"])

    def test_invites_are_off(self):
        self.assertEqual(E.CONVITE_HORARIOS, [])

    def test_scheduled_invites_with_link_card(self):
        self.state.update(turn=2, t2=["pres"], armed=True, zz_check_at=4e9, convites=[])  # exterior fora deste teste
        horarios = mock.patch.object(E, "CONVITE_HORARIOS", ["15:30", "16:00", "16:15", "16:30", "16:45", "17:00", "17:15", "17:30"])
        horarios.start()
        self.addCleanup(horarios.stop)
        links, textos = [], []
        dia = datetime(2026, 10, 25, tzinfo=E.BRT)

        def tick(h, m):
            E.tick(self.state, lambda t: (textos.append(t), True)[1], now=dia.replace(hour=h, minute=m),
                   fetcher=self.tse, send_link=lambda t: (links.append(t), True)[1])

        tick(15, 31)
        tick(15, 33)  # mesmo horário não repete
        tick(15, 58)  # antes das 16h, nada
        tick(16, 0)
        tick(16, 26)  # 16:15 perdido por > 10 min: pula; 16:30 ainda não
        tick(16, 30)
        tick(17, 2)
        self.assertEqual(self.state["convites"], ["15:30", "16:00", "16:30", "17:00"])
        self.assertEqual(len(links), 4)
        self.assertFalse(any("chat.whatsapp.com" in t for t in textos))  # convite só pelo envio com card
        self.assertIn("ao vivo hoje, a partir das 17h", links[0])
        self.assertIn("2º turno ao vivo agora", links[-1])
        self.assertTrue(all(E.GRUPO_LINK in t for t in links))

    def test_rate_limit_backoff(self):
        self.state["armed"] = True
        throttled = lambda url, etag="": (429, None, "", {"retry-after": "90"})  # noqa: E731
        E.tick(self.state, lambda t: True, now=T1_NIGHT, fetcher=throttled)
        self.assertGreater(self.state["backoff_until"], 0)
        calls = []
        E.tick(self.state, lambda t: True, now=T1_NIGHT, fetcher=lambda u, e="": calls.append(u))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
