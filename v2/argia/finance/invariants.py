"""Billing invariants (v341) - the arithmetic every invoice must satisfy.

Tomasz, 2 Nov 2026: "it looks like we are failing on basic mathematical
rules ... it is not possible that we have overflow of any energy or mxn
between months ... once we deliver this software to Prologis there will be
no margin for errors."

An invoice annex is published only when every HARD rule below holds; one
broken rule blocks that plant's invoice (fail closed) and says which rule
and by how much. PURE functions: the callers read the database.

Rules on the annex itself (``check_annex``):
  AXIS    the day axis is one calendar year, every day exactly once, in order
  LEAK    nothing after the invoiced month: no day with data, no register month
  VALUE   no negative, NaN or infinite energy or deemed energy on any day
  PHYSICS no day above kWp x MAX_YIELD_KWH_PER_KWP (a vendor glitch)
  EMPTY   the invoiced month has data and bills more than 0 kWh
  YEAR    the January-December table adds up to the year-to-date figure
  MXN     the month's pesos = kWh x that month's tariff, rounded once

Rules against the books (``check_books``):
  CLOSED   the month's reconciliation is closed
  CLOSE    invoiced kWh = the closed reconciliation figure
  TABLE    the month's daily table adds up to the invoiced kWh, every day present
  DEEMED   the compensated kWh on the days = the compensated kWh invoiced (v342)
  PRIOR    every earlier month on the annex = what was invoiced for it
"""
from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

from argia.finance.annex import A_DEEMED, A_MEASURED, annual_rollup, rollup_month

KWH_TOL = 0.05
"""The annex shows kWh to one decimal: anything above 0.05 is a real difference."""
MXN_TOL = 0.005
"""Pesos are rounded once, to the centavo: any difference of a centavo is real."""
TABLE_TOL_KWH = 2.0
"""Daily table vs billed month: the vendors' own daily rounding (same value
as argia.recon.engine.DAILY_TABLE_TOL_KWH)."""
MAX_YIELD_KWH_PER_KWP = 9.0
"""A rooftop in Mexico peaks around 7 kWh/kWp on a clear day; 9 is
physically out of reach. Above it the day is a counter glitch."""


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


def month_end(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return "%04d-%02d-%02d" % (y, m, calendar.monthrange(y, m)[1])


def _bad(v) -> bool:
    return v is not None and (not isinstance(v, (int, float)) or math.isnan(v) or math.isinf(v) or v < 0)


def check_annex(payload: Dict, ym: str, kwp: Optional[float] = None) -> List[Violation]:
    """The annex for invoiced month ``ym`` against its own arithmetic. Pure."""
    out: List[Violation] = []
    days = payload.get("days") or []
    atoms = payload.get("atoms") or []
    year = ym[:4]

    # AXIS
    if len(days) != len(atoms):
        out.append(Violation("AXIS", f"{len(days)} days but {len(atoms)} day records"))
    if days:
        first = dt.date.fromisoformat(days[0])
        expect = [(first + dt.timedelta(days=i)).isoformat() for i in range(len(days))]
        if days != expect:
            out.append(Violation("AXIS", "days missing, repeated or out of order"))
        if days[0] != f"{year}-01-01" or days[-1] != f"{year}-12-31":
            out.append(Violation("AXIS", f"axis {days[0]} to {days[-1]}, expected the year {year}"))
    else:
        out.append(Violation("AXIS", "no days"))

    # LEAK
    cutoff = month_end(ym)
    late = [d for d, a in zip(days, atoms) if d > cutoff and any(x is not None for x in a)]
    if late:
        kwh = sum((a[A_MEASURED] or 0) for d, a in zip(days, atoms) if d > cutoff)
        out.append(Violation("LEAK", f"{len(late)} day(s) after {cutoff} carry data ({kwh:,.1f} kWh), first {late[0]}"))
    later = sorted(k for k in (payload.get("history") or {}) if str(k)[:7] > ym)
    if later:
        out.append(Violation("LEAK", f"register months after {ym} on the annex: {', '.join(later)}"))

    # VALUE / PHYSICS
    cap = kwp * MAX_YIELD_KWH_PER_KWP if kwp else None
    for d, a in zip(days, atoms):
        e, dm = a[A_MEASURED], a[A_DEEMED]
        if _bad(e) or _bad(dm):
            out.append(Violation("VALUE", f"{d}: energy {e}, deemed {dm}"))
        elif cap and e is not None and e > cap:
            out.append(Violation("PHYSICS", f"{d}: {e:,.1f} kWh is above {cap:,.0f} kWh "
                                            f"({MAX_YIELD_KWH_PER_KWP:g} kWh/kWp on {kwp:g} kWp)"))

    # EMPTY / MXN
    r = rollup_month(payload, ym)
    if not r.get("has_data") or not r.get("billable_kwh") or r["billable_kwh"] <= 0:
        out.append(Violation("EMPTY", f"{ym} has no billable energy on the annex"))
    tariff = (payload.get("tariff_by_month") or {}).get(ym)
    if tariff is None:
        out.append(Violation("MXN", f"no tariff for {ym}"))
    elif r.get("amount_mxn") is not None and r.get("billable_kwh"):
        h = (payload.get("history") or {}).get(ym) or {}
        kwh = (h.get("kwh") + (h.get("penalty") or 0.0)) if h.get("kwh") is not None else r["billable_kwh"]
        want = round(kwh * tariff, 2)
        if abs(r["amount_mxn"] - want) > MXN_TOL:
            out.append(Violation("MXN", f"{ym}: MXN {r['amount_mxn']:,.2f} but {kwh:,.3f} kWh x {tariff} = {want:,.2f}"))

    # YEAR
    rows = annual_rollup(payload)
    year_total = sum(x["billable_kwh"] or 0 for x in rows)
    ytd = sum(x["billable_kwh"] or 0 for x in rows if x["ym"] <= ym)
    if abs(year_total - ytd) > KWH_TOL:
        out.append(Violation("YEAR", f"January-December {year_total:,.1f} kWh but year to date {ytd:,.1f} kWh"))
    return out


def check_books(ym: str, payload: Dict, close: Optional[Dict],
                daily_sum_kwh: Optional[float], daily_days: int) -> List[Violation]:
    """The annex against the books. Pure.

    ``close``: {"kwh", "closed"} the month's reconciliation (billing_kwh,
    closed_at set). ``daily_sum_kwh`` / ``daily_days``: Σ of the month's
    daily_production energy and the days present. The invoiced kWh is the
    register month on the annex when there is one, else the annex's own
    day records (the 1st-of-month run writes the register from the annex).
    """
    out: List[Violation] = []
    days_in = int(month_end(ym)[-2:])
    if not close or not close.get("closed"):
        out.append(Violation("CLOSED", f"the {ym} reconciliation is not closed"))
    h = (payload.get("history") or {}).get(ym) or {}
    r = rollup_month(payload, ym)
    if h.get("kwh") is not None:
        produced, tol, src = float(h["kwh"]), KWH_TOL, "register"
    else:
        produced, tol, src = float(r["measured_kwh"] or 0.0), TABLE_TOL_KWH, "daily records"
    if close and close.get("kwh") is not None and abs(produced - close["kwh"]) > tol:
        out.append(Violation("CLOSE", f"invoiced {produced:,.3f} kWh ({src}) but the closed month is "
                                      f"{close['kwh']:,.3f} ({produced - close['kwh']:+,.3f})"))
    if daily_days < days_in:
        out.append(Violation("TABLE", f"the daily table has {daily_days}/{days_in} days"))
    if daily_sum_kwh is None or abs(daily_sum_kwh - produced) > TABLE_TOL_KWH:
        out.append(Violation("TABLE", "the daily table adds up to "
                                      + ("nothing" if daily_sum_kwh is None else f"{daily_sum_kwh:,.3f} kWh")
                                      + f", invoiced {produced:,.3f}"))
    # v342 DEEMED: the compensated kWh on the month's days = the compensated kWh invoiced
    if h.get("kwh") is not None:
        days_deemed = round(sum((a[A_DEEMED] or 0.0) for d, a in zip(payload.get("days") or [], payload.get("atoms") or [])
                                if str(d)[:7] == ym), 1)
        invoiced = float(h.get("penalty") or 0.0)
        if abs(days_deemed - invoiced) > KWH_TOL:
            out.append(Violation("DEEMED", f"the days carry {days_deemed:,.1f} kWh compensated, the invoice "
                                           f"{invoiced:,.1f} - run invoice_reissue.py --month {ym}"))
    for m, row in sorted((payload.get("history") or {}).items()):
        if str(m)[:4] != ym[:4] or str(m) >= ym or row.get("kwh") is None:
            continue
        rm = rollup_month(payload, m)
        want = float(row["kwh"]) + float(row.get("penalty") or 0.0)
        if abs((rm["billable_kwh"] or 0) - want) > KWH_TOL or (
                row.get("income") is not None and abs((rm["amount_mxn"] or 0) - float(row["income"])) > MXN_TOL):
            out.append(Violation("PRIOR", f"{m}: annex {rm['billable_kwh']:,.1f} kWh / MXN {rm['amount_mxn']}, "
                                          f"invoiced {want:,.3f} kWh / MXN {row.get('income')}"))
    return out


def check_invoice(ym: str, payload: Dict, close: Optional[Dict], daily_sum_kwh: Optional[float],
                  daily_days: int) -> List[Violation]:
    """Every rule, annex and books. Pure."""
    return check_annex(payload, ym, payload.get("kwp")) + check_books(ym, payload, close, daily_sum_kwh, daily_days)
