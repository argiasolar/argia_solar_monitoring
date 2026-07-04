"""Daily performance report — HTML generator (report family, part 1).

Renders one day of the fleet as a self-contained HTML document: semaphore
rail, production-vs-theoretical chart, alerts with plain-language
explanations, and per-plant sections (facts, per-inverter specific-yield
chart with peer median, inverter table with flags).

Design contract, kept deliberately honest:
- Every number comes from KPI_Daily / Alerts / telemetry — the report
  COMPOSES, it never recomputes plant-level metrics differently from the
  pipeline (one truth, two renderings).
- Per-inverter "theoretical" is the plant theoretical split by nameplate
  share, and the footer says so — one method, stated, which is what fixes
  the old report's contradictory-percentage bug.
- Semaphore and flag logic mirror the alert engine's severity bands.

Pure parts (semaphores, allocation, SVG, HTML) are unit-tested; the
``build_report_data`` function does the sheet I/O.
"""

from __future__ import annotations

import datetime as dt
import html as _html
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from argia.analytics.acute import TEMP_CRIT_C, TEMP_WARN_C
from argia.analytics.inverter_health import (
    InverterReading,
    evaluate_inverter_relative,
)
from argia.analytics.vendor_flags import fault_tokens
from argia.archive.kpi_daily import KPI_DAILY_TAB
from argia.core.alerts_state import AlertRecord, load_alerts_ledger
from argia.core.config import Portfolio
from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.kpi import compute_plant_energy, read_day_bundle
from argia.kpi.reconcile import date_key

LOG = logging.getLogger("argia.report.daily")

GREEN, AMBER, RED, GRAY = "green", "amber", "red", "gray"
COLORS = {GREEN: "#1F7A4D", AMBER: "#C77E00", RED: "#B3372B", GRAY: "#8A938F"}


# ---------------------------------------------------------------- data model

@dataclass
class InverterDay:
    sn: str
    label: str
    kwh: float
    rated_kw: Optional[float]
    tmax_c: Optional[float]
    faults: List[str] = field(default_factory=list)
    rel: Optional[Tuple[str, float]] = None    # (severity, ratio) vs peers


@dataclass
class PlantDay:
    plant_key: str
    name: str
    energy_kwh: Optional[float]
    expected_kwh: Optional[float]
    production_pct: Optional[float]     # float, or None (missing/gated)
    pr: Optional[float]
    availability: Optional[float]
    soiling: Optional[float]
    cloud_pct: Optional[float]
    data_class: str
    status_note: str
    inverters: List[InverterDay] = field(default_factory=list)


@dataclass
class ReportData:
    date_iso: str
    plants: List[PlantDay]
    alerts: List[AlertRecord]


# ------------------------------------------------------------- pure logic

def plant_semaphore(p: PlantDay, has_critical_alert: bool,
                    has_any_alert: bool) -> str:
    """Status lamp for a plant. Mirrors the alert engine's bands."""
    if p.data_class != "full" or p.production_pct is None:
        return GRAY
    if p.production_pct < 0.85 or (p.availability or 1.0) < 0.90 \
            or has_critical_alert:
        return RED
    if p.production_pct < 0.95 or has_any_alert:
        return AMBER
    return GREEN


def inverter_dot(inv: InverterDay) -> str:
    """Status dot for an inverter row. Same thresholds as the detectors."""
    if (inv.rel and inv.rel[0] == "CRITICAL") or \
            (inv.tmax_c is not None and inv.tmax_c >= TEMP_CRIT_C):
        return RED
    if inv.faults or (inv.rel and inv.rel[0] == "WARNING") or \
            (inv.tmax_c is not None and inv.tmax_c >= TEMP_WARN_C):
        return AMBER
    return GREEN


def allocate_theoretical(expected_kwh: Optional[float],
                         inverters: List[InverterDay]) -> Dict[str, float]:
    """Split the plant's theoretical by nameplate share. Sums back exactly
    to the plant figure (last inverter absorbs rounding)."""
    if not expected_kwh or not inverters:
        return {}
    rsum = sum(i.rated_kw or 0 for i in inverters)
    if rsum <= 0:
        return {}
    out: Dict[str, float] = {}
    running = 0.0
    for i, inv in enumerate(inverters):
        if i == len(inverters) - 1:
            out[inv.sn] = round(expected_kwh - running, 1)
        else:
            share = round(expected_kwh * (inv.rated_kw or 0) / rsum, 1)
            out[inv.sn] = share
            running += share
    return out


def _median(vals: List[float]) -> float:
    v = sorted(vals)
    n = len(v)
    if n == 0:
        return 0.0
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


# --------------------------------------------------------------- SVG charts

def svg_fleet_bars(plants: List[PlantDay],
                   sem_of: Dict[str, str]) -> str:
    drawable = [p for p in plants if p.expected_kwh]
    if not drawable:
        return ""
    m = max(p.expected_kwh for p in drawable) * 1.05
    W, rows = 620, []
    for i, p in enumerate(drawable):
        y = i * 46
        we = (p.energy_kwh or 0) / m * W
        wx = p.expected_kwh / m * W
        col = COLORS[sem_of.get(p.plant_key, GRAY)]
        pct = (f"{p.production_pct * 100:.0f}%"
               if p.production_pct is not None else "n/a")
        rows.append(
            f'<g transform="translate(150,{y})">'
            f'<text x="-12" y="19" text-anchor="end" class="axl">'
            f'{p.plant_key}</text>'
            f'<rect x="0" y="4" width="{wx:.0f}" height="22" fill="none" '
            f'stroke="#9AA5A0" stroke-dasharray="4 3"/>'
            f'<rect x="0" y="4" width="{we:.0f}" height="22" '
            f'fill="{col}" opacity="0.88"/>'
            f'<text x="{max(we, wx) + 8:.0f}" y="19" class="axv">'
            f'{(p.energy_kwh or 0):,.0f} / {p.expected_kwh:,.0f} kWh '
            f'&#183; {pct}</text></g>')
    h = len(drawable) * 46
    return (f'<svg viewBox="0 0 860 {h}" role="img" '
            f'aria-label="Production vs theoretical per plant">'
            f'{"".join(rows)}</svg>')


def svg_inverter_bars(p: PlantDay) -> str:
    inv = [i for i in p.inverters if i.rated_kw]
    if not inv:
        return ""
    spec = [(i, i.kwh / i.rated_kw) for i in inv]
    med = _median([s for _, s in spec])
    m = max(max(s for _, s in spec), med, 0.01) * 1.15
    W, out = 430, []
    for row, (i, s) in enumerate(spec):
        y = row * 34
        w = s / m * W
        col = COLORS[inverter_dot(i)]
        out.append(
            f'<g transform="translate(120,{y})">'
            f'<text x="-10" y="16" text-anchor="end" class="axl">'
            f'{_html.escape(i.label or i.sn)}</text>'
            f'<rect x="0" y="3" width="{w:.0f}" height="18" '
            f'fill="{col}" opacity="0.85"/>'
            f'<text x="{w + 7:.0f}" y="16" class="axv">{s:.2f} kWh/kW'
            f'</text></g>')
    mx = med / m * W
    h = len(spec) * 34
    out.append(f'<line x1="{120 + mx:.0f}" y1="0" x2="{120 + mx:.0f}" '
               f'y2="{h}" stroke="#16211C" stroke-width="1.5" '
               f'stroke-dasharray="2 3"/>')
    out.append(f'<text x="{120 + mx + 4:.0f}" y="10" class="axv" '
               f'fill="#16211C">peer median</text>')
    return (f'<svg viewBox="0 0 660 {h + 4}" role="img" '
            f'aria-label="Specific yield per inverter, {p.plant_key}">'
            f'{"".join(out)}</svg>')


# ------------------------------------------------------------- HTML render

_CSS = """
:root{--ink:#16211C;--paper:#F6F7F5;--card:#FFFFFF;--mut:#5E6B66;
--line:#DDE2DF;--green:#1F7A4D;--amber:#C77E00;--red:#B3372B}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif}
.mono{font-family:"IBM Plex Mono",ui-monospace,monospace}
.wrap{max-width:960px;margin:0 auto;padding:28px 22px 60px}
header{display:flex;justify-content:space-between;align-items:baseline;
border-bottom:3px solid var(--ink);padding-bottom:14px}
h1{font-size:21px;margin:0;letter-spacing:.02em}
h1 .sub{color:var(--mut);font-weight:400}
.date{font-family:"IBM Plex Mono";font-size:18px;font-weight:600}
.rail{display:flex;gap:10px;margin:20px 0 6px;flex-wrap:wrap}
.stop{background:var(--card);border:1px solid var(--line);border-radius:4px;
padding:10px 14px;text-align:center;min-width:96px}
.lamp{width:16px;height:16px;border-radius:50%;margin:0 auto 6px}
.lamp.green{background:var(--green)}.lamp.amber{background:var(--amber)}
.lamp.red{background:var(--red)}.lamp.gray{background:#8A938F}
.stopk{font-weight:700;font-size:13px}
.stopv{font-family:"IBM Plex Mono";font-size:13px;color:var(--mut)}
.fleetline{color:var(--mut);font-size:13px;margin:2px 0 26px}
h2{font-size:13px;letter-spacing:.14em;text-transform:uppercase;
color:var(--mut);border-top:1px solid var(--line);padding-top:18px;
margin:30px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:4px;
padding:16px 18px}
.axl{font:600 12px "IBM Plex Sans"} .axv{font:12px "IBM Plex Mono";
fill:#5E6B66}
.alert{background:var(--card);border:1px solid var(--line);
border-left:4px solid var(--amber);border-radius:4px;padding:12px 14px;
margin-bottom:10px}
.alert.critical{border-left-color:var(--red)}
.ahead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.badge{font:700 10px "IBM Plex Mono";letter-spacing:.08em;padding:2px 7px;
border-radius:3px;color:#fff}
.badge.critical{background:var(--red)}.badge.warning{background:var(--amber)}
.awho{font-weight:700}
.ametric{font-family:"IBM Plex Mono";font-size:12px;color:var(--mut)}
.afact{margin-top:4px;font-weight:600}
.aexp{margin-top:3px;color:var(--mut);font-size:13.5px}
.plant{background:var(--card);border:1px solid var(--line);border-radius:4px;
padding:16px 18px;margin-bottom:18px;page-break-inside:avoid}
.phead{display:grid;grid-template-columns:22px 1fr;gap:4px 12px;
align-items:center}
.phead .lamp{margin:0}.phead h3{margin:0;font-size:17px}
.pname{color:var(--mut);font-weight:400;font-size:14px}
.pnote{grid-column:2;color:var(--mut);font-size:13.5px}
.pgrid{display:grid;grid-template-columns:250px 1fr;gap:18px;
margin:14px 0 4px}
.pfacts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fk{font-size:11px;letter-spacing:.08em;text-transform:uppercase;
color:var(--mut)}
.fv{font-size:17px;font-weight:600}
.fu{font-size:11px;color:var(--mut);font-weight:400}
.itab{width:100%;border-collapse:collapse;margin-top:10px;font-size:13.5px}
.itab th{text-align:left;font-size:11px;letter-spacing:.06em;
text-transform:uppercase;color:var(--mut);
border-bottom:1px solid var(--line);padding:4px 8px}
.itab td{border-bottom:1px solid var(--line);padding:6px 8px;
vertical-align:middle}
.itab .num{text-align:right;font-family:"IBM Plex Mono"}
.sn{display:block;font-size:10.5px;color:var(--mut)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}
.dot.red{background:var(--red)}
.chip{display:inline-block;font:600 10.5px "IBM Plex Mono";padding:1px 6px;
border-radius:3px;margin-right:4px;color:#fff}
.chip.red{background:var(--red)}.chip.amber{background:var(--amber)}
footer{margin-top:34px;color:var(--mut);font-size:12px;
border-top:1px solid var(--line);padding-top:12px}
@media(max-width:720px){.pgrid{grid-template-columns:1fr}}
@media print{body{background:#fff}.wrap{padding:0}
.plant,.card,.alert,.stop{border-color:#bbb}}
"""

_FONTS = ('<link href="https://fonts.googleapis.com/css2?family='
          'IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono:'
          'wght@400;600&display=swap" rel="stylesheet">')


def _esc(x) -> str:
    return _html.escape(str(x)) if x is not None else ""


def render_html(data: ReportData) -> str:
    crit_plants = {a.plant_key for a in data.alerts
                   if a.severity == "CRITICAL"}
    any_plants = {a.plant_key for a in data.alerts}
    sem_of = {p.plant_key: plant_semaphore(
        p, p.plant_key in crit_plants, p.plant_key in any_plants)
        for p in data.plants}

    rail = "".join(
        f'<div class="stop"><div class="lamp {sem_of[p.plant_key]}"></div>'
        f'<div class="stopk">{p.plant_key}</div>'
        f'<div class="stopv">'
        f'{(f"{p.production_pct*100:.0f}%" if p.production_pct is not None else "n/a")}'
        f'</div></div>'
        for p in data.plants)

    fe = sum(p.energy_kwh or 0 for p in data.plants)
    fx = sum(p.expected_kwh or 0 for p in data.plants)
    n_crit = sum(1 for a in data.alerts if a.severity == "CRITICAL")
    n_warn = len(data.alerts) - n_crit
    fleetline = (f'Fleet: {fe:,.0f} kWh produced'
                 + (f' &#183; {fx:,.0f} kWh theoretical &#183; '
                    f'{fe / fx * 100:.0f}% of plan' if fx else "")
                 + f' &#183; {n_crit} critical / {n_warn} warning alerts')

    alerts_html = "".join(
        f'<div class="alert {a.severity.lower()}">'
        f'<div class="ahead">'
        f'<span class="badge {a.severity.lower()}">{a.severity}</span>'
        f'<span class="awho">{_esc(a.plant_key)}'
        f'{(" &#183; " + _esc(a.inverter_sn)) if a.inverter_sn else ""}</span>'
        f'<span class="ametric">{_esc(a.metric)}</span></div>'
        f'<div class="afact">{_esc(a.message)}</div>'
        f'<div class="aexp">{_esc(a.explanation)}</div></div>'
        for a in data.alerts) or \
        '<div class="card">No open alerts.</div>'

    plants_html = ""
    for p in data.plants:
        theo = allocate_theoretical(p.expected_kwh, p.inverters)
        rows = ""
        for inv in p.inverters:
            d = inverter_dot(inv)
            th = theo.get(inv.sn)
            pct = (f"{inv.kwh / th * 100:.0f}%" if th else "&#8212;")
            chips = "".join(f'<span class="chip red">{_esc(f)}</span>'
                            for f in inv.faults)
            if inv.rel:
                cls = "red" if inv.rel[0] == "CRITICAL" else "amber"
                chips += (f'<span class="chip {cls}">'
                          f'{inv.rel[1] * 100:.0f}% of peers</span>')
            if inv.tmax_c is not None and inv.tmax_c >= TEMP_WARN_C:
                cls = "red" if inv.tmax_c >= TEMP_CRIT_C else "amber"
                chips += (f'<span class="chip {cls}">'
                          f'{inv.tmax_c:.0f} &#176;C peak</span>')
            rows += (
                f'<tr><td><span class="dot {d}"></span></td>'
                f'<td class="mono">{_esc(inv.label or inv.sn)}'
                f'<span class="sn">{_esc(inv.sn)}</span></td>'
                f'<td class="num">{inv.kwh:,.1f}</td>'
                f'<td class="num">{(f"{th:,.0f}" if th else "&#8212;")}</td>'
                f'<td class="num">{pct}</td>'
                f'<td class="num">'
                f'{(f"{inv.tmax_c:.1f}" if inv.tmax_c is not None else "&#8212;")}</td>'
                f'<td>{chips or "&#8212;"}</td></tr>')

        def fact(k, v):
            return (f'<div class="fact"><div class="fk">{k}</div>'
                    f'<div class="fv mono">{v}</div></div>')

        facts = (
            fact("Production", f'{(p.energy_kwh or 0):,.0f} '
                               f'<span class="fu">kWh</span>')
            + fact("Theoretical",
                   f'{p.expected_kwh:,.0f} <span class="fu">kWh</span>'
                   if p.expected_kwh else "&#8212;")
            + fact("Of plan",
                   f'{p.production_pct*100:.0f}<span class="fu">%</span>'
                   if p.production_pct is not None else "&#8212;")
            + fact("Cloud cover",
                   f'{p.cloud_pct:.0f}<span class="fu">%</span>'
                   if p.cloud_pct is not None else "&#8212;")
            + fact("Availability",
                   f'{p.availability*100:.0f}<span class="fu">%</span>'
                   if p.availability is not None else "&#8212;")
            + fact("Soiling/drift",
                   f'{p.soiling*100:.0f}<span class="fu">%</span>'
                   if p.soiling is not None else "&#8212;")
        )
        plants_html += (
            f'<section class="plant">'
            f'<div class="phead"><div class="lamp {sem_of[p.plant_key]}">'
            f'</div><h3>{p.plant_key} '
            f'<span class="pname">{_esc(p.name)}</span></h3>'
            f'<div class="pnote">{_esc(p.status_note)}</div></div>'
            f'<div class="pgrid"><div class="pfacts">{facts}</div>'
            f'<div class="pchart">{svg_inverter_bars(p)}</div></div>'
            f'<table class="itab"><thead><tr><th></th><th>Inverter</th>'
            f'<th>kWh</th><th>Theor.</th><th>% of th.</th>'
            f'<th>T max &#176;C</th><th>Flags</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></section>')

    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><title>ARGIA Daily &#8212; {data.date_iso}'
        f'</title>{_FONTS}<style>{_CSS}</style></head><body>'
        f'<div class="wrap"><header><h1>ARGIA '
        f'<span class="sub">&#183; Daily performance report</span></h1>'
        f'<div class="date">{data.date_iso}</div></header>'
        f'<div class="rail">{rail}</div>'
        f'<div class="fleetline mono">{fleetline}</div>'
        f'<h2>Production vs theoretical &#8212; per plant</h2>'
        f'<div class="card">{svg_fleet_bars(data.plants, sem_of)}'
        f'<div style="color:var(--mut);font-size:12px;margin-top:6px">'
        f'Solid bar = measured production, colored by plant status. '
        f'Dashed outline = theoretical (kWp &#215; measured irradiance '
        f'&#215; expected factor).</div></div>'
        f'<h2>Alerts &#8212; {len(data.alerts)} open</h2>{alerts_html}'
        f'<h2>Plants</h2>{plants_html}'
        f'<footer>Generated from Argia_Mont_v2 &#183; KPI_Daily '
        f'{data.date_iso} &#183; Per-inverter theoretical = plant '
        f'theoretical split by nameplate share. Semaphores &#8212; plant: '
        f'red &lt;85% of plan / low availability / critical alert; amber '
        f'&lt;95% or open warning; green otherwise; gray = day not fully '
        f'measured. Inverter: red = critical peer lag or &#8805;75 '
        f'&#176;C; amber = fault code, peer lag, or &#8805;65 &#176;C. '
        f'ShineMaster-metered plants: theoretical inherits sparse '
        f'irradiance sampling until the Pi migration &#8212; treat % of '
        f'plan as directional.</footer></div></body></html>')


# ------------------------------------------------------------ data assembly

def build_report_data(sheets: SheetsClient, portfolio: Portfolio,
                      date_iso: str) -> ReportData:
    """Read KPI_Daily + Alerts + telemetry and assemble the report model."""
    # plant-level from KPI_Daily
    kpi: Dict[str, Dict] = {}
    data = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ")
    header = [normalize_text(h) for h in (data[0] if data else [])]
    idx = {n: header.index(n) for n in header if n}
    for row in (data[1:] if data else []):
        try:
            if date_key(row[idx["date_iso"]]) != date_iso:
                continue
            pk = normalize_text(row[idx["plant_key"]]).upper()
        except (KeyError, IndexError):
            continue

        def cell(name):
            i = idx.get(name)
            return row[i] if i is not None and i < len(row) else None
        kpi[pk] = {
            "energy": safe_float(cell("energy_kwh")),
            "expected": safe_float(cell("expected_kwh")),
            "pp": safe_float(cell("production_pct")),
            "pr": safe_float(cell("pr")),
            "av": safe_float(cell("availability")),
            "soil": safe_float(cell("soiling_loss_pct")),
            "cloud": safe_float(cell("cloud_coverage_pct")),
            "dc": normalize_text(cell("data_class")).lower() or "no_data",
            "note": normalize_text(cell("status_note")),
        }

    # per-inverter from telemetry
    bundle = read_day_bundle(sheets, date_iso)
    rated = {i.inverter_sn: i.rated_kw
             for p in portfolio.active_plants()
             for i in portfolio.inverters_for(p.plant_key)}
    labels = {i.inverter_sn: i.inverter_label
              for p in portfolio.active_plants()
              for i in portfolio.inverters_for(p.plant_key)}
    readings: List[InverterReading] = []
    per_plant_inv: Dict[str, Dict[str, InverterDay]] = defaultdict(dict)
    for plant in portfolio.active_plants():
        rows = bundle.rows_for_plant(plant.plant_key)
        tmax: Dict[str, float] = {}
        faults: Dict[str, set] = defaultdict(set)
        for r in rows:
            sn = str(r.inverter_sn)
            if r.temperature_c is not None:
                tmax[sn] = max(tmax.get(sn, -999.0), float(r.temperature_c))
            for tok in fault_tokens(r.fault_code):
                faults[sn].add(tok)
        for sn, eday in compute_plant_energy(rows).items():
            if eday.energy_kwh is None:
                continue
            readings.append(InverterReading(plant.plant_key, sn,
                                            eday.energy_kwh, rated.get(sn)))
            per_plant_inv[plant.plant_key][sn] = InverterDay(
                sn=sn, label=labels.get(sn, ""), kwh=round(eday.energy_kwh, 1),
                rated_kw=rated.get(sn),
                tmax_c=round(tmax[sn], 1) if sn in tmax else None,
                faults=sorted(faults[sn]))
    rel = {b.inverter_sn: (b.severity.value, round(b.ratio, 3))
           for b in evaluate_inverter_relative(readings)}
    for invs in per_plant_inv.values():
        for sn, inv in invs.items():
            inv.rel = rel.get(sn)

    plants: List[PlantDay] = []
    for plant in portfolio.active_plants():
        k = kpi.get(plant.plant_key, {})
        invs = sorted(per_plant_inv.get(plant.plant_key, {}).values(),
                      key=lambda i: (i.label or "", i.sn))
        plants.append(PlantDay(
            plant_key=plant.plant_key,
            name=getattr(plant, "customer", "") or plant.plant_key,
            energy_kwh=k.get("energy"), expected_kwh=k.get("expected"),
            production_pct=k.get("pp"), pr=k.get("pr"),
            availability=k.get("av"), soiling=k.get("soil"),
            cloud_pct=k.get("cloud"), data_class=k.get("dc", "no_data"),
            status_note=k.get("note", ""), inverters=invs))

    ledger = load_alerts_ledger(sheets)
    alerts = [r for r in ledger.records if r.is_open() or r.is_silenced()]
    sev_rank = {"CRITICAL": 0, "WARNING": 1}
    alerts.sort(key=lambda a: (sev_rank.get(a.severity, 2), a.plant_key,
                               a.inverter_sn))
    return ReportData(date_iso=date_iso, plants=plants, alerts=alerts)
