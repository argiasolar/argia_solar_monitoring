"""v385: the plant page's "Consumption and solar coverage" card (pure, server-side SVG).

Consumption = solar + grid import - grid export (the grid meter at the
connection point plus the inverters). Solar share = the solar used on site
(solar - export) over the consumption: the part of the site's electricity
the solar supplied. Several days = ratio of the sums, never a mean of days.
A day whose numbers cannot be one site behind one meter (more export than
solar) is shown greyed and left out of the sums.
"""
from __future__ import annotations

import html
from typing import Dict, List, Optional, Sequence, Tuple

from argia.telemetry import meter as M

GREEN, GREY, AMBER, RED = "#2e9e5b", "#9aa3ab", "#f0a03a", "#c5221f"


def _n(v: Optional[float], nd: int = 0) -> str:
    return " - " if v is None else f"{v:,.{nd}f}"


def _pct(v: Optional[float]) -> str:
    return " - " if v is None else f"{v * 100:.0f}%"


def _bi(en: str, es: str, tag: str = "span", cls: str = "") -> str:
    c = f' class="{cls}"' if cls else ""
    return f'<{tag}{c} data-en="{html.escape(en)}" data-es="{html.escape(es)}">{html.escape(en)}</{tag}>'


def _bars(items: Sequence[Tuple[str, Optional[dict], bool]], label_every: int, W: int = 940, H: int = 230) -> str:
    """items: (x label, {'used','imp','exp'} kWh or None, dimmed). Used solar + import up, export down."""
    PL, PR, PT, PB = 52, 12, 10, 26
    up = max([(b["used"] + b["imp"]) for _, b, _ in items if b] + [1.0])
    dn = max([b["exp"] for _, b, _ in items if b] + [0.0])
    span = up + dn
    ph = H - PT - PB
    y0 = PT + ph * up / span
    sc = ph / span
    slot = (W - PL - PR) / max(len(items), 1)
    bw = slot * 0.66
    out = [f'<line x1="{PL}" y1="{y0:.1f}" x2="{W - PR}" y2="{y0:.1f}" stroke="#80868b"/>']
    for frac in (0.5, 1.0):
        v = up * frac
        yy = y0 - v * sc
        out.append(f'<line x1="{PL}" y1="{yy:.1f}" x2="{W - PR}" y2="{yy:.1f}" stroke="#eceef0"/>'
                   f'<text x="{PL - 6}" y="{yy + 4:.1f}" text-anchor="end" font-size="11" fill="#80868b">{v:,.0f}</text>')
    if dn > 0.05 * up:
        out.append(f'<text x="{PL - 6}" y="{y0 + dn * sc:.1f}" text-anchor="end" font-size="11" fill="#80868b">-{dn:,.0f}</text>')
    for k, (lab, b, dim) in enumerate(items):
        x = PL + k * slot + (slot - bw) / 2
        op = ' opacity="0.35"' if dim else ""
        if b:
            used, imp, exp = max(b["used"], 0.0), max(b["imp"], 0.0), max(b["exp"], 0.0)
            tip = html.escape(f"{lab}: solar used {used:,.0f} kWh, import {imp:,.0f}, export {exp:,.0f}")
            out.append(f'<g{op}><title>{tip}</title>'
                       f'<rect x="{x:.1f}" y="{y0 - used * sc:.1f}" width="{bw:.1f}" height="{used * sc:.1f}" fill="{GREEN}"/>'
                       f'<rect x="{x:.1f}" y="{y0 - (used + imp) * sc:.1f}" width="{bw:.1f}" height="{imp * sc:.1f}" fill="{GREY}"/>'
                       f'<rect x="{x:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{exp * sc:.1f}" fill="{AMBER}"/></g>')
        if k % label_every == 0:
            out.append(f'<text x="{x + bw / 2:.1f}" y="{H - 8}" text-anchor="middle" font-size="11" fill="#80868b">{html.escape(lab)}</text>')
    return f'<svg viewBox="0 0 {W} {H}" style="width:100%;height:auto">' + "".join(out) + "</svg>"


def _legend() -> str:
    sw = lambda c: f'<span style="display:inline-block;width:10px;height:10px;background:{c};border-radius:2px"></span> '
    return ('<div style="margin-top:4px">'
            f'<span class="note" style="margin-right:14px">{sw(GREEN)}{_bi("Solar used on site", "Solar consumida en sitio")}</span>'
            f'<span class="note" style="margin-right:14px">{sw(GREY)}{_bi("Grid import", "Importación de la red")}</span>'
            f'<span class="note" style="margin-right:14px">{sw(AMBER)}{_bi("Export to the grid (below the axis)", "Exportación a la red (bajo el eje)")}</span>'
            '</div>')


def _tile(en: str, es: str, value: str, sub: str = "") -> str:
    sub = f'<div class="l">{sub}</div>' if sub else ""
    return f'<div class="kpi"><div class="v">{value}</div><div class="l">{_bi(en, es)}</div>{sub}</div>'



def card(day: str, hours: Dict[int, dict], days: List[Tuple[str, Optional[dict], str]], live: bool) -> str:
    """``hours``: {0..23: balance of that hour} for ``day``; ``days``: [(date, balance or None, meter basis)]
    oldest first, ending at ``day``. Returns '' when the day has no meter data."""
    by = {d: (b, basis) for d, b, basis in days}
    b, basis = by.get(day, (None, ""))
    if b is None and not hours:
        return ""
    last7 = [x for d, x, _ in days[-7:]]
    month = [x for d, x, _ in days if d[:7] == day[:7]]
    s7, sm = M.period_share(last7), M.period_share(month)
    cons_m = sum(x["cons"] for x in month if x and x["ok"])
    so_far = " so far" if live else ""
    tiles = "".join([
        _tile("Consumption" + so_far, "Consumo" + (" a la hora" if live else ""), f"{_n(b and b['cons'])} kWh"),
        _tile("Solar share", "Cobertura solar", _pct(b and b["share"]),
              (f'{_n(b["used"])} of {_n(b["cons"])} kWh' if b and b["ok"] else "")),
        _tile("Grid import", "Importación", f"{_n(b and b['imp'])} kWh"),
        _tile("Export", "Exportación", f"{_n(b and b['exp'])} kWh"),
        _tile("Solar share, 7 days", "Cobertura, 7 días", _pct(s7)),
        _tile("Solar share, month", "Cobertura, mes", _pct(sm), f"of {_n(cons_m)} kWh consumed"),
    ])
    warn = ""
    if b is not None and not b["ok"]:
        warn = ('<p class="note" style="color:%s">%s</p>' % (RED, _bi(
            "This day's export is larger than its solar: the meter or the solar data is incomplete, so no share is shown.",
            "La exportación del día supera la generación solar: faltan datos del medidor o de la planta; no se muestra cobertura.")))
    hbars = _bars([(f"{h:02d}", hours.get(h), False) for h in range(24)], 2) if hours else ""
    dbars = _bars([(d[5:], x, bool(x and not x["ok"])) for d, x, _ in days], max(1, len(days) // 15))
    rows = []
    for d, x, bs in reversed(days):
        if x is None:
            continue
        cls = ' style="opacity:.45"' if not x["ok"] else ""
        rows.append(f'<tr{cls}><td>{d}</td><td>{_n(x["cons"])}</td><td>{_n(x["pv"])}</td><td>{_n(x["used"])}</td>'
                    f'<td>{_n(x["imp"])}</td><td>{_n(x["exp"])}</td><td>{_pct(x["share"])}</td><td class="note">{html.escape(bs)}</td></tr>')
    head = "".join(f"<th>{_bi(en, es)}</th>" for en, es in (
        ("Day", "Día"), ("Consumption kWh", "Consumo kWh"), ("Solar kWh", "Solar kWh"), ("Solar used kWh", "Solar consumida kWh"),
        ("Import kWh", "Importación kWh"), ("Export kWh", "Exportación kWh"), ("Solar share", "Cobertura"), ("Meter register", "Registro")))
    table = (f'<details><summary>{_bi("Daily table", "Tabla diaria")}</summary>'
             f'<table><tr>{head}</tr>{"".join(rows)}</table></details>') if rows else ""
    return f'''
<div class="card mtcard"><h2>{_bi("Consumption and solar coverage - grid meter + inverters", "Consumo y cobertura solar - medidor de red + inversores")}</h2>
<div class="kpis" style="display:flex;flex-wrap:wrap;gap:26px;margin:10px 0 2px;align-items:flex-end">{tiles}</div>{warn}
{('<h3 style="margin:12px 0 2px;font-size:14px">' + _bi("By hour, kWh", "Por hora, kWh") + '</h3>' + hbars) if hbars else ''}
<h3 style="margin:12px 0 2px;font-size:14px">{_bi("Last 30 days, kWh per day", "Últimos 30 días, kWh por día")}</h3>{dbars}{_legend()}
{table}
<p class="note">{_bi(
    "Consumption = solar + grid import - export. Solar share = solar used on site (solar - export) / consumption; several days = ratio of the sums. Import and export come from the plant's grid meter as read by the inverter vendor's portal (day registers or lifetime counters; the hourly bars integrate its 5-minute power), solar from the inverters. A CFE bill is the reference for billing; this card is for operations.",
    "Consumo = solar + importación - exportación. Cobertura solar = solar consumida en sitio (solar - exportación) / consumo; varios días = cociente de las sumas. Importación y exportación provienen del medidor de red de la planta leído por el portal del fabricante (registros diarios o contadores acumulados; las barras por hora integran su potencia de 5 minutos), la solar de los inversores. El recibo de CFE es la referencia para facturación; esta tarjeta es para operación.")}</p></div>'''
