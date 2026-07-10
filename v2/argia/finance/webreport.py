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

    ppa = {p.plant_key.upper(): p for p in portfolio.financial_plants()}
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
                       "kwp": p.kwp_dc,
                       "om_missing": p.om_cost_monthly_mxn is None})
    for pk in laas_keys:
        plants.append({"key": pk, "name": loan_names.get(pk, pk),
                       "typ": "LaaS", "usd": pk in usd_plants,
                       "kwp": None,
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

    # loan position labels per plant-month ("22/84", "24/24 · 2/12",
    # "paid off") — computed here so the browser only LOOKS THEM UP by
    # the selected end month, same no-logic-client-side rule as atoms
    from argia.finance.loans import installment_label
    inst: Dict[str, Dict[str, str]] = {}
    for pl in plants:
        pk = pl["key"]
        inst[pk] = {}
        for (y, m, _, _) in months:
            ym = "%04d-%02d" % (y, m)
            inst[pk][ym] = installment_label(schedule, pk, ym)

    last_actual = max((d for (pk, d) in kpi.keys()), default=None)
    return {"days": days, "plants": plants, "atoms": atoms,
            "inst": inst, "last_actual_day": last_actual}


def _logo_uri() -> str:
    """The dashboard's embedded logotype (851x96) — imported so the
    financial report and the performance dashboard carry the IDENTICAL
    image; the repo PNG asset is the fallback. The asset has a much
    squarer aspect ratio, so at the same 28px height it rendered
    visibly smaller than the dashboard's (user report, 2026-07-09)."""
    try:
        from argia.report.dashboard_html import LOGO_B64
        return "data:image/png;base64," + LOGO_B64
    except ImportError:
        pass
    try:
        return ("data:image/png;base64,"
                + base64.b64encode(LOGO_PATH.read_bytes()).decode())
    except OSError:
        return ""


def render_financial_report_html(data: Dict, generated_at: str) -> str:
    """One design system with the performance dashboard (approved sample
    2026-07-09): same fonts/palette/cards/badges, FINANCIAL REPORT
    letterspaced top-left, logotype top-right, audit text collapsed into
    a <details> block. The date picker still only SUMS the embedded
    daily atoms — no financial logic in the browser."""
    payload = json.dumps(data, separators=(",", ":"))
    logo = _logo_uri()
    logo_html = ('<img src="%s" alt="ARGIA SOLAR" '
                 'style="height:28px; display:block;">' % logo
                 ) if logo else "<b>ARGIA SOLAR</b>"
    footer = _footer_sources()
    default_from = data["days"][0]
    default_to = data["last_actual_day"] or data["days"][-1]
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Argia &mdash; Financial Report</title><style>
  :root {{ font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }}
  body {{ margin: 0; background: #f4f3ef; color: #1a1a19; }}
  .wrap {{ max-width: 1080px; margin: 0 auto; padding: 20px 16px 40px; }}
  .sub {{ font-size: 12px; color: #6b6a64; }}
  .sn {{ display: block; font-size: 10.5px; color: #9a998f; }}
  input[type=date], button.quick {{
           font-size: 14px; padding: 7px 10px; border: 1px solid #c9c8c0;
           border-radius: 8px; background: #fff; color: #1a1a19; }}
  button.quick {{ cursor: pointer; }}
  button.quick:hover {{ border-color: #8a897f; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 12px; margin-bottom: 14px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 14px 16px;
          border: 1px solid #e4e3dc; }}
  .card .lbl {{ font-size: 12px; color: #6b6a64; }}
  .card .val {{ font-size: 24px; font-weight: 600; margin-top: 2px; }}
  .card .val small {{ font-size: 12px; font-weight: 400; color: #6b6a64; }}
  .row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
         gap: 12px; margin-bottom: 14px; }}
  .panel {{ background: #fff; border-radius: 10px; border: 1px solid #e4e3dc;
           padding: 14px 16px; margin-bottom: 14px; }}
  .panel h2 {{ font-size: 13px; font-weight: 600; margin: 0 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; font-weight: 400; color: #8a897f; padding: 4px 6px; }}
  td {{ border-top: 1px solid #eceae2; padding: 7px 6px; }}
  .badge {{ padding: 2px 10px; border-radius: 10px; font-size: 12px;
           white-space: nowrap; }}
  .note {{ font-size: 12px; color: #9a6a1f; background: #faeeda;
          border-radius: 8px; padding: 8px 12px; margin-bottom: 12px; }}
  .note.warn {{ background:#fcebeb; color:#791f1f; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .stmt td:last-child {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .stmt tr.tot td {{ font-weight: 600; border-top: 2px solid #1a1a19; }}
  details.audit {{ margin-top: 6px; }}
  details.audit summary {{ cursor: pointer; font-size: 12px; color: #6b6a64; }}
  details.audit summary:hover {{ color: #1a1a19; }}
  details.audit .body {{ font-size: 12px; color: #6b6a64; line-height: 1.6;
                        padding: 10px 2px 0; }}
  .dl {{ text-align: center; margin-top: 18px; }}
  @media print {{
    body {{ background: #fff; }}
    .no-print, .dl {{ display: none !important; }}
    .card, .panel {{ border: 1px solid #d5d4cc; break-inside: avoid; }}
    .badge, .card, .note {{ -webkit-print-color-adjust: exact;
                            print-color-adjust: exact; }}
    @page {{ margin: 12mm; }}
  }}
</style></head><body><div class="wrap">
  <header style="display:block; margin-bottom:16px;">
    <div style="display:flex; align-items:center; justify-content:space-between;
                gap:14px; margin-bottom:12px;">
      <span style="font-size:16px; font-weight:600; letter-spacing:3.5px;
                   color:#3c3b37; white-space:nowrap;">FINANCIAL&nbsp;REPORT
        <span class="sub" id="printperiod" style="letter-spacing:normal;
              font-weight:400; margin-left:10px;"></span></span>
      {logo_html}
    </div>
    <div class="no-print" style="display:flex; align-items:center;
                justify-content:space-between; gap:10px; flex-wrap:wrap;">
      <div style="display:flex; gap:8px; align-items:center;">
        <span class="sub">From</span>
        <input type="date" id="from" min="{data['days'][0]}" max="{data['days'][-1]}" value="{default_from}">
        <span class="sub">To</span>
        <input type="date" id="to" min="{data['days'][0]}" max="{data['days'][-1]}" value="{default_to}">
        <span class="sub" id="plabel"></span>
      </div>
      <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
        <button class="quick" onclick="setMTD()">Month to date</button>
        <button class="quick" onclick="setPrevMonth()">Previous month</button>
        <button class="quick" onclick="setAll()">Full range</button>
        <span style="white-space:nowrap; font-size:14px; color:#4a4a45;">
          generated {html.escape(generated_at)} &middot; actuals through {html.escape(str(default_to))}</span>
      </div>
    </div>
  </header>

  <div class="cards">
    <div class="card"><div class="lbl">Expected revenue</div>
      <div class="val" id="c_exp">&ndash;</div></div>
    <div class="card"><div class="lbl">Actual revenue</div>
      <div class="val" id="c_act">&ndash;</div></div>
    <div class="card"><div class="lbl">Net cash (actual)</div>
      <div class="val" id="c_net">&ndash;</div></div>
    <div class="card"><div class="lbl">DSCR expected</div>
      <div class="val" id="c_de">&ndash;</div></div>
    <div class="card"><div class="lbl">DSCR actual</div>
      <div class="val" id="c_da">&ndash;</div></div>
  </div>

  <div class="row">
    <div class="panel" style="margin-bottom:0;">
      <h2>Expected &mdash; contracted</h2>
      <table class="stmt">
        <tr><td>Revenue</td><td id="e_rev"></td></tr>
        <tr><td>O&amp;M costs</td><td id="e_om"></td></tr>
        <tr><td>Debt service</td><td id="e_svc"></td></tr>
        <tr class="tot"><td>Net cash after debt service</td><td id="e_net"></td></tr>
        <tr class="tot"><td>Portfolio DSCR</td><td id="e_dscr"></td></tr>
      </table>
    </div>
    <div class="panel" style="margin-bottom:0;">
      <h2>Actual &mdash; accrued <span class="sub" id="acov"></span></h2>
      <table class="stmt">
        <tr><td>Revenue</td><td id="a_rev"></td></tr>
        <tr><td>O&amp;M costs</td><td id="a_om"></td></tr>
        <tr><td>Debt service</td><td id="a_svc"></td></tr>
        <tr class="tot"><td>Net cash after debt service</td><td id="a_net"></td></tr>
        <tr class="tot"><td>Portfolio DSCR</td><td id="a_dscr"></td></tr>
      </table>
    </div>
  </div>

  <div id="notes"></div>

  <div class="panel">
    <h2>Per-asset detail</h2>
    <table>
      <thead><tr><th>Asset</th><th>Type</th><th class="num">Exp. revenue</th>
        <th class="num">Actual revenue</th><th class="num">O&amp;M</th>
        <th class="num">Debt service</th><th class="num">Loan position</th>
        <th class="num">DSCR exp.</th>
        <th class="num">DSCR act.</th></tr></thead>
      <tbody id="rows"></tbody>
      <tfoot><tr id="totrow" style="font-weight:600;"></tr></tfoot>
    </table>

    <details class="audit">
      <summary>Data sources &amp; audit</summary>
      <div class="body">
        <span id="fxline"></span>
        {footer}<br>
        <b>Loan position:</b> installments paid / total per active loan
        as of the selected end month, from Loan_Schedule (a completed
        loan drops out; several active loans show as
        "24/24 &middot; 2/12"). <b>Plant size:</b> kWp DC from the
        Plants tab (PPA assets).
        This page embeds per-day figures computed server-side by the same
        engine as the PDF report; the date picker only sums them &mdash;
        no financial logic runs in the browser.
      </div>
    </details>
  </div>
  <div class="dl">
    <button class="quick" onclick="downloadPdf()">Download PDF
      (current selection)</button>
  </div>
</div>
<script>
const D = {payload};
const idx = Object.fromEntries(D.days.map((d,i)=>[d,i]));
const fmt = x => x==null ? "\u2013" : Math.round(x).toLocaleString("en-US");
function dscrBadge(inc, svc) {{
  if (inc==null || !(svc>0))
    return '<span class="badge" style="background:#efeee8;color:#6b6a64;">n/a</span>';
  const v = inc/svc;
  const c = v<1 ? ["#FDECEB","#791f1f"]
          : (v<1.15 ? ["#FAEEDA","#9a6a1f"] : ["#E1F5EE","#085041"]);
  return '<span class="badge" style="background:'+c[0]+';color:'+c[1]+';">'
       + Math.round(v*100) + '%</span>';
}}
function typBadge(t) {{
  const c = t==="LaaS" ? ["#EBEAFB","#3F3A8F"] : ["#E1F5EE","#085041"];
  return '<span class="badge" style="background:'+c[0]+';color:'+c[1]+';">'
       + t + '</span>';
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
    const sub = p.kwp ? p.key + ' \u00b7 ' + Math.round(p.kwp) + ' kWp'
                      : p.key;
    const instLabel = (D.inst[p.key] || {{}})[t.slice(0,7)] || '\u2013';
    rowsHtml += '<tr><td>'+p.name+'<span class="sn">'+sub+'</span></td>'
      + '<td>'+typBadge(p.typ)+'</td>'
      + '<td class="num">'+fmt(s.exp)+'</td><td class="num">'+fmt(actual)
      + '</td><td class="num">'+(s.om>0?fmt(s.om):"\u2013")+'</td>'
      + '<td class="num">'+fmt(s.svc)+'</td>'
      + '<td class="num">'+(instLabel||'\u2013')+'</td>'
      + '<td class="num">'+dscrBadge(s.exp||null, s.svc)+'</td>'
      + '<td class="num">'+dscrBadge(actual, s.svc)+'</td></tr>';
    if (actual!=null && s.svc>0 && actual/s.svc < 1)
      notes += '<div class="note warn">Watch: '+p.name+' &middot; '+p.key
        + ' &mdash; actual DSCR '+Math.round(actual/s.svc*100)
        + '%, accrued income below debt service for the selection.</div>';
  }}
  const missing = D.plants.filter(p=>p.om_missing).map(p=>p.key);
  if (missing.length)
    notes += '<div class="note">O&amp;M missing for '+missing.join(", ")
      + ' &mdash; Plants.om_cost_monthly_mxn is blank; opex shows 0 for '
      + 'these plants.</div>';
  const usdSvc = D.plants.filter(p=>p.usd)
      .reduce((acc,p)=>acc+sumRange(p.key,a,b).svc, 0);
  document.getElementById("fxline").innerHTML = T.svc>0
    ? '<b>FX position:</b> ' + (usdSvc/T.svc*100).toFixed(1)
      + '% of the selected debt service is USD-denominated, matched by '
      + 'USD-indexed LaaS fees at the same rate &mdash; net portfolio FX '
      + 'exposure &asymp; zero. ' : '';
  const set = (id,v)=>document.getElementById(id).textContent=v;
  const setH = (id,v)=>document.getElementById(id).innerHTML=v;
  set("e_rev", fmt(T.exp)); set("e_om","("+fmt(T.om)+")");
  set("e_svc","("+fmt(T.svc)+")");
  set("e_net", fmt(T.exp-T.om-T.svc));
  setH("e_dscr", dscrBadge(T.exp||null,T.svc));
  const actTotal = anyActual ? T.rev : null;
  set("a_rev", fmt(actTotal)); set("a_om","("+fmt(T.om)+")");
  set("a_svc","("+fmt(T.svc)+")");
  set("a_net", actTotal==null ? "\u2013" : fmt(actTotal-T.om-T.svc));
  setH("a_dscr", dscrBadge(actTotal,T.svc));
  document.getElementById("acov").textContent =
      anyActual ? "(" + maxRevDays + "/" + nDays + " days with data)"
                : "(no data in range)";
  set("c_exp", fmt(T.exp)); set("c_act", fmt(actTotal));
  set("c_net", actTotal==null ? "\u2013" : fmt(actTotal-T.om-T.svc));
  set("c_de", (T.svc>0 && T.exp>0) ? Math.round(T.exp/T.svc*100)+"%" : "\u2013");
  set("c_da", (T.svc>0 && actTotal!=null) ? Math.round(actTotal/T.svc*100)+"%" : "\u2013");
  document.getElementById("rows").innerHTML = rowsHtml;
  document.getElementById("totrow").innerHTML =
      '<td>PORTFOLIO</td><td></td><td class="num">'+fmt(T.exp)
      + '</td><td class="num">'+fmt(actTotal)+'</td><td class="num">'
      + fmt(T.om)+'</td><td class="num">'+fmt(T.svc)+'</td><td></td>'
      + '<td class="num">'+dscrBadge(T.exp||null,T.svc)+'</td>'
      + '<td class="num">'+dscrBadge(actTotal,T.svc)+'</td>';
  document.getElementById("notes").innerHTML = notes;
}}
function downloadPdf() {{ window.print(); }}
let _auditWasOpen = false;
window.addEventListener("beforeprint", function () {{
  const d = document.querySelector("details.audit");
  _auditWasOpen = d.open;
  d.open = true;   // a saved PDF must carry its provenance
  const f = document.getElementById("from").value,
        t = document.getElementById("to").value;
  document.getElementById("printperiod").textContent = f + " \u2013 " + t;
  document.title = "ARGIA_Finance_" + f + "_" + t;
}});
window.addEventListener("afterprint", function () {{
  document.querySelector("details.audit").open = _auditWasOpen;
}});
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
  d.setDate(0);
  const last = d.toISOString().slice(0,10);
  setRange(last.slice(0,8)+"01", last);
}}
function setAll() {{ setRange(D.days[0], D.days[D.days.length-1]); }}
document.getElementById("from").addEventListener("change", recompute);
document.getElementById("to").addEventListener("change", recompute);
recompute();
</script>
</body></html>"""
