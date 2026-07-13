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

import html as _html
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from argia.kpi.design import design_kwh_for_day, load_design_monthly
from argia.maintenance.events import (
    load_maintenance_events, plant_maintenance_on_date, maintenance_badge_text,
)
from argia.analytics.acute import TEMP_CRIT_C, TEMP_WARN_C
from argia.analytics.inverter_health import (
    InverterReading,
    evaluate_inverter_relative,
)
from argia.analytics.vendor_flags import fault_tokens
from argia.archive.kpi_daily import KPI_DAILY_TAB
from argia.alerts.digest import reportable_alerts
from argia.core.alerts_state import AlertRecord, load_alerts_ledger
from argia.core.config import Portfolio
from argia.core.constants import CO2_KG_PER_KWH
import datetime as dt

from argia.core.normalize import normalize_text, safe_float
from argia.core.time_utils import now_mx
from argia.core.sheets import SheetsClient
from argia.kpi import compute_plant_energy, read_day_bundle
from argia.kpi.reconcile import date_key
from argia.report.dashboard_html import LOGO_B64

LOG = logging.getLogger("argia.report.daily")

GREEN, AMBER, RED, GRAY = "green", "amber", "red", "gray"
MAINT = "maint"   # v92: plant in a logged maintenance window (not a fault)
# Dashboard-family palette — one visual language across page and PDF
COLORS = {GREEN: "#0E8A6D", AMBER: "#B7791F", RED: "#A32D2D", GRAY: "#9aa39e"}


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
    kwp_dc: Optional[float] = None
    tariff_mxn_per_kwh: Optional[float] = None
    design_kwh: Optional[float] = None
    # v92: badge text when the plant is in a logged maintenance window on
    # the report date (None otherwise). Distinguishes "known maintenance"
    # from a fault so the plant reads neutral, not red.
    maintenance_note: Optional[str] = None
    # v88: (hour_label, production_kwh, theoretical_kwh) per completed
    # 60-min bucket, from Dashboard_Plant — rendered as the hourly
    # chart on small (client) reports
    buckets: List[Tuple[str, float, float]] = field(default_factory=list)


@dataclass
class ReportData:
    date_iso: str
    plants: List[PlantDay]
    alerts: List[AlertRecord]


# ------------------------------------------------------------- pure logic

def plant_semaphore(p: PlantDay, has_critical_alert: bool,
                    has_any_alert: bool) -> str:
    """Status lamp for a plant. Mirrors the alert engine's bands."""
    if p.maintenance_note:
        return MAINT   # v92: known maintenance reads neutral, never red
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

def short_name(p: PlantDay) -> str:
    """Customer name for compact display: cut at ' PPA', at ' (' and at
    the first comma. The ' (' cut is for CAPEX-style names —
    "SMS (CDMX,MEX)" rendered as the broken "SMS (CDMX" under the
    comma-only rule (user report 2026-07-10). PPA names are unaffected:
    the ' PPA' cut fires first for them."""
    n = (p.name or p.plant_key)
    n = n.split(" PPA")[0].split(" (")[0].split(",")[0].strip()
    return n or p.plant_key


def fleet_stats(plants: List[PlantDay]) -> Dict[str, Optional[float]]:
    """Portfolio-level numbers for the summary block. Pure.

    - availability is kWp-WEIGHTED (a 189 kWp plant must not count as
      much as an 818 kWp one), over plants that reported it
    - income counts only plants with a tariff (all six have one today;
      the guard is for config drift, not decoration)
    """
    fe = sum(p.energy_kwh or 0 for p in plants)
    fx = sum(p.expected_kwh or 0 for p in plants)
    # Portfolio %% only from plants the KPI layer deemed measurable
    # (2026-07-08: a block day halved measured sun; kpi withheld every
    # production_pct and wrote "unreliable" — but this function divided
    # raw sums anyway and the report shouted 183%% two lines under an
    # INCOMPLETE DAY verdict).
    ge = sum(p.energy_kwh or 0 for p in plants
             if p.production_pct is not None and not p.maintenance_note)
    gx = sum(p.expected_kwh or 0 for p in plants
             if p.production_pct is not None and not p.maintenance_note)
    kwp = sum(p.kwp_dc or 0 for p in plants)
    aw = [(p.availability, p.kwp_dc or 0) for p in plants
          if p.availability is not None and (p.kwp_dc or 0) > 0]
    avail = (sum(a * w for a, w in aw) / sum(w for _, w in aw)
             if aw else None)
    income = sum((p.energy_kwh or 0) * p.tariff_mxn_per_kwh
                 for p in plants if p.tariff_mxn_per_kwh)
    de = sum(p.energy_kwh or 0 for p in plants
             if p.design_kwh and not p.maintenance_note)
    dx = sum(p.design_kwh or 0 for p in plants if not p.maintenance_note)
    return {
        "production_kwh": fe,
        "expected_kwh": fx,
        "pct": (ge / gx) if gx else None,
        "design_pct": (de / dx) if dx else None,
        "kwp": kwp,
        "availability": avail,
        "income_mxn": income if income else None,
        "co2_kg": fe * CO2_KG_PER_KWH,
    }


def summary_sentence(stats: Dict[str, Optional[float]],
                     port_title: str, port_why: str,
                     subject: str = "the portfolio") -> str:
    """One human sentence: the whole day for someone who reads nothing
    else. Verdict first, numbers after, offenders (from the semaphore's
    why-line) only when the day wasn't clean. ``subject`` is "the
    portfolio" for the internal report, or the company name on a
    single-client page (e.g. "TETRA PAK")."""
    bits = [f"{subject} produced {stats['production_kwh']:,.0f} kWh"]
    if stats["pct"] is not None:
        bits.append(f"{stats['pct'] * 100:.0f}% of expected")
    if stats["design_pct"] is not None:
        bits.append(f"{stats['design_pct'] * 100:.0f}% of contract design")
    if stats["availability"] is not None:
        bits.append(f"{stats['availability'] * 100:.0f}% availability")
    sentence = f"{port_title}: " + ", ".join(bits)
    if stats["income_mxn"]:
        sentence += (f" \u2014 \u2248${stats['income_mxn']:,.0f} MXN "
                     f"income and {stats['co2_kg'] / 1000:.1f} t "
                     f"CO\u2082 avoided")
    sentence += "."
    if port_title != "ON PLAN":
        sentence += f" ({port_why}.)"
    return sentence


def scoped_alerts(alerts: List[AlertRecord],
                  visible_keys: set) -> List[AlertRecord]:
    """v76: the daily report is a PORTFOLIO-SCOPED document — its alert
    section and its verdict counters must speak only about the plants
    the report shows (show_daily_report). A CAPEX plant's open alert
    must not flip the PPA report to ATTENTION; those plants get their
    own per-client reports and channels (v77+). An alert with a blank
    plant_key (none exist today) is kept — never silently drop
    something that can't be attributed."""
    return [a for a in alerts
            if not a.plant_key or a.plant_key in visible_keys]


def portfolio_semaphore(plants: List[PlantDay], sem_of: Dict[str, str],
                        n_crit: int, n_warn: int,
                        fleet_pct: Optional[float],
                        live: bool = False) -> Tuple[str, str, str]:
    """One verdict for the whole portfolio: (color, title, why).

    Same philosophy as the plant lamps — the worst signal wins:
      RED    any red plant, any critical alert, or fleet < 85% of plan
      AMBER  any amber plant, any warning alert, or fleet < 95%
      GRAY   every plant gray (day not fully measured)
      GREEN  otherwise
    The `why` line NAMES the offenders so the verdict is auditable at a
    glance instead of being a mood light.

    ``live`` distinguishes the two honest meanings of an all-gray board
    (user report 2026-07-09): in the EVENING edition the day simply has
    not been stamped yet — telemetry may be perfect — so the title is
    "DAY IN PROGRESS", a state, not an alarm. "INCOMPLETE DAY" is
    reserved for final editions, where an unstamped/partial day means
    measurement genuinely failed and should read like a problem.
    """
    colors = [sem_of.get(p.plant_key, GRAY) for p in plants]
    red_names = [short_name(p) for p in plants
                 if sem_of.get(p.plant_key) == RED]
    amber_names = [short_name(p) for p in plants
                   if sem_of.get(p.plant_key) == AMBER]
    parts = []
    if red_names:
        parts.append("below plan / needs action: " + ", ".join(red_names))
    if amber_names:
        parts.append("watch: " + ", ".join(amber_names))
    if n_crit:
        parts.append(f"{n_crit} critical alert{'s' if n_crit > 1 else ''}")
    if n_warn:
        parts.append(f"{n_warn} warning{'s' if n_warn > 1 else ''}")
    if fleet_pct is not None:
        parts.append(f"fleet at {fleet_pct * 100:.0f}% of plan")

    if colors and all(c == GRAY for c in colors):
        if live:
            return (GRAY, "DAY IN PROGRESS",
                    "live estimate; telemetry running, day is classified "
                    "tonight — final numbers in tomorrow's 07:05 report")
        return GRAY, "INCOMPLETE DAY", "no plant fully measured yet"
    if red_names or n_crit or (fleet_pct is not None and fleet_pct < 0.85):
        return RED, "ATTENTION", " \u00b7 ".join(parts)
    if amber_names or n_warn or (fleet_pct is not None and fleet_pct < 0.95):
        return AMBER, "WATCH", " \u00b7 ".join(parts)
    why = (f"all {len(plants)} plants on plan"
           + (f" \u00b7 fleet at {fleet_pct * 100:.0f}%"
              if fleet_pct is not None else ""))
    return GREEN, "ON PLAN", why


def svg_fleet_bars(plants: List[PlantDay],
                   sem_of: Dict[str, str]) -> str:
    drawable = [p for p in plants if p.expected_kwh]
    if not drawable:
        return ""
    m = max(p.expected_kwh for p in drawable) * 1.05
    # Geometry: 200px name gutter + 540px bars + 200px caption room in a
    # 940px viewBox. The caption previously sat at max(bar)+8 inside 860px
    # and CLIPPED when the theoretical outline was long (user screenshot,
    # GTO1 2026-07-05: "... kWh (cut)").
    GUTTER, W, VIEW = 200, 540, 940
    rows = []
    for i, p in enumerate(drawable):
        y = i * 46
        we = (p.energy_kwh or 0) / m * W
        wx = p.expected_kwh / m * W
        col = COLORS[sem_of.get(p.plant_key, GRAY)]
        pct = (f"{p.production_pct * 100:.0f}%"
               if p.production_pct is not None else "n/a")
        rows.append(
            f'<g transform="translate({GUTTER},{y})">'
            f'<text x="-12" y="19" text-anchor="end" class="axl">'
            f'{_html.escape(short_name(p))}</text>'
            f'<rect x="0" y="4" width="{wx:.0f}" height="22" fill="none" '
            f'stroke="#c9c8c0" stroke-dasharray="4 3"/>'
            f'<rect x="0" y="4" width="{we:.0f}" height="22" '
            f'fill="{col}" opacity="0.88"/>'
            f'<text x="{max(we, wx) + 8:.0f}" y="19" class="axv">'
            f'{(p.energy_kwh or 0):,.0f} / {p.expected_kwh:,.0f} kWh '
            f'&#183; {pct}</text></g>')
    h = len(drawable) * 46
    return (f'<svg viewBox="0 0 {VIEW} {h}" role="img" '
            f'aria-label="Production vs theoretical per plant">'
            f'{"".join(rows)}</svg>')


def hourly_chart(p: PlantDay) -> str:
    """v88: intraday production vs theoretical per 60-min bucket, as
    static CSS bars (no JS — renders identically in the browser client
    pages and WeasyPrint). Ghost bar = theoretical, solid bar =
    production, colored like the plant lamp. Empty when no buckets."""
    if not p.buckets:
        return ""
    peak = max(max(prod, theor) for _, prod, theor in p.buckets)
    if peak <= 0:
        return ""
    cols = ""
    for hour, prod, theor in p.buckets:
        hp = round(prod / peak * 100)
        ht = round(theor / peak * 100)
        cols += (
            f'<div class="ibcol">'
            f'<div class="ibbars">'
            f'<div class="ibghost" style="height:{ht}%"></div>'
            f'<div class="ibbar" style="height:{hp}%"></div>'
            f'</div>'
            f'<div class="iblab">{_esc(hour)}</div>'
            f'</div>')
    return (f'<div class="ibchart"><div class="ibtitle">Intraday '
            f'production &#183; 60-min buckets '
            f'<span class="ibsub">solid = measured &#183; outline = '
            f'theoretical (kWp &#215; measured irradiance &#215; '
            f'expected factor)</span></div>'
            f'<div class="ibrow">{cols}</div></div>')


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
    # Label sits BELOW the chart, hanging off the median line; it flips to
    # the left side when the median is near the right edge. Reason: at the
    # top-right it collided with the last bar's value text whenever
    # inverters sat near the median — i.e. on every healthy plant
    # (user-reported, 2026-07-07).
    flip = mx > 0.72 * W
    out.append(f'<text x="{120 + mx + (-4 if flip else 4):.0f}" '
               f'y="{h + 13}" class="axv" fill="#16211C" '
               f'text-anchor="{"end" if flip else "start"}">'
               f'peer median</text>')
    return (f'<svg viewBox="0 0 660 {h + 18}" role="img" '
            f'aria-label="Specific yield per inverter, {p.plant_key}">'
            f'{"".join(out)}</svg>')


# ------------------------------------------------------------- HTML render

_CSS = """
:root{--ink:#1a1a19;--paper:#f4f3ef;--card:#ffffff;--mut:#6b6a64;
--line:#e4e3dc;--green:#0E8A6D;--amber:#B7791F;--red:#A32D2D;--maint:#2F6DB0}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:14px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.mono{font-variant-numeric:tabular-nums}
.wrap{max-width:960px;margin:0 auto;padding:24px 20px 48px}
header{display:block;margin-bottom:14px}
.lockup{display:flex;justify-content:space-between;align-items:center;
gap:14px;margin-bottom:10px}
.lockup .title{font-size:16px;font-weight:600;letter-spacing:3.5px;
white-space:nowrap}
.lockup img{height:26px;display:block}
.subrow{display:flex;justify-content:space-between;align-items:baseline}
.subrow .kind{color:var(--mut);font-size:13px}
.date{font-size:16px;font-weight:600}
.rail{display:grid;margin:16px 0 6px;gap:10px}
.stop{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:10px 6px;text-align:center;min-width:0}
.lamp{width:14px;height:14px;border-radius:50%;margin:0 auto 6px}
.lamp.green{background:var(--green)}.lamp.amber{background:var(--amber)}
.lamp.red{background:var(--red)}.lamp.gray{background:#9aa39e}
.lamp.maint{background:var(--maint)}
.stopk{font-weight:600;font-size:12px;line-height:1.25}
.stopv{font-size:13px;color:var(--mut)}
.fleetline{color:var(--ink);font-size:16px;margin:8px 0 24px;
background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 16px}
.fleetline b{font-weight:700}
.portrow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
margin-bottom:6px}
.portlamp{width:18px;height:18px;border-radius:50%;flex:0 0 auto}
.portlamp.green{background:var(--green)}.portlamp.amber{background:var(--amber)}
.portlamp.red{background:var(--red)}.portlamp.gray{background:#9aa39e}
.portlamp.maint{background:var(--maint)}
.porttitle{font-weight:700;font-size:15px;letter-spacing:2px}
.portwhy{color:var(--mut);font-size:13px}
.portnums{font-size:14px;color:var(--ink)}
.portsummary{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:16px 18px;margin:16px 0 6px}
.portsentence{font-size:15px;margin:8px 0 14px;line-height:1.5}
.pstats{display:grid;gap:10px}
.ibchart{margin:12px 0 4px}
.ibtitle{font-size:12px;font-weight:600;margin-bottom:6px}
.ibsub{font-weight:400;color:#8a897f;font-size:11px}
.ibrow{display:flex;align-items:flex-end;gap:3px;height:120px}
.ibcol{flex:1;display:flex;flex-direction:column;height:100%}
.ibbars{position:relative;flex:1}
.ibghost{position:absolute;bottom:0;left:0;right:0;background:#e7e5dc;border:1px dashed #c9c8c0;border-bottom:none;border-radius:3px 3px 0 0}
.ibbar{position:absolute;bottom:0;left:15%;right:15%;background:#0d8a6a;border-radius:2px 2px 0 0}
.iblab{font-size:9px;color:#8a897f;text-align:center;margin-top:3px}
.pstats.n7{grid-template-columns:repeat(7,1fr)}
.pstats.n6{grid-template-columns:repeat(6,1fr)}
.pstat{background:var(--paper);border:1px solid var(--line);
border-radius:8px;padding:10px 12px;text-align:center}
.pstatv{font-size:19px;font-weight:700}
.pstatu{font-size:11px;color:var(--mut);font-weight:400}
.pstatk{font-size:11px;color:var(--mut);margin-top:2px}
@media(max-width:720px){.pstats.n7,.pstats.n6{grid-template-columns:repeat(3,1fr)}}
h2{font-size:13px;font-weight:600;color:var(--ink);margin:26px 0 10px}
.card{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px}
.axl{font:600 12px -apple-system,"Segoe UI",Roboto,Arial,sans-serif}
.axv{font:12px -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
fill:#6b6a64}
.alert{background:var(--card);border:1px solid var(--line);
border-left:4px solid var(--amber);border-radius:10px;
padding:12px 14px;margin-bottom:10px}
.alert.critical{border-left-color:var(--red)}
.ahead{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.badge{font-weight:700;font-size:10px;letter-spacing:.08em;
padding:2px 8px;border-radius:9px;color:#fff}
.badge.critical{background:var(--red)}.badge.warning{background:var(--amber)}
.badge.maint{background:var(--maint)}
.awho{font-weight:600}
.ametric{font-size:12px;color:var(--mut)}
.afact{margin-top:4px;font-weight:600}
.aexp{margin-top:3px;color:var(--mut);font-size:13px}
.plant{background:var(--card);border:1px solid var(--line);
border-radius:10px;padding:14px 16px;margin-bottom:16px;
page-break-inside:avoid}
.phead{display:grid;grid-template-columns:20px 1fr;gap:4px 12px;
align-items:center}
.phead .lamp{margin:0}.phead h3{margin:0;font-size:16px;font-weight:600}
.pname{color:var(--mut);font-weight:400;font-size:13px}
.pnote{grid-column:2;color:var(--mut);font-size:13px}
.pgrid{display:grid;grid-template-columns:250px 1fr;gap:18px;
margin:12px 0 4px}
.pfacts{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.fk{font-size:11px;color:var(--mut)}
.fv{font-size:17px;font-weight:600}
.fu{font-size:11px;color:var(--mut);font-weight:400}
.itab{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
.itab th{text-align:left;font-size:11px;font-weight:600;color:var(--mut);
border-bottom:1px solid var(--line);padding:4px 8px}
.itab td{border-bottom:1px solid var(--line);padding:6px 8px;
vertical-align:middle}
.itab .num{text-align:right;font-variant-numeric:tabular-nums}
.sn{display:block;font-size:10.5px;color:var(--mut)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%}
.dot.green{background:var(--green)}.dot.amber{background:var(--amber)}
.dot.red{background:var(--red)}
.chip{display:inline-block;font-weight:600;font-size:10.5px;
padding:1px 7px;border-radius:9px;margin-right:4px;color:#fff}
.chip.red{background:var(--red)}.chip.amber{background:var(--amber)}
footer{margin-top:30px;color:var(--mut);font-size:12px;
border-top:1px solid var(--line);padding-top:12px;line-height:1.55}
@media(max-width:720px){.pgrid{grid-template-columns:1fr}
.rail{grid-template-columns:repeat(3,1fr)}}
@media print{body{background:#fff}.wrap{padding:0}
.plant,.card,.alert,.stop{border-color:#ccc}}
"""

# System font stack (dashboard family) — no external font fetch inside the
# PDF-printing Chromium, so the PDF renders identically offline.
_FONTS = ''


def _esc(x) -> str:
    return _html.escape(str(x)) if x is not None else ""


def render_html(data: ReportData) -> str:
    crit_plants = {a.plant_key for a in data.alerts
                   if a.severity == "CRITICAL"}
    any_plants = {a.plant_key for a in data.alerts}
    sem_of = {p.plant_key: plant_semaphore(
        p, p.plant_key in crit_plants, p.plant_key in any_plants)
        for p in data.plants}

    # v83: on small reports (client pages, <=3 plants) there is room
    # for the FULL customer name incl. location; the short name exists
    # for the crowded internal rail (user report 2026-07-11)
    rail_name = (lambda p: p.name or p.plant_key) \
        if len(data.plants) <= 3 else short_name
    rail = "".join(
        f'<div class="stop"><div class="lamp {sem_of[p.plant_key]}"></div>'
        f'<div class="stopk">{_esc(rail_name(p))}</div>'
        f'<div class="stopv">'
        f'{(f"{p.production_pct*100:.0f}%" if p.production_pct is not None else "n/a")}'
        f'</div></div>'
        for p in data.plants)

    fe = sum(p.energy_kwh or 0 for p in data.plants)
    fx = sum(p.expected_kwh or 0 for p in data.plants)
    n_crit = sum(1 for a in data.alerts if a.severity == "CRITICAL")
    n_warn = len(data.alerts) - n_crit
    fleet_pct = (fe / fx) if fx else None
    live = any(p.data_class == "live" for p in data.plants)
    port_color, port_title, port_why = portfolio_semaphore(
        data.plants, sem_of, n_crit, n_warn, fleet_pct, live=live)
    subtitle = ("live evening estimate" if live else "KPI-final numbers")
    stats = fleet_stats(data.plants)
    # v97: on a single-client page (all plants share one customer — the
    # CAPEX per-client reports, e.g. Tetra Pak = one plant) the header
    # and sentence use the COMPANY name, not "PORTFOLIO" — it isn't a
    # portfolio, it's one client. The internal report (many customers)
    # stays "PORTFOLIO".
    companies = {short_name(p) for p in data.plants}
    single_client = len(companies) == 1
    scope_label = next(iter(companies)).upper() if single_client \
        else "PORTFOLIO"
    subject = next(iter(companies)) if single_client else "the portfolio"
    size_label = "Plant size" if len(data.plants) == 1 else "Portfolio size"
    sentence = summary_sentence(stats, port_title, port_why, subject=subject)

    def stat(label, value, unit=""):
        return (f'<div class="pstat"><div class="pstatv">{value}'
                f'<span class="pstatu">{unit}</span></div>'
                f'<div class="pstatk">{label}</div></div>')

    cards = (
        stat("Production", f"{stats['production_kwh']:,.0f}", " kWh")
        + stat("Of expected",
               f"{stats['pct'] * 100:.0f}" if stats["pct"] is not None
               else "&#8212;", "%")
        + stat("Availability",
               f"{stats['availability'] * 100:.0f}"
               if stats["availability"] is not None else "&#8212;", "%")
        + stat(size_label, f"{stats['kwp']:,.0f}", " kWp DC")
        + (stat("Income (est.)", f"${stats['income_mxn']:,.0f}",
                " MXN") if stats["income_mxn"] else "")
        + stat("Of design",
               f"{stats['design_pct'] * 100:.0f}"
               if stats["design_pct"] is not None else "&#8212;", "%")
        + stat("CO&#8322; avoided", f"{stats['co2_kg'] / 1000:.1f}", " t")
    )
    # WeasyPrint needs the grid template to MATCH the card count (fixed
    # columns, hard-learned 2026-07-08); the Income card is conditional
    # since v87, so the count is computed, never assumed.
    n_cards = cards.count('class="pstat"')
    summary_block = (
        f'<section class="portsummary">'
        f'<div class="portrow"><span class="portlamp {port_color}"></span>'
        f'<span class="porttitle">{scope_label}: {port_title}</span></div>'
        f'<div class="portsentence">{sentence}</div>'
        f'<div class="pstats n{n_cards}">{cards}</div>'
        f'</section>')
    fleetline = (f'<div class="portnums"><b>{n_crit}</b> critical / '
                 f'<b>{n_warn}</b> warning alerts open</div>')

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
            + fact("Of design",
                   f'{(p.energy_kwh or 0)/p.design_kwh*100:.0f}'
                   f'<span class="fu">%</span>'
                   if p.design_kwh else "&#8212;")
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
        maint_badge = ('<span class="badge maint">MAINTENANCE</span>'
                       if p.maintenance_note else '')
        head_note = p.maintenance_note or p.status_note
        plants_html += (
            f'<section class="plant">'
            f'<div class="phead"><div class="lamp {sem_of[p.plant_key]}">'
            f'</div><h3>{p.plant_key} '
            f'<span class="pname">{_esc(p.name)}'
            f'{" &#183; %d kWp DC" % round(p.kwp_dc) if p.kwp_dc else ""}'
            f'</span>{maint_badge}</h3>'
            f'<div class="pnote">{_esc(head_note)}</div></div>'
            f'<div class="pgrid"><div class="pfacts">{facts}</div>'
            f'<div class="pchart">{svg_inverter_bars(p)}</div></div>'
            f'{hourly_chart(p) if len(data.plants) <= 3 else ""}'
            f'<table class="itab"><thead><tr><th></th><th>Inverter</th>'
            f'<th class="num">kWh</th><th class="num">Theor.</th>'
            f'<th class="num">% of th.</th>'
            f'<th class="num">T max &#176;C</th><th>Flags</th>'
            f'</tr></thead>'
            f'<tbody>{rows}</tbody></table></section>')

    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,'
        f'initial-scale=1"><title>ARGIA Daily &#8212; {data.date_iso}'
        f'</title>{_FONTS}<style>{_CSS}</style></head><body>'
        f'<div class="wrap"><header>'
        f'<div class="lockup"><span class="title">PERFORMANCE&nbsp;'
        f'REPORT</span><img src="data:image/png;base64,{LOGO_B64}" '
        f'alt="ARGIA SOLAR"></div>'
        f'<div class="subrow"><span class="kind">Daily performance '
        f'report &#183; {subtitle}</span>'
        f'<span class="date">{data.date_iso}</span></div></header>'
        f'{summary_block}'
        f'<div class="rail" style="grid-template-columns:'
        f'repeat({max(1, len(data.plants))},1fr)">{rail}</div>'
        f'<div class="fleetline mono">{fleetline}</div>'
        + ((
            f'<h2>Production vs theoretical &#8212; per plant</h2>'
            f'<div class="card">{svg_fleet_bars(data.plants, sem_of)}'
            f'<div style="color:var(--mut);font-size:12px;'
            f'margin-top:6px">'
            f'Solid bar = measured production, colored by plant status. '
            f'Dashed outline = theoretical (kWp &#215; measured '
            f'irradiance &#215; expected factor).</div></div>'
        ) if len(data.plants) > 3 else "")
        # v89: on client pages (one plant per customer) this section
        # duplicated the plant card + hourly chart — removed there
        + f'<h2>Alerts &#8212; {len(data.alerts)} open</h2>{alerts_html}'
        f'<h2>Plants</h2>{plants_html}'
        f'<footer>Generated from Argia_Mont_v2 &#183; KPI_Daily '
        f'{data.date_iso} &#183; Per-inverter theoretical = plant '
        f'theoretical split by nameplate share. Semaphores &#8212; plant: '
        f'red &lt;85% of plan / low availability / critical alert; amber '
        f'&lt;95% or open warning; green otherwise; gray = day not fully '
        f'measured. Inverter: red = critical peer lag or &#8805;75 '
        f'&#176;C; amber = fault code, peer lag, or &#8805;65 &#176;C. '
        f'Irradiance: ShineMaster stored minute-scale history '
        f'(~300 samples/day, trapezoidal), validated to &lt;1% against an '
        f'independent weather model; snapshot/cloud-model fallback when '
        f'the fetch fails &#8212; KPI records the source per day. '
        f'Of design = production vs the contract design estimate '
        f'(PVsyst/Helioscope monthly kWh &#247; calendar days) &#8212; '
        f'static, unaffected by sensor outages. The portfolio %% counts '
        f'only plants whose sun was reliably measured that day; energy, '
        f'income and CO&#8322; always count every plant. Income (est.) = '
        f'energy &#215; PPA tariff, before billing adjustments. '
        f'CO&#8322; avoided uses the national grid emission '
        f'factor (0.444 kg/kWh). Portfolio availability is kWp-weighted. '
        f'Live editions estimate expected from the dashboard\'s intraday '
        f'irradiance buckets (same formula as end-of-day KPI, \u00b110%, '
        f'pro-rated to the last complete hour); the stamped KPI replaces '
        f'it next morning. '
        f'Report scope: plants with show_daily_report enabled; the alert '
        f'section and the verdict counters cover only those plants '
        f'(other portfolios report through their own channels). '
        f'"INCOMPLETE DAY" appears only in final editions when measurement '
        f'gaps remain; evening editions show "DAY IN PROGRESS" while the '
        f'day awaits its end-of-day classification. '
        f'Evening editions carry live telemetry-derived energy ahead of '
        f'the final KPI numbers mailed the next morning.'
        f'</footer></div></body></html>')


# ------------------------------------------------------------ data assembly

def plant_buckets_from_dashboard(rows, date_iso: str,
                                 now_mx: dt.datetime
                                 ) -> Dict[str, List[Tuple[str, float,
                                                           float]]]:
    """v88: per-plant hourly (hour_label, production, theoretical)
    series from Dashboard_Plant, for the intraday chart on client
    pages. Same rules as live_expected_from_dashboard: only the
    report's date, in-flight bucket excluded on the live day,
    date_key/safe_float on the formatted Sheets values."""
    out: Dict[str, List[Tuple[str, float, float]]] = {}
    cur_hour = now_mx.strftime("%H")
    today = now_mx.date().isoformat()
    for r in rows or []:
        if date_key(r.get("date_mx")) != date_iso:
            continue
        hour = str(r.get("hour_label") or "")
        if date_iso == today and hour[:2] == cur_hour:
            continue
        pk = str(r.get("plant_key") or "").strip().upper()
        if not pk or not hour:
            continue
        prod = safe_float(r.get("total_kwh")) or 0.0
        theor = safe_float(r.get("theoretical_kwh")) or 0.0
        out.setdefault(pk, []).append((hour, prod, theor))
    for pk in out:
        out[pk].sort(key=lambda b: b[0])
    return out


def live_conditions_from_dashboard(rows, date_iso: str,
                                   now_mx: dt.datetime
                                   ) -> Dict[str, Dict[str, float]]:
    """v89: live cloud cover and availability per plant from the same
    Dashboard_Plant buckets (the interactive dashboard's own live
    sources — single engine). Cloud = mean of buckets that carry it;
    availability = mean of inverters_reporting/inverters_total over
    buckets with any production or irradiance (skeleton rows for
    future hours carry zeros and must not count)."""
    clouds: Dict[str, List[float]] = {}
    avails: Dict[str, List[float]] = {}
    cur_hour = now_mx.strftime("%H")
    today = now_mx.date().isoformat()
    for r in rows or []:
        if date_key(r.get("date_mx")) != date_iso:
            continue
        hour = str(r.get("hour_label") or "")
        if date_iso == today and hour[:2] >= cur_hour:
            continue                      # in-flight + future skeleton
        pk = str(r.get("plant_key") or "").strip().upper()
        if not pk:
            continue
        prod = safe_float(r.get("total_kwh")) or 0.0
        irr = safe_float(r.get("irradiance_kwh_m2")) or 0.0
        if prod <= 0 and irr <= 0:
            continue                      # empty/night bucket
        c = safe_float(r.get("cloud_cover_pct"))
        if c is not None:
            clouds.setdefault(pk, []).append(c)
        tot = safe_float(r.get("inverters_total")) or 0.0
        rep = safe_float(r.get("inverters_reporting")) or 0.0
        if tot > 0:
            avails.setdefault(pk, []).append(min(1.0, rep / tot))
    out: Dict[str, Dict[str, float]] = {}
    for pk in set(clouds) | set(avails):
        d: Dict[str, float] = {}
        if clouds.get(pk):
            d["cloud"] = round(sum(clouds[pk]) / len(clouds[pk]), 1)
        if avails.get(pk):
            d["availability"] = round(
                sum(avails[pk]) / len(avails[pk]), 4)
        out[pk] = d
    return out


def live_expected_from_dashboard(rows, date_iso: str,
                                 now_mx: dt.datetime) -> Dict[str, float]:
    """v85: live 'expected so far' per plant from the Dashboard_Plant
    hourly buckets — the SAME engine and numbers the interactive
    dashboard headlines (kWp x measured irradiance x expected factor
    per 60-min bucket), so the report cannot drift from it. Rules:
    only the report's date; the in-flight bucket is excluded
    (pro-rated to the last complete hour, matching the dashboard);
    Sheets values arrive FORMATTED, so date_key/safe_float throughout
    (house rule)."""
    out: Dict[str, float] = {}
    cur_hour = now_mx.strftime("%H")
    today = now_mx.date().isoformat()
    for r in rows or []:
        if date_key(r.get("date_mx")) != date_iso:
            continue
        hour = str(r.get("hour_label") or "")[:2]
        if date_iso == today and hour == cur_hour:
            continue                      # in-flight bucket
        pk = str(r.get("plant_key") or "").strip().upper()
        th = safe_float(r.get("theoretical_kwh"))
        if pk and th:
            out[pk] = out.get(pk, 0.0) + th
    return {pk: round(v, 1) for pk, v in out.items() if v > 0}


def synthesize_live_energy(invs) -> Optional[float]:
    """Plant energy from the day's telemetry (sum of per-inverter EToday
    maxima) for reports that run before kpi-eod stamps the day. None when
    telemetry has nothing — the caller keeps its honest empty state."""
    vals = [i.kwh for i in invs if i.kwh is not None]
    return round(sum(vals), 1) if vals else None


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
            "design": safe_float(cell("design_kwh")),
            "dc": normalize_text(cell("data_class")).lower() or "no_data",
            "note": normalize_text(cell("status_note")),
        }

    # Contract design fallback: evening (live) editions and pre-stamp
    # days have no KPI design cell — compute it from the Design_Monthly
    # tab directly. Static data, so this is exact, not an estimate.
    design_map = load_design_monthly(sheets)

    # v92: which plants are in a logged maintenance window on this date —
    # drives the badge and the neutral (non-red) lamp.
    maint_by_plant = plant_maintenance_on_date(
        load_maintenance_events(sheets), date_iso)

    # per-inverter from telemetry
    bundle = read_day_bundle(sheets, date_iso)
    rated = {i.inverter_sn: i.rated_kw
             for p in portfolio.daily_report_plants()
             for i in portfolio.inverters_for(p.plant_key)}
    labels = {i.inverter_sn: i.inverter_label
              for p in portfolio.daily_report_plants()
              for i in portfolio.inverters_for(p.plant_key)}
    readings: List[InverterReading] = []
    per_plant_inv: Dict[str, Dict[str, InverterDay]] = defaultdict(dict)
    for plant in portfolio.daily_report_plants():
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
    # v85: live editions borrow "expected so far" from the dashboard's
    # intraday buckets (single engine — no second estimator). Loaded
    # once; a missing/empty tab degrades to the old design-only view.
    try:
        _dash_rows = sheets.read_table("Dashboard_Plant", "A1:ZZ")
    except Exception:  # noqa: BLE001 - report must render regardless
        _dash_rows = []
    live_exp = live_expected_from_dashboard(_dash_rows, date_iso,
                                            now_mx())
    buckets_by_plant = plant_buckets_from_dashboard(_dash_rows, date_iso,
                                                    now_mx())
    live_cond = live_conditions_from_dashboard(_dash_rows, date_iso,
                                               now_mx())

    for plant in portfolio.daily_report_plants():
        k = kpi.get(plant.plant_key, {})
        invs = sorted(per_plant_inv.get(plant.plant_key, {}).values(),
                      key=lambda i: (i.label or "", i.sn))
        energy, dc, note = (k.get("energy"), k.get("dc", "no_data"),
                            k.get("note", ""))
        expected, pp = k.get("expected"), k.get("pp")
        cloud_val, avail_val = k.get("cloud"), k.get("av")
        if energy is None:
            live = synthesize_live_energy(invs)
            if live is not None:
                # Evening report before kpi-eod has stamped the day
                # (2026-07-08: this path NEVER worked — it demanded KPI
                # rows that only exist next morning; SyncRuns exposed the
                # silent exit-2 on its first instrumented night).
                energy, dc = live, "live"
                note = ("Live evening estimate from telemetry — final "
                        "numbers in tomorrow's 07:05 report.")
                cond = live_cond.get(plant.plant_key, {})
                if cloud_val is None:
                    cloud_val = cond.get("cloud")
                if avail_val is None:
                    avail_val = cond.get("availability")
                if expected is None:
                    live_e = live_exp.get(plant.plant_key)
                    # v87 sunrise guard: a near-zero denominator turns
                    # the ratio into noise ("340% of expected" at 07:00
                    # on 0.6 kWh, user report 2026-07-11). The live
                    # figure speaks only once expected-so-far reaches
                    # 5% of the design day (or 25 kWh when no design):
                    # below that, honest dashes.
                    design_day = (k.get("design")
                                  or design_kwh_for_day(
                                      design_map, plant.plant_key,
                                      date_iso))
                    floor = (0.05 * design_day) if design_day else 25.0
                    if live_e and live_e >= floor:
                        expected = live_e
                        pp = round(energy / expected, 4)
                        note = ("Live evening estimate from telemetry; "
                                "expected is a live \u00b110% estimate "
                                "from measured irradiance, pro-rated to "
                                "the last complete hour — final numbers "
                                "in tomorrow's 07:05 report.")
        plants.append(PlantDay(
            plant_key=plant.plant_key,
            name=getattr(plant, "customer", "") or plant.plant_key,
            energy_kwh=energy, expected_kwh=expected,
            production_pct=pp, pr=k.get("pr"),
            availability=avail_val, soiling=k.get("soil"),
            cloud_pct=cloud_val, data_class=dc,
            status_note=note, inverters=invs,
            kwp_dc=getattr(plant, "kwp_dc", None),
            tariff_mxn_per_kwh=getattr(plant, "tariff_mxn_per_kwh", None),
            design_kwh=(k.get("design")
                        or design_kwh_for_day(design_map,
                                              plant.plant_key, date_iso)),
            maintenance_note=(
                maintenance_badge_text(maint_by_plant[plant.plant_key])
                if plant.plant_key in maint_by_plant else None),
            buckets=buckets_by_plant.get(plant.plant_key, [])))

    ledger = load_alerts_ledger(sheets)
    visible_keys = {p.plant_key for p in portfolio.daily_report_plants()}
    alerts = scoped_alerts(reportable_alerts(ledger.records),
                           visible_keys)
    sev_rank = {"CRITICAL": 0, "WARNING": 1}
    alerts.sort(key=lambda a: (sev_rank.get(a.severity, 2), a.plant_key,
                               a.inverter_sn))
    return ReportData(date_iso=date_iso, plants=plants, alerts=alerts)
