"""Customer invoicing annex (v93) — per-plant, self-contained HTML.

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
from argia.core.constants import CO2_KG_PER_KWH
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


def build_annex_data(sheets: SheetsClient, portfolio: Portfolio,
                     plant_key: str, window: Period) -> Dict:
    """Assemble the embedded dataset for one plant over ``window`` (the
    range the picker can select within, e.g. a calendar year)."""
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
    raw = sheets.read_range(KPI_DAILY_TAB, "A1:ZZ")
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
            safe_float(get(row, "cloud_coverage_pct")),
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
        "kwp": plant.kwp_dc,
        "days": days,
        "atoms": atoms,
        "tariff_by_month": tariff_by_month,
        "co2_factor": CO2_KG_PER_KWH,
    }


def _one_day():
    import datetime as dt
    return dt.timedelta(days=1)


def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def rollup_month(payload: Dict, ym: str) -> Dict:
    """Aggregate one month (``'YYYY-MM'``) from the embedded atoms. PURE —
    the JS ``rollupMonth`` mirrors this exactly. Money/energy come only
    from summing atoms; nothing is recomputed from contracts here."""
    days = payload["days"]
    atoms = payload["atoms"]
    tariff = payload["tariff_by_month"].get(ym)
    co2f = payload["co2_factor"]

    measured = deemed = 0.0
    prs: List[float] = []
    avails: List[float] = []
    soils: List[float] = []
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
        if a[A_PR] is not None:
            prs.append(a[A_PR])
        if a[A_AVAIL] is not None:
            avails.append(a[A_AVAIL])
        if a[A_SOIL] is not None:
            soils.append(a[A_SOIL])

    billable = measured + deemed
    amount = billable * tariff if tariff is not None else None
    prod_pct = (measured / design_sum) if design_sum else None
    return {
        "ym": ym,
        "measured_kwh": round(measured, 1),
        "deemed_kwh": round(deemed, 1),
        "billable_kwh": round(billable, 1),
        "tariff": tariff,
        "amount_mxn": round(amount, 2) if amount is not None else None,
        "co2_kg": round(billable * co2f, 1),
        "pr": _mean(prs),
        "availability": _mean(avails),
        "production_pct": prod_pct,
        "soiling": _mean(soils),
        "has_data": have_any,
    }


def annual_rollup(payload: Dict) -> List[Dict]:
    """One row per month present in the window: measured / deemed / total.
    PURE — mirrors the JS ``annualRollup``."""
    months = []
    seen = set()
    for d in payload["days"]:
        ym = d[:7]
        if ym not in seen:
            seen.add(ym)
            months.append(ym)
    return [rollup_month(payload, ym) for ym in months]


# ----------------------------------------------------------------- render

def _logo_uri() -> str:
    try:
        from argia.report.dashboard_html import LOGO_B64
        return "data:image/png;base64," + LOGO_B64
    except Exception:  # noqa: BLE001
        return ""


def _fmt(x: Optional[float], unit: str = "") -> str:
    if x is None:
        return "&mdash;"
    return "{:,.0f}{}".format(x, unit)


def render_annex_html(payload: Dict, generated_at: str) -> str:
    """Self-contained HTML annex: embedded atoms + month picker + charts.
    The picker only sums atoms (JS mirrors :func:`rollup_month`)."""
    data_json = json.dumps(payload, separators=(",", ":"))
    client = _html.escape(payload["client"])
    pk = _html.escape(payload["plant_key"])
    kwp = payload.get("kwp")
    kwp_txt = ("%d kWp" % round(kwp)) if kwp else ""
    logo = _logo_uri()
    # default selected month = last month in the window that has data
    default_ym = ""
    for r in reversed(annual_rollup(payload)):
        if r["has_data"]:
            default_ym = r["ym"]
            break
    if not default_ym and payload["days"]:
        default_ym = payload["days"][-1][:7]

    logo_img = (f'<img src="{logo}" alt="ARGIA SOLAR" style="height:30px">'
                if logo else '<span style="font-weight:600">ARGIA SOLAR</span>')

    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Anexo de facturaci\u00f3n \u2014 {client}</title>
<style>
:root{{--bg:#faf9f5;--card:#fff;--ink:#1f1e1b;--muted:#6b6a64;
--line:#e4e3dc;--blue:#2F6DB0;--blued:#185FA5;--green:#0E8A6D;
--amber:#B7791F;--red:#A32D2D;--accentbg:#eef3fb}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;font-size:14px}}
.wrap{{max-width:1120px;margin:0 auto;padding:20px}}
.top{{display:flex;justify-content:space-between;align-items:center;
gap:16px;flex-wrap:wrap;border-bottom:1px solid var(--line);
padding-bottom:14px;margin-bottom:20px}}
.top h1{{font-size:16px;font-weight:600;margin:0}}
.top .sub{{color:var(--muted);font-size:13px}}
select{{font:inherit;padding:7px 12px;border:1px solid var(--line);
border-radius:8px;background:var(--card)}}
button.dl{{font:inherit;padding:7px 14px;border:1px solid var(--line);
border-radius:8px;background:var(--card);cursor:pointer}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;
margin-bottom:20px}}
.card{{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:14px 16px}}
.card .lab{{color:var(--muted);font-size:12px}}
.card .val{{font-size:24px;font-weight:600;margin-top:4px}}
.card.pay .val{{color:var(--blued)}}
.card.co2 .val{{color:var(--green)}}
.sec{{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:16px 18px;margin-bottom:20px}}
.sec h2{{font-size:14px;font-weight:600;margin:0 0 12px}}
.perf{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;
margin-bottom:14px}}
.perf .lab{{color:var(--muted);font-size:12px}}
.perf .val{{font-size:22px;font-weight:600;margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:right;padding:7px 10px}}
th:first-child,td:first-child{{text-align:left}}
thead th{{color:var(--muted);font-weight:500;border-bottom:1px solid var(--line)}}
tbody tr{{border-bottom:1px solid var(--line)}}
tfoot td{{font-weight:600;border-top:2px solid var(--line)}}
.foot{{color:var(--muted);font-size:11.5px;line-height:1.6;
border-top:1px solid var(--line);padding-top:12px;margin-top:8px}}
@media print{{select,button.dl{{display:none}}
body{{background:#fff}}.sec,.card{{break-inside:avoid}}}}
</style></head>
<body><div class="wrap">
<div class="top">
  <div>{logo_img}
    <h1>Anexo de facturaci\u00f3n \u2014 {client}</h1>
    <div class="sub">{pk} \u00b7 {kwp_txt} \u00b7 energ\u00eda PPA</div>
  </div>
  <div style="display:flex;gap:10px;align-items:center">
    <select id="month"></select>
    <button class="dl" onclick="window.print()">Descargar</button>
  </div>
</div>

<div class="cards">
  <div class="card pay"><div class="lab">Total a pagar (sin IVA)</div>
    <div class="val" id="c_pay">&mdash;</div></div>
  <div class="card"><div class="lab">Tarifa ARGIA</div>
    <div class="val" id="c_tar">&mdash;</div></div>
  <div class="card"><div class="lab">Energ\u00eda producida</div>
    <div class="val" id="c_prod">&mdash;</div></div>
  <div class="card co2"><div class="lab">CO\u2082 evitado</div>
    <div class="val" id="c_co2">&mdash;</div></div>
</div>

<div class="sec">
  <h2>Rendimiento del sistema \u00b7 system performance</h2>
  <div class="perf">
    <div><div class="lab">Rendimiento (PR)</div>
      <div class="val" id="p_pr">&mdash;</div></div>
    <div><div class="lab">Disponibilidad</div>
      <div class="val" id="p_av">&mdash;</div></div>
    <div><div class="lab">Generaci\u00f3n vs esperada</div>
      <div class="val" id="p_vs">&mdash;</div></div>
    <div><div class="lab">P\u00e9rdida por suciedad</div>
      <div class="val" id="p_soil">&mdash;</div></div>
  </div>
  <div class="lab" style="color:var(--muted);font-size:12px;margin-bottom:4px">
    Generaci\u00f3n diaria \u00b7 producida (barras) vs te\u00f3rica y esperada (l\u00edneas)</div>
  <div id="chart_gen"></div>
  <div class="lab" style="color:var(--muted);font-size:12px;margin:10px 0 4px">
    Performance ratio diario</div>
  <div id="chart_pr"></div>
</div>

<div class="sec">
  <h2>Generaci\u00f3n anual</h2>
  <table id="annual"><thead><tr>
    <th>Mes</th><th>Energ\u00eda producida</th><th>Energ\u00eda compensada</th>
    <th>Total a pagar</th></tr></thead>
    <tbody id="annual_body"></tbody>
    <tfoot id="annual_foot"></tfoot></table>
</div>

<div class="foot" id="foot"></div>
</div>

<script>
const D = {data_json};
const AI = {{measured:{A_MEASURED},theo:{A_THEORETICAL},design:{A_DESIGN},
  deemed:{A_DEEMED},cloud:{A_CLOUD},pr:{A_PR},avail:{A_AVAIL},soil:{A_SOIL}}};
const MNAME = ["","Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio",
  "Agosto","Septiembre","Octubre","Noviembre","Diciembre"];
const money = x => x==null ? "\u2014" :
  "$"+x.toLocaleString("es-MX",{{maximumFractionDigits:2,minimumFractionDigits:2}});
const kwh = x => x==null ? "\u2014" :
  Math.round(x).toLocaleString("es-MX")+" kWh";
const pct = x => x==null ? "\u2014" : (x*100).toFixed(0)+"%";
const mean = a => a.length ? a.reduce((s,v)=>s+v,0)/a.length : null;
const monthLabel = ym => {{const [y,m]=ym.split("-");return MNAME[+m]+" "+y;}};

// PURE mirror of annex.rollup_month
function rollupMonth(ym){{
  const t = D.tariff_by_month[ym];
  let measured=0, deemed=0, design=0, any=false;
  const prs=[], avs=[], sos=[];
  D.days.forEach((d,i)=>{{
    if(d.slice(0,7)!==ym) return;
    const a=D.atoms[i];
    if(a[AI.measured]!=null){{measured+=a[AI.measured];any=true;}}
    if(a[AI.deemed]) deemed+=a[AI.deemed];
    if(a[AI.design]!=null) design+=a[AI.design];
    if(a[AI.pr]!=null) prs.push(a[AI.pr]);
    if(a[AI.avail]!=null) avs.push(a[AI.avail]);
    if(a[AI.soil]!=null) sos.push(a[AI.soil]);
  }});
  const billable=measured+deemed;
  return {{ym, measured, deemed, billable, tariff:t,
    amount: t!=null ? billable*t : null,
    co2: billable*D.co2_factor,
    pr: mean(prs), availability: mean(avs), soiling: mean(sos),
    production_pct: design ? measured/design : null, has_data:any}};
}}

function svgChart(rows, opts){{
  // rows: [{{label, bar, line1, line2}}]; simple bars + up to 2 lines
  const W=1040, H=210, PL=44, PR=12, PT=14, PB=28;
  const iw=W-PL-PR, ih=H-PT-PB;
  let max=0;
  rows.forEach(r=>{{["bar","line1","line2"].forEach(k=>{{
    if(r[k]!=null && r[k]>max) max=r[k];}});}});
  max = max>0 ? max*1.1 : 1;
  const n=rows.length||1;
  const bw=Math.max(2, iw/n*0.6);
  const x=i=>PL + iw*(i+0.5)/n;
  const y=v=>PT + ih*(1-v/max);
  let s=`<svg viewBox="0 0 ${{W}} ${{H}}" style="width:100%;height:auto">`;
  s+=`<line x1="${{PL}}" y1="${{PT}}" x2="${{PL}}" y2="${{PT+ih}}" stroke="var(--line)"/>`;
  s+=`<line x1="${{PL}}" y1="${{PT+ih}}" x2="${{W-PR}}" y2="${{PT+ih}}" stroke="var(--line)"/>`;
  s+=`<text x="6" y="${{PT+6}}" font-size="10" fill="var(--muted)">${{Math.round(max).toLocaleString("es-MX")}}</text>`;
  rows.forEach((r,i)=>{{ if(r.bar!=null){{
    const h=ih*r.bar/max;
    s+=`<rect x="${{x(i)-bw/2}}" y="${{y(r.bar)}}" width="${{bw}}" height="${{h}}" fill="#7EB6E8"/>`;}}}});
  ["line1","line2"].forEach((k,li)=>{{
    const col= li==0 ? "var(--amber)" : "var(--green)";
    const pts=rows.map((r,i)=> r[k]!=null ? `${{x(i)}},${{y(r[k])}}`:null).filter(Boolean).join(" ");
    if(pts) s+=`<polyline points="${{pts}}" fill="none" stroke="${{col}}" stroke-width="1.6"/>`;}});
  if(opts&&opts.baseline!=null){{
    const yb=y(opts.baseline);
    s+=`<line x1="${{PL}}" y1="${{yb}}" x2="${{W-PR}}" y2="${{yb}}" stroke="#888780" stroke-dasharray="5 4"/>`;}}
  const step=Math.ceil(n/8);
  rows.forEach((r,i)=>{{ if(i%step===0)
    s+=`<text x="${{x(i)}}" y="${{H-8}}" font-size="10" fill="var(--muted)" text-anchor="middle">${{r.label}}</text>`;}});
  return s+"</svg>";
}}

function drawMonth(ym){{
  const r=rollupMonth(ym);
  document.getElementById("c_pay").innerHTML=money(r.amount);
  document.getElementById("c_tar").innerHTML=r.tariff!=null?money(r.tariff):"\u2014";
  document.getElementById("c_prod").innerHTML=kwh(r.measured);
  document.getElementById("c_co2").innerHTML=
    r.co2!=null?Math.round(r.co2).toLocaleString("es-MX")+" kg":"\u2014";
  document.getElementById("p_pr").innerHTML=pct(r.pr);
  document.getElementById("p_av").innerHTML=pct(r.availability);
  document.getElementById("p_vs").innerHTML=pct(r.production_pct);
  document.getElementById("p_soil").innerHTML=pct(r.soiling);

  const genRows=[], prRows=[];
  D.days.forEach((d,i)=>{{ if(d.slice(0,7)!==ym) return;
    const a=D.atoms[i], lbl=d.slice(8);
    genRows.push({{label:lbl, bar:a[AI.measured], line1:a[AI.theo],
      line2:a[AI.design]}});
    prRows.push({{label:lbl, line1:a[AI.pr]}});
  }});
  document.getElementById("chart_gen").innerHTML=svgChart(genRows,{{}});
  document.getElementById("chart_pr").innerHTML=svgChart(prRows,{{baseline:0.86}});
}}

function drawAnnual(){{
  const seen=[], out=[];
  D.days.forEach(d=>{{const ym=d.slice(0,7); if(!seen.includes(ym)){{seen.push(ym);out.push(rollupMonth(ym));}}}});
  let tb="", tm=0,td=0,ta=0;
  out.forEach(r=>{{
    tb+=`<tr><td>${{monthLabel(r.ym)}}</td><td>${{kwh(r.measured)}}</td>`+
        `<td>${{kwh(r.deemed)}}</td><td>${{money(r.amount)}}</td></tr>`;
    tm+=r.measured; td+=r.deemed; ta+=(r.amount||0);
  }});
  document.getElementById("annual_body").innerHTML=tb;
  document.getElementById("annual_foot").innerHTML=
    `<tr><td>Total</td><td>${{kwh(tm)}}</td><td>${{kwh(td)}}</td><td>${{money(ta)}}</td></tr>`;
}}

(function init(){{
  const sel=document.getElementById("month");
  const seen=[];
  D.days.forEach(d=>{{const ym=d.slice(0,7); if(!seen.includes(ym)) seen.push(ym);}});
  seen.forEach(ym=>{{const o=document.createElement("option");
    o.value=ym; o.textContent=monthLabel(ym); sel.appendChild(o);}});
  sel.value="{default_ym}";
  sel.addEventListener("change",()=>drawMonth(sel.value));
  document.getElementById("foot").innerHTML=
    "Energ\u00eda compensada = billable_kwh \u2212 energy_kwh de KPI_Daily "+
    "(motor de compensaci\u00f3n v91, anclado al contrato). Producida y "+
    "compensada facturadas a la tarifa del mes. IVA se aplica en el CFDI "+
    "fiscal. CO\u2082 a "+D.co2_factor+" kg/kWh. Generado "+
    "{generated_at}.";
  drawMonth("{default_ym}");
  drawAnnual();
}})();
</script>
</body></html>"""
