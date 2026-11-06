"""Parcial do exterior antes das 17h, compilada pelo g1 a partir dos boletins de urna afixados nos postos de votação.

O TSE só divulga qualquer resultado (inclusive do exterior) depois das 17h de Brasília. Até lá, o g1 publica, país a
país, os votos do 2º turno (e os do 1º turno, para comparação) numa matéria ao vivo. robots.txt do g1 permite
/mundo/noticia/; lemos a página no máximo a cada 5 min. A partir das 17h o bot usa só a API oficial do TSE.

Estrutura da página (out/2026): cada país é um intertítulo <h2> seguido de uma lista
    <li><strong>2º turno:</strong> Lula, 6.017 votos (63,46%)</li> ... <li><strong>1º turno:</strong> Lula, 4.562 votos</li>
Continentes também são <h2>, em caixa alta. "ainda não há dados disponíveis" = país sem resultado ainda.
"""
from __future__ import annotations

import gzip
import html
import re
import urllib.request
from typing import Dict, Optional

URL = ("https://g1.globo.com/mundo/noticia/2026/10/25/"
       "votacao-no-exterior-confira-os-resultados-do-2o-turno-entre-lula-e-flavio-bolsonaro.ghtml")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

_TOKEN = re.compile(r"<h2[^>]*>(.*?)</h2>|<li[^>]*>(.*?)</li>", re.S)
_VOTO = re.compile(r"(Lula|Fl[áa]vio(?:\s+Bolsonaro)?)\s*,\s*([\d.]+)\s*(?:votos?)?", re.I)


def _texto(fragmento: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragmento))).strip()


def parse(page: str) -> Dict[str, dict]:
    """{país: {"continente", "t2": {"22": v, "13": v} | None, "t1": {...}, "repetido_de": país | None}}.

    Só conta como país o intertítulo seguido de linhas "1º/2º turno" (os outros <h2> da página são chamadas de
    outras matérias). Se dois países têm exatamente o mesmo 2º turno (erro de copiar-colar já visto no g1: Síria com
    os números de Singapura, Estônia com os da Finlândia), fica o que é coerente com o próprio 1º turno e o outro é
    marcado `repetido_de` e sai da soma."""
    out: Dict[str, dict] = {}
    com_turno = set()
    continente, pais = "", None
    for h2, li in _TOKEN.findall(page):
        if h2:
            nome = _texto(h2)
            if not nome or nome.lower().startswith("voltar"):
                continue
            if nome == nome.upper():  # OCEANIA E ÁSIA, EUROPA, ...
                continente, pais = nome.title(), None
            else:
                pais = nome
                out.setdefault(pais, {"continente": continente, "t2": None, "t1": None})
            continue
        if not pais:
            continue
        t = _texto(li)
        turno = "t2" if t.startswith("2º turno") else "t1" if t.startswith("1º turno") else None
        if not turno:
            continue
        com_turno.add(pais)
        m = _VOTO.search(t)
        if not m:
            continue  # "ainda não há dados disponíveis"
        cand = "13" if m.group(1).lower().startswith("lula") else "22"
        votos = int(m.group(2).replace(".", ""))
        d = out[pais][turno] or {}
        d[cand] = votos
        out[pais][turno] = d
    out = {p: d for p, d in out.items() if p in com_turno}
    for d in out.values():
        d["repetido_de"] = None
    vistos: Dict[tuple, list] = {}
    for p, d in out.items():
        if d["t2"] and sum(d["t2"].values()) >= 30:
            vistos.setdefault((d["t2"].get("22", 0), d["t2"].get("13", 0)), []).append(p)

    def coerencia(p: str) -> float:  # |log(votos 2º turno / votos 1º turno)|: quanto menor, mais plausível
        import math
        t1 = sum((out[p]["t1"] or {}).values()) or 1
        return abs(math.log(sum(out[p]["t2"].values()) / t1))

    for iguais in vistos.values():
        if len(iguais) > 1:
            certo = min(iguais, key=coerencia)
            for p in iguais:
                if p != certo:
                    out[p]["repetido_de"] = certo
    return out


def fetch(url: str = URL, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip",
                                               "Accept": "text/html,application/xhtml+xml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def resumo(paises: Dict[str, dict]) -> Optional[dict]:
    """Totais dos países que já têm 2º turno + o 1º turno nesses mesmos países (para comparar)."""
    com = {p: d for p, d in paises.items() if d["t2"] and sum(d["t2"].values()) > 0 and not d.get("repetido_de")}
    if not com:
        return None
    a = sum(d["t2"].get("22", 0) for d in com.values())
    b = sum(d["t2"].get("13", 0) for d in com.values())
    a1 = sum((d["t1"] or {}).get("22", 0) for d in com.values())
    b1 = sum((d["t1"] or {}).get("13", 0) for d in com.values())
    maiores = sorted(com.items(), key=lambda kv: -sum(kv[1]["t2"].values()))[:5]
    return {"paises_com": len(com), "paises": len(paises), "a": a, "b": b, "a1": a1, "b1": b1,
            "repetidos": {p: d["repetido_de"] for p, d in paises.items() if d.get("repetido_de")},
            "maiores": [(p, d["t2"].get("22", 0), d["t2"].get("13", 0)) for p, d in maiores]}
