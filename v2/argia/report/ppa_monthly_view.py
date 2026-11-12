"""PPA monthly performance report (v374): the page, the print (PDF) and the month index. Pure rendering.

``body(rep, L)`` renders a report dict (argia.report.ppa_monthly.build, or a frozen published copy) with a
language helper ``L``: ``web_lang()`` makes bilingual spans the portal's language switch flips (data-en/data-es),
``print_lang("en"|"es")`` writes one language only, for the PDF. The same body serves the page and the PDF, so
the two can never tell different stories.
"""
from __future__ import annotations

import datetime as dt
import html
from typing import Dict, List, Optional, Sequence, Tuple

from argia.report import ppa_monthly as PM

MESES = PM.MESES
COL = {"actual": "#05b1a9", "contract": "#9aa3ad", "design": "#053b38", "good": "#05847d", "warn": "#d08a17",
       "bad": "#c2554e", "neg": "#c2554e", "pos": "#05847d", "grid": "#eceef0", "axis": "#d5d9dd", "ink": "#41474f",
       "muted": "#6b7480", "weather": "#7fa7c9", "customer": "#b49be0"}
STEP_LABEL = {
    "contract": ("Contract curve", "Curva de contrato"),
    "design": ("Design vs contract", "Diseño vs contrato"),
    "weather": ("Weather", "Clima"),
    "customer": ("Customer maintenance", "Mantenimiento del cliente"),
    "availability": ("Unavailability", "Indisponibilidad"),
    "performance": ("Plant performance", "Desempeño de planta"),
    "unsplit": ("Weather + performance (no design yet)", "Clima + desempeño (sin diseño aún)"),
    "actual": ("Actual", "Real"),
}
STEP_WHY = {
    "design": ("What the HelioScope design expects in a typical year, against the contract curve: the cushion (or "
               "the gap) the sale built in.",
               "Lo que el diseño HelioScope espera en un año típico contra la curva de contrato: el colchón (o la "
               "brecha) que dejó la venta."),
    "weather": ("The design for the sun that actually came, against the design for a typical month. Nobody "
                "controls this.",
                "El diseño para el sol que realmente hubo contra el diseño de un mes típico. Nadie lo controla."),
    "customer": ("Energy not produced on days the customer stopped the plant; compensated on the invoice.",
                 "Energía no producida los días que el cliente paró la planta; se compensa en la factura."),
    "availability": ("Energy lost while equipment was down (loss engine, from July 2026).",
                     "Energía perdida con equipo fuera de servicio (motor de pérdidas, desde julio 2026)."),
    "performance": ("Everything else the plant did better or worse than its design for the measured sun: soiling, "
                    "heat, strings, inverters.",
                    "Todo lo demás que la planta hizo mejor o peor que su diseño para el sol medido: suciedad, "
                    "calor, cadenas, inversores."),
    "unsplit": ("Plants without a HelioScope design yet: weather and plant are one step.",
                "Plantas sin diseño HelioScope todavía: clima y planta son un solo paso."),
}


# ------------------------------------------------------------------ language helpers
class Lang:
    """t(en, es) -> html text; svg(en, es) -> text for inside an SVG <text>; code 'web' | 'en' | 'es'."""

    def __init__(self, code: str):
        self.code = code

    def t(self, en: str, es: Optional[str] = None) -> str:
        es = en if es is None else es
        if self.code == "web":
            return (f'<span data-en="{html.escape(en, quote=True)}" data-es="{html.escape(es, quote=True)}">'
                    f'{html.escape(en)}</span>')
        return html.escape(es if self.code == "es" else en)

    def svg(self, en: str, es: Optional[str] = None) -> str:
        es = en if es is None else es
        if self.code == "web":
            return (f'<tspan data-en="{html.escape(en, quote=True)}" data-es="{html.escape(es, quote=True)}">'
                    f'{html.escape(en)}</tspan>')
        return html.escape(es if self.code == "es" else en)

    def month(self, ym: str, short: bool = False) -> str:
        d = dt.date(int(ym[:4]), int(ym[5:7]), 1)
        en = d.strftime("%b %Y" if short else "%B %Y")
        es_m = MESES[d.month - 1]
        es = f"{es_m[:3] if short else es_m} {d.year}"
        return self.t(en, es[:1].upper() + es[1:])

    def month_svg(self, ym: str) -> str:
        d = dt.date(int(ym[:4]), int(ym[5:7]), 1)
        return self.svg(d.strftime("%b %y"), f"{MESES[d.month - 1][:3]} {d.strftime('%y')}")


def web_lang() -> Lang:
    return Lang("web")


def print_lang(code: str) -> Lang:
    return Lang("es" if code == "es" else "en")


# ------------------------------------------------------------------ numbers
def mwh(kwh: Optional[float], d: int = 1) -> str:
    return "-" if kwh is None else f"{kwh / 1000:,.{d}f}"


def mxn(x: Optional[float], signed: bool = False) -> str:
    if x is None:
        return "-"
    s = f"{abs(x):,.0f}"
    return (("+" if x >= 0 else "-") if signed else ("-" if x < 0 else "")) + "$" + s


def pct(x: Optional[float], d: int = 0, signed: bool = False) -> str:
    if x is None:
        return "-"
    v = x * 100
    return (f"{v:+.{d}f}" if signed else f"{v:.{d}f}") + "%"


def delta(x: Optional[float]) -> str:
    """0.85 -> '-15%' (a ratio as a difference to 1)."""
    return "-" if x is None else f"{(x - 1) * 100:+.0f}%"


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def dot(v: Optional[str]) -> str:
    return f'<i class="pmdot {v or "none"}"></i>'


# ------------------------------------------------------------------ charts (inline SVG)
def bridge_svg(port: Dict, L: Lang, unit: str = "kwh", width: int = 980, height: int = 300) -> str:
    """The waterfall from the contract curve to the actual energy (MWh), MXN under each step."""
    steps = [(k, port["bridge_kwh"].get(k), port["bridge_mxn"].get(k)) for k in PM.STEPS[1:-1]
             if port["bridge_kwh"].get(k) is not None and abs(port["bridge_kwh"].get(k)) >= 0.5]
    start = port["contract_kwh"] or 0.0
    end = port["actual_with_contract_kwh"] or 0.0
    bars = [("contract", start, None, None)]
    run = start
    for k, v, m in steps:
        bars.append((k, v, m, run))
        run += v
    bars.append(("actual", end, None, None))
    top = max([start, end] + [b[3] + max(b[1], 0) for b in bars[1:-1]] + [b[3] for b in bars[1:-1]]) * 1.12 or 1.0
    low = min([0.0] + [b[3] + min(b[1], 0) for b in bars[1:-1]])
    pad_l, pad_b, pad_t = 56, 64, 18
    plot_h = height - pad_b - pad_t
    n = len(bars)
    cw = (width - pad_l - 10) / n
    y = lambda v: pad_t + plot_h * (1 - (v - low) / (top - low))                       # noqa: E731
    out = [f'<svg viewBox="0 0 {width} {height}" class="pmsvg" role="img" font-family="Segoe UI,Roboto,Arial" font-size="12.5">']
    for i in range(5):
        v = low + (top - low) * i / 4
        out.append(f'<line x1="{pad_l}" x2="{width - 6}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{COL["grid"]}"/>'
                   f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="{COL["muted"]}">{v / 1000:,.0f}</text>')
    out.append(f'<text x="8" y="10" fill="{COL["muted"]}">MWh</text>')
    prev_top = None
    for i, (k, v, m, base) in enumerate(bars):
        x = pad_l + i * cw + cw * 0.16
        w = cw * 0.68
        if base is None:
            y0, y1, col = y(v), y(low if low < 0 else 0), COL["contract"] if k == "contract" else COL["actual"]
            lab = f"{v / 1000:,.1f}"
        else:
            a, b = base, base + v
            y0, y1 = y(max(a, b)), y(min(a, b))
            col = (COL["weather"] if k == "weather" else COL["customer"] if k == "customer"
                   else COL["pos"] if v >= 0 else COL["neg"])
            lab = f"{v / 1000:+,.1f}"
        out.append(f'<rect x="{x:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{max(1.0, y1 - y0):.1f}" rx="3" fill="{col}"/>')
        if prev_top is not None:
            out.append(f'<line x1="{x - cw * 0.32:.1f}" x2="{x:.1f}" y1="{prev_top:.1f}" y2="{prev_top:.1f}" '
                       f'stroke="{COL["axis"]}" stroke-dasharray="3 3"/>')
        prev_top = y(base + v) if base is not None else y(v)
        out.append(f'<text x="{x + w / 2:.1f}" y="{y0 - 5:.1f}" text-anchor="middle" font-weight="700" fill="{COL["ink"]}">{lab}</text>')
        en, es = STEP_LABEL[k]
        words = _wrap(en, 16), _wrap(es, 16)
        for j in range(max(len(words[0]), len(words[1]))):
            we = words[0][j] if j < len(words[0]) else ""
            ws = words[1][j] if j < len(words[1]) else ""
            out.append(f'<text x="{x + w / 2:.1f}" y="{height - pad_b + 16 + j * 13}" text-anchor="middle" '
                       f'fill="{COL["ink"]}">{L.svg(we, ws)}</text>')
        if m is not None:
            out.append(f'<text x="{x + w / 2:.1f}" y="{height - 8}" text-anchor="middle" fill="{COL["muted"]}">'
                       f'{"+" if m >= 0 else "-"}MXN {abs(m) / 1000:,.0f}k</text>')
    out.append("</svg>")
    return "".join(out)


def _wrap(s: str, n: int) -> List[str]:
    lines, cur = [], ""
    for w in s.split():
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines[:3]


def trend_svg(trend: Sequence[Dict], L: Lang, width: int = 980, height: int = 260) -> str:
    """13 months: actual vs contract (bars, MWh) and EPI / sun index (lines, right axis %)."""
    if not trend:
        return ""
    vals = [max(t["actual_kwh"] or 0, t["contract_kwh"] or 0) for t in trend]
    top = max(vals) * 1.15 or 1.0
    pad_l, pad_r, pad_b, pad_t = 56, 46, 30, 16
    ph = height - pad_b - pad_t
    n = len(trend)
    cw = (width - pad_l - pad_r) / n
    y = lambda v: pad_t + ph * (1 - v / top)                                              # noqa: E731
    r_lo, r_hi = 0.5, 1.3
    yr = lambda v: pad_t + ph * (1 - (min(max(v, r_lo), r_hi) - r_lo) / (r_hi - r_lo))    # noqa: E731
    out = [f'<svg viewBox="0 0 {width} {height}" class="pmsvg" role="img" font-family="Segoe UI,Roboto,Arial" font-size="11">']
    for i in range(5):
        v = top * i / 4
        out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="{COL["grid"]}"/>'
                   f'<text x="{pad_l - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="{COL["muted"]}">{v / 1000:,.0f}</text>')
    for v in (0.6, 0.8, 1.0, 1.2):
        out.append(f'<text x="{width - pad_r + 6}" y="{yr(v) + 4:.1f}" fill="{COL["muted"]}">{v * 100:.0f}%</text>')
    out.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{yr(1.0):.1f}" y2="{yr(1.0):.1f}" stroke="{COL["design"]}" '
               f'stroke-dasharray="2 4" opacity=".5"/>')
    pts_e, pts_s = [], []
    for i, t in enumerate(trend):
        x = pad_l + i * cw
        w = cw * 0.34
        if t["contract_kwh"]:
            out.append(f'<rect x="{x + cw * 0.14:.1f}" y="{y(t["contract_kwh"]):.1f}" width="{w:.1f}" '
                       f'height="{y(0) - y(t["contract_kwh"]):.1f}" rx="2" fill="{COL["contract"]}"/>')
        out.append(f'<rect x="{x + cw * 0.14 + w:.1f}" y="{y(t["actual_kwh"] or 0):.1f}" width="{w:.1f}" '
                   f'height="{y(0) - y(t["actual_kwh"] or 0):.1f}" rx="2" fill="{COL["actual"]}"/>')
        out.append(f'<text x="{x + cw / 2:.1f}" y="{height - 10}" text-anchor="middle" fill="{COL["muted"]}">'
                   f'{L.month_svg(t["month"])}</text>')
        if t.get("epi") is not None:
            pts_e.append(f"{x + cw / 2:.1f},{yr(t['epi']):.1f}")
        if t.get("sun_index") is not None:
            pts_s.append(f"{x + cw / 2:.1f},{yr(t['sun_index']):.1f}")
    for pts, col, dash in ((pts_s, COL["weather"], "5 3"), (pts_e, COL["design"], "")):
        if len(pts) > 1:
            out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="2.2" '
                       f'{f"stroke-dasharray={chr(34)}{dash}{chr(34)}" if dash else ""}/>')
        for p in pts:
            a, b = p.split(",")
            out.append(f'<circle cx="{a}" cy="{b}" r="3" fill="{col}"/>')
    out.append("</svg>")
    return "".join(out)


def hbars_svg(rows: Sequence[Tuple[str, Optional[float], str]], width: int = 460, height: Optional[int] = None,
              lo: float = 0.5, hi: float = 1.3, ref: float = 1.0) -> str:
    """Horizontal ratio bars around a reference line (sun index or EPI per plant). rows: (label html, value, tone)."""
    rh = 26
    height = height or rh * len(rows) + 24
    lab_w, pad_r = 150, 46
    pw = width - lab_w - pad_r
    x = lambda v: lab_w + pw * (min(max(v, lo), hi) - lo) / (hi - lo)                       # noqa: E731
    out = [f'<svg viewBox="0 0 {width} {height}" class="pmsvg" font-family="Segoe UI,Roboto,Arial" font-size="11">']
    for v in (0.6, 0.8, 1.0, 1.2):
        if lo <= v <= hi:
            out.append(f'<line x1="{x(v):.1f}" x2="{x(v):.1f}" y1="4" y2="{height - 18}" stroke="{COL["grid"]}"/>'
                       f'<text x="{x(v):.1f}" y="{height - 4}" text-anchor="middle" fill="{COL["muted"]}">{v * 100:.0f}%</text>')
    for i, (lab, v, tone) in enumerate(rows):
        yy = 6 + i * rh
        out.append(f'<text x="{lab_w - 8}" y="{yy + 14}" text-anchor="end" fill="{COL["ink"]}">{lab}</text>')
        if v is None:
            out.append(f'<text x="{lab_w + 4}" y="{yy + 14}" fill="{COL["muted"]}">-</text>')
            continue
        a, b = sorted((x(ref), x(v)))
        out.append(f'<rect x="{a:.1f}" y="{yy + 3}" width="{max(1.5, b - a):.1f}" height="16" rx="3" fill="{COL.get(tone, COL["actual"])}"/>'
                   f'<text x="{(b + 4) if v >= ref else (a - 4):.1f}" y="{yy + 15}" text-anchor="{"start" if v >= ref else "end"}" '
                   f'font-weight="700" fill="{COL["ink"]}">{v * 100:.0f}%</text>')
    out.append(f'<line x1="{x(ref):.1f}" x2="{x(ref):.1f}" y1="2" y2="{height - 16}" stroke="{COL["design"]}" stroke-width="1.5"/>')
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ the body
CSS = """
.pm{--pm-teal:#05b1a9;--pm-deep:#053b38;--pm-line:#e3e6ea;--pm-muted:#6b7480;--pm-ink:#1a1d23}
.pm .pmhero{display:flex;flex-wrap:wrap;gap:18px;align-items:flex-start;justify-content:space-between}
.pm .pmkick{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#05847d;font-weight:800}
.pm h1{font-size:32px;line-height:1.1;margin:4px 0 6px;color:var(--pm-deep);font-weight:800}
.pm .pmlead{font-size:15.5px;line-height:1.55;color:#2b3038;max-width:860px;margin:8px 0 0}
.pm .pmstat{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:700}
.pm .pmstat.pub{background:#e6f7f5;color:#05847d}.pm .pmstat.draft{background:#fff4e0;color:#b26a00}.pm .pmstat.late{background:#fdeaea;color:#c2554e}
.pm .pmdl{display:flex;gap:8px;flex-wrap:wrap}
.pm .pmdl a,.pm .pmdl button{background:#fff;border:1px solid #d2d7dd;border-radius:8px;padding:8px 13px;font:600 13px inherit;font-family:inherit;color:#41474f;cursor:pointer}
.pm .pmdl a.prim{background:var(--pm-teal);border-color:var(--pm-teal);color:var(--pm-deep)}
.pm .pmtiles{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:12px;margin:20px 0}
.pm .pmtile{background:#fff;border:1px solid var(--pm-line);border-radius:12px;padding:13px 15px;display:flex;flex-direction:column;gap:3px}
.pm .pmtile.good{background:#e6f7f5;border-color:#b8e6e1}.pm .pmtile.warn{background:#fff4e0;border-color:#f3dcae}.pm .pmtile.bad{background:#fdeaea;border-color:#f3b9b9}
.pm .pmtile .l{font-size:11.5px;color:var(--pm-muted);font-weight:700}.pm .pmtile .v{font-size:23px;font-weight:800;color:var(--pm-deep);line-height:1.15}
.pm .pmtile .s{font-size:11.5px;color:#41474f}
.pm .pmcard{background:#fff;border:1px solid var(--pm-line);border-radius:12px;padding:16px 18px;margin:0 0 14px}
.pm .pmcard h2{font-size:15px;margin:0 0 4px;font-weight:800;color:var(--pm-deep)}
.pm .pmcard .sub{font-size:12.5px;color:var(--pm-muted);margin:0 0 10px}
.pm .pmgrid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.pm table.pmt{width:100%;border-collapse:collapse;font-size:12.5px}
.pm table.pmt th{text-align:right;font-size:11px;color:var(--pm-muted);font-weight:700;padding:6px 8px;border-bottom:1px solid var(--pm-line);white-space:nowrap}
.pm table.pmt th:first-child,.pm table.pmt td:first-child{text-align:left}
.pm table.pmt td{text-align:right;padding:7px 8px;border-bottom:1px solid #f0f2f4;white-space:nowrap}
.pm table.pmt td.wrap{white-space:normal;text-align:left}
.pm table.pmt tr.tot td{font-weight:800;border-top:2px solid var(--pm-line)}
.pm .pmdot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#d2d7dd;margin:0 2px;vertical-align:-1px}
.pm .pmdot.good{background:#05b1a9}.pm .pmdot.warn{background:#f0a83b}.pm .pmdot.bad{background:#e0645b}
.pm .pmsvg{width:100%;height:auto;display:block}
.pm .pmlegend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:#41474f;margin:6px 0 0}
.pm .pmlegend i{display:inline-block;width:11px;height:11px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.pm .pmsteps{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 18px;margin-top:10px;font-size:12.5px;color:#41474f}
.pm .pmsteps b{color:var(--pm-deep)}
.pm .pmnote{font-size:12px;color:var(--pm-muted);margin:8px 0 0}
.pm .pmplant{font-weight:700;color:var(--pm-ink)}
.pm .neg{color:#c2554e}.pm .pos{color:#05847d}
.pm .pmlist{margin:6px 0 0;padding-left:18px;font-size:12.5px;color:#2b3038}
@media(max-width:1000px){.pm .pmtiles{grid-template-columns:repeat(3,minmax(0,1fr))}.pm .pmgrid2,.pm .pmsteps{grid-template-columns:1fr}}
@media(max-width:640px){.pm .pmtiles{grid-template-columns:repeat(2,minmax(0,1fr))}.pm .pmcard{overflow-x:auto}}
"""

PRINT_CSS = """
@page{size:A4 landscape;margin:11mm 12mm 12mm}
html{-webkit-print-color-adjust:exact;print-color-adjust:exact}
body{margin:0;font:11.5px/1.45 "Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:#1a1d23;background:#fff}
.pm .pmtiles{grid-template-columns:repeat(6,minmax(0,1fr))!important}
.pm .pmgrid2{grid-template-columns:1fr 1fr!important}
.pm .pmsteps{grid-template-columns:repeat(3,minmax(0,1fr))!important}
.pm .pmcard{break-inside:avoid;padding:12px 14px}
.pm .pb{break-before:page}
.pm .pmdl,.pm .noprint{display:none!important}
.pm h1{font-size:26px}
.pm .pmsvg{max-height:62mm}
.pm .pmbridge .pmsvg{max-height:50mm}
.pm .pmtiles{margin:12px 0;gap:8px}.pm .pmtile{padding:9px 11px}.pm .pmtile .v{font-size:19px}
.pm .pmlead{font-size:13px;margin-top:4px}.pm h1{margin:2px 0 4px}
.pm .pmsteps{grid-template-columns:repeat(3,minmax(0,1fr))!important}
.pm .pmsteps{font-size:10.5px;gap:4px 14px}
.pm table.pmt{font-size:11px}.pm table.pmt td{padding:5px 7px}
.pmfoot{font-size:9.5px;color:#6b7480;margin-top:10px;border-top:1px solid #e3e6ea;padding-top:6px;display:flex;justify-content:space-between}
"""


def status_pill(rep: Dict, L: Lang, today: str) -> str:
    pub = rep.get("published")
    if pub:
        return (f'<span class="pmstat pub">{L.t("Published", "Publicado")} {esc(pub["utc"][:10])} · {esc(pub["by"])}'
                f'{" · " + L.t("after the day-10 deadline", "después del día 10") if pub.get("on_time") is False else ""}</span>')
    if not rep["complete"]:
        return f'<span class="pmstat draft">{L.t("Month to date - draft", "Mes en curso - borrador")}</span>'
    if not PM.deadline_applies(rep["month"]):
        return f'<span class="pmstat draft">{L.t("Draft - not yet published", "Borrador - aún no publicado")}</span>'
    late = today > rep["deadline"]
    return (f'<span class="pmstat {"late" if late else "draft"}">{L.t("Draft", "Borrador")} · '
            f'{L.t("publish by", "publicar antes del")} {esc(rep["deadline"])}'
            f'{" · " + L.t("overdue", "vencido") if late else ""}</span>')


def tiles(rep: Dict, L: Lang) -> str:
    P = rep["portfolio"]
    loss = P.get("loss") or {}
    rev = P["revenue_invoiced_mxn"] if P["invoiced_plants"] == P["plants"] else P["revenue_earned_mxn"]
    rev_lab = (L.t("Revenue invoiced", "Ingreso facturado") if P["invoiced_plants"] == P["plants"]
               else L.t("Revenue earned", "Ingreso devengado"))
    cells = [
        ("good" if (P["promise"] or 0) >= PM.PROMISE_BANDS[0] else "warn" if (P["promise"] or 0) >= PM.PROMISE_BANDS[1] else "bad",
         L.t("Energy delivered", "Energía entregada"), f'{mwh(P["actual_kwh"])} <small>MWh</small>',
         L.t(f'{delta(P["promise"])} vs contract', f'{delta(P["promise"])} vs contrato')),
        (PM.band(ratio_or(rev, P["budget_mxn"]), PM.PROMISE_BANDS) or "", rev_lab, mxn(rev),
         L.t(f'budget {mxn(P["budget_mxn"])}', f'presupuesto {mxn(P["budget_mxn"])}')),
        ("", L.t("Sun vs typical", "Sol vs típico"), delta(P["sun_index"]),
         L.t("measured irradiance vs design year", "irradiancia medida vs año de diseño")),
        (PM.band(P["epi"], PM.ENGINEERING_BANDS) or "", L.t("Performance vs design", "Desempeño vs diseño"),
         delta(P["epi"]), L.t(f'for the measured sun · {P["epi_plants"]} plants', f'para el sol medido · {P["epi_plants"]} plantas')),
        (PM.band(P["availability"], PM.AVAILABILITY_BANDS) or "", L.t("Availability", "Disponibilidad"),
         pct(P["availability"], 1), L.t("customer stops excused", "paros del cliente excluidos")),
        ("bad" if (loss.get("lost_mxn") or 0) > 0.03 * (P["revenue_earned_mxn"] or 1) else "",
         L.t("Recoverable losses", "Pérdidas recuperables"), mxn(loss.get("lost_mxn")) if loss else "-",
         L.t(f'{mwh(loss.get("lost_kwh"))} MWh', f'{mwh(loss.get("lost_kwh"))} MWh') if loss else L.t("loss engine from Jul 2026", "motor de pérdidas desde jul 2026")),
    ]
    return '<div class="pmtiles">' + "".join(
        f'<div class="pmtile {tone}"><span class="l">{lab}</span><span class="v">{val}</span><span class="s">{sub}</span></div>'
        for tone, lab, val, sub in cells) + "</div>"


def ratio_or(a, b):
    return a / b if a is not None and b else None


def plants_table(rep: Dict, L: Lang) -> str:
    head = [L.t("Plant", "Planta"), "MWh", L.t("vs contract", "vs contrato"), L.t("Sun", "Sol"),
            L.t("vs design", "vs diseño"), L.t("Availability", "Disponib."), L.t("Lost MXN", "Pérdida MXN"),
            L.t("Revenue MXN", "Ingreso MXN"), "CO2 t", L.t("Promise · design · avail.", "Promesa · diseño · disp.")]
    rows = []
    for p in rep["plants"]:
        v = p["verdict"]
        rev = p["revenue_invoiced_mxn"] if p["revenue_invoiced_mxn"] is not None else p["revenue_earned_mxn"]
        rows.append("<tr>" + "".join([
            f'<td><span class="pmplant">{esc(p["name"])}</span> <span class="pmnote">{p["kwp_dc"] or 0:,.0f} kWp</span></td>',
            f'<td>{mwh(p["actual_kwh"])}</td>', f'<td class="{_cls(p["promise"])}">{delta(p["promise"])}</td>',
            f'<td>{delta(p["sun_index"])}</td>', f'<td class="{_cls(p["epi"])}">{delta(p["epi"])}</td>',
            f'<td>{pct(p["availability"], 1)}</td>', f'<td>{mxn((p["loss"] or {}).get("lost_mxn")) if p["loss"] else "-"}</td>',
            f'<td>{mxn(rev)}</td>', f'<td>{p["co2_t"]:,.1f}</td>',
            f'<td>{dot(v["promise"])}{dot(v["engineering"])}{dot(v["availability"])}</td>']) + "</tr>")
    P = rep["portfolio"]
    rev = P["revenue_invoiced_mxn"] if P["invoiced_plants"] == P["plants"] else P["revenue_earned_mxn"]
    rows.append('<tr class="tot">' + "".join([
        f'<td>{L.t("Portfolio", "Portafolio")} <span class="pmnote">{P["kwp_dc"] or 0:,.0f} kWp</span></td>',
        f'<td>{mwh(P["actual_kwh"])}</td>', f'<td class="{_cls(P["promise"])}">{delta(P["promise"])}</td>',
        f'<td>{delta(P["sun_index"])}</td>', f'<td class="{_cls(P["epi"])}">{delta(P["epi"])}</td>',
        f'<td>{pct(P["availability"], 1)}</td>', f'<td>{mxn((P["loss"] or {}).get("lost_mxn")) if P["loss"] else "-"}</td>',
        f'<td>{mxn(rev)}</td>', f'<td>{P["co2_t"] or 0:,.1f}</td>', "<td></td>"]) + "</tr>")
    return ('<table class="pmt"><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _cls(r: Optional[float]) -> str:
    return "" if r is None else "pos" if r >= 1 else "neg"


def money_table(rep: Dict, L: Lang) -> str:
    head = [L.t("Plant", "Planta"), L.t("Tariff", "Tarifa"), L.t("Budget", "Presupuesto"),
            L.t("Earned", "Devengado"), L.t("Invoiced", "Facturado"), L.t("vs budget", "vs presupuesto"),
            L.t("Compensated kWh", "kWh compensados"), L.t("Unavailability", "Indisponibilidad"),
            L.t("Heat", "Calor"), L.t("Under-performance", "Bajo desempeño")]
    rows = []
    for p in rep["plants"]:
        lo = p["loss"] or {}
        t = p["tariff"] or 0
        got = p["revenue_invoiced_mxn"] if p["revenue_invoiced_mxn"] is not None else p["revenue_earned_mxn"]
        rows.append("<tr>" + "".join([
            f'<td class="pmplant">{esc(p["name"])}</td>', f'<td>{t:.4f}</td>', f'<td>{mxn(p["budget_mxn"])}</td>',
            f'<td>{mxn(p["revenue_earned_mxn"])}</td>',
            f'<td>{mxn(p["revenue_invoiced_mxn"]) if p["revenue_invoiced_mxn"] is not None else L.t("not yet", "aún no")}</td>',
            f'<td class="{_cls(ratio_or(got, p["budget_mxn"]))}">{mxn((got or 0) - (p["budget_mxn"] or 0), True) if p["budget_mxn"] else "-"}</td>',
            f'<td>{p["compensated_kwh"]:,.0f}</td>',
            f'<td>{mxn(lo.get("unavailability_kwh", 0) * t) if lo else "-"}</td>',
            f'<td>{mxn(lo.get("overheating_kwh", 0) * t) if lo else "-"}</td>',
            f'<td>{mxn(lo.get("underperformance_kwh", 0) * t) if lo else "-"}</td>']) + "</tr>")
    return ('<table class="pmt"><thead><tr>' + "".join(f"<th>{h}</th>" for h in head) + "</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def ops_section(rep: Dict, L: Lang) -> str:
    eps = [(p["name"], e) for p in rep["plants"] for e in p["episodes"]]
    evs = [(p["name"], e) for p in rep["plants"] for e in p["events"]]
    tk = rep["tickets"]
    ep_rows = "".join(
        f'<tr><td class="pmplant">{esc(n)}</td><td>{esc(e["start"])}</td><td>{esc(e["end"])}</td><td>{e["days"]}</td>'
        f'<td>{pct(e["min"], 0)}</td><td>{mwh(e["unavailability_kwh"], 2) if e["unavailability_kwh"] is not None else "-"}</td></tr>'
        for n, e in eps) or f'<tr><td colspan="6" class="wrap">{L.t("No day below the availability target.", "Ningún día bajo la meta de disponibilidad.")}</td></tr>'
    ev_rows = "".join(
        f'<tr><td class="pmplant">{esc(n)}</td><td>{esc(e["start"])}</td><td>{esc(e["end"] or "")}</td>'
        f'<td>{L.t("Customer", "Cliente") if e["category"] == "customer" else L.t("ARGIA", "ARGIA")}</td>'
        f'<td class="wrap">{esc(e["note"])}</td></tr>' for n, e in evs) or \
        f'<tr><td colspan="5" class="wrap">{L.t("No plant events logged.", "Sin eventos de planta registrados.")}</td></tr>'
    open_rows = "".join(
        f'<tr><td>{esc(t["number"])}</td><td>{esc(t["priority"])}</td><td>{esc(t["status"])}</td><td>{esc(t["created"])}</td>'
        f'<td class="wrap">{esc(t["title"])}</td></tr>' for t in tk["open_list"]) or \
        f'<tr><td colspan="5" class="wrap">{L.t("No open ticket at month end.", "Ningún ticket abierto al cierre.")}</td></tr>'
    resp = "-" if tk["median_response_h"] is None else f'{tk["median_response_h"]:.1f} h'
    return f"""
<div class="pmgrid2">
 <div class="pmcard"><h2>{L.t("Days below the availability target", "Días bajo la meta de disponibilidad")}</h2>
  <p class="sub">{L.t("Consecutive days under each plant's target (98% unless the contract says otherwise). Customer stops are excused.",
                     "Días seguidos bajo la meta de cada planta (98% salvo que el contrato diga otra cosa). Los paros del cliente se excluyen.")}</p>
  <table class="pmt"><thead><tr><th>{L.t("Plant", "Planta")}</th><th>{L.t("From", "Desde")}</th><th>{L.t("To", "Hasta")}</th>
  <th>{L.t("Days", "Días")}</th><th>{L.t("Lowest", "Mínimo")}</th><th>{L.t("Lost MWh", "MWh perdidos")}</th></tr></thead><tbody>{ep_rows}</tbody></table></div>
 <div class="pmcard"><h2>{L.t("Plant events", "Eventos de planta")}</h2>
  <p class="sub">{L.t("Logged stops: customer maintenance (compensated on the invoice) and ARGIA interventions.",
                     "Paros registrados: mantenimiento del cliente (compensado en la factura) e intervenciones de ARGIA.")}</p>
  <table class="pmt"><thead><tr><th>{L.t("Plant", "Planta")}</th><th>{L.t("From", "Desde")}</th><th>{L.t("To", "Hasta")}</th>
  <th>{L.t("Who", "Quién")}</th><th>{L.t("Note", "Nota")}</th></tr></thead><tbody>{ev_rows}</tbody></table></div>
</div>
<div class="pmcard pb"><h2>{L.t("Maintenance tickets", "Tickets de mantenimiento")}</h2>
 <p class="sub">{L.t(f'Opened {tk["opened"]} · resolved {tk["resolved"]} · open at month end {tk["open_at_end"]} · median time to start work {resp}',
                    f'Abiertos {tk["opened"]} · resueltos {tk["resolved"]} · abiertos al cierre {tk["open_at_end"]} · mediana hasta iniciar trabajo {resp}')}</p>
 <table class="pmt"><thead><tr><th>{L.t("Ticket", "Ticket")}</th><th>{L.t("Priority", "Prioridad")}</th><th>{L.t("Status", "Estado")}</th>
 <th>{L.t("Opened", "Abierto")}</th><th>{L.t("What", "Qué")}</th></tr></thead><tbody>{open_rows}</tbody></table></div>"""


def quality_section(rep: Dict, L: Lang) -> str:
    rows = []
    for p in rep["plants"]:
        sc = p.get("sensor_check")
        notes = "".join(f"<li>{L.t(en, es)}</li>" for en, es in p["notes"])
        rows.append(
            f'<tr><td class="pmplant">{esc(p["name"])}</td><td>{p["data_days"]} / {p["days"]}</td><td>{p["sun_days"]}</td>'
            f'<td class="wrap">{esc(p["design_source"]) if p["design_source"] else L.t("none yet", "aún no")}</td>'
            f'<td>{_sensor(sc)}</td>'
            f'<td class="wrap"><ul class="pmlist">{notes}</ul></td></tr>')
    return f"""<div class="pmcard"><h2>{L.t("Data quality and caveats", "Calidad de datos y salvedades")}</h2>
 <p class="sub">{L.t("Days with production data, days with a sun reading, the design used, the pyranometer check against satellite, and what to keep in mind.",
                    "Días con datos de producción, días con lectura de sol, el diseño usado, la revisión del piranómetro contra satélite y qué tener en cuenta.")}</p>
 <table class="pmt"><thead><tr><th>{L.t("Plant", "Planta")}</th><th>{L.t("Data days", "Días con datos")}</th><th>{L.t("Sun days", "Días con sol")}</th>
 <th>{L.t("Design", "Diseño")}</th><th>{L.t("Sensor check", "Revisión sensor")}</th><th>{L.t("Notes", "Notas")}</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>"""


def _sensor(sc: Optional[Dict]) -> str:
    if not sc:
        return "-"
    drift = sc.get("drift_pct")
    return esc(sc.get("status") or "") + (f" {drift:+.1f}%" if drift is not None else "")


def method_section(L: Lang) -> str:
    items = [
        ("Actual: gross production per day (energy_kwh), the billing basis. Revenue earned adds the energy "
         "compensated for customer stops, at the PPA tariff of the month.",
         "Real: producción bruta por día (energy_kwh), la base de facturación. El ingreso devengado suma la energía "
         "compensada por paros del cliente, a la tarifa PPA del mes."),
        ("Contract curve: the monthly kWh in each customer's contract, prorated to the days with data. It is the "
         "promise and the business case.",
         "Curva de contrato: los kWh mensuales del contrato de cada cliente, prorrateados a los días con datos. "
         "Es la promesa y el caso de negocio."),
        ("Design: the HelioScope as-connected run of each plant (Nov 2026), aged 0.55% a year from interconnection. "
         "'For the measured sun' scales each day by the irradiance the plant's own sensor measured.",
         "Diseño: la corrida HelioScope tal como se conectó cada planta (nov 2026), envejecida 0.55% al año desde la "
         "interconexión. 'Para el sol medido' escala cada día con la irradiancia que midió el sensor de la planta."),
        ("Sun vs typical: measured irradiance against the design year's irradiance on the same plane for the same days.",
         "Sol vs típico: irradiancia medida contra la del año de diseño en el mismo plano y los mismos días."),
        ("Performance vs design (EPI): actual over design for the measured sun, on days with both readings. Within "
         "3% is in line.",
         "Desempeño vs diseño (EPI): real sobre diseño para el sol medido, en días con ambas lecturas. Dentro de 3% "
         "está en línea."),
        ("Availability and losses: the monitoring's daily availability and loss engine (from July 2026). Lost MXN "
         "is lost energy at the PPA tariff.",
         "Disponibilidad y pérdidas: la disponibilidad diaria y el motor de pérdidas del monitoreo (desde julio 2026). "
         "Las pérdidas MXN son energía perdida a la tarifa PPA."),
        ("A draft changes as data arrives. A published month is frozen: the page, the PDF and the Excel keep the "
         "numbers of the day it was published.",
         "Un borrador cambia al llegar datos. Un mes publicado queda congelado: la página, el PDF y el Excel "
         "conservan las cifras del día en que se publicó."),
    ]
    return (f'<div class="pmcard"><h2>{L.t("How the numbers are made", "Cómo se calculan las cifras")}</h2>'
            f'<ul class="pmlist">{"".join(f"<li>{L.t(en, es)}</li>" for en, es in items)}</ul></div>')


def body(rep: Dict, L: Lang, today: str, links: Optional[Dict[str, str]] = None, publish_form: str = "") -> str:
    """The report. links: {'pdf_en', 'pdf_es', 'xlsx', 'index', 'prev', 'next'} (web only)."""
    P = rep["portfolio"]
    ym = rep["month"]
    links = links or {}
    dl = ""
    if links:
        dl = '<div class="pmdl noprint">' + "".join(
            f'<a class="{c}" href="{esc(links[k])}"{" download" if k != "index" else ""}>{lab}</a>'
            for k, c, lab in (("pdf_en", "prim", "PDF (English)"), ("pdf_es", "prim", "PDF (español)"),
                              ("xlsx", "", L.t("Excel with every number", "Excel con todas las cifras")),
                              ("index", "", L.t("All months", "Todos los meses"))) if links.get(k)) + publish_form + "</div>"
    nav = ""
    if links.get("prev") or links.get("next"):
        nav = ('<div class="pmnote noprint" style="margin-top:6px">'
               + (f'<a href="{esc(links["prev"])}">‹ {L.t("previous month", "mes anterior")}</a>' if links.get("prev") else "")
               + (" · " if links.get("prev") and links.get("next") else "")
               + (f'<a href="{esc(links["next"])}">{L.t("next month", "mes siguiente")} ›</a>' if links.get("next") else "")
               + "</div>")
    head = rep["headline"]
    lead = L.t(head["en"], head["es"])
    ytd, life = rep.get("ytd") or {}, rep.get("lifetime") or {}
    steps = "".join(
        f'<div><b>{L.t(*STEP_LABEL[k])}</b> {_signed_mwh(P["bridge_kwh"].get(k))} · {mxn(P["bridge_mxn"].get(k), True)}<br>'
        f'<span class="pmnote">{L.t(*STEP_WHY[k])}</span></div>'
        for k in PM.STEPS[1:-1] if P["bridge_kwh"].get(k) is not None)
    sun_rows = [(esc(p["name"]), p["sun_index"], "weather") for p in rep["plants"]]
    epi_rows = [(esc(p["name"]), p["epi"], p["verdict"]["engineering"] or "contract") for p in rep["plants"]]
    return f"""<div class="pm">
<div class="pmhero">
 <div>
  <div class="pmkick">{L.t("ARGIA PPA portfolio · monthly performance report", "Portafolio PPA ARGIA · reporte mensual de desempeño")}</div>
  <h1>{L.month(ym)}</h1>
  {status_pill(rep, L, today)}
  <p class="pmlead">{lead}</p>
  {nav}
 </div>
 {dl}
</div>
{tiles(rep, L)}
<div class="pmcard pmbridge"><h2>{L.t("From the promise to the result", "De la promesa al resultado")}</h2>
 <p class="sub">{L.t("Contract curve to actual energy, step by step (MWh; MXN at each plant's PPA tariff). Only plants with a contract curve.",
                    "De la curva de contrato a la energía real, paso a paso (MWh; MXN a la tarifa PPA de cada planta). Solo plantas con curva de contrato.")}</p>
 {bridge_svg(P, L)}
 <div class="pmsteps">{steps}</div>
</div>
<div class="pmcard pb"><h2>{L.t("Plant by plant", "Planta por planta")}</h2>
 <p class="sub">{L.t("Dots: the promise (actual vs contract), the engineering (vs design for the measured sun), availability. Green on target, amber watch, red act.",
                    "Puntos: la promesa (real vs contrato), la ingeniería (vs diseño para el sol medido), disponibilidad. Verde en meta, ámbar vigilar, rojo actuar.")}</p>
 {plants_table(rep, L)}
</div>
<div class="pmgrid2">
 <div class="pmcard"><h2>{L.t("Weather: sun vs a typical month", "Clima: sol vs un mes típico")}</h2>
  <p class="sub">{L.t("Below 100% the sun was weaker than the design year. This is the part of the result nobody controls.",
                     "Bajo 100% el sol fue más débil que el año de diseño. Es la parte del resultado que nadie controla.")}</p>
  {hbars_svg(sun_rows)}</div>
 <div class="pmcard"><h2>{L.t("Engineering: actual vs design for the measured sun", "Ingeniería: real vs diseño para el sol medido")}</h2>
  <p class="sub">{L.t("Weather removed. 100% means the plant produced exactly what its HelioScope design predicts for the sun it got.",
                     "Sin el clima. 100% significa que la planta produjo exactamente lo que su diseño HelioScope predice para el sol que tuvo.")}</p>
  {hbars_svg(epi_rows)}</div>
</div>
<div class="pmcard pb"><h2>{L.t("Money", "Dinero")}</h2>
 <p class="sub">{L.t("Budget = contract curve x tariff. Earned = billable kWh (production + compensated customer stops) x tariff; invoiced = the published invoice. Losses priced at the tariff.",
                    "Presupuesto = curva de contrato x tarifa. Devengado = kWh facturables (producción + paros del cliente compensados) x tarifa; facturado = la factura publicada. Pérdidas a la tarifa.")}</p>
 {money_table(rep, L)}
</div>
{ops_section(rep, L)}
<div class="pmcard"><h2>{L.t("The last 13 months", "Los últimos 13 meses")}</h2>
 <p class="sub">{L.t("Bars: contract curve and actual energy (MWh). Lines: performance vs design (solid) and sun vs typical (dashed), right axis.",
                    "Barras: curva de contrato y energía real (MWh). Líneas: desempeño vs diseño (continua) y sol vs típico (punteada), eje derecho.")}</p>
 {trend_svg(rep["trend"], L)}
 <div class="pmlegend"><span><i style="background:{COL["contract"]}"></i>{L.t("Contract curve", "Curva de contrato")}</span>
 <span><i style="background:{COL["actual"]}"></i>{L.t("Actual", "Real")}</span>
 <span><i style="background:{COL["design"]}"></i>{L.t("Performance vs design", "Desempeño vs diseño")}</span>
 <span><i style="background:{COL["weather"]}"></i>{L.t("Sun vs typical", "Sol vs típico")}</span></div>
</div>
<div class="pmgrid2 pb">
 <div class="pmcard"><h2>{L.t("Year to date", "En el año")} <span class="pmnote">{esc(ytd.get("from", ""))} - {esc(ytd.get("to", ""))}</span></h2>
  {_period_table(ytd, L)}</div>
 <div class="pmcard"><h2>{L.t("Since the first PPA month", "Desde el primer mes PPA")} <span class="pmnote">{esc(life.get("from", ""))} - {esc(life.get("to", ""))}</span></h2>
  {_period_table(life, L)}</div>
</div>
{quality_section(rep, L)}
{method_section(L)}
</div>"""


def _signed_mwh(kwh: Optional[float]) -> str:
    return "-" if kwh is None else f"{kwh / 1000:+,.1f} MWh"


def _period_table(p: Dict, L: Lang) -> str:
    if not p:
        return f'<p class="pmnote">{L.t("No data.", "Sin datos.")}</p>'
    loss = p.get("loss") or {}
    rows = [(L.t("Energy delivered", "Energía entregada"), f'{mwh(p["actual_kwh"])} MWh'),
            (L.t("Contract curve", "Curva de contrato"), f'{mwh(p["contract_kwh"])} MWh'),
            (L.t("Actual vs contract", "Real vs contrato"), delta(p["promise"])),
            (L.t("Performance vs design", "Desempeño vs diseño"), delta(p["epi"])),
            (L.t("Revenue earned", "Ingreso devengado"), mxn(p["revenue_earned_mxn"])),
            (L.t("Budget", "Presupuesto"), mxn(p["budget_mxn"])),
            (L.t("Weather effect", "Efecto clima"), mxn((p.get("bridge_mxn") or {}).get("weather"), True)),
            (L.t("Recoverable losses", "Pérdidas recuperables"), mxn(loss.get("lost_mxn")) if loss else "-"),
            (L.t("CO2 avoided", "CO2 evitado"), f'{p["co2_t"] or 0:,.0f} t')]
    return '<table class="pmt"><tbody>' + "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in rows) + "</tbody></table>"


def print_document(rep: Dict, code: str, today: str) -> str:
    """A standalone HTML document for headless Chromium -> PDF (one language)."""
    L = print_lang(code)
    title = f"ARGIA PPA {rep['month']}"
    foot = (f'<div class="pmfoot pm"><span>ARGIA México · {L.t("PPA monthly performance report", "Reporte mensual de desempeño PPA")} · '
            f'{esc(rep["month"])}</span><span>{L.t("Generated", "Generado")} {esc(rep.get("generated_utc", "")[:16])} UTC'
            f'{" · " + L.t("published", "publicado") + " " + esc(rep["published"]["utc"][:10]) if rep.get("published") else " · " + L.t("draft", "borrador")}</span></div>')
    return (f'<!doctype html><html lang="{"es" if code == "es" else "en"}"><head><meta charset="utf-8"><title>{esc(title)}</title>'
            f"<style>{CSS}{PRINT_CSS}</style></head><body>{body(rep, L, today)}{foot}</body></html>")


# ------------------------------------------------------------------ the month index
def index_body(cards: Sequence[Dict], L: Lang, today: str) -> str:
    """cards: [{'month', 'href', 'status' ('published'|'draft'|'late'|'mtd'), 'actual_kwh', 'promise', 'epi',
    'sun_index', 'lost_mxn'}], newest first."""
    years: Dict[str, List[Dict]] = {}
    for c in cards:
        years.setdefault(c["month"][:4], []).append(c)
    lab = {"published": ("Published", "Publicado", "pub"), "draft": ("Draft", "Borrador", "draft"),
           "late": ("Overdue", "Vencido", "late"), "mtd": ("Month to date", "Mes en curso", "draft")}
    out = []
    for y, cs in years.items():
        rows = "".join(
            f'<tr><td><a href="{esc(c["href"])}" class="pmplant">{L.month(c["month"])}</a></td>'
            f'<td><span class="pmstat {lab[c["status"]][2]}">{L.t(lab[c["status"]][0], lab[c["status"]][1])}</span></td>'
            f'<td>{mwh(c["actual_kwh"])}</td><td class="{_cls(c["promise"])}">{delta(c["promise"])}</td>'
            f'<td>{delta(c["sun_index"])}</td><td class="{_cls(c["epi"])}">{delta(c["epi"])}</td>'
            f'<td>{mxn(c["lost_mxn"]) if c["lost_mxn"] is not None else "-"}</td>'
            f'<td><a href="{esc(c["href"])}report-en.pdf">PDF</a> · <a href="{esc(c["href"])}report-es.pdf">PDF ES</a> · '
            f'<a href="{esc(c["href"])}report.xlsx">Excel</a></td></tr>' for c in cs)
        out.append(f'<div class="pmcard"><h2>{esc(y)}</h2><table class="pmt"><thead><tr><th>{L.t("Month", "Mes")}</th>'
                   f'<th>{L.t("Status", "Estado")}</th><th>MWh</th><th>{L.t("vs contract", "vs contrato")}</th>'
                   f'<th>{L.t("Sun", "Sol")}</th><th>{L.t("vs design", "vs diseño")}</th>'
                   f'<th>{L.t("Lost MXN", "Pérdida MXN")}</th><th>{L.t("Download", "Descargar")}</th></tr></thead>'
                   f"<tbody>{rows}</tbody></table></div>")
    return f"""<div class="pm">
<div class="pmkick">{L.t("ARGIA PPA portfolio", "Portafolio PPA ARGIA")}</div>
<h1>{L.t("PPA monthly performance report", "Reporte mensual de desempeño PPA")}</h1>
<p class="pmlead">{L.t("One report per month for the whole PPA portfolio: what we promised, what the weather gave, what the plants made of it, what it earned and what it cost. A month is a draft until ARGIA publishes it; published months never change. Each month downloads as PDF (English, español) and as Excel with every number.",
                       "Un reporte por mes para todo el portafolio PPA: lo que prometimos, lo que dio el clima, lo que hicieron las plantas con ello, lo que se ganó y lo que costó. Un mes es borrador hasta que ARGIA lo publica; los meses publicados no cambian. Cada mes se descarga en PDF (English, español) y en Excel con todas las cifras.")}</p>
<div style="height:14px"></div>
{"".join(out)}
</div>"""
