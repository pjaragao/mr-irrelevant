"""Gráfico da apuração do 2º turno (PNG para o WhatsApp), desenhado com Pillow — o mesmo motor/tokens de charts.py.

Uma imagem, de cima para baixo:
  1. a chance de vitória (número grande) — a pergunta que todo mundo faz;
  2. duas barras divididas Flávio × Lula: "apurado até agora" e "projeção do resultado final";
     logo abaixo, um zoom em volta dos 50% mostra onde a divisória pode cair (faixa de 90% + ponto estimado): do lado
     esquerdo dos 50% vence quem está à direita na barra (Lula), do direito vence Flávio — a margem fica visível;
  3. a noite até aqui: % de Flávio no apurado (tracejado) e na projeção (linha + faixa) conforme as urnas entram,
     com a linha do empate — mostra o viés da ordem de chegada se desfazendo.

Regras do dataviz skill: cores categóricas validadas (slot 1 azul = Flávio, slot 2 laranja = Lula), texto sempre na
tinta de texto (nunca na cor da série), legenda + rótulos diretos, um único eixo, marcas finas, tema escuro do bot.
Layout em unidades lógicas de celular (540 de largura), renderizado a 4× e reduzido a 2× (1080 px) para nitidez.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import List, Optional, Sequence, Tuple

import charts

TH = charts.THEMES["dark"]
COR_A, COR_B = TH["series"][0], TH["series"][1]   # azul (Flávio), laranja (Lula) — validados para o tema escuro
W, PAD = 540, 24
SS, OUT = 4, 2  # desenha a 4×, entrega a 2×


def _rgb(h: str, a: int = 255) -> Tuple[int, int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a


def _br(x: float, nd: int = 1) -> str:
    return f"{x:.{nd}f}".replace(".", ",")


class _Canvas:
    def __init__(self, height: int):
        from PIL import Image, ImageDraw
        self.Image = Image
        self.img = Image.new("RGB", (W * SS, height * SS), _rgb(TH["surface"])[:3])  # RGB: o desenho com alfa mistura
        self.d = ImageDraw.Draw(self.img, "RGBA")

    def s(self, v: float) -> int:
        return int(round(v * SS))

    def text(self, xy, s: str, size: float, color: str, bold=False, anchor="la"):
        self.d.text((self.s(xy[0]), self.s(xy[1])), s, font=charts.font(size * SS, bold), fill=_rgb(color), anchor=anchor)

    def width(self, s: str, size: float, bold=False) -> float:
        return charts.font(size * SS, bold).getlength(s) / SS

    def rect(self, x0, y0, x1, y1, fill, r=0.0, alpha=255):
        box = [self.s(x0), self.s(y0), self.s(x1), self.s(y1)]
        if r:
            self.d.rounded_rectangle(box, radius=self.s(r), fill=_rgb(fill, alpha))
        else:
            self.d.rectangle(box, fill=_rgb(fill, alpha))

    def line(self, pts: Sequence[Tuple[float, float]], color: str, w: float, alpha=255, dash: float = 0.0):
        pts = [(self.s(x), self.s(y)) for x, y in pts]
        if not dash:
            self.d.line(pts, fill=_rgb(color, alpha), width=self.s(w), joint="curve")
            return
        on, step = self.s(dash), self.s(dash * 1.8)
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            L = max(((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5, 1)
            t = 0.0
            while t < L:
                t1 = min(t + on, L)
                self.d.line([(x0 + (x1 - x0) * t / L, y0 + (y1 - y0) * t / L),
                             (x0 + (x1 - x0) * t1 / L, y0 + (y1 - y0) * t1 / L)], fill=_rgb(color, alpha), width=self.s(w))
                t += step

    def poly(self, pts, color, alpha):
        self.d.polygon([(self.s(x), self.s(y)) for x, y in pts], fill=_rgb(color, alpha))

    def dot(self, x, y, r, color, ring=2.0):
        self.d.ellipse([self.s(x - r - ring), self.s(y - r - ring), self.s(x + r + ring), self.s(y + r + ring)],
                       fill=_rgb(TH["surface"]))
        self.d.ellipse([self.s(x - r), self.s(y - r), self.s(x + r), self.s(y + r)], fill=_rgb(color))

    def hatch(self, x0, y0, x1, y1, color, alpha=150, gap=5.0):
        """Textura a 45° (faixa de incerteza) recortada no retângulo."""
        layer = self.Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        from PIL import ImageDraw
        dl = ImageDraw.Draw(layer)
        X0, Y0, X1, Y1 = self.s(x0), self.s(y0), self.s(x1), self.s(y1)
        g = self.s(gap)
        for k in range(X0 - (Y1 - Y0), X1, g):
            dl.line([(k, Y1), (k + (Y1 - Y0), Y0)], fill=_rgb(color, alpha), width=self.s(1.4))
        mask = self.Image.new("L", self.img.size, 0)
        ImageDraw.Draw(mask).rectangle([X0, Y0, X1, Y1], fill=255)
        self.img.paste(layer, (0, 0), self.Image.composite(layer, self.Image.new("RGBA", self.img.size), mask))

    def save(self, path: str, height: Optional[float] = None) -> str:
        img = self.img.crop((0, 0, self.img.size[0], self.s(height))) if height else self.img
        out = img.resize((W * OUT, img.size[1] * OUT // SS), self.Image.LANCZOS).convert("RGB")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        out.save(path, "PNG", optimize=True)
        return path


def _split_bar(c: _Canvas, y: float, share_a: float, h: float = 24.0, x0: float = PAD, x1: float = W - PAD):
    """Barra 100% dividida: A à esquerda, B à direita, 2px de respiro na divisória, pontas arredondadas (4px)."""
    xs = x0 + (x1 - x0) * share_a
    if xs - x0 > 1:
        c.rect(x0, y, max(xs - 1, x0 + 4.5), y + h, COR_A, r=4)
        c.rect(max(xs - 5, x0), y, xs - 1, y + h, COR_A)
    if x1 - xs > 1:
        c.rect(min(xs + 1, x1 - 4.5), y, x1, y + h, COR_B, r=4)
        c.rect(xs + 1, y, min(xs + 5, x1), y + h, COR_B)
    return xs


def _mark_50(c: _Canvas, y0: float, y1: float, x0: float = PAD, x1: float = W - PAD):
    xm = (x0 + x1) / 2
    c.line([(xm, y0), (xm, y1)], TH["text1"], 1.2, alpha=200)
    return xm


def render(path: str, now: datetime, apurado_pct: float, obs_share_a: Optional[float], proj: Optional[dict],
           hist: Optional[List[list]] = None, names: Tuple[str, str] = ("Flávio", "Lula"), exemplo: bool = False) -> str:
    """
    apurado_pct: % das urnas apuradas (TSE); obs_share_a: parcela de A no apurado (0–1) ou None;
    proj: {"share_a","lo","hi","prob_a"} (0–1) ou None; hist: [[ts, %apurado, %proj, %sd, %bruto], ...].
    """
    a, b = names
    hist = [h for h in (hist or []) if len(h) >= 5 and h[4] is not None]
    H = 1100  # folga; a imagem é cortada no fim do conteúdo
    c = _Canvas(H)
    y = PAD
    if exemplo:  # simulação didática: faixa no topo + marca d'água, para não circular como resultado real
        c.rect(0, 0, W, 34, TH["text1"])
        c.text((W / 2, 9), "EXEMPLO · SIMULAÇÃO · NÃO É RESULTADO REAL", 13, TH["surface"], bold=True, anchor="ma")
        y += 34

    # cabeçalho
    c.text((PAD, y), "2º turno · Presidente", 20, TH["text1"], bold=True)
    c.text((W - PAD, y + 4), f"{now:%H:%M}", 14, TH["text2"], anchor="ra")
    y += 30
    c.text((PAD, y), f"{_br(apurado_pct, 2)}% das urnas apuradas", 14, TH["text2"])
    y += 34

    # chance de vitória (número grande)
    if proj:
        pa = proj["prob_a"]
        lead, p_lead, cor = (a, pa, COR_A) if pa >= 0.5 else (b, 1 - pa, COR_B)
        big = ">99,5%" if p_lead >= 0.995 else f"{_br(100 * p_lead, 0)}%"
        c.rect(PAD, y + 8, PAD + 6, y + 50, cor, r=3)
        c.text((PAD + 16, y), big, 44, TH["text1"], bold=True)
        wbig = c.width(big, 44, True)
        c.text((PAD + 26 + wbig, y + 8), f"chance de vitória de {lead}", 15, TH["text1"], bold=True)
        c.text((PAD + 26 + wbig, y + 30), "segundo a projeção do modelo", 12, TH["muted"])
        y += 70

    # legenda (identidade nunca só pela cor: nome + amostra)
    lx = PAD
    for nome, cor in ((a, COR_A), (b, COR_B)):
        c.rect(lx, y + 3, lx + 12, y + 15, cor, r=2)
        c.text((lx + 18, y), nome, 13, TH["text2"])
        lx += 18 + c.width(nome, 13) + 22
    y += 30

    # barra 1: apurado até agora
    c.text((PAD, y), "Apurado até agora", 14, TH["text1"], bold=True)
    y += 24
    if obs_share_a is not None:
        _split_bar(c, y, obs_share_a)
        _mark_50(c, y - 5, y + 29)
        c.text((PAD, y + 32), f"{a} {_br(100 * obs_share_a, 2)}%", 13, TH["text1"])
        c.text((W - PAD, y + 32), f"{b} {_br(100 * (1 - obs_share_a), 2)}%", 13, TH["text1"], anchor="ra")
    else:
        c.rect(PAD, y, W - PAD, y + 24, TH["grid"], r=4)
        c.text((W / 2, y + 5), "aguardando os primeiros boletins", 12, TH["muted"], anchor="ma")
    y += 62

    # barra 2: projeção + zoom da margem (onde cai a divisória)
    if proj:
        c.text((PAD, y), "Projeção do resultado final", 14, TH["text1"], bold=True)
        y += 24
        _split_bar(c, y, proj["share_a"])
        _mark_50(c, y - 5, y + 29)
        c.text((PAD, y + 32), f"{a} {_br(100 * proj['share_a'])}%", 15, TH["text1"], bold=True)
        c.text((W - PAD, y + 32), f"{b} {_br(100 * (1 - proj['share_a']))}%", 15, TH["text1"], bold=True, anchor="ra")
        y += 62
        # zoom: eixo = % de A (posição da divisória), centrado em 50
        lo, hi, est = 100 * proj["lo"], 100 * proj["hi"], 100 * proj["share_a"]
        w = min(15.0, max(3.0, float(int(max(abs(lo - 50), abs(hi - 50), abs(est - 50)) + 1.5))))
        zx0, zx1 = PAD, W - PAD

        def Z(v):
            return zx0 + (zx1 - zx0) * (min(max(v, 50 - w), 50 + w) - (50 - w)) / (2 * w)

        c.text((PAD, y), "Margem: onde a divisória pode cair", 12.5, TH["text1"], bold=True)
        c.text((W - PAD, y + 1), f"% de {a} · faixa de 90%", 11.5, TH["muted"], anchor="ra")
        y += 22
        zy0, zy1 = y, y + 34
        c.rect(zx0, zy0, Z(50) - 1, zy1, COR_B, r=4, alpha=60)   # divisória à esquerda dos 50%: B na frente
        c.rect(Z(50) + 1, zy0, zx1, zy1, COR_A, r=4, alpha=60)   # à direita: A na frente
        c.rect(Z(lo), zy0 + 6, Z(hi), zy1 - 6, TH["text1"], r=3, alpha=95)
        c.line([(Z(lo), zy0 + 3), (Z(lo), zy1 - 3)], TH["text1"], 1.4)
        c.line([(Z(hi), zy0 + 3), (Z(hi), zy1 - 3)], TH["text1"], 1.4)
        c.line([(Z(50), zy0 - 4), (Z(50), zy1 + 4)], TH["text1"], 1.2, alpha=200)
        c.dot(Z(est), (zy0 + zy1) / 2, 5, TH["text1"], ring=2.5)
        # rótulos das zonas por último, numa pílula escura: legíveis mesmo com a faixa por baixo
        for txt, x, anchor in ((f"← {b} vence", zx0 + 8, "la"), (f"{a} vence →", zx1 - 8, "ra")):
            tw = c.width(txt, 12)
            px0 = x - 5 if anchor == "la" else x - tw - 5
            c.rect(px0, zy0 + 7, px0 + tw + 10, zy1 - 7, TH["surface"], r=4, alpha=200)
            c.text((x, zy0 + 9), txt, 12, TH["text1"], anchor=anchor)
        for v in (50 - w, 50, 50 + w):
            c.text((Z(v), zy1 + 5), f"{_br(v, 0)}%", 10.5, TH["muted"], anchor="ma")
        y = zy1 + 22
        cruza = proj["lo"] < 0.5 < proj["hi"]
        msg = (f"a faixa ainda cruza os 50%: disputa aberta ({a} entre {_br(lo)}% e {_br(hi)}%)" if cruza else
               f"a faixa inteira está do lado de {a if est > 50 else b}: {a} entre {_br(lo)}% e {_br(hi)}%")
        c.text((W / 2, y), msg, 11.5, TH["text2"], anchor="ma")
        y += 30

    # a noite até aqui
    if len(hist) >= 2:
        c.text((PAD, y), f"A noite até aqui · % de {a}", 14, TH["text1"], bold=True)
        c.text((PAD, y + 20), f"acima da linha do empate, {a} na frente; abaixo, {b}", 11.5, TH["muted"])
        y += 40
        ph = 150
        gx0, gx1, gy0, gy1 = PAD + 34, W - PAD - 8, y + 8, y + 8 + ph
        xs = [h[1] for h in hist]
        vals = [v for h in hist for v in (h[2] - 1.645 * h[3], h[2] + 1.645 * h[3], h[4])] + [50.0]
        pad_v = max(0.6, 0.08 * (max(vals) - min(vals)))
        lo_v, hi_v = min(min(vals) - pad_v, 48.5), max(max(vals) + pad_v, 51.5)  # sempre há espaço dos dois lados do empate
        xmax = next((v for v in (5, 10, 20, 30, 50, 75, 100) if v >= max(xs) * 1.15), 100)

        def X(v):
            return gx0 + (gx1 - gx0) * v / xmax

        def Y(v):
            return gy1 - (gy1 - gy0) * (v - lo_v) / (hi_v - lo_v)

        for t in _nice_ticks(lo_v, hi_v):
            c.line([(gx0, Y(t)), (gx1, Y(t))], TH["grid"], 1)
            c.text((gx0 - 6, Y(t) - 7), f"{_br(t, 0 if t == int(t) else 1)}", 10.5, TH["muted"], anchor="ra")
        xstep = next(v for v in (1, 2, 5, 10, 25) if xmax / v <= 5)
        for t in range(0, int(xmax) + 1, xstep):
            c.text((X(t), gy1 + 6), f"{t}%", 10.5, TH["muted"], anchor="ma")
        c.text((gx1, gy1 + 20), "% apurado", 10.5, TH["muted"], anchor="ra")
        # empate (linha cheia, fina — o tracejado é do apurado bruto)
        c.line([(gx0, Y(50)), (gx1, Y(50))], TH["text2"], 1.0, alpha=170)
        c.text((gx1 - 2, Y(50) - 15), "empate", 10.5, TH["text2"], anchor="ra")
        # faixa da projeção + linhas
        band = [(X(h[1]), Y(h[2] + 1.645 * h[3])) for h in hist] + \
               [(X(h[1]), Y(h[2] - 1.645 * h[3])) for h in reversed(hist)]
        c.poly(band, COR_A, 42)
        c.line([(X(h[1]), Y(h[4])) for h in hist], TH["text2"], 2, alpha=230, dash=5)
        c.line([(X(h[1]), Y(h[2])) for h in hist], COR_A, 2.4)
        last = hist[-1]
        c.dot(X(last[1]), Y(last[2]), 4, COR_A)
        y = gy1 + 34
        # legenda das linhas
        lx = PAD
        c.rect(lx, y + 5, lx + 18, y + 8, COR_A)
        c.text((lx + 24, y), "projeção (faixa de 90% sombreada)", 11.5, TH["text2"])
        lx += 24 + c.width("projeção (faixa de 90% sombreada)", 11.5) + 18
        c.line([(lx, y + 7), (lx + 18, y + 7)], TH["text2"], 2, dash=5)
        c.text((lx + 24, y), "apurado (bruto)", 11.5, TH["text2"])
        y += 26

    # rodapé (a imagem é cortada logo abaixo dele)
    y += 8
    c.text((PAD, y), "Projeção estatística (1º turno por município + desvio medido em cada UF).", 10.5, TH["muted"])
    c.text((PAD, y + 15), "Não é resultado oficial · Fonte: TSE", 10.5, TH["muted"])
    h_final = min(H, y + 15 + 26)
    if exemplo:
        c.rect(0, h_final - 6, W, h_final, TH["text1"])
        _watermark(c, "EXEMPLO", h_final)
    return c.save(path, height=h_final)


def _watermark(c: _Canvas, txt: str, height: float):
    from PIL import ImageDraw
    big = charts.font(110 * SS, True)
    tw = int(big.getlength(txt)) + 40
    layer = c.Image.new("RGBA", (tw, int(150 * SS)), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((20, 0), txt, font=big, fill=_rgb(TH["text1"], 34))
    layer = layer.rotate(28, expand=True, resample=c.Image.BICUBIC)
    for cy in (0.33, 0.72):
        x = (c.img.size[0] - layer.size[0]) // 2
        y = int(c.s(height * cy)) - layer.size[1] // 2
        c.img.paste(layer, (x, y), layer)


def _nice_ticks(lo: float, hi: float) -> List[float]:
    span = hi - lo
    step = next(s for s in (0.5, 1, 2, 2.5, 5, 10, 20) if span / s <= 6)
    t = (int(lo / step) + 1) * step
    out = []
    while t < hi:
        out.append(round(t, 2))
        t += step
    return out
