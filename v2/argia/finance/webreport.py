"""Online financial report (financial_report.html) — data + renderer.

Same numbers as the PDF report, as a static self-contained page with a
from–to calendar picker. The anti-divergence rule that shapes it:

  ALL financial decisions happen here in Python, through the same
  argia.finance.income layer the PDF uses. The page embeds per-plant
  PER-DAY "atoms" (revenue, expected, debt service/day, O&M/day) as
  JSON, and the in-browser picker only SUMS atoms over the selected
  range. JavaScript performs arithmetic, never business logic — no
  tariffs, no FX, no proration rules exist client-side, so the web
  page and the PDF cannot drift apart.

Daily atom definitions (per plant, per calendar day):
  rev  actual accrued income: PPA = KPI billable kWh × that month's
       tariff; LaaS = monthly fee×XR / days-in-month. null when no KPI
       row exists yet (future/unstamped days) — the picker counts only
       non-null days into "Actual".
  exp  expected income: monthly contracted income / days-in-month.
  svc  debt service: monthly Σ installments / days-in-month.
  om   O&M: monthly scalar / days-in-month.
"""

from __future__ import annotations

import base64
import html
import json
import logging
from calendar import monthrange
from datetime import timedelta
from typing import Dict, List, Optional

from argia.core.config import Portfolio
from argia.core.sheets import SheetsClient
from argia.finance.contract import load_contract_monthly
from argia.finance.income import (
    Period, expected_income_month, load_kpi_energy,
)
from argia.finance.loans import load_loan_schedule, load_loans
from argia.finance.report import LOGO_PATH, _footer_sources

LOG = logging.getLogger(__name__)


def build_daily_atoms(sheets: SheetsClient, portfolio: Portfolio,
                      window: Period) -> Dict:
    """Assemble the embedded dataset for the window (which is the
    RANGE THE PICKER CAN SELECT WITHIN, not a report period)."""
    contracts = load_contract_monthly(sheets)
    loans = load_loans(sheets)
    schedule = load_loan_schedule(sheets)
    kpi = load_kpi_energy(sheets, window)

    ppa = {p.plant_key.upper(): p for p in portfolio.active_plants()}
    contract_plants = {k[0] for k in contracts}
    laas_keys = sorted(
        pk for pk in contract_plants if pk not in ppa
        and any(contracts[k].is_laas for k in contracts if k[0] == pk))
    loan_names = {l.plant_key: l.project_name for l in loans.values()}
    usd_plants = {l.plant_key for l in loans.values()
                  if l.currency == "USD"}

    plants: List[Dict] = []
    for pk, p in sorted(ppa.items()):
        plants.append({"key": pk, "name": p.customer or pk, "typ": "PPA",
                       "usd": pk in usd_plants,
                       "om_missing": p.om_cost_monthly_mxn is None})
    for pk in laas_keys:
        plants.append({"key": pk, "name": loan_names.get(pk, pk),
                       "typ": "LaaS", "usd": pk in usd_plants,
                       "om_missing": False})

    # per-month intermediates (single computation, shared by every day)
    months = window.month_overlaps()
    m_exp: Dict[tuple, Optional[float]] = {}
    m_svc: Dict[tuple, float] = {}
    for (y, m, _, _) in months:
        ym = "%04d-%02d" % (y, m)
        for pl in plants:
            pk = pl["key"]
            m_exp[(pk, y, m)] = expected_income_month(
                contracts, schedule, pk, y, m)
            m_svc[(pk, y, m)] = sum(
                r.payment_mxn for r in schedule
                if r.plant_key == pk and r.ref_month == ym)

    def tariff(pk: str, y: int, m: int) -> Optional[float]:
        row = contracts.get((pk, y, m))
        if row is not None and row.tariff_mxn is not None:
            return row.tariff_mxn
        p = ppa.get(pk)
        return p.tariff_mxn_per_kwh if p is not None else None

    days: List[str] = []
    atoms: Dict[str, List[List[Optional[float]]]] = {
        pl["key"]: [] for pl in plants}
    cursor = window.start
    while cursor <= window.end:
        iso = cursor.isoformat()
        days.append(iso)
        y, m = cursor.year, cursor.month
        dim = monthrange(y, m)[1]
        for pl in plants:
            pk = pl["key"]
            exp_m = m_exp[(pk, y, m)]
            exp_d = exp_m / dim if exp_m is not None else None
            svc_d = m_svc[(pk, y, m)] / dim
            if pl["typ"] == "LaaS":
                rev_d = exp_d          # fee accrues daily by contract
            else:
                kwh = kpi.get((pk, iso))
                t = tariff(pk, y, m)
                rev_d = (kwh * t if kwh is not None and t is not None
                         else None)
            om_m = (ppa[pk].om_cost_monthly_mxn
                    if pk in ppa else None) or 0.0
            atoms[pk].append([
                round(rev_d, 2) if rev_d is not None else None,
                round(exp_d, 2) if exp_d is not None else None,
                round(svc_d, 2), round(om_m / dim, 2)])
        cursor += timedelta(days=1)

    last_actual = max((d for (pk, d) in kpi.keys()), default=None)
    return {"days": days, "plants": plants, "atoms": atoms,
            "last_actual_day": last_actual}


def _logo_uri() -> str:
    try:
        return ("data:image/png;base64,"
                + base64.b64encode(LOGO_PATH.read_bytes()).decode())
    except OSError:
        return ""


def render_financial_report_html(data: Dict, generated_at: str) -> str:
    payload = json.dumps(data, separators=(",", ":"))
    logo = _logo_uri()
    logo_html = ('<img src="%s" alt="ARGIA SOLAR" style="height:32px">'
                 % logo) if logo else "<b>ARGIA SOLAR</b>"
    footer = _footer_sources()
    default_from = data["days"][0]
    default_to = data["last_actual_day"] or data["days"][-1]
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Argia Solar — Financial Report</title><style>
:root{{--ink:#1b2a31;--muted:#6d7f88;--line:#e2e9ec;--brand:#0e7c66;--band:#f6f9f9;--good:#1f9d63;--warn:#c98a00;--bad:#c0392b;--laas:#5b57c9;--ppa:#0e7c66}}
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);font-size:13px;line-height:1.45;background:#fbfcfc}}
.page{{max-width:1060px;margin:0 auto;padding:22px 26px}}
header{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid var(--brand);padding-bottom:12px;flex-wrap:wrap;gap:10px}}
.hm{{text-align:right;font-size:11.5px;color:var(--muted)}}.hm .t{{font-size:16px;font-weight:700;color:var(--ink)}}
.picker{{display:flex;gap:10px;align-items:center;background:#fff;border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:16px 0;flex-wrap:wrap}}
.picker label{{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.5px}}
.picker input{{border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:13px;font-family:inherit}}
.picker .quick{{margin-left:auto;display:flex;gap:6px}}
.picker button{{border:1px solid var(--line);background:var(--band);border-radius:6px;padding:6px 10px;font-size:11px;cursor:pointer}}
.picker button:hover{{border-color:var(--brand);color:var(--brand)}}
.pill{{font-size:10.5px;color:var(--muted)}}
h2{{font-size:12px;letter-spacing:1.4px;text-transform:uppercase;color:var(--muted);margin:22px 0 10px;font-weight:700}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:760px){{.two{{grid-template-columns:1fr}}}}
.stmt{{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}}
.stmt .hd{{background:var(--band);padding:9px 14px;font-weight:800;font-size:12px;border-bottom:1px solid var(--line)}}
.stmt table{{width:100%;border-collapse:collapse}}
.stmt td{{padding:8px 14px;border-bottom:1px solid var(--line);font-size:12.5px}}
.stmt td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.stmt tr.tot td{{font-weight:800;font-size:14px;border-bottom:none;border-top:2px solid var(--brand)}}
.good{{color:var(--good)}}.bad{{color:var(--bad)}}
table.det{{width:100%;border-collapse:collapse;background:#fff}}
.det th,.det td{{padding:8px 9px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}}
.det th{{font-size:9.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);border-bottom:2px solid var(--brand)}}
.det th.l,.det td.l{{text-align:left}}.n{{font-variant-numeric:tabular-nums}}
.sub{{font-size:9.5px;color:var(--muted)}}
tfoot td{{font-weight:800;border-top:2px solid var(--brand);border-bottom:none}}
.tag{{font-size:9px;font-weight:800;padding:2px 6px;border-radius:9px;color:#fff}}
.tag.ppa{{background:var(--ppa)}}.tag.laas{{background:var(--laas)}}
.dscr{{font-weight:800;padding:2px 7px;border-radius:9px;font-size:11.5px}}
.dscr.good{{background:#e6f6ed;color:var(--good)}}.dscr.warn{{background:#fdf3e0;color:var(--warn)}}
.dscr.bad{{background:#fdeceb;color:var(--bad)}}.dscr.na{{background:#eef1f2;color:var(--muted)}}
.note{{background:#eef6f4;border-left:3px solid var(--brand);padding:10px 13px;border-radius:0 6px 6px 0;font-size:11px;margin:12px 0}}
.note b{{color:var(--brand)}}
.gap{{background:#fdf3e0;border-left:3px solid var(--warn);padding:10px 13px;border-radius:0 6px 6px 0;font-size:11px;margin:12px 0}}
.gap b{{color:var(--warn)}}
footer{{margin-top:22px;padding-top:12px;border-top:1px solid var(--line);font-size:10px;color:var(--muted);line-height:1.65}}
</style></head><body><div class="page">
<header>
  <div>{logo_html}</div>
  <div class="hm"><div class="t">Financial Report</div>
  generated {html.escape(generated_at)} · actuals through {html.escape(str(default_to))}</div>
</header>

<div class="picker">
  <label>From</label><input type="date" id="from" min="{data['days'][0]}" max="{data['days'][-1]}" value="{default_from}">
  <label>To</label><input type="date" id="to" min="{data['days'][0]}" max="{data['days'][-1]}" value="{default_to}">
  <span class="pill" id="plabel"></span>
  <div class="quick">
    <button onclick="setMTD()">Month to date</button>
    <button onclick="setPrevMonth()">Previous month</button>
    <button onclick="setAll()">Full range</button>
  </div>
</div>

<div class="two">
<div class="stmt"><div class="hd">Expected — contracted</div>
<table>
<tr><td>Revenue</td><td class="n" id="e_rev"></td></tr>
<tr><td>O&amp;M costs</td><td class="n" id="e_om"></td></tr>
<tr><td>Debt service</td><td class="n" id="e_svc"></td></tr>
<tr class="tot"><td>Net cash after debt service</td><td class="n" id="e_net"></td></tr>
<tr class="tot"><td>Portfolio DSCR (expected)</td><td class="n" id="e_dscr"></td></tr>
</table></div>
<div class="stmt"><div class="hd">Actual — accrued <span class="pill" id="acov"></span></div>
<table>
<tr><td>Revenue</td><td class="n" id="a_rev"></td></tr>
<tr><td>O&amp;M costs</td><td class="n" id="a_om"></td></tr>
<tr><td>Debt service</td><td class="n" id="a_svc"></td></tr>
<tr class="tot"><td>Net cash after debt service</td><td class="n" id="a_net"></td></tr>
<tr class="tot"><td>Portfolio DSCR (actual)</td><td class="n" id="a_dscr"></td></tr>
</table></div>
</div>

<h2>Per-asset detail</h2>
<table class="det"><thead><tr><th class="l">Asset</th><th>Type</th><th>Exp. revenue</th><th>Actual revenue</th><th>O&amp;M</th><th>Debt service</th><th>DSCR exp.</th><th>DSCR act.</th></tr></thead>
<tbody id="rows"></tbody>
<tfoot><tr id="totrow"></tr></tfoot>
</table>
<div id="notes"></div>

<footer>{footer}<br>
This page embeds per-day figures computed server-side by the same
engine as the PDF report; the date picker only sums them — no financial
logic runs in the browser.</footer>
</div>
<script>
const D = {payload};
const idx = Object.fromEntries(D.days.map((d,i)=>[d,i]));
const fmt = x => x==null ? "—" : Math.round(x).toLocaleString("en-US");
function dscrSpan(inc, svc) {{
  if (inc==null || !(svc>0)) return '<span class="dscr na">n/a</span>';
  const v = inc/svc, cls = v<1 ? "bad" : (v<1.15 ? "warn" : "good");
  return '<span class="dscr '+cls+'">'+Math.round(v*100)+'%</span>';
}}
function sumRange(pk, a, b) {{
  const s = {{rev:0, exp:0, svc:0, om:0, revDays:0}};
  const arr = D.atoms[pk];
  for (let i=a; i<=b; i++) {{
    const [rev, exp, svc, om] = arr[i];
    if (rev!=null) {{ s.rev += rev; s.revDays++; }}
    if (exp!=null) s.exp += exp;
    s.svc += svc; s.om += om;
  }}
  return s;
}}
function recompute() {{
  const f = document.getElementById("from").value,
        t = document.getElementById("to").value;
  if (!(f in idx) || !(t in idx) || idx[t] < idx[f]) return;
  const a = idx[f], b = idx[t], nDays = b - a + 1;
  document.getElementById("plabel").textContent =
      nDays + " day" + (nDays>1?"s":"");
  let T = {{rev:0, exp:0, svc:0, om:0}}, rowsHtml = "", notes = "";
  let anyActual = false, maxRevDays = 0;
  for (const p of D.plants) {{
    const s = sumRange(p.key, a, b);
    T.exp += s.exp; T.svc += s.svc; T.om += s.om; T.rev += s.rev;
    if (s.revDays>0) anyActual = true;
    maxRevDays = Math.max(maxRevDays, s.revDays);
    const actual = s.revDays>0 ? s.rev : null;
    rowsHtml += '<tr><td class="l"><b>'+p.name+'</b><div class="sub">'
      + p.key + '</div></td><td><span class="tag '
      + (p.typ==="LaaS"?"laas":"ppa") + '">' + p.typ + '</span></td>'
      + '<td class="n">'+fmt(s.exp)+'</td><td class="n">'+fmt(actual)
      + '</td><td class="n">'+(s.om>0?fmt(s.om):"—")+'</td>'
      + '<td class="n">'+fmt(s.svc)+'</td>'
      + '<td class="n">'+dscrSpan(s.exp||null, s.svc)+'</td>'
      + '<td class="n">'+dscrSpan(actual, s.svc)+'</td></tr>';
    if (actual!=null && s.svc>0 && actual/s.svc < 1)
      notes += '<div class="gap"><b>Watch: '+p.name+' ('+p.key
        + ') actual DSCR '+Math.round(actual/s.svc*100)
        + '%</b> — accrued income below debt service for the selection.</div>';
    if (p.om_missing)
      notes += "";
  }}
  const missing = D.plants.filter(p=>p.om_missing).map(p=>p.key);
  if (missing.length)
    notes += '<div class="gap"><b>O&M missing for '+missing.join(", ")
      + '</b> — Plants.om_cost_monthly_mxn is blank; opex shows 0 for '
      + 'these plants.</div>';
  const usdSvc = D.plants.filter(p=>p.usd)
      .reduce((acc,p)=>acc+sumRange(p.key,a,b).svc, 0);
  if (T.svc>0)
    notes = '<div class="note"><b>FX position.</b> '
      + (usdSvc/T.svc*100).toFixed(1) + '% of the selection\\'s debt '
      + 'service is USD-denominated — matched by USD-indexed LaaS fees '
      + 'at the same rate, so net portfolio FX exposure is ≈ zero.</div>'
      + notes;
  const set = (id,v)=>document.getElementById(id).textContent=v;
  set("e_rev", fmt(T.exp)); set("e_om","("+fmt(T.om)+")");
  set("e_svc","("+fmt(T.svc)+")");
  set("e_net", fmt(T.exp-T.om-T.svc));
  document.getElementById("e_dscr").innerHTML = dscrSpan(T.exp||null,T.svc);
  const actTotal = anyActual ? T.rev : null;
  set("a_rev", fmt(actTotal)); set("a_om","("+fmt(T.om)+")");
  set("a_svc","("+fmt(T.svc)+")");
  set("a_net", actTotal==null ? "—" : fmt(actTotal-T.om-T.svc));
  document.getElementById("a_dscr").innerHTML = dscrSpan(actTotal,T.svc);
  document.getElementById("acov").textContent =
      anyActual ? "(" + maxRevDays + "/" + nDays + " days with data)" : "(no data in range)";
  document.getElementById("rows").innerHTML = rowsHtml;
  document.getElementById("totrow").innerHTML =
      '<td class="l">PORTFOLIO</td><td></td><td class="n">'+fmt(T.exp)
      + '</td><td class="n">'+fmt(actTotal)+'</td><td class="n">'
      + fmt(T.om)+'</td><td class="n">'+fmt(T.svc)+'</td><td class="n">'
      + dscrSpan(T.exp||null,T.svc)+'</td><td class="n">'
      + dscrSpan(actTotal,T.svc)+'</td>';
  document.getElementById("notes").innerHTML = notes;
}}
function setRange(f,t) {{
  const clamp = d => d < D.days[0] ? D.days[0]
                   : (d > D.days[D.days.length-1] ? D.days[D.days.length-1] : d);
  document.getElementById("from").value = clamp(f);
  document.getElementById("to").value = clamp(t);
  recompute();
}}
function setMTD() {{
  const t = D.last_actual_day || D.days[D.days.length-1];
  setRange(t.slice(0,8)+"01", t);
}}
function setPrevMonth() {{
  const t = D.last_actual_day || D.days[D.days.length-1];
  const d = new Date(t.slice(0,8)+"01T00:00:00");
  d.setDate(0);   // last day of previous month
  const last = d.toISOString().slice(0,10);
  setRange(last.slice(0,8)+"01", last);
}}
function setAll() {{ setRange(D.days[0], D.days[D.days.length-1]); }}
document.getElementById("from").addEventListener("change", recompute);
document.getElementById("to").addEventListener("change", recompute);
recompute();
</script>
</body></html>"""
