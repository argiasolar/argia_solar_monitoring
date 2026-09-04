"""Customer invoicing annex (v158) — per-plant, self-contained HTML.

v158 (2026-09-01, Tomasz's comparison against the old Looker factura):
the layout mirrors the Looker "anexo de la factura" — landscape, the
four stat cards, the daily generation chart with teórica/expectativa
lines and cloud cover, the ANNUAL chart with the pay line, and the
January–December table. The PR-diario chart is gone ("it is a noise").
Monthly figures for invoiced months come from the ARGIA Solar
workbook's Invoicing_Overview tab — the invoicing authority — so past
facturas always match what was actually billed; atoms only fill months
the workbook does not cover.

Replaces the Looker "anexo de la factura" page with a self-contained
HTML report in the ``webreport.py`` style: the whole selectable year is
embedded as per-day atoms, and the in-browser month picker only SUMS
atoms — no billing logic runs client-side (the anti-divergence rule).

Single source of truth for money and energy: the annex reads the
ALREADY-STAMPED ``billable_kwh`` and ``energy_kwh`` from KPI_Daily.

    measured  (energía producida)   = energy_kwh
    compensada (energía compensada) = max(0, billable_kwh - energy_kwh)
    billable                        = billable_kwh
    total a pagar (sin IVA)         = billable * tariff

``compensada`` therefore comes straight from the v91 deemed engine
(kpi_eod stamped it, contract-anchored, from approved customer events) —
the annex never recomputes it, so the customer document and the finance
report can never disagree. IVA is applied on the fiscal CFDI, not here.

The pure ``rollup_month`` / ``annual_rollup`` functions are the tested
reference; the embedded JS mirrors them exactly.
"""

from __future__ import annotations

import html as _html
import json
import logging
from typing import Dict, List, Optional

from argia.core.config import Portfolio
from argia.core import co2 as co2reg
from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.archive.kpi_daily import KPI_DAILY_TAB
from argia.finance.contract import load_contract_monthly
from argia.finance.income import Period
from argia.kpi.reconcile import date_key

LOG = logging.getLogger(__name__)

# Atom layout (one row per day). The JS uses the same fixed indices.
A_MEASURED = 0      # energy_kwh (energía producida)
A_THEORETICAL = 1   # expected_kwh (irradiance-based, "teórica")
A_DESIGN = 2        # design_kwh (contract expectation, "expectativa")
A_DEEMED = 3        # max(0, billable - energy) (energía compensada)
A_CLOUD = 4         # cloud_coverage_pct (0..1)
A_PR = 5            # performance ratio
A_AVAIL = 6         # availability
A_SOIL = 7          # soiling_loss_pct
ATOM_WIDTH = 8


FACTURA_NAME = {
    "GTO1": "TAIGENE", "MEX1": "SAG", "MEX2": "VITALMEX",
    "NL1": "PLASTIC_OMNIUM", "SLP1": "QUIMICA_COYOACAN",
    "SLP2": "HOLIDAY_INN",
}
"""File-name identity per plant: factura_<NAME>_<yyyymm>.pdf ("keep the
name as factura_plantname_yyyymm where plantname=TAIGENE not GTO1")."""


def parse_invoicing_overview(grid, year):
    """Pure: {PLANT: {ym: {kwh, penalty, income, expected}}} from the
    Invoicing_Overview grid (header Year|Month|Month_No|Plant_Key|
    Total_kWh|Penalty_kWh|Total_Income|Expected_kWh)."""
    out: Dict[str, Dict] = {}
    for r in grid[1:] if grid else []:
        if len(r) < 7 or str(r[0]).strip() != str(year):
            continue
        pk = str(r[3]).strip().upper()
        if not pk:
            continue
        try:
            m = int(float(r[2]))
        except (TypeError, ValueError):
            continue
        ym = "%s-%02d" % (year, m)
        out.setdefault(pk, {})[ym] = {
            "kwh": safe_float(r[4]),
            "penalty": safe_float(r[5]) or 0.0,
            "income": safe_float(r[6]),
            "expected": safe_float(r[7]) if len(r) > 7 else None,
        }
    return out


def load_invoicing_overview(year):
    """The invoicing authority: the ARGIA Solar workbook's
    Invoicing_Overview tab. Best-effort — no sheet id or an API error
    returns {} and the annex falls back to atoms (logged)."""
    import os as _os
    from argia.finance import invoicing_pg
    if invoicing_pg.source() == "pg":
        # v192: the invoicing register (PostgreSQL) in the sheet's shape;
        # a read failure degrades exactly like an unreadable tab.
        try:
            return parse_invoicing_overview(invoicing_pg.read_grid(), year)
        except Exception as e:  # noqa: BLE001
            LOG.error("invoicing register unreadable (%s) — falling back "
                      "to KPI atoms", e)
            return {}
    sid = _os.environ.get("ARGIA_SOLAR_SHEET_ID", "").strip()
    if not sid:
        LOG.warning("ARGIA_SOLAR_SHEET_ID not set — annex months fall "
                    "back to KPI atoms (invoiced history unavailable)")
        return {}
    try:
        s = SheetsClient(sheet_id=sid)
        grid = s.read_range("Invoicing_Overview", "A1:H2000")
        return parse_invoicing_overview(grid, year)
    except Exception as e:  # noqa: BLE001
        LOG.error("Invoicing_Overview unreadable (%s) — falling back "
                  "to KPI atoms", e)
        return {}


def _client_logo(pk):
    """Grayscale client logo data URI from the server bundle, '' when
    unavailable (the annex then shows the client name as text)."""
    try:
        import os as _os
        import sys as _sys
        bundle = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "..", "..", "server", "bundle")
        if bundle not in _sys.path:
            _sys.path.insert(0, bundle)
        from argia_client_logos import CLIENT_LOGOS
        return CLIENT_LOGOS.get(pk.upper(), ("", ""))[1]
    except Exception:  # noqa: BLE001
        return ""


def build_annex_data(sheets: SheetsClient, portfolio: Portfolio,
                     plant_key: str, window: Period,
                     history: Optional[Dict] = None) -> Dict:
    """Assemble the embedded dataset for one plant over ``window`` (the
    range the picker can select within, e.g. a calendar year).
    ``history`` is this plant's slice of Invoicing_Overview."""
    pk = plant_key.upper()
    plant = portfolio.plants.get(pk) or portfolio.plants.get(plant_key)
    if plant is None:
        raise ValueError("unknown plant_key: %s" % plant_key)

    contracts = load_contract_monthly(sheets)

    # tariff per month: Contract_Monthly first, plant tariff as fallback
    def tariff(y: int, m: int) -> Optional[float]:
        row = contracts.get((pk, y, m))
        if row is not None and row.tariff_mxn is not None:
            return row.tariff_mxn
        return plant.tariff_mxn_per_kwh

    # --- read KPI_Daily for this plant over the window (by header name) ---
    from argia.kpi.pg_kpi_source import kpi_grid
    raw = kpi_grid(sheets, "A1:ZZ")            # v190: sheet or PG
    header = [normalize_text(h) for h in (raw[0] if raw else [])]
    idx = {n: i for i, n in enumerate(header) if n}

    def get(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    by_day: Dict[str, List] = {}
    for row in (raw[1:] if raw else []):
        try:
            if normalize_text(get(row, "plant_key")).upper() != pk:
                continue
            d_iso = date_key(get(row, "date_iso"))
        except (KeyError, IndexError, TypeError):
            continue
        if not d_iso or not window.contains_iso(d_iso):
            continue
        energy = safe_float(get(row, "energy_kwh"))
        billable = safe_float(get(row, "billable_kwh"))
        # deemed straight from the stamped billable — never recomputed
        if billable is not None and energy is not None:
            deemed = max(0.0, billable - energy)
        else:
            deemed = 0.0
        by_day[d_iso] = [
            energy,
            safe_float(get(row, "expected_kwh")),
            safe_float(get(row, "design_kwh")),
            round(deemed, 2),
            _cloud_fraction(safe_float(get(row, "cloud_coverage_pct"))),
            safe_float(get(row, "pr")),
            safe_float(get(row, "availability")),
            safe_float(get(row, "soiling_loss_pct")),
        ]

    # dense day axis across the window (missing days -> all-None atom)
    days: List[str] = []
    atoms: List[List[Optional[float]]] = []
    tariff_by_month: Dict[str, Optional[float]] = {}
    cursor = window.start
    while cursor <= window.end:
        iso = cursor.isoformat()
        days.append(iso)
        atoms.append(by_day.get(iso, [None] * ATOM_WIDTH))
        ym = "%04d-%02d" % (cursor.year, cursor.month)
        if ym not in tariff_by_month:
            tariff_by_month[ym] = tariff(cursor.year, cursor.month)
        cursor += _one_day()

    return {
        "plant_key": pk,
        "client": plant.customer or pk,
        "factura_name": FACTURA_NAME.get(pk, pk),
        "client_logo": _client_logo(pk),
        "kwp": plant.kwp_dc,
        "days": days,
        "atoms": atoms,
        "tariff_by_month": tariff_by_month,
        "co2_factor": co2reg.factor(None, pk),
        "co2_factor_by_year": co2reg.factors_by_year(pk),
        "co2_factor_label": co2reg.label(pk),
        "co2_factor_contracted": pk in co2reg.PLANT_OVERRIDE,
        "history": history or {},
    }


def _cloud_fraction(v):
    """Cloud cover as a 0..1 fraction. KPI_Daily stores PERCENT
    (GTO1 Aug-26: 51.8 avg); rendered unscaled it painted the whole
    month as 100% cloud (Tomasz, 2026-09-01: "I doubt that in taigene
    or quimica coyocan there where clouds all month long"). Values
    already below 1.5 are taken as fractions and pass through."""
    if v is None:
        return None
    return round(v / 100.0, 4) if v > 1.5 else v


def _one_day():
    import datetime as dt
    return dt.timedelta(days=1)


def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def rollup_month(payload: Dict, ym: str) -> Dict:
    """Aggregate one month from the embedded data. PURE — the JS
    ``rollupMonth`` mirrors this exactly.

    Authority order: an invoiced month in ``history`` (the ARGIA Solar
    Invoicing_Overview slice) wins outright — produced, compensada and
    the peso amount are what was actually billed. Atoms only fill
    months the workbook does not carry."""
    days = payload["days"]
    atoms = payload["atoms"]
    tariff = payload["tariff_by_month"].get(ym)
    co2f = (payload.get("co2_factor_by_year") or {}).get(
        str(ym)[:4], payload["co2_factor"])
    h = (payload.get("history") or {}).get(ym)

    measured = deemed = 0.0
    design_sum = 0.0
    have_any = False
    for d, a in zip(days, atoms):
        if d[:7] != ym:
            continue
        if a[A_MEASURED] is not None:
            measured += a[A_MEASURED]
            have_any = True
        if a[A_DEEMED]:
            deemed += a[A_DEEMED]
        if a[A_DESIGN] is not None:
            design_sum += a[A_DESIGN]

    expected = design_sum if design_sum else None
    if h is not None:
        if h.get("expected") is not None:
            expected = h["expected"]
        if h.get("kwh") is not None:
            measured = h["kwh"]
            deemed = h.get("penalty") or 0.0
            have_any = True

    billable = measured + deemed
    if h is not None and h.get("kwh") is not None:
        amount = h.get("income")
    else:
        amount = billable * tariff if tariff is not None else None
    return {
        "ym": ym,
        "measured_kwh": round(measured, 1),
        "deemed_kwh": round(deemed, 1),
        "billable_kwh": round(billable, 1),
        "expected_kwh": round(expected, 1) if expected is not None else None,
        "tariff": tariff,
        "amount_mxn": round(amount, 2) if amount is not None else None,
        "co2_kg": round(billable * co2f, 1),
        "has_data": have_any,
    }


def annual_rollup(payload: Dict) -> List[Dict]:
    """All twelve months of the window's year, in order — the old
    factura table shows January through December with zeros for months
    not yet invoiced, so this does too. PURE — mirrors the JS."""
    year = payload["days"][0][:4] if payload["days"] else "1970"
    return [rollup_month(payload, "%s-%02d" % (year, m))
            for m in range(1, 13)]


def _logo_uri() -> str:
    try:
        import os as _os
        import sys as _sys
        bundle = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               "..", "..", "server", "bundle")
        if bundle not in _sys.path:
            _sys.path.insert(0, bundle)
        from argia_logo import LOGO_URI
        return LOGO_URI
    except Exception:  # noqa: BLE001
        return ""


def render_annex_html(payload: Dict, generated_at: str,
                      default_ym: Optional[str] = None) -> str:
    """Self-contained HTML factura in the Looker layout: header with
    client + ARGIA identity and the period picker, four stat cards,
    the daily generation chart (bars, teórica + expectativa lines,
    cloud cover on the right axis), the annual chart (generada +
    expectativa bars, pay line) and the January–December table.
    Landscape print. Spanish only — the PDF is a customer document."""
    data_json = json.dumps(payload, separators=(",", ":"))
    client = _html.escape(payload["client"])
    logo = _logo_uri()
    clogo = payload.get("client_logo") or ""

    if not default_ym:
        for r in reversed(annual_rollup(payload)):
            if r["has_data"]:
                default_ym = r["ym"]
                break
    if not default_ym and payload["days"]:
        default_ym = payload["days"][-1][:7]

    argia_img = (f'<img src="{logo}" alt="ARGIA SOLAR" style="height:30px">'
                 if logo else '<span style="font-weight:600;letter-spacing:'
                 '.2em">ARGIA SOLAR</span>')
    client_img = (f'<img src="{clogo}" alt="{client}" style="max-height:44px;'
                  'max-width:150px;object-fit:contain">' if clogo else
                  f'<span style="font-weight:700">{client}</span>')

    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anexo de facturación — {client}</title>
<style>
:root{{--bg:#fafafa;--card:#fff;--ink:#1f1e1b;--muted:#6b6a64;
--line:#e0e0da;--blue:#4C9BE8;--blued:#185FA5;--green:#0E8A6D;
--amber:#E8A13D;--grey:#C9C9C2}}
*{{box-sizing:border-box}}
@page{{size:A4 landscape;margin:7mm}}
body{{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;font-size:13px}}
.wrap{{max-width:1350px;margin:0 auto;padding:8px 14px}}
.top{{display:flex;align-items:center;gap:18px;flex-wrap:wrap;
margin-bottom:10px}}
.top .ttl{{font-size:19px;font-weight:600;color:var(--ink)}}
.top .sub{{font-size:13px;color:var(--muted)}}
.top .right{{margin-left:auto;display:flex;gap:12px;align-items:center}}
select{{font:inherit;padding:6px 10px;border:1px solid var(--line);
border-radius:6px;background:var(--card)}}
button.dl{{font:inherit;padding:6px 14px;border:1px solid var(--line);
border-radius:6px;background:#ececec;cursor:pointer}}
.row{{display:grid;gap:12px;margin-bottom:12px}}
.row1{{grid-template-columns:330px 1fr}}
.row2{{grid-template-columns:1fr 1fr}}
.cards{{display:grid;grid-template-columns:1fr 1fr;gap:12px;
align-content:start}}
.card{{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 12px;min-height:64px}}
.card .lab{{color:var(--muted);font-size:11px;margin-bottom:5px}}
.card .val{{font-size:21px;font-weight:600}}
.card.pay .val{{color:var(--blued)}}
.card.co2 .val{{color:var(--green)}}
.card .fnote{{color:var(--muted);font-size:10px;margin-top:3px;
 line-height:1.3}}
.sec{{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:10px 14px}}
.sec h2{{font-size:12.5px;font-weight:600;margin:0 0 8px;
text-align:center;color:#3c3b36}}
.leg{{display:flex;gap:16px;justify-content:center;flex-wrap:wrap;
font-size:10.5px;color:var(--muted);margin-top:4px}}
.leg span{{display:inline-flex;align-items:center;gap:5px}}
.k{{width:11px;height:11px;border-radius:2px;display:inline-block}}
.kl{{width:14px;height:0;border-top:2.4px solid;display:inline-block}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{text-align:right;padding:4.5px 8px}}
th:first-child,td:first-child{{text-align:left}}
thead th{{color:var(--muted);font-weight:500;background:#f2f2ee;
border-bottom:1px solid var(--line)}}
tbody tr{{border-bottom:1px solid #efefe9}}
tfoot td{{font-weight:700;border-top:2px solid var(--line)}}
@media print{{select,button.dl{{display:none}}
body{{background:#fff}}.sec,.card{{break-inside:avoid}}
.wrap{{padding:0}}}}
</style></head>
<body><div class="wrap">
<div class="top">
  {client_img}
  <div><div class="ttl">El anexo de la factura correspondiente
   al periodo.</div>
  <div class="sub">energía PPA · <span id="period"></span></div></div>
  <div class="right">
    <select id="month"></select>
    <button class="dl" onclick="window.print()">Descargar</button>
    {argia_img}
  </div>
</div>

<div class="row row1">
  <div class="cards">
    <div class="card pay"><div class="lab">Total a Pagar Este Mes
      (sin IVA)</div><div class="val" id="c_pay">&mdash;</div></div>
    <div class="card"><div class="lab">Tarifa ARGIA</div>
      <div class="val" id="c_tar">&mdash;</div></div>
    <div class="card"><div class="lab">Energía Producida</div>
      <div class="val" id="c_prod">&mdash;</div></div>
    <div class="card co2"><div class="lab">CO₂ Emisiones
      Evitadas</div><div class="val" id="c_co2">&mdash;</div>
      <div class="fnote" id="c_co2f"></div></div>
  </div>
  <div class="sec"><h2>Generación Fotovoltaica (kWh)</h2>
    <div id="chart_gen"></div>
    <div class="leg">
     <span><span class="k" style="background:var(--blue)"></span>
      Energía generada kWh</span>
     <span><span class="kl" style="border-color:var(--amber)"></span>
      Energía teórica</span>
     <span><span class="kl" style="border-color:var(--green)"></span>
      Energía expectativa</span>
     <span><span class="k" style="background:var(--grey)"></span>
      Cobertura de nubes</span>
    </div>
  </div>
</div>

<div class="row row2">
  <div class="sec"><h2>Generación Fotovoltaica Anual</h2>
    <div id="chart_annual"></div>
    <div class="leg">
     <span><span class="k" style="background:var(--blue)"></span>
      Energía generada kWh</span>
     <span><span class="k" style="background:var(--grey)"></span>
      Energía expectativa kWh</span>
     <span><span class="kl" style="border-color:var(--amber)"></span>
      Total a pagar</span>
    </div>
  </div>
  <div class="sec"><h2>Generación Anual</h2>
    <table id="annual"><thead><tr>
      <th>Mes</th><th>Energía Producida</th>
      <th>Energía Compensada</th><th>Total a Pagar</th></tr></thead>
      <tbody id="annual_body"></tbody>
      <tfoot id="annual_foot"></tfoot></table>
  </div>
</div>

</div>

<script>
const D = {data_json};
const AI = {{measured:{A_MEASURED},theo:{A_THEORETICAL},design:{A_DESIGN},
  deemed:{A_DEEMED},cloud:{A_CLOUD}}};
const MNAME = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio",
  "Agosto","Septiembre","Octubre","Noviembre","Diciembre"];
const money = x => x==null ? "$0" :
  "$"+x.toLocaleString("es-MX",{{maximumFractionDigits:2,minimumFractionDigits:2}});
const kwh0 = x => x==null ? "0 kWh" :
  Math.round(x).toLocaleString("es-MX")+" kWh";
const monthLabel = ym => {{const [y,m]=ym.split("-");return MNAME[+m]+" "+y;}};

// PURE mirror of annex.rollup_month — invoiced history wins outright
function rollupMonth(ym){{
  const t = D.tariff_by_month[ym];
  const h = (D.history||{{}})[ym];
  let measured=0, deemed=0, design=0, any=false;
  D.days.forEach((d,i)=>{{
    if(d.slice(0,7)!==ym) return;
    const a=D.atoms[i];
    if(a[AI.measured]!=null){{measured+=a[AI.measured];any=true;}}
    if(a[AI.deemed]) deemed+=a[AI.deemed];
    if(a[AI.design]!=null) design+=a[AI.design];
  }});
  let expected = design||null, amount=null;
  if(h && h.expected!=null) expected=h.expected;
  if(h && h.kwh!=null){{
    measured=h.kwh; deemed=h.penalty||0; any=true; amount=h.income;
  }}else if(t!=null){{ amount=(measured+deemed)*t; }}
  const billable=measured+deemed;
  return {{ym, measured, deemed, billable, expected, tariff:t, amount,
    co2: billable*((D.co2_factor_by_year||{{}})[ym.slice(0,4)]
                   ??D.co2_factor), has_data:any}};
}}

function dailyChart(rows){{
  // bars = generada; amber line = teórica; green line = expectativa;
  // grey area = cobertura de nubes on the right 0–100% axis
  const W=980, H=252, PL=46, PR=40, PT=10, PB=26;
  const iw=W-PL-PR, ih=H-PT-PB;
  let max=0;
  rows.forEach(r=>{{["bar","theo","design"].forEach(k=>{{
    if(r[k]!=null && r[k]>max) max=r[k];}});}});
  max = max>0 ? max*1.12 : 1;
  const n=rows.length||1;
  const bw=Math.max(2, iw/n*0.62);
  const x=i=>PL + iw*(i+0.5)/n;
  const y=v=>PT + ih*(1-v/max);
  const yc=v=>PT + ih*(1-v);                     // cloud 0..1
  let s=`<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;height:auto">`;
  s+=`<line x1="${{PL}}" y1="${{PT+ih}}" x2="${{W-PR}}" y2="${{PT+ih}}" stroke="var(--line)"/>`;
  [0,0.5,1].forEach(f=>{{
    s+=`<text x="${{PL-5}}" y="${{y(max*f)+3}}" font-size="9" fill="var(--muted)" text-anchor="end">${{Math.round(max*f/1000)}}k</text>`;
    s+=`<text x="${{W-PR+5}}" y="${{yc(f)+3}}" font-size="9" fill="var(--muted)">${{f*100}}%</text>`;}});
  // cloud area first (behind the bars)
  const cp=rows.map((r,i)=> r.cloud!=null?`${{x(i)}},${{yc(Math.min(1,r.cloud))}}`:null);
  if(cp.some(Boolean)){{
    const pts=cp.map((p,i)=>p||`${{x(i)}},${{yc(0)}}`).join(" ");
    s+=`<polygon points="${{PL}},${{yc(0)}} ${{pts}} ${{W-PR}},${{yc(0)}}" fill="var(--grey)" opacity="0.45"/>`;}}
  rows.forEach((r,i)=>{{ if(r.bar!=null){{
    s+=`<rect x="${{x(i)-bw/2}}" y="${{y(r.bar)}}" width="${{bw}}" height="${{PT+ih-y(r.bar)}}" fill="var(--blue)"/>`;}}}});
  [["theo","var(--amber)"],["design","var(--green)"]].forEach(([k,col])=>{{
    const pts=rows.map((r,i)=> r[k]!=null ? `${{x(i)}},${{y(r[k])}}`:null).filter(Boolean).join(" ");
    if(pts) s+=`<polyline points="${{pts}}" fill="none" stroke="${{col}}" stroke-width="1.7"/>`;}});
  const step=Math.ceil(n/16);
  rows.forEach((r,i)=>{{ if(i%step===0)
    s+=`<text x="${{x(i)}}" y="${{H-8}}" font-size="8.5" fill="var(--muted)" text-anchor="middle">${{r.label}}</text>`;}});
  return s+"</svg>";
}}

function annualChart(rows){{
  // grouped bars generada + expectativa, pay line on the right $ axis
  const W=640, H=250, PL=52, PR=56, PT=12, PB=40;
  const iw=W-PL-PR, ih=H-PT-PB;
  let maxK=0, maxP=0;
  rows.forEach(r=>{{
    if(r.measured>maxK) maxK=r.measured;
    if(r.expected!=null && r.expected>maxK) maxK=r.expected;
    if(r.amount!=null && r.amount>maxP) maxP=r.amount;}});
  maxK=maxK>0?maxK*1.15:1; maxP=maxP>0?maxP*1.15:1;
  const n=rows.length||1;
  const gw=iw/n, bw=Math.max(3, gw*0.28);
  const x=i=>PL + gw*(i+0.5);
  const y=v=>PT + ih*(1-v/maxK);
  const yp=v=>PT + ih*(1-v/maxP);
  let s=`<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;height:auto">`;
  s+=`<line x1="${{PL}}" y1="${{PT+ih}}" x2="${{W-PR}}" y2="${{PT+ih}}" stroke="var(--line)"/>`;
  [0,0.5,1].forEach(f=>{{
    s+=`<text x="${{PL-5}}" y="${{y(maxK*f)+3}}" font-size="9" fill="var(--muted)" text-anchor="end">${{Math.round(maxK*f/1000)}}k</text>`;
    s+=`<text x="${{W-PR+5}}" y="${{yp(maxP*f)+3}}" font-size="9" fill="var(--muted)">$${{Math.round(maxP*f/1000)}}k</text>`;}});
  rows.forEach((r,i)=>{{
    if(r.expected!=null)
      s+=`<rect x="${{x(i)+1}}" y="${{y(r.expected)}}" width="${{bw}}" height="${{PT+ih-y(r.expected)}}" fill="var(--grey)"/>`;
    if(r.measured>0)
      s+=`<rect x="${{x(i)-bw-1}}" y="${{y(r.measured)}}" width="${{bw}}" height="${{PT+ih-y(r.measured)}}" fill="var(--blue)"/>`;
  }});
  const pts=rows.map((r,i)=> r.amount!=null&&r.amount>0 ? `${{x(i)}},${{yp(r.amount)}}`:null).filter(Boolean).join(" ");
  if(pts) s+=`<polyline points="${{pts}}" fill="none" stroke="var(--amber)" stroke-width="2"/>`;
  rows.forEach((r,i)=>{{
    s+=`<text x="${{x(i)}}" y="${{H-22}}" font-size="8.5" fill="var(--muted)" text-anchor="middle" transform="rotate(28 ${{x(i)}} ${{H-22}})">${{MNAME[+r.ym.slice(5)].slice(0,3)}}</text>`;}});
  return s+"</svg>";
}}

function drawMonth(ym){{
  const r=rollupMonth(ym);
  document.getElementById("period").textContent=monthLabel(ym);
  document.getElementById("c_pay").innerHTML=money(r.amount);
  document.getElementById("c_tar").innerHTML=r.tariff!=null?
    "$"+r.tariff.toLocaleString("es-MX",{{maximumFractionDigits:4,minimumFractionDigits:2}}):"—";
  document.getElementById("c_prod").innerHTML=kwh0(r.measured);
  document.getElementById("c_co2").innerHTML=
    Math.round(r.co2).toLocaleString("es-MX")+" kg";
  const cf=(D.co2_factor_by_year||{{}})[ym.slice(0,4)]??D.co2_factor;
  document.getElementById("c_co2f").textContent=
    "factor "+cf.toFixed(3)+" kg CO\u2082/kWh"+
    (D.co2_factor_contracted?" (contratado)":" (SEMARNAT/CRE)");
  const rows=[];
  D.days.forEach((d,i)=>{{ if(d.slice(0,7)!==ym) return;
    const a=D.atoms[i];
    rows.push({{label:d.slice(8), bar:a[AI.measured], theo:a[AI.theo],
      design:a[AI.design], cloud:a[AI.cloud]}});
  }});
  document.getElementById("chart_gen").innerHTML=dailyChart(rows);
}}

function drawAnnual(){{
  const year=D.days.length?D.days[0].slice(0,4):"";
  const out=[];
  for(let m=1;m<=12;m++) out.push(rollupMonth(year+"-"+String(m).padStart(2,"0")));
  let tb="", tm=0,td=0,ta=0;
  out.forEach(r=>{{
    tb+=`<tr><td>${{MNAME[+r.ym.slice(5)]}}</td><td>${{kwh0(r.measured)}}</td>`+
        `<td>${{kwh0(r.deemed)}}</td><td>${{money(r.amount!=null&&r.has_data?r.amount:0)}}</td></tr>`;
    tm+=r.measured; td+=r.deemed; if(r.has_data&&r.amount!=null) ta+=r.amount;
  }});
  document.getElementById("annual_body").innerHTML=tb;
  document.getElementById("annual_foot").innerHTML=
    `<tr><td>Total general</td><td>${{kwh0(tm)}}</td><td>${{kwh0(td)}}</td><td>${{money(ta)}}</td></tr>`;
  document.getElementById("chart_annual").innerHTML=annualChart(out);
}}

(function init(){{
  const sel=document.getElementById("month");
  const year=D.days.length?D.days[0].slice(0,4):"";
  for(let m=1;m<=12;m++){{const ym=year+"-"+String(m).padStart(2,"0");
    const o=document.createElement("option");
    o.value=ym; o.textContent=monthLabel(ym); sel.appendChild(o);}}
  sel.value="{default_ym}";
  sel.addEventListener("change",()=>drawMonth(sel.value));
  drawMonth("{default_ym}");
  drawAnnual();
}})();
</script>
</body></html>"""
