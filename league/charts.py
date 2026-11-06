"""Chart engine: one JSON-able spec -> a scene of primitives -> interactive HTML (inline SVG) + PNG.

Why a scene instead of a browser screenshot: the bot's container has Pillow but no headless browser (and a 1 GB
limit), so the same layout code emits both the SVG that goes inside the HTML page and the pixels of the PNG that is
sent to WhatsApp. Both outputs are therefore identical by construction, offline, in ~100 ms.

Spec (see `compose`):
    {"title", "subtitle", "footer",
     "blocks": [{"type": "tiles",   "items": [{"value", "label", "sub"}]},
                {"type": "heatmap", "title", "subtitle", "rows", "cols", "values", "row_totals", "row_notes", "note", "unit"},
                {"type": "lines",   "title", "subtitle", "x", "series": [{"name", "values", "highlight"}], "unit"},
                {"type": "bars",    "title", "subtitle", "items": [{"label", "value", "note", "muted"}],
                                    "orientation": "v"|"h", "unit", "fmt": "int"|"float"|"pct"}]}

Design rules come from the dataviz skill: validated categorical order, single-hue sequential ramp, thin marks,
2px surface gaps, direct labels with leader lines, text in ink tokens (never in the series colour), a table view in
the HTML, light + dark themes selected (not flipped).
"""

from __future__ import annotations

import html as _html
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

W = 1080          # canvas width (px); height grows with the blocks
M = 56            # outer margin
SS = 2            # PNG supersampling factor

THEMES: Dict[str, Dict[str, Any]] = {
    "dark": {
        "surface": "#1a1a19", "page": "#0d0d0d", "card": "#242423", "text1": "#ffffff", "text2": "#c3c2b7",
        "muted": "#9a988f", "grid": "#2c2c2a", "axis": "#4a4a46", "cell0": "#232322", "hatch": "#2f2f2d",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
        # sequential blue, near-surface -> bright (step 700 -> 200)
        "ramp": ["#0d366b", "#104281", "#184f95", "#1c5cab", "#256abf", "#2a78d6", "#3987e5", "#5598e7", "#6da7ec", "#9ec5f4"],
    },
    "light": {
        "surface": "#fcfcfb", "page": "#f9f9f7", "card": "#f1f0ec", "text1": "#0b0b0b", "text2": "#52514e",
        "muted": "#6e6c66", "grid": "#e1e0d9", "axis": "#c3c2b7", "cell0": "#f0efec", "hatch": "#e1e0d9",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
        # sequential blue, near-surface -> dark (step 100 -> 700)
        "ramp": ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#1c5cab", "#0d366b"],
    },
}

_FONT_FILES = {
    False: ["/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/freefont/FreeSans.ttf"],
    True: ["/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"],
}
_font_cache: Dict[Tuple[bool, int], Any] = {}


# --------------------------------------------------------------------------- colours
def _hex_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_hex(c: Sequence[float]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in c)


def _lum(h: str) -> float:
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in _hex_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ramp_color(theme: Dict[str, Any], t: float) -> str:
    stops = theme["ramp"]
    t = max(0.0, min(1.0, t)) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    f = t - i
    a, b = _hex_rgb(stops[i]), _hex_rgb(stops[i + 1])
    return _rgb_hex([a[k] + (b[k] - a[k]) * f for k in range(3)])


def resolve(color: Any, theme: Dict[str, Any]) -> str:
    """'$token' -> theme colour; ('ramp', t) -> sequential step; ('on', t) -> ink that clears contrast on that step."""
    if isinstance(color, tuple):
        if color[0] == "ramp":
            return ramp_color(theme, color[1])
        if color[0] == "on":
            bg = ramp_color(theme, color[1])
            return "#0b0b0b" if _lum(bg) > 0.30 else "#ffffff"
        if color[0] == "series":
            return theme["series"][color[1] % len(theme["series"])]
        if color[0] == "on_series":  # ink that clears contrast on a series colour
            bg = theme["series"][color[1] % len(theme["series"])]
            return "#0b0b0b" if _lum(bg) > 0.30 else "#ffffff"
    if isinstance(color, str) and color.startswith("$"):
        return theme[color[1:]]
    return color


# --------------------------------------------------------------------------- fonts / text metrics
def font(size: float, bold: bool = False):
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    key = (bold, int(round(size)))
    if key not in _font_cache:
        f = None
        for p in _FONT_FILES[bold]:
            if os.path.isfile(p):
                try:
                    f = ImageFont.truetype(p, int(round(size)))
                    break
                except OSError:
                    continue
        _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]


def text_width(s: str, size: float, bold: bool = False) -> float:
    f = font(size * 4, bold)
    if f is not None and hasattr(f, "getlength"):
        return f.getlength(s) / 4.0
    return len(s) * size * (0.58 if bold else 0.54)


def fit_size(s: str, size: float, max_w: float, bold: bool = False, min_size: float = 15) -> float:
    """Largest font size <= `size` at which `s` fits `max_w` (labels must never overflow their mark)."""
    while size > min_size and text_width(s, size, bold) > max_w:
        size -= 1
    return size


# --------------------------------------------------------------------------- scene
class Scene:
    def __init__(self, width: int = W):
        self.width, self.height = width, 0
        self.items: List[Dict[str, Any]] = []

    def rect(self, x, y, w, h, fill, rx=0, opacity=1.0, tip=None, stroke=None, sw=0):
        self.items.append(dict(k="rect", x=x, y=y, w=w, h=h, fill=fill, rx=rx, op=opacity, tip=tip, stroke=stroke, sw=sw))

    def line(self, x1, y1, x2, y2, stroke, sw=1.0, opacity=1.0, dash=None):
        self.items.append(dict(k="line", x1=x1, y1=y1, x2=x2, y2=y2, stroke=stroke, sw=sw, op=opacity, dash=dash))

    def poly(self, pts, stroke, sw=4.0, opacity=1.0):
        self.items.append(dict(k="poly", pts=list(pts), stroke=stroke, sw=sw, op=opacity))

    def circle(self, cx, cy, r, fill, ring=None, ring_w=0, tip=None):
        self.items.append(dict(k="circle", cx=cx, cy=cy, r=r, fill=fill, ring=ring, rw=ring_w, tip=tip))

    def arc(self, cx, cy, r_out, r_in, a0, a1, fill, tip=None):
        """Ring slice; angles in degrees, clockwise from 12 o'clock."""
        self.items.append(dict(k="arc", cx=cx, cy=cy, ro=r_out, ri=r_in, a0=a0, a1=a1, fill=fill, tip=tip))

    def text(self, x, y, s, size, fill, bold=False, anchor="start", halo=None):
        self.items.append(dict(k="text", x=x, y=y, s=str(s), size=size, fill=fill, bold=bold, anchor=anchor, halo=halo))

    def hatch(self, x, y, w, h, stroke, gap=11, sw=2):
        """45° hatch clipped to the rect (emitted as plain lines so both renderers agree)."""
        d = -h
        while d < w:
            x1, y1, x2, y2 = x + d, y + h, x + d + h, y
            if x1 < x:
                y1 -= x - x1; x1 = x
            if x2 > x + w:
                y2 += x2 - (x + w); x2 = x + w
            if x1 < x2:
                self.line(x1, y1, x2, y2, stroke, sw)
            d += gap


# --------------------------------------------------------------------------- number formatting
def fmt_value(v: Optional[float], fmt: str = "int") -> str:
    if v is None:
        return "–"
    if fmt == "pct":
        return f"{v:.1f}%"
    if fmt == "float":
        return f"{v:.1f}"
    return f"{int(round(v)):,}".replace(",", ".")


def fmt_tick(v: float, fmt: str = "int") -> str:
    if fmt == "pct":
        return f"{v:.0f}%"
    return fmt_value(v, "int" if float(v).is_integer() or fmt == "int" else "float")


def nice_ticks(vmax: float, n: int = 4) -> List[float]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    ticks, v = [], 0.0
    while v <= vmax + step * 0.999:
        ticks.append(v)
        v += step
    return ticks


# --------------------------------------------------------------------------- blocks
def _section_title(sc: Scene, y: float, title: str, subtitle: str = "") -> float:
    sc.text(M, y + 34, title, fit_size(title, 36, W - 2 * M, True, 22), "$text1", bold=True)
    y += 34
    if subtitle:
        sc.text(M, y + 34, subtitle, fit_size(subtitle, 23, W - 2 * M, False, 16), "$text2")
        y += 34
    return y + 26


def block_header(sc: Scene, y: float, title: str, subtitle: str) -> float:
    sc.text(M, y + 62, title, fit_size(title, 58, W - 2 * M, True, 34), "$text1", bold=True)
    sc.text(M, y + 104, subtitle, fit_size(subtitle, 27, W - 2 * M, False, 18), "$text2")
    return y + 104 + 40


def block_tiles(sc: Scene, y: float, items: List[Dict[str, Any]]) -> float:
    n = max(1, len(items))
    gap = 18
    w = (W - 2 * M - gap * (n - 1)) / n
    h = 176
    for i, it in enumerate(items):
        x = M + i * (w + gap)
        sc.rect(x, y, w, h, "$card", rx=22, tip=f"{it.get('label', '')}: {it.get('value', '')} {it.get('sub', '')}".strip())
        sc.rect(x + 22, y + 26, 6, 52, ("series", 0), rx=3)
        inner = w - 44
        val = str(it.get("value", ""))
        sc.text(x + 44, y + 76, val, fit_size(val, 56, inner - 22, True, 30), "$text1", bold=True)
        lab, sub = str(it.get("label", "")), str(it.get("sub", ""))
        sc.text(x + 22, y + 118, lab, fit_size(lab, 23, inner, True), "$text1", bold=True)
        sc.text(x + 22, y + 148, sub, fit_size(sub, 20, inner), "$muted")
    return y + h + 44


def block_heatmap(sc: Scene, y: float, b: Dict[str, Any]) -> float:
    rows, cols, values = b["rows"], b["cols"], b["values"]
    unit = b.get("unit", "")
    y = _section_title(sc, y, b.get("title", ""), b.get("subtitle", ""))
    fmt = b.get("fmt", "int")
    label_w = int(min(250, max(100, max(text_width(str(r), 28, True) for r in rows) + 18)))
    total_w = (112 if fmt != "int" else 92) if b.get("row_totals") else 0
    gap = 4
    n = len(cols)
    cell_w = (W - 2 * M - label_w - total_w - gap * (n - 1) - (10 if total_w else 0)) / n
    cell_h = 66
    x0 = M + label_w
    flat = [v for r in values for v in r if v is not None]
    vmax = max(flat) if flat else 1
    widest = max((fmt_value(v, fmt) for v in flat), key=len, default="0")
    cell_font = fit_size(widest, 30, cell_w - 8, True, 16)  # one size for every cell: numbers must be comparable
    # column headers
    for j, c in enumerate(cols):
        sc.text(x0 + j * (cell_w + gap) + cell_w / 2, y + 22, c, 21, "$muted", anchor="middle")
    if total_w:
        sc.text(W - M, y + 22, "Total", 21, "$muted", anchor="end")
    y += 36
    for i, r in enumerate(rows):
        ry = y + i * (cell_h + gap)
        note = (b.get("row_notes") or {}).get(r)
        sc.text(M, ry + (cell_h / 2 + (2 if note else 10)), str(r), fit_size(str(r), 30, label_w - 12, True, 15), "$text1", bold=True)
        if note:
            sc.text(M, ry + cell_h / 2 + 24, note, 17, "$muted")
        for j, v in enumerate(values[i]):
            cx = x0 + j * (cell_w + gap)
            if v is None:  # week not played yet
                sc.rect(cx, ry, cell_w, cell_h, "$cell0", rx=9)
                sc.hatch(cx + 1, ry + 1, cell_w - 2, cell_h - 2, "$hatch", gap=12, sw=2)
                continue
            tip = f"{r} · {cols[j]}: {fmt_value(v, fmt)} {unit}".strip()
            if v == 0:
                sc.rect(cx, ry, cell_w, cell_h, "$cell0", rx=9, tip=tip)
                sc.text(cx + cell_w / 2, ry + cell_h / 2 + cell_font * 0.34, "0", cell_font * 0.9, "$muted", anchor="middle")
            else:
                t = 0.12 + 0.88 * (v / vmax) ** 0.6
                sc.rect(cx, ry, cell_w, cell_h, ("ramp", t), rx=9, tip=tip)
                txt = fmt_value(v, fmt)
                sc.text(cx + cell_w / 2, ry + cell_h / 2 + cell_font * 0.36, txt, cell_font, ("on", t),
                        bold=True, anchor="middle")
        if total_w:
            tot = sum(v for v in values[i] if v is not None)
            tot_txt = fmt_value(tot, fmt)
            sc.text(W - M, ry + cell_h / 2 + 11, tot_txt, fit_size(tot_txt, 30, total_w - 6, True, 16), "$text1",
                    bold=True, anchor="end")
    y += len(rows) * (cell_h + gap)
    if b.get("note"):
        sc.text(M, y + 30, b["note"], fit_size(b["note"], 20, W - 2 * M - 270, False, 14), "$muted")
        y += 30
    # colour legend: low -> high
    steps, sw_ = 10, 22
    mais_w = text_width("mais", 18)
    lx, ly = W - M - mais_w - 12 - steps * sw_, y + 14
    sc.text(lx - 12, ly + 13, "menos", 18, "$muted", anchor="end")
    for k in range(steps):
        sc.rect(lx + k * sw_, ly, sw_, 14, ("ramp", 0.12 + 0.88 * (k / (steps - 1))), rx=0)
    sc.text(lx + steps * sw_ + 12, ly + 13, "mais", 18, "$muted")
    return y + 64


def _end_labels(sc: Scene, ends: List[Dict[str, Any]], x_label: float, min_gap: float) -> None:
    """Right-side value labels with leader lines; vertical nudging is bounded and visible through the leader."""
    ends = sorted(ends, key=lambda e: e["y"])
    pos = []
    for e in ends:
        ly = e["y"]
        if pos and ly - pos[-1] < min_gap:
            ly = pos[-1] + min_gap
        pos.append(ly)
    for e, ly in zip(ends, pos):
        sc.line(e["x"] + 9, e["y"], x_label - 8, ly, "$axis", 1.5)
        sc.text(x_label, ly + 9, e["label"], 26, "$text1", bold=True, halo="$surface")


def block_lines(sc: Scene, y: float, b: Dict[str, Any]) -> float:
    xs, series = b["x"], b["series"]
    unit, fmt = b.get("unit", ""), b.get("fmt", "int")
    y = _section_title(sc, y, b.get("title", ""), b.get("subtitle", ""))
    if len(series) > 1:  # legend (dot + ink text; identity never by colour alone); a lone series is named by the title
        lx = M
        for i, s in enumerate(series):
            sc.circle(lx + 8, y + 12, 8, ("series", i))
            sc.text(lx + 24, y + 20, s["name"], 24, "$text1" if s.get("highlight") else "$text2", bold=bool(s.get("highlight")))
            lx += 24 + text_width(s["name"], 24, bool(s.get("highlight"))) + 34
        y += 52
    left, right, top, ph = M + 58, W - M - 96, y + 10, 400
    vmax = max([v for s in series for v in s["values"] if v is not None] or [1])
    ticks = nice_ticks(vmax * 1.04, 4)
    ymax = ticks[-1]
    ypx = lambda v: top + ph - (v / ymax) * ph  # noqa: E731
    xpx = lambda i: left + (right - left) * (i / max(1, len(xs) - 1))  # noqa: E731
    for t in ticks:
        sc.line(left, ypx(t), right, ypx(t), "$grid" if t else "$axis", 2)
        sc.text(left - 14, ypx(t) + 8, fmt_tick(t, fmt), 20, "$muted", anchor="end")
    step = 1 if len(xs) <= 18 else math.ceil(len(xs) / 14)
    for i, lab in enumerate(xs):
        if i % step == 0 or i == len(xs) - 1:
            sc.text(xpx(i), top + ph + 34, lab, 19 if len(xs) > 10 else 20, "$muted", anchor="middle")
    ends = []
    order = sorted(range(len(series)), key=lambda i: bool(series[i].get("highlight")))  # highlighted on top
    for i in order:
        s = series[i]
        pts = [(xpx(k), ypx(v)) for k, v in enumerate(s["values"]) if v is not None]
        if not pts:
            continue
        sc.poly(pts, ("series", i), 6 if s.get("highlight") else 4, opacity=1.0 if s.get("highlight") else 0.9)
        last_k = max(k for k, v in enumerate(s["values"]) if v is not None)
        for k, v in enumerate(s["values"]):  # small dots carry the hover tip; the last point gets the ringed end-marker
            if v is None:
                continue
            tip = f"{s['name']} · {xs[k]}: {fmt_value(v, fmt)} {unit}".strip()
            if k == last_k:
                sc.circle(xpx(k), ypx(v), 9, ("series", i), ring="$surface", ring_w=2, tip=tip)
            else:
                sc.circle(xpx(k), ypx(v), 4, ("series", i), tip=tip)
        if last_k == len(xs) - 1:
            ends.append({"x": pts[-1][0], "y": pts[-1][1], "label": fmt_value(s["values"][last_k], fmt)})
        else:  # series that stopped early (season in progress): label next to its own end
            sc.text(pts[-1][0] + 16, pts[-1][1] - 14, fmt_value(s["values"][last_k], fmt), 26, "$text1", bold=True, halo="$surface")
    _end_labels(sc, ends, right + 30, 30)
    return top + ph + 76


def block_bars(sc: Scene, y: float, b: Dict[str, Any]) -> float:
    items, fmt, unit = b["items"], b.get("fmt", "int"), b.get("unit", "")
    y = _section_title(sc, y, b.get("title", ""), b.get("subtitle", ""))
    vmax = max([it["value"] for it in items if it.get("value") is not None] or [1]) or 1
    if b.get("orientation", "h") == "h":
        label_w, row_h, bar_h = 250, 58, 34
        area = W - 2 * M - label_w - 130
        for i, it in enumerate(items):
            ry = y + i * row_h
            lab = str(it["label"])
            sc.text(M + label_w - 16, ry + bar_h / 2 + 9, lab, fit_size(lab, 25, label_w - 20, i == 0), "$text1",
                    anchor="end", bold=(i == 0))
            bw = max(4.0, area * (it["value"] or 0) / vmax)
            tip = f"{it['label']}: {fmt_value(it['value'], fmt)} {unit}".strip()
            sc.rect(M + label_w, ry, bw, bar_h, ("series", 0), rx=6, opacity=0.55 if it.get("muted") else 1.0, tip=tip)
            sc.text(M + label_w + bw + 14, ry + bar_h / 2 + 10, fmt_value(it["value"], fmt), 27, "$text1", bold=True)
        return y + len(items) * row_h + 34
    n = len(items)
    ph = 330
    slot = (W - 2 * M) / n
    bw = min(120, slot - 28)
    base = y + 56 + ph
    sc.line(M, base, W - M, base, "$axis", 2)
    for i, it in enumerate(items):
        v = it["value"] or 0
        h = max(4.0, ph * v / vmax)
        x = M + slot * i + (slot - bw) / 2
        tip = f"{it['label']}: {fmt_value(it['value'], fmt)} {unit}".strip()
        sc.rect(x, base - h, bw, h, ("series", 0), rx=8, opacity=0.5 if it.get("muted") else 1.0, tip=tip)
        if it.get("muted"):
            sc.hatch(x + 1, base - h + 1, bw - 2, h - 2, "$surface", gap=14, sw=2)
        sc.text(x + bw / 2, base - h - 14, fmt_value(it["value"], fmt), 32, "$text1", bold=True, anchor="middle")
        sc.text(x + bw / 2, base + 34, it["label"], 26, "$text1", anchor="middle", bold=True)
        if it.get("note"):
            sc.text(x + bw / 2, base + 62, it["note"], 19, "$muted", anchor="middle")
    return base + 100


def _legend(sc: Scene, y: float, names: List[str]) -> float:
    lx = M
    for i, n in enumerate(names):
        sc.circle(lx + 8, y + 12, 8, ("series", i))
        sc.text(lx + 24, y + 20, n, 24, "$text2")
        lx += 24 + text_width(n, 24) + 34
    return y + 52


def block_columns(sc: Scene, y: float, b: Dict[str, Any]) -> float:
    """Grouped or stacked columns, one group per x label, up to 6 series."""
    xs, series, fmt, unit = b["x"], b["series"], b.get("fmt", "int"), b.get("unit", "")
    stacked = bool(b.get("stacked"))
    y = _section_title(sc, y, b.get("title", ""), b.get("subtitle", ""))
    if len(series) > 1:
        y = _legend(sc, y, [s["name"] for s in series])
    left, right, top, ph = M + 58, W - M, y + 24, 340
    totals = [sum(max(0, s["values"][i] or 0) for s in series) for i in range(len(xs))]
    vmax = max(totals) if stacked else max([v for s in series for v in s["values"] if v is not None] or [1])
    ticks = nice_ticks(max(vmax, 1e-9) * 1.1, 4)
    ymax = ticks[-1]
    ypx = lambda v: top + ph - (v / ymax) * ph  # noqa: E731
    for t in ticks:
        sc.line(left, ypx(t), right, ypx(t), "$grid" if t else "$axis", 2)
        sc.text(left - 14, ypx(t) + 8, fmt_tick(t, fmt), 20, "$muted", anchor="end")
    n, k = len(xs), len(series)
    slot = (right - left) / n
    inner = slot * 0.78
    bw = inner if stacked else min(64, (inner - 4 * (k - 1)) / k)
    show_vals = (not stacked) and k <= 2 and n <= 8
    for i, lab in enumerate(xs):
        gx = left + slot * i + slot / 2
        if stacked:
            acc = 0.0
            for j, s in enumerate(series):
                v = s["values"][i] or 0
                if v <= 0:
                    continue
                h = max(2.0, ypx(acc) - ypx(acc + v) - 2)  # 2px surface gap between segments
                tip = f"{lab} · {s['name']}: {fmt_value(v, fmt)} {unit}".strip()
                sc.rect(gx - bw / 2, ypx(acc + v) + 1, bw, h, ("series", j), rx=4, tip=tip)
                txt = fmt_value(v, fmt)
                if h > 30 and text_width(txt, 20, True) < bw - 10:
                    sc.text(gx, ypx(acc + v) + 1 + h / 2 + 7, txt, 20, ("on_series", j), bold=True, anchor="middle")
                acc += v
            sc.text(gx, ypx(acc) - 12, fmt_value(acc, fmt), 22, "$text1", bold=True, anchor="middle")
        else:
            x0 = gx - (k * bw + (k - 1) * 4) / 2
            for j, s in enumerate(series):
                v = s["values"][i]
                if v is None:
                    continue
                h = max(3.0, ypx(0) - ypx(v))
                x = x0 + j * (bw + 4)
                sc.rect(x, ypx(0) - h, bw, h, ("series", j), rx=5,
                        tip=f"{lab} · {s['name']}: {fmt_value(v, fmt)} {unit}".strip())
                if show_vals:
                    sc.text(x + bw / 2, ypx(0) - h - 10, fmt_value(v, fmt), fit_size(fmt_value(v, fmt), 24, bw + 22, True, 14),
                            "$text1", bold=True, anchor="middle")
        sc.text(gx, ypx(0) + 34, str(lab), fit_size(str(lab), 22, slot - 6, True, 13), "$text1", anchor="middle", bold=True)
    return ypx(0) + 76


def block_donut(sc: Scene, y: float, b: Dict[str, Any]) -> float:
    """Share of a whole: ring + a legend with value and percentage (max 8 slices, the rest folds into 'Outros')."""
    fmt, unit = b.get("fmt", "int"), b.get("unit", "")
    items = sorted([i for i in b["items"] if (i.get("value") or 0) > 0], key=lambda i: -i["value"])
    if len(items) > 8:
        items = items[:7] + [{"label": "Outros", "value": sum(i["value"] for i in items[7:])}]
    y = _section_title(sc, y, b.get("title", ""), b.get("subtitle", ""))
    total = sum(i["value"] for i in items) or 1
    cx, cy, ro, ri = M + 210, y + 215, 205, 122
    a = 0.0
    for j, it in enumerate(items):
        span = 360.0 * it["value"] / total
        tip = f"{it['label']}: {fmt_value(it['value'], fmt)} {unit} ({it['value'] / total * 100:.0f}%)".strip()
        if span >= 359.9:
            sc.arc(cx, cy, ro, ri, 0, 180, ("series", j), tip)
            sc.arc(cx, cy, ro, ri, 180, 360, ("series", j), tip)
        else:
            sc.arc(cx, cy, ro, ri, a, a + span, ("series", j), tip)
        a += span
    tot_txt = fmt_value(total, fmt)
    sc.text(cx, cy + 14, tot_txt, fit_size(tot_txt, 60, ri * 1.7, True, 28), "$text1", bold=True, anchor="middle")
    sc.text(cx, cy + 48, b.get("center_label", "total"), 22, "$muted", anchor="middle")
    lx, ly = cx + ro + 56, cy - len(items) * 25
    for j, it in enumerate(items):
        yy = ly + j * 50
        sc.circle(lx + 10, yy + 12, 10, ("series", j))
        lab = str(it["label"])
        sc.text(lx + 32, yy + 21, lab, fit_size(lab, 26, 380 - 32 - 80, True, 15), "$text1", bold=True)
        sc.text(lx + 380, yy + 21, f"{it['value'] / total * 100:.0f}%", 26, "$text1", bold=True, anchor="end")
        sc.text(lx + 380, yy + 44, fmt_value(it["value"], fmt), 18, "$muted", anchor="end")
    return cy + ro + 56


def _nice_range(lo: float, hi: float, n: int = 5):
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    a = math.floor(lo / step) * step
    ticks = []
    v = a
    while v < hi + step * 0.999:
        ticks.append(round(v, 10))
        v += step
    return ticks


def block_scatter(sc: Scene, y: float, b: Dict[str, Any]) -> float:
    """Labelled dots on two numeric axes with league-average guides (who is on which side of the average)."""
    pts, fmt = b["points"], b.get("fmt", "float")
    y = _section_title(sc, y, b.get("title", ""), b.get("subtitle", ""))
    left, right, top, ph = M + 78, W - M - 20, y + 48, 520
    xs_t, ys_t = _nice_range(min(p["x"] for p in pts), max(p["x"] for p in pts)), _nice_range(min(p["y"] for p in pts), max(p["y"] for p in pts))
    x0, x1, y0, y1 = xs_t[0], xs_t[-1], ys_t[0], ys_t[-1]
    xpx = lambda v: left + (right - left) * (v - x0) / (x1 - x0)  # noqa: E731
    ypx = lambda v: top + ph - ph * (v - y0) / (y1 - y0)  # noqa: E731
    for t in ys_t:
        sc.line(left, ypx(t), right, ypx(t), "$grid", 2)
        sc.text(left - 14, ypx(t) + 8, fmt_tick(t, fmt), 20, "$muted", anchor="end")
    for t in xs_t:
        sc.text(xpx(t), top + ph + 34, fmt_tick(t, fmt), 20, "$muted", anchor="middle")
    sc.line(left, top + ph, right, top + ph, "$axis", 2)
    mx, my = sum(p["x"] for p in pts) / len(pts), sum(p["y"] for p in pts) / len(pts)
    if b.get("mean_lines", True):
        sc.line(xpx(mx), top, xpx(mx), top + ph, "$axis", 2, dash="10 8")
        sc.line(left, ypx(my), right, ypx(my), "$axis", 2, dash="10 8")
        sc.text(right, ypx(my) - 10, f"média {fmt_value(my, fmt)}", 18, "$muted", anchor="end")
        sc.text(xpx(mx) + 8, top + 16, f"média {fmt_value(mx, fmt)}", 18, "$muted")
    placed: List[Tuple[float, float, float, float]] = []
    quad = b.get("quadrants") or {}
    # captions live outside the plot (above / below), so they never hide a dot or a label
    for pos, (qx, qy, anc) in {"tl": (left, top - 16, "start"), "tr": (right, top - 16, "end"),
                               "bl": (left, top + ph + 70, "start"), "br": (right, top + ph + 70, "end")}.items():
        if quad.get(pos):
            sc.text(qx, qy, quad[pos], 19, "$muted", anchor=anc)
    for p in pts:  # dots first so labels never hide one
        sc.circle(xpx(p["x"]), ypx(p["y"]), 9, ("series", 0), ring="$surface", ring_w=2,
                  tip=f"{p['label']}: {b.get('x_label', 'x')} {fmt_value(p['x'], fmt)} · {b.get('y_label', 'y')} {fmt_value(p['y'], fmt)}")
        placed.append((xpx(p["x"]) - 10, ypx(p["y"]) - 10, xpx(p["x"]) + 10, ypx(p["y"]) + 10))
    for p in pts:
        cx, cy = xpx(p["x"]), ypx(p["y"])
        lab = str(p["label"])
        size = 21
        w_, h_ = text_width(lab, size, True), size
        for dx, dy, anc in ((14, 7, "start"), (-14, 7, "end"), (0, -16, "middle"), (0, 32, "middle"),
                            (14, -12, "start"), (-14, -12, "end"), (14, 28, "start"), (-14, 28, "end")):
            bx = cx + dx - (0 if anc == "start" else w_ if anc == "end" else w_ / 2)
            box = (bx, cy + dy - h_, bx + w_, cy + dy + 4)
            ok = box[0] >= left - 40 and box[2] <= W - 8 and box[1] >= top - 6
            if ok and not any(not (box[2] < o[0] or box[0] > o[2] or box[3] < o[1] or box[1] > o[3]) for o in placed):
                sc.text(cx + dx, cy + dy, lab, size, "$text1", bold=True, anchor=anc, halo="$surface")
                placed.append(box)
                break
        else:  # nowhere free: keep it readable anyway, next to the dot
            sc.text(cx + 14, cy + 7, lab, size, "$text1", bold=True, halo="$surface")
    sc.text((left + right) / 2, top + ph + 104, f"→ {b.get('x_label', '')}", 22, "$text2", anchor="middle")
    if b.get("y_label"):
        sc.text(M, top - 16, f"↑ {b['y_label']}" if not quad.get("tl") else "", 22, "$text2")
    return top + ph + 140


# --------------------------------------------------------------------------- validation
LIMITS = {"blocks": 8, "rows": 30, "cols": 30, "series": 8, "x": 40, "items": 30, "points": 40, "text": 140}


def _txt(v: Any, what: str) -> str:
    s = str(v if v is not None else "")
    if len(s) > LIMITS["text"] * 2:
        raise ValueError(f"{what}: texto longo demais")
    return s[: LIMITS["text"]]


def _num(v: Any, what: str, allow_none: bool = True) -> Optional[float]:
    if v is None:
        if allow_none:
            return None
        raise ValueError(f"{what}: valor ausente")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: '{v}' não é número") from None
    if math.isnan(f) or math.isinf(f):
        raise ValueError(f"{what}: número inválido")
    return f


def _check_len(seq: Any, mx: int, what: str) -> List[Any]:
    if not isinstance(seq, list) or not seq:
        raise ValueError(f"{what}: lista vazia ou inválida")
    if len(seq) > mx:
        raise ValueError(f"{what}: no máximo {mx} itens (recebi {len(seq)}); filtre ou agrupe")
    return seq


def validate_spec(spec: Any) -> Dict[str, Any]:
    """Normalise a spec (also one written by a model): types, sizes and shapes are checked before anything is drawn."""
    if not isinstance(spec, dict):
        raise ValueError("a especificação precisa ser um objeto JSON")
    out: Dict[str, Any] = {"title": _txt(spec.get("title"), "title"), "subtitle": _txt(spec.get("subtitle"), "subtitle"),
                           "footer": _txt(spec.get("footer"), "footer"), "caption": _txt(spec.get("caption"), "caption"),
                           "blocks": []}
    for bi, b in enumerate(_check_len(spec.get("blocks"), LIMITS["blocks"], "blocks")):
        if not isinstance(b, dict):
            raise ValueError(f"blocks[{bi}] precisa ser um objeto")
        t = b.get("type")
        c: Dict[str, Any] = {"type": t, "title": _txt(b.get("title"), "title"), "subtitle": _txt(b.get("subtitle"), "subtitle"),
                             "unit": _txt(b.get("unit"), "unit"), "fmt": b.get("fmt") if b.get("fmt") in ("int", "float", "pct") else "int",
                             "note": _txt(b.get("note"), "note")}
        if t == "tiles":
            c["items"] = [{k: _txt(i.get(k), k) for k in ("value", "label", "sub")}
                          for i in _check_len(b.get("items"), 4, "tiles.items")]
        elif t == "heatmap":
            rows = [_txt(r, "rows") for r in _check_len(b.get("rows"), LIMITS["rows"], "heatmap.rows")]
            cols = [_txt(r, "cols") for r in _check_len(b.get("cols"), LIMITS["cols"], "heatmap.cols")]
            vals = _check_len(b.get("values"), LIMITS["rows"], "heatmap.values")
            if len(vals) != len(rows) or any(not isinstance(r, list) or len(r) != len(cols) for r in vals):
                raise ValueError("heatmap.values precisa ter uma linha por 'rows' e uma coluna por 'cols'")
            c.update(rows=rows, cols=cols, values=[[_num(v, "heatmap.values") for v in r] for r in vals],
                     row_totals=bool(b.get("row_totals")),
                     row_notes={_txt(k, "row_notes"): _txt(v, "row_notes") for k, v in (b.get("row_notes") or {}).items()})
        elif t in ("lines", "columns"):
            xs = [_txt(x, "x") for x in _check_len(b.get("x"), LIMITS["x"], f"{t}.x")]
            mx = LIMITS["series"] if t == "lines" else 6
            ser = []
            for s_ in _check_len(b.get("series"), mx, f"{t}.series"):
                vals = s_.get("values") if isinstance(s_, dict) else None
                if not isinstance(vals, list) or len(vals) != len(xs):
                    raise ValueError(f"{t}.series[].values precisa ter o mesmo tamanho de x ({len(xs)})")
                ser.append({"name": _txt(s_.get("name"), "name"), "values": [_num(v, "values") for v in vals],
                            "highlight": bool(s_.get("highlight"))})
            c.update(x=xs, series=ser, stacked=bool(b.get("stacked")))
        elif t in ("bars", "donut"):
            items = []
            for i in _check_len(b.get("items"), LIMITS["items"], f"{t}.items"):
                items.append({"label": _txt(i.get("label"), "label"), "value": _num(i.get("value"), "value", False),
                              "note": _txt(i.get("note"), "note"), "muted": bool(i.get("muted"))})
            c.update(items=items, orientation="v" if b.get("orientation") == "v" else "h",
                     center_label=_txt(b.get("center_label") or "total", "center_label"))
        elif t == "scatter":
            pts = [{"label": _txt(p.get("label"), "label"), "x": _num(p.get("x"), "x", False), "y": _num(p.get("y"), "y", False)}
                   for p in _check_len(b.get("points"), LIMITS["points"], "scatter.points")]
            c.update(points=pts, x_label=_txt(b.get("x_label"), "x_label"), y_label=_txt(b.get("y_label"), "y_label"),
                     mean_lines=b.get("mean_lines", True) is not False,
                     quadrants={k: _txt(v, "quadrants") for k, v in (b.get("quadrants") or {}).items() if k in ("tl", "tr", "bl", "br")})
        else:
            raise ValueError(f"tipo de bloco desconhecido: {t!r} (use tiles, heatmap, lines, columns, bars, donut, scatter)")
        out["blocks"].append(c)
    return out


# --------------------------------------------------------------------------- compose
def compose(spec: Dict[str, Any]) -> Scene:
    spec = validate_spec(spec)
    sc = Scene()
    y = 48
    y = block_header(sc, y, spec.get("title", ""), spec.get("subtitle", ""))
    for b in spec.get("blocks", []):
        t = b.get("type")
        if t != "tiles" and b.get("title") and b.get("title") == spec.get("title"):
            b = dict(b, title=b.get("subtitle") or "", subtitle="")  # the header already says it; don't repeat
        if t == "tiles":
            y = block_tiles(sc, y, b["items"])
        elif t == "heatmap":
            y = block_heatmap(sc, y, b)
        elif t == "lines":
            y = block_lines(sc, y, b)
        elif t == "bars":
            y = block_bars(sc, y, b)
        elif t == "columns":
            y = block_columns(sc, y, b)
        elif t == "donut":
            y = block_donut(sc, y, b)
        elif t == "scatter":
            y = block_scatter(sc, y, b)
        else:
            raise ValueError(f"unknown block type: {t!r}")
        y += 18
    if spec.get("footer"):
        sc.text(M, y + 22, spec["footer"], 19, "$muted")
        y += 48
    sc.height = int(y + 24)
    return sc


# --------------------------------------------------------------------------- renderers
def _arc_pts(it: Dict[str, Any], step: float = 1.0) -> List[Tuple[float, float]]:
    """Outer arc clockwise, then inner arc back (a closed ring slice)."""
    def pt(r, a):
        t = math.radians(a)
        return it["cx"] + r * math.sin(t), it["cy"] - r * math.cos(t)
    n = max(2, int(abs(it["a1"] - it["a0"]) / step) + 1)
    outer = [pt(it["ro"], it["a0"] + (it["a1"] - it["a0"]) * k / (n - 1)) for k in range(n)]
    inner = [pt(it["ri"], it["a1"] - (it["a1"] - it["a0"]) * k / (n - 1)) for k in range(n)]
    return outer + inner


def _arc_path(it: Dict[str, Any]) -> str:
    def pt(r, a):
        t = math.radians(a)
        return it["cx"] + r * math.sin(t), it["cy"] - r * math.cos(t)
    big = 1 if it["a1"] - it["a0"] > 180 else 0
    (x1, y1), (x2, y2) = pt(it["ro"], it["a0"]), pt(it["ro"], it["a1"])
    (x3, y3), (x4, y4) = pt(it["ri"], it["a1"]), pt(it["ri"], it["a0"])
    return (f"M{x1:.1f},{y1:.1f} A{it['ro']},{it['ro']} 0 {big} 1 {x2:.1f},{y2:.1f} L{x3:.1f},{y3:.1f} "
            f"A{it['ri']},{it['ri']} 0 {big} 0 {x4:.1f},{y4:.1f} Z")


def render_svg(sc: Scene, theme_name: str = "dark", css_class: str = "") -> str:
    th = THEMES[theme_name]
    col = lambda c: resolve(c, th)  # noqa: E731
    esc = _html.escape
    out = [f'<svg class="{css_class}" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {sc.width} {sc.height}" '
           f'role="img" font-family="Liberation Sans, Helvetica Neue, Helvetica, Arial, system-ui, sans-serif">',
           f'<rect width="{sc.width}" height="{sc.height}" fill="{th["surface"]}"/>']
    for it in sc.items:
        k = it["k"]
        tip = f' data-tip="{esc(it["tip"], quote=True)}"' if it.get("tip") else ""
        if k == "rect":
            extra = f' stroke="{col(it["stroke"])}" stroke-width="{it["sw"]}"' if it.get("stroke") else ""
            op = f' fill-opacity="{it["op"]}"' if it["op"] != 1.0 else ""
            out.append(f'<rect x="{it["x"]:.1f}" y="{it["y"]:.1f}" width="{it["w"]:.1f}" height="{it["h"]:.1f}" '
                       f'rx="{it["rx"]}" fill="{col(it["fill"])}"{op}{extra}{tip}/>')
        elif k == "line":
            dash = f' stroke-dasharray="{it["dash"]}"' if it.get("dash") else ""
            op = f' stroke-opacity="{it["op"]}"' if it["op"] != 1.0 else ""
            out.append(f'<line x1="{it["x1"]:.1f}" y1="{it["y1"]:.1f}" x2="{it["x2"]:.1f}" y2="{it["y2"]:.1f}" '
                       f'stroke="{col(it["stroke"])}" stroke-width="{it["sw"]}"{dash}{op}/>')
        elif k == "poly":
            pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in it["pts"])
            op = f' stroke-opacity="{it["op"]}"' if it["op"] != 1.0 else ""
            out.append(f'<polyline points="{pts}" fill="none" stroke="{col(it["stroke"])}" stroke-width="{it["sw"]}" '
                       f'stroke-linejoin="round" stroke-linecap="round"{op}/>')
        elif k == "circle":
            ring = f' stroke="{col(it["ring"])}" stroke-width="{it["rw"]}"' if it.get("ring") else ""
            out.append(f'<circle cx="{it["cx"]:.1f}" cy="{it["cy"]:.1f}" r="{it["r"]}" fill="{col(it["fill"])}"{ring}{tip}/>')
        elif k == "arc":
            out.append(f'<path d="{_arc_path(it)}" fill="{col(it["fill"])}" stroke="{th["surface"]}" stroke-width="3" '
                       f'stroke-linejoin="round"{tip}/>')
        elif k == "text":
            halo = (f' stroke="{col(it["halo"])}" stroke-width="7" paint-order="stroke" stroke-linejoin="round"'
                    if it.get("halo") else "")
            anchor = {"start": "start", "middle": "middle", "end": "end"}[it["anchor"]]
            out.append(f'<text x="{it["x"]:.1f}" y="{it["y"]:.1f}" font-size="{it["size"]}" '
                       f'font-weight="{700 if it["bold"] else 400}" text-anchor="{anchor}" '
                       f'fill="{col(it["fill"])}"{halo}>{esc(it["s"])}</text>')
    out.append("</svg>")
    return "".join(out)


def render_png(sc: Scene, path: str, theme_name: str = "dark") -> str:
    from PIL import Image, ImageDraw
    th = THEMES[theme_name]
    S = SS
    img = Image.new("RGB", (sc.width * S, sc.height * S), th["surface"])
    d = ImageDraw.Draw(img, "RGBA")

    def rgba(c, op=1.0):
        r, g, b = _hex_rgb(resolve(c, th))
        return (r, g, b, int(255 * op))

    for it in sc.items:
        k = it["k"]
        if k == "rect":
            box = [it["x"] * S, it["y"] * S, (it["x"] + it["w"]) * S - 1, (it["y"] + it["h"]) * S - 1]
            if it["rx"]:
                d.rounded_rectangle(box, radius=it["rx"] * S, fill=rgba(it["fill"], it["op"]))
            else:
                d.rectangle(box, fill=rgba(it["fill"], it["op"]))
        elif k == "line":
            d.line([it["x1"] * S, it["y1"] * S, it["x2"] * S, it["y2"] * S], fill=rgba(it["stroke"], it["op"]),
                   width=max(1, int(round(it["sw"] * S))))
        elif k == "poly":
            pts = [(x * S, y * S) for x, y in it["pts"]]
            w = max(1, int(round(it["sw"] * S)))
            c = rgba(it["stroke"], it["op"])
            if len(pts) > 1:
                d.line(pts, fill=c, width=w, joint="curve")
            for x, y in pts:  # round joins/caps
                d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=c)
        elif k == "circle":
            r = it["r"] * S
            if it.get("ring"):
                rr = r + it["rw"] * S
                d.ellipse([it["cx"] * S - rr, it["cy"] * S - rr, it["cx"] * S + rr, it["cy"] * S + rr], fill=rgba(it["ring"]))
            d.ellipse([it["cx"] * S - r, it["cy"] * S - r, it["cx"] * S + r, it["cy"] * S + r], fill=rgba(it["fill"]))
        elif k == "arc":
            pts = [(x * S, y * S) for x, y in _arc_pts(it)]
            d.polygon(pts, fill=rgba(it["fill"]), outline=rgba("$surface"), width=max(1, int(1.5 * S)))
        elif k == "text":
            f = font(it["size"] * S, it["bold"])
            anchor = {"start": "ls", "middle": "ms", "end": "rs"}[it["anchor"]]
            kw = {}
            if it.get("halo"):
                kw = dict(stroke_width=int(3.5 * S), stroke_fill=rgba(it["halo"]))
            d.text((it["x"] * S, it["y"] * S), it["s"], font=f, fill=rgba(it["fill"]), anchor=anchor, **kw)
    out = img.resize((sc.width, sc.height), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    out.save(path, "PNG", optimize=True)
    return path


_HTML = """<!doctype html>
<html lang="pt-BR" data-theme="__THEME__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--page:#0d0d0d;--ink:#fff;--ink2:#c3c2b7;--line:#2c2c2a;--card:#1a1a19;color-scheme:dark}
:root[data-theme="light"]{--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--line:#e1e0d9;--card:#fcfcfb;color-scheme:light}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--line:#e1e0d9;--card:#fcfcfb;color-scheme:light}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.bar{display:flex;justify-content:flex-end;gap:8px;margin-bottom:10px}
button{background:transparent;color:var(--ink2);border:1px solid var(--line);border-radius:999px;padding:6px 14px;font:inherit;cursor:pointer}
button:hover{color:var(--ink)}
.chart{border:1px solid var(--line);border-radius:20px;overflow:hidden;background:var(--card)}
svg{display:block;width:100%;height:auto}
.s-light{display:none}
:root[data-theme="light"] .s-dark{display:none}:root[data-theme="light"] .s-light{display:block}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]) .s-dark{display:none}:root:not([data-theme="dark"]) .s-light{display:block}}
[data-tip]{cursor:default}[data-tip]:hover{filter:brightness(1.18)}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--page);padding:6px 10px;border-radius:8px;font-size:14px;font-weight:600;opacity:0;transition:opacity .08s;z-index:5;white-space:nowrap}
details{margin-top:16px;color:var(--ink2)}summary{cursor:pointer}
table{border-collapse:collapse;margin-top:8px;font-size:14px;width:100%}th,td{border-bottom:1px solid var(--line);padding:6px 10px;text-align:right}th:first-child,td:first-child{text-align:left}
caption{text-align:left;font-weight:700;color:var(--ink);padding:10px 0 4px}
</style>
</head>
<body>
<div class="wrap">
<div class="bar"><button id="theme" type="button" aria-label="Alternar tema claro/escuro">Tema claro/escuro</button></div>
<div class="chart">__SVG_DARK__ __SVG_LIGHT__</div>
<details><summary>Ver dados em tabela</summary>__TABLES__</details>
</div>
<div id="tip" role="tooltip"></div>
<script>
(function(){
  var tip=document.getElementById('tip'),root=document.documentElement;
  document.addEventListener('mousemove',function(e){
    var t=e.target.closest&&e.target.closest('[data-tip]');
    if(!t){tip.style.opacity=0;return}
    tip.textContent=t.getAttribute('data-tip');tip.style.opacity=1;
    tip.style.left=Math.min(e.clientX+14,innerWidth-tip.offsetWidth-8)+'px';tip.style.top=(e.clientY+16)+'px';
  });
  document.addEventListener('touchstart',function(e){var t=e.target.closest&&e.target.closest('[data-tip]');
    if(t){var p=e.touches[0];tip.textContent=t.getAttribute('data-tip');tip.style.opacity=1;
      tip.style.left=Math.max(8,Math.min(p.clientX-40,innerWidth-tip.offsetWidth-8))+'px';tip.style.top=(p.clientY-48)+'px';
      setTimeout(function(){tip.style.opacity=0},1800)}},{passive:true});
  document.getElementById('theme').onclick=function(){
    var dark=(root.getAttribute('data-theme')==='dark')||(root.getAttribute('data-theme')!=='light'&&!matchMedia('(prefers-color-scheme: light)').matches);
    root.setAttribute('data-theme',dark?'light':'dark');
  };
})();
</script>
</body>
</html>
"""


def _tables(spec: Dict[str, Any]) -> str:
    esc = _html.escape
    out = []
    for b in spec.get("blocks", []):
        t = b.get("type")
        if t == "heatmap":
            head = "".join(f"<th>{esc(str(c))}</th>" for c in b["cols"]) + ("<th>Total</th>" if b.get("row_totals") else "")
            body = ""
            for r, vals in zip(b["rows"], b["values"]):
                cells = "".join(f"<td>{'' if v is None else fmt_value(v, b.get('fmt', 'int'))}</td>" for v in vals)
                tot = (f"<td>{fmt_value(sum(v for v in vals if v is not None), b.get('fmt', 'int'))}</td>"
                       if b.get("row_totals") else "")
                body += f"<tr><td>{esc(str(r))}</td>{cells}{tot}</tr>"
            out.append(f"<table><caption>{esc(b.get('title', ''))}</caption><tr><th></th>{head}</tr>{body}</table>")
        elif t == "lines":
            head = "".join(f"<th>{esc(str(x))}</th>" for x in b["x"])
            body = "".join(f"<tr><td>{esc(s['name'])}</td>" + "".join(
                f"<td>{'' if v is None else fmt_value(v, b.get('fmt', 'int'))}</td>" for v in s["values"]) + "</tr>" for s in b["series"])
            out.append(f"<table><caption>{esc(b.get('title', ''))}</caption><tr><th></th>{head}</tr>{body}</table>")
        elif t in ("bars", "donut"):
            body = "".join(f"<tr><td>{esc(str(i['label']))}</td><td>{fmt_value(i['value'], b.get('fmt', 'int'))}</td></tr>"
                           for i in b["items"])
            out.append(f"<table><caption>{esc(b.get('title', ''))}</caption>{body}</table>")
        elif t == "columns":
            head = "".join(f"<th>{esc(str(x))}</th>" for x in b["x"])
            body = "".join(f"<tr><td>{esc(s['name'])}</td>" + "".join(
                f"<td>{'' if v is None else fmt_value(v, b.get('fmt', 'int'))}</td>" for v in s["values"]) + "</tr>"
                for s in b["series"])
            out.append(f"<table><caption>{esc(b.get('title', ''))}</caption><tr><th></th>{head}</tr>{body}</table>")
        elif t == "scatter":
            body = "".join(f"<tr><td>{esc(str(p['label']))}</td><td>{fmt_value(p['x'], b.get('fmt', 'float'))}</td>"
                           f"<td>{fmt_value(p['y'], b.get('fmt', 'float'))}</td></tr>" for p in b["points"])
            out.append(f"<table><caption>{esc(b.get('title', ''))}</caption><tr><th></th><th>{esc(b.get('x_label', 'x'))}</th>"
                       f"<th>{esc(b.get('y_label', 'y'))}</th></tr>{body}</table>")
    return "".join(out)


def render_html(sc: Scene, spec: Dict[str, Any], path: str, theme: str = "dark") -> str:
    page = (_HTML.replace("__THEME__", theme).replace("__TITLE__", _html.escape(spec.get("title", "Gráfico")))
            .replace("__SVG_DARK__", render_svg(sc, "dark", "s-dark"))
            .replace("__SVG_LIGHT__", render_svg(sc, "light", "s-light"))
            .replace("__TABLES__", _tables(spec)))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def render_all(spec: Dict[str, Any], png_path: str, html_path: Optional[str] = None) -> Dict[str, Any]:
    """HTML (interactive, light+dark) and PNG (dark) from the same scene."""
    spec = validate_spec(spec)
    sc = compose(spec)
    res = {"width": sc.width, "height": sc.height}
    if html_path:
        res["html"] = render_html(sc, spec, html_path)
    res["png"] = render_png(sc, png_path)
    return res
