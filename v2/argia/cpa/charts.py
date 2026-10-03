"""SVG charts for the CPA site (v296). Pure: data in, markup out.

Marks follow the ARGIA dataviz rules: thin bars with a 2px surface gap
between stacked segments, 4px rounded data ends, 2px lines, recessive
grid, a legend for every multi-series chart, and a hover layer - each
column carries ``data-tip`` / ``data-tip-es`` (lines split by "|") that
the page's one tooltip script shows. Series colours are passed in and
follow the site, never its rank.
"""
from __future__ import annotations

import html
import math
from typing import Dict, List, Optional, Sequence, Tuple

GRID, AXIS, INK2 = "#e6e8ef", "#c9ccd8", "#5b5f73"
MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS_ES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
MONTHS_FULL_EN = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                  "November", "December")
MONTHS_FULL_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
                  "noviembre", "diciembre")


def e(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def nice_max(v: float) -> float:
    """The smallest 1/2/2.5/5 x 10^n at or above v (v <= 0 -> 1)."""
    if v <= 0:
        return 1.0
    p = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * p >= v - 1e-9:
            return m * p
    return 10 * p


def fmt(v: Optional[float], d: int = 0) -> str:
    return "-" if v is None else f"{v:,.{d}f}"


def month_label(m: str, es: bool = False) -> str:
    y, mm = m[:4], int(m[5:7])
    return f"{(MONTHS_ES if es else MONTHS_EN)[mm - 1]} {y[2:]}"


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """A bar from the baseline up whose top corners are rounded."""
    r = min(r, w / 2, h)
    return (f"M{x:.1f},{y + h:.1f}V{y + r:.1f}Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f}"
            f"H{x + w - r:.1f}Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f}V{y + h:.1f}Z")


def _y_axis(ymax: float, x0: float, x1: float, top: float, bottom: float, unit: str, d: int = 0) -> str:
    d = 0 if ymax >= 10 else d                      # 50, 100, 150 - not 50.0
    out = []
    for i in range(5):
        v = ymax * i / 4
        y = bottom - (bottom - top) * i / 4
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID if i else AXIS}" stroke-width="1"/>'
                   f'<text x="{x0 - 8}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="{INK2}">{fmt(v, d)}</text>')
    out.append(f'<text x="{x0 - 8}" y="{top - 10:.1f}" text-anchor="end" font-size="11" fill="{INK2}">{e(unit)}</text>')
    return "".join(out)


def legend(series: Sequence[Tuple[str, str]]) -> str:
    return ('<div class="clegend">' + "".join(f'<span><i style="background:{c}"></i>{e(n)}</span>' for n, c in series)
            + "</div>")


def stacked_bars(cats: Sequence[str], series: Sequence[Tuple[str, str, Dict[str, float]]], unit: str,
                 labels_en: Sequence[str], labels_es: Sequence[str], w: int = 760, h: int = 260,
                 tip_unit: str = "", d: int = 0) -> str:
    """Stacked columns: ``series`` = [(name, colour, {category: value})]."""
    if not cats:
        return '<div class="nodata">-</div>'
    left, right, top, bottom = 56, 10, 22, h - 28
    tot = [sum(s[2].get(c, 0.0) for s in series) for c in cats]
    ymax = nice_max(max(tot) if tot else 0)
    step = (w - left - right) / len(cats)
    bw = max(3.0, min(34.0, step * 0.62))
    every = max(1, math.ceil(len(cats) / 12))
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">',
           _y_axis(ymax, left, w - right, top, bottom, unit, d)]
    for i, c in enumerate(cats):
        x = left + step * i + (step - bw) / 2
        base = bottom
        segs = [(n, col, s.get(c, 0.0)) for n, col, s in series if s.get(c, 0.0) > 0]
        for j, (n, col, v) in enumerate(segs):
            hh = (bottom - top) * v / ymax
            y = base - hh
            gap = 2 if j else 0
            hh_draw = max(0.0, hh - gap)
            if j == len(segs) - 1:
                out.append(f'<path d="{_bar_path(x, y, bw, hh_draw, 4)}" fill="{col}"/>')
            else:
                out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{hh_draw:.1f}" fill="{col}"/>')
            base = y
        tip_en = "|".join([labels_en[i]] + [f"{n}: {fmt(v, d)} {tip_unit or unit}" for n, _, v in segs]
                          + ([f"Total: {fmt(tot[i], d)} {tip_unit or unit}"] if len(segs) > 1 else []))
        tip_es = "|".join([labels_es[i]] + [f"{n}: {fmt(v, d)} {tip_unit or unit}" for n, _, v in segs]
                          + ([f"Total: {fmt(tot[i], d)} {tip_unit or unit}"] if len(segs) > 1 else []))
        out.append(f'<rect class="hit" x="{left + step * i:.1f}" y="{top}" width="{step:.1f}" height="{bottom - top}" '
                   f'fill="transparent" data-tip="{e(tip_en)}" data-tip-es="{e(tip_es)}"/>')
        if i % every == 0 or i == len(cats) - 1:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{h - 8}" text-anchor="middle" font-size="11" fill="{INK2}">'
                       f'<tspan class="t-en">{e(labels_en[i])}</tspan><tspan class="t-es">{e(labels_es[i])}</tspan></text>')
    out.append("</svg>")
    return "".join(out)


def area(points: Sequence[Tuple[str, float]], colour: str, unit: str, labels_en: Sequence[str],
         labels_es: Sequence[str], w: int = 760, h: int = 260, d: int = 0, tip_unit: str = "") -> str:
    """One filled line (cumulative CO2): points = [(category, value)]."""
    if not points:
        return '<div class="nodata">-</div>'
    left, right, top, bottom = 56, 14, 22, h - 28
    ymax = nice_max(max(v for _, v in points))
    n = len(points)
    step = (w - left - right) / max(1, n - 1)
    xy = [(left + step * i, bottom - (bottom - top) * v / ymax) for i, (_, v) in enumerate(points)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    every = max(1, math.ceil(n / 12))
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">',
           _y_axis(ymax, left, w - right, top, bottom, unit, d),
           f'<polygon points="{xy[0][0]:.1f},{bottom} {line} {xy[-1][0]:.1f},{bottom}" fill="{colour}" fill-opacity=".16"/>',
           f'<polyline points="{line}" fill="none" stroke="{colour}" stroke-width="2" stroke-linejoin="round"/>',
           f'<circle cx="{xy[-1][0]:.1f}" cy="{xy[-1][1]:.1f}" r="5" fill="{colour}" stroke="#fff" stroke-width="2"/>']
    for i, ((c, v), (x, y)) in enumerate(zip(points, xy)):
        out.append(f'<rect class="hit" x="{x - step / 2:.1f}" y="{top}" width="{step:.1f}" height="{bottom - top}" fill="transparent" '
                   f'data-tip="{e(labels_en[i] + "|" + fmt(v, d) + " " + (tip_unit or unit))}" '
                   f'data-tip-es="{e(labels_es[i] + "|" + fmt(v, d) + " " + (tip_unit or unit))}"/>')
        if i % every == 0 or i == n - 1:
            out.append(f'<text x="{x:.1f}" y="{h - 8}" text-anchor="middle" font-size="11" fill="{INK2}">'
                       f'<tspan class="t-en">{e(labels_en[i])}</tspan><tspan class="t-es">{e(labels_es[i])}</tspan></text>')
    out.append("</svg>")
    return "".join(out)


MAX_BRIDGE = 2          # 15-minute slots: up to 30 minutes are bridged


def fill_gaps(times: Sequence[str], vals: Dict[str, float]) -> Dict[str, float]:
    """A short gap (up to MAX_BRIDGE missing slots) between two readings is
    drawn on the straight line between them - a late upload is not an
    outage. A longer gap stays empty: v302 drops repeated (frozen) logger
    readings, and an hour without data must not be drawn as production.
    Before the first and after the last reading nothing is invented."""
    known = [i for i, t in enumerate(times) if t in vals]
    out = dict(vals)
    for a, b in zip(known, known[1:]):
        if b - a - 1 > MAX_BRIDGE:
            continue
        va, vb = vals[times[a]], vals[times[b]]
        for i in range(a + 1, b):
            out[times[i]] = va + (vb - va) * (i - a) / (b - a)
    return out


def stacked_area(times: Sequence[str], series: Sequence[Tuple[str, str, Dict[str, float]]], unit: str,
                 w: int = 760, h: int = 240) -> str:
    """Today's power by site, stacked; times = ["HH:MM", ...] in order."""
    if not times or not any(s[2] for s in series):
        return '<div class="nodata"><span lang="en">No readings yet today.</span><span lang="es">Aún sin lecturas hoy.</span></div>'
    last = max((t for _, _, v in series for t in v), default=None)
    times = [t for t in times if last is None or t <= last]          # nothing after the newest reading
    series = [(nm, col, fill_gaps(times, v)) for nm, col, v in series]
    left, right, top, bottom = 56, 14, 22, h - 28
    n = len(times)
    tot = [sum(s[2].get(t, 0.0) for s in series) for t in times]
    ymax = nice_max(max(tot))
    step = (w - left - right) / max(1, n - 1)
    out = [f'<svg class="chart" viewBox="0 0 {w} {h}" role="img" preserveAspectRatio="xMidYMid meet">',
           _y_axis(ymax, left, w - right, top, bottom, unit)]
    base = [0.0] * n
    for name, col, vals in series:
        upper = [base[i] + vals.get(t, 0.0) for i, t in enumerate(times)]
        pts_u = [(left + step * i, bottom - (bottom - top) * upper[i] / ymax) for i in range(n)]
        pts_b = [(left + step * i, bottom - (bottom - top) * base[i] / ymax) for i in range(n)][::-1]
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts_u + pts_b)
        out.append(f'<polygon points="{poly}" fill="{col}" fill-opacity=".85" stroke="#fff" stroke-width="1"/>')
        base = upper
    for i, t in enumerate(times):
        parts = [f"{nm}: {fmt(v.get(t, 0.0), 0)} {unit}" for nm, _, v in series]
        tip = "|".join([t] + parts + [f"Total: {fmt(tot[i], 0)} {unit}"])
        out.append(f'<rect class="hit" x="{left + step * i - step / 2:.1f}" y="{top}" width="{step:.1f}" height="{bottom - top}" '
                   f'fill="transparent" data-tip="{e(tip)}" data-tip-es="{e(tip)}"/>')
        if t.endswith(":00") and int(t[:2]) % 2 == 0:
            out.append(f'<text x="{left + step * i:.1f}" y="{h - 8}" text-anchor="middle" font-size="11" fill="{INK2}">{t}</text>')
    out.append("</svg>")
    return "".join(out)


def spark_months(cats: List[str]) -> Tuple[List[str], List[str]]:
    return [month_label(c) for c in cats], [month_label(c, True) for c in cats]
