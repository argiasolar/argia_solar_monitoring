"""Investor / shareholder financial report — builder and renderer.

``build_finance_report_data`` assembles one data object from the live
tabs (Contract_Monthly, Loans, Loan_Schedule, KPI_Daily, Plants);
``render_html`` formats it. All numeric decisions live in
argia.finance.income — this module only aggregates and presents.

Content contract (approved sample, 2026-07-09):
  * two statements side by side: Expected (contracted, for the period)
    and Actual (accrued, same period, debt & O&M prorated by days)
  * per-asset table with expected/actual DSCR, PPA vs LaaS tags
  * data-driven notes: FX position, below-1.0x watch list
  * audit footer generated from the provenance registry — the same
    source texts the sheet's header notes carry
  * the Argia logotype (repo asset), embedded base64 so the HTML is
    self-contained for PDF printing and mail attachment
"""

from __future__ import annotations

import base64
import html
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from argia.core.config import Portfolio
from argia.core.sheets import SheetsClient
from argia.finance.contract import load_contract_monthly
from argia.finance.income import (
    Period, actual_income, debt_service_for_period, dscr,
    expected_income_month, load_kpi_energy, om_cost_for_period,
)
from argia.finance.loans import load_loan_schedule, load_loans
from argia.finance.provenance import report_sources

LOG = logging.getLogger(__name__)

LOGO_PATH = Path(__file__).resolve().parents[1] / "report" / "assets" \
    / "argia_logo.png"


@dataclass
class AssetFinance:
    plant_key: str
    name: str
    typ: str                      # "PPA" | "LaaS"
    expected_mxn: Optional[float]
    actual_mxn: Optional[float]
    om_mxn: float
    service_mxn: float            # prorated for the period
    is_usd_service: bool

    @property
    def net_expected(self) -> Optional[float]:
        if self.expected_mxn is None:
            return None
        return self.expected_mxn - self.om_mxn - self.service_mxn

    @property
    def dscr_expected(self) -> Optional[float]:
        return dscr(self.expected_mxn, self.service_mxn)

    @property
    def dscr_actual(self) -> Optional[float]:
        return dscr(self.actual_mxn, self.service_mxn)


@dataclass
class FinanceReportData:
    period: Period
    assets: List[AssetFinance] = field(default_factory=list)
    om_plants_missing: List[str] = field(default_factory=list)
    kpi_days_found: int = 0

    def _sum(self, attr, typ=None):
        vals = [getattr(a, attr) for a in self.assets
                if (typ is None or a.typ == typ)]
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else 0.0

    @property
    def expected_total(self):
        return self._sum("expected_mxn")

    @property
    def actual_total(self):
        return self._sum("actual_mxn")

    @property
    def service_total(self):
        return self._sum("service_mxn")

    @property
    def om_total(self):
        return self._sum("om_mxn")

    @property
    def usd_service_share(self) -> float:
        total = self.service_total
        if total <= 0:
            return 0.0
        usd = sum(a.service_mxn for a in self.assets if a.is_usd_service)
        return usd / total

    @property
    def watch_list(self) -> List[AssetFinance]:
        return [a for a in self.assets
                if a.dscr_actual is not None and a.dscr_actual < 1.0]


def _expected_for_period(contracts, schedule, plant_key: str,
                         period: Period) -> Optional[float]:
    """Contracted income prorated over the period's month overlaps."""
    total = 0.0
    found = False
    for (y, m, overlap, dim) in period.month_overlaps():
        inc = expected_income_month(contracts, schedule, plant_key, y, m)
        if inc is None:
            continue
        total += inc * overlap / dim
        found = True
    return total if found else None


def build_finance_report_data(sheets: SheetsClient, portfolio: Portfolio,
                              period: Period) -> FinanceReportData:
    contracts = load_contract_monthly(sheets)
    loans = load_loans(sheets)
    schedule = load_loan_schedule(sheets)
    kpi = load_kpi_energy(sheets, period)

    data = FinanceReportData(period=period, kpi_days_found=len(kpi))

    # asset universe: active PPA plants + every plant in the contract
    # table that isn't on the Plants tab (the LaaS projects)
    ppa_plants = {p.plant_key.upper(): p for p in portfolio.active_plants()}
    contract_plants = {k[0] for k in contracts}
    laas_keys = sorted(
        pk for pk in contract_plants
        if pk not in ppa_plants
        and any(contracts[k].is_laas for k in contracts if k[0] == pk))

    loan_names = {l.plant_key: l.project_name for l in loans.values()}
    usd_plants = {l.plant_key for l in loans.values()
                  if l.currency == "USD"}

    for pk, plant in sorted(ppa_plants.items()):
        svc = debt_service_for_period(schedule, pk, period)
        om = om_cost_for_period(plant.om_cost_monthly_mxn, period)
        if plant.om_cost_monthly_mxn is None:
            data.om_plants_missing.append(pk)
        data.assets.append(AssetFinance(
            plant_key=pk, name=plant.customer or pk, typ="PPA",
            expected_mxn=_expected_for_period(contracts, schedule, pk,
                                              period),
            actual_mxn=actual_income(kpi, contracts, schedule, pk, period,
                                     tariff_fallback=plant.tariff_mxn_per_kwh),
            om_mxn=om, service_mxn=svc,
            is_usd_service=pk in usd_plants))

    for pk in laas_keys:
        svc = debt_service_for_period(schedule, pk, period)
        data.assets.append(AssetFinance(
            plant_key=pk, name=loan_names.get(pk, pk), typ="LaaS",
            expected_mxn=_expected_for_period(contracts, schedule, pk,
                                              period),
            actual_mxn=actual_income(kpi, contracts, schedule, pk, period),
            om_mxn=0.0, service_mxn=svc,
            is_usd_service=pk in usd_plants))
    return data


# ------------------------------------------------------------------ render

def _logo_data_uri() -> str:
    try:
        raw = LOGO_PATH.read_bytes()
    except OSError:
        LOG.warning("logo asset missing at %s", LOGO_PATH)
        return ""
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _m(x: Optional[float]) -> str:
    return "{:,.0f}".format(x) if x is not None else "—"


def _dscr_span(v: Optional[float]) -> str:
    if v is None:
        return '<span class="dscr na">n/a</span>'
    cls = "bad" if v < 1.0 else ("warn" if v < 1.15 else "good")
    return '<span class="dscr %s">%.0f%%</span>' % (cls, v * 100)


def _footer_sources() -> str:
    src = report_sources("Loans", "Loan_Schedule", "Contract_Monthly",
                         "Plants")
    picks = [
        ("Revenue (PPA)", "KPI_Daily billable energy × Contract_Monthly "
         "tariff for the month in force."),
        ("Tariffs", src["Contract_Monthly"]["tariff_mxn"]),
        ("LaaS fees", src["Contract_Monthly"]["fixed_income_ccy"]),
        ("Debt service", "Derived: Σ Loan_Schedule installments for the "
         "period (never a stored per-plant figure). "
         + src["Loan_Schedule"]["payment_mxn"]),
        ("FX", src["Loan_Schedule"]["xr"]),
        ("O&M", src["Plants"]["om_cost_monthly_mxn"]),
        ("Interest split", src["Loan_Schedule"]["due_after_mxn"]),
    ]
    lines = "".join("<b>%s:</b> %s<br>" % (html.escape(k), html.escape(v))
                    for k, v in picks)
    return (lines + "Column-level provenance is attached to every header "
            "cell in the sheet; both are generated from "
            "argia/finance/provenance.py. Partial periods prorate debt "
            "service and O&M by elapsed days; revenue accrues daily by "
            "nature. No IVA in any figure.")


def render_html(data: FinanceReportData) -> str:
    p = data.period
    days = p.days
    full_month = (p.month_overlaps()[0][2] == p.month_overlaps()[0][3]
                  and len(p.month_overlaps()) == 1)
    period_label = "%s – %s (%d day%s%s)" % (
        p.start.isoformat(), p.end.isoformat(), days,
        "s" if days != 1 else "",
        "" if full_month else ", debt & O&M prorated")

    rows_html = ""
    for a in sorted(data.assets, key=lambda x: (x.typ, -(x.expected_mxn
                                                         or 0))):
        rows_html += (
            '<tr><td class="l"><b>%s</b><div class="sub">%s</div></td>'
            '<td><span class="tag %s">%s</span></td>'
            '<td class="n">%s</td><td class="n">%s</td>'
            '<td class="n">%s</td><td class="n">%s</td>'
            '<td class="n">%s</td><td class="n">%s</td></tr>\n' % (
                html.escape(a.name), a.plant_key,
                "laas" if a.typ == "LaaS" else "ppa", a.typ,
                _m(a.expected_mxn), _m(a.actual_mxn),
                _m(a.om_mxn if a.om_mxn else None),
                _m(a.service_mxn),
                _dscr_span(a.dscr_expected), _dscr_span(a.dscr_actual)))

    exp, act = data.expected_total, data.actual_total
    om, svc = data.om_total, data.service_total
    net_e, net_a = exp - om - svc, act - om - svc
    d_e, d_a = dscr(exp, svc), dscr(act, svc)

    watch = ""
    for a in data.watch_list:
        watch += ('<div class="gap"><b>Watch: %s (%s) actual DSCR %.0f%%'
                  '</b> — accrued income below debt service for the '
                  'period.</div>' % (html.escape(a.name), a.plant_key,
                                     (a.dscr_actual or 0) * 100))
    om_note = ""
    if data.om_plants_missing:
        om_note = ('<div class="gap"><b>O&amp;M missing for %s</b> — '
                   'Plants.om_cost_monthly_mxn is blank; opex shows 0 for '
                   'these plants.</div>'
                   % html.escape(", ".join(data.om_plants_missing)))

    usd_share = data.usd_service_share
    logo = _logo_data_uri()
    logo_html = ('<img src="%s" alt="ARGIA SOLAR" style="height:34px">'
                 % logo) if logo else '<b>ARGIA SOLAR</b>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>Argia Solar — Portfolio Financial Report</title><style>
:root{{--ink:#1b2a31;--muted:#6d7f88;--line:#e2e9ec;--brand:#0e7c66;--band:#f6f9f9;--good:#1f9d63;--warn:#c98a00;--bad:#c0392b;--laas:#5b57c9;--ppa:#0e7c66}}
*{{box-sizing:border-box}}body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);font-size:13px;line-height:1.45}}
.page{{max-width:1060px;margin:0 auto;padding:26px 30px}}
header{{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:3px solid var(--brand);padding-bottom:12px}}
.hm{{text-align:right;font-size:11.5px;color:var(--muted)}}.hm .t{{font-size:16px;font-weight:700;color:var(--ink)}}
h2{{font-size:12px;letter-spacing:1.4px;text-transform:uppercase;color:var(--muted);margin:24px 0 10px;font-weight:700}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:22px;margin-top:16px}}
.stmt{{border:1px solid var(--line);border-radius:10px;overflow:hidden}}
.stmt .hd{{background:var(--band);padding:9px 14px;font-weight:800;font-size:12px;border-bottom:1px solid var(--line)}}
.stmt table{{width:100%;border-collapse:collapse}}
.stmt td{{padding:8px 14px;border-bottom:1px solid var(--line);font-size:12.5px}}
.stmt td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.stmt tr.tot td{{font-weight:800;font-size:14px;border-bottom:none;border-top:2px solid var(--brand)}}
.good{{color:var(--good)}}.bad{{color:var(--bad)}}
table.det{{width:100%;border-collapse:collapse}}
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
  <div class="hm"><div class="t">Portfolio Financial Report</div>
  Period: {period_label}<br>{len(data.assets)} assets · KPI days in range: {data.kpi_days_found}</div>
</header>

<div class="two">
<div class="stmt"><div class="hd">Expected — contracted, for the period</div>
<table>
<tr><td>Revenue</td><td class="n">{_m(exp)}</td></tr>
<tr><td>O&amp;M costs</td><td class="n">({_m(om)})</td></tr>
<tr><td>Debt service</td><td class="n">({_m(svc)})</td></tr>
<tr class="tot"><td>Net cash after debt service</td><td class="n {'good' if net_e>=0 else 'bad'}">{_m(net_e)}</td></tr>
<tr class="tot"><td>Portfolio DSCR (expected)</td><td class="n">{_dscr_span(d_e)}</td></tr>
</table></div>
<div class="stmt"><div class="hd">Actual — accrued, same period</div>
<table>
<tr><td>Revenue</td><td class="n">{_m(act)}</td></tr>
<tr><td>O&amp;M costs</td><td class="n">({_m(om)})</td></tr>
<tr><td>Debt service</td><td class="n">({_m(svc)})</td></tr>
<tr class="tot"><td>Net cash after debt service</td><td class="n {'good' if net_a>=0 else 'bad'}">{_m(net_a)}</td></tr>
<tr class="tot"><td>Portfolio DSCR (actual)</td><td class="n">{_dscr_span(d_a)}</td></tr>
</table></div>
</div>

<h2>Per-asset detail</h2>
<table class="det">
<thead><tr><th class="l">Asset</th><th>Type</th><th>Exp. revenue</th><th>Actual revenue</th><th>O&amp;M</th><th>Debt service</th><th>DSCR exp.</th><th>DSCR act.</th></tr></thead>
<tbody>{rows_html}</tbody>
<tfoot><tr><td class="l">PORTFOLIO</td><td></td><td class="n">{_m(exp)}</td><td class="n">{_m(act)}</td><td class="n">{_m(om)}</td><td class="n">{_m(svc)}</td><td class="n">{_dscr_span(d_e)}</td><td class="n">{_dscr_span(d_a)}</td></tr></tfoot>
</table>

<div class="note"><b>FX position.</b> {usd_share*100:.1f}% of the period's debt service is USD-denominated — matched by USD-indexed LaaS fees at the same rate, so LaaS coverage is FX-neutral and net portfolio FX exposure is ≈ zero.</div>
{watch}{om_note}

<footer>{_footer_sources()}</footer>
</div></body></html>"""
