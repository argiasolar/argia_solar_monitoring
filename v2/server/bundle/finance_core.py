"""Admin finance editor — pure core (no flask, importable in tests).

Validation and SQL builders behind /setup/finance, where an admin
adjusts the commercial inputs the financial report derives everything
from: loan principal, future installments, FX rates, schedule length,
O&M, LaaS fees and PPA tariffs. The report itself never stores these —
it re-derives revenue, debt service and DSCR from the tables each run,
so a saved edit shows up on the next regeneration (which the app
triggers immediately after every write).

Honesty rules, enforced here so they are testable:

  * Paid history is immutable. Bulk edits only touch rows with
    ref_month >= from_month, and from_month must not lie in the past
    (>= the current MX month). What the bank already collected is a
    fact, not a parameter.
  * USD loans keep ``payment_ccy`` authoritative: an FX edit recomputes
    payment_mxn = round(payment_ccy * xr, 2), a payment edit on a USD
    loan sets the CCY amount and recomputes MXN through the stored xr.
    Future-month MXN figures remain projections, exactly as v1 defined
    them.
  * Every write leaves a ``finance_audit`` row (who, when, what) —
    a number that silently changes is indistinguishable from a bug.
"""

from __future__ import annotations

import re
from typing import List, Optional, Sequence

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

# sane bounds — a typo'd extra zero must not sail through
MAX_PAYMENT_MXN = 5_000_000.0
MAX_PAYMENT_CCY = 500_000.0
MAX_PRINCIPAL = 1_000_000_000.0
MAX_OM = 1_000_000.0
MAX_FEE_CCY = 10_000_000.0
MAX_TARIFF = 50.0
FX_MIN, FX_MAX = 5.0, 50.0
MAX_EXTEND_MONTHS = 480

ENSURE_AUDIT_SQL = """CREATE TABLE IF NOT EXISTS finance_audit (
    id        serial PRIMARY KEY,
    ts        timestamptz NOT NULL DEFAULT now(),
    username  text NOT NULL,
    plant_key text,
    loan_id   text,
    action    text NOT NULL,
    detail    text);"""


def sq(value) -> str:
    """Single-quoted SQL literal (validated inputs only reach here,
    but quote anyway — belt and braces)."""
    return "'" + str(value).replace("'", "''") + "'"


# ----------------------------- validation -----------------------------

def parse_month(raw, min_month: Optional[str] = None) -> Optional[str]:
    """'YYYY-MM' within 2024-01..2050-12, optionally >= min_month.
    Also accepts the <input type=month> value verbatim."""
    s = str(raw or "").strip()
    if not MONTH_RE.match(s) or not ("2024-01" <= s <= "2050-12"):
        return None
    if min_month and s < min_month:
        return None
    return s


def parse_num(raw, lo: float, hi: float) -> Optional[float]:
    """Positive decimal within [lo, hi]; commas tolerated."""
    s = str(raw or "").strip().replace(",", "").replace(" ", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if not (lo <= v <= hi):
        return None
    return v


def month_date(ym: str) -> str:
    return f"DATE '{ym}-01'"


def months_seq(after_ym: str, to_ym: str) -> List[str]:
    """Months strictly after ``after_ym`` through ``to_ym`` inclusive."""
    if not (MONTH_RE.match(after_ym) and MONTH_RE.match(to_ym)):
        return []
    y, m = int(after_ym[:4]), int(after_ym[5:7])
    out: List[str] = []
    while len(out) <= MAX_EXTEND_MONTHS:
        m += 1
        if m == 13:
            y, m = y + 1, 1
        ym = "%04d-%02d" % (y, m)
        if ym > to_ym:
            break
        out.append(ym)
    return out


# ----------------------------- SQL builders -----------------------------

def sql_audit(user: str, plant: str, loan_id: str, action: str,
              detail: str) -> str:
    return ("INSERT INTO finance_audit (username, plant_key, loan_id,"
            " action, detail) VALUES (%s, %s, %s, %s, %s);"
            % (sq(user), sq(plant or ""), sq(loan_id or ""),
               sq(action), sq(detail[:400])))


def sql_set_om(plant: str, amount: float) -> str:
    return ("UPDATE plant SET om_cost_monthly_mxn = %.2f"
            " WHERE plant_key = %s;" % (amount, sq(plant)))


def sql_set_principal(loan_id: str, amount: float) -> str:
    return ("UPDATE loan SET principal_mxn = %.2f"
            " WHERE loan_id = %s;" % (amount, sq(loan_id)))


def sql_set_payment_mxn(loan_id: str, from_ym: str, amount: float) -> str:
    """MXN loan: flat future installment."""
    return ("UPDATE loan_schedule SET payment_mxn = %.2f"
            " WHERE loan_id = %s AND ref_month >= %s;"
            % (amount, sq(loan_id), month_date(from_ym)))


def sql_set_payment_ccy(loan_id: str, from_ym: str, amount: float) -> str:
    """USD loan: CCY amount is authoritative; MXN follows the stored xr."""
    return ("UPDATE loan_schedule SET payment_ccy = %.2f,"
            " payment_mxn = round(%.2f * xr, 2)"
            " WHERE loan_id = %s AND ref_month >= %s"
            " AND xr IS NOT NULL;"
            % (amount, amount, sq(loan_id), month_date(from_ym)))


def sql_set_fx(loan_id: str, from_ym: str, rate: float) -> str:
    """Future FX projection; MXN recomputed from the authoritative CCY."""
    return ("UPDATE loan_schedule SET xr = %.4f,"
            " payment_mxn = round(payment_ccy * %.4f, 2)"
            " WHERE loan_id = %s AND ref_month >= %s"
            " AND payment_ccy IS NOT NULL;"
            % (rate, rate, sq(loan_id), month_date(from_ym)))


def sql_truncate(loan_id: str, from_ym: str) -> List[str]:
    """Drop future (unpaid, projected) rows and refresh the loan span."""
    return [
        ("DELETE FROM loan_schedule WHERE loan_id = %s"
         " AND ref_month >= %s;" % (sq(loan_id), month_date(from_ym))),
        sql_loan_span_refresh(loan_id),
    ]


def sql_extend(loan_id: str, start_no: int, months: Sequence[str],
               payment_mxn: float, payment_ccy: Optional[float] = None,
               xr: Optional[float] = None) -> str:
    """Append future installments (numbering continues from start_no)."""
    values = []
    for i, ym in enumerate(months):
        if payment_ccy is not None and xr is not None:
            mxn = round(payment_ccy * xr, 2)
            values.append("(%s,%s,%d,%.2f,%.2f,%.4f)"
                          % (sq(loan_id), month_date(ym), start_no + i,
                             mxn, payment_ccy, xr))
        else:
            values.append("(%s,%s,%d,%.2f,NULL,NULL)"
                          % (sq(loan_id), month_date(ym), start_no + i,
                             payment_mxn))
    return ("INSERT INTO loan_schedule (loan_id, ref_month,"
            " installment_no, payment_mxn, payment_ccy, xr) VALUES\n"
            + ",\n".join(values)
            + "\nON CONFLICT DO NOTHING;")


def sql_loan_span_refresh(loan_id: str) -> str:
    """total_installments / first / last derived from the schedule —
    the loans doctrine: never store what can be derived and go stale."""
    lid = sq(loan_id)
    return ("UPDATE loan SET"
            " total_installments = s.n,"
            " first_month = s.f,"
            " last_month = s.l"
            " FROM (SELECT count(*) AS n, min(ref_month) AS f,"
            " max(ref_month) AS l FROM loan_schedule"
            " WHERE loan_id = %s) s"
            " WHERE loan.loan_id = %s;" % (lid, lid))


def sql_set_fee(plant: str, from_ym: str, fee_ccy: float) -> str:
    """LaaS fixed monthly fee (native currency), future months."""
    return ("UPDATE contract_monthly SET fixed_income_ccy = %.2f"
            " WHERE plant_key = %s"
            " AND make_date(year, month, 1) >= %s"
            " AND fixed_income_ccy IS NOT NULL;"
            % (fee_ccy, sq(plant), month_date(from_ym)))


def sql_set_tariff(plant: str, from_ym: str, tariff: float) -> str:
    """PPA tariff (MXN/kWh), future months."""
    return ("UPDATE contract_monthly SET tariff_mxn = %.4f"
            " WHERE plant_key = %s"
            " AND make_date(year, month, 1) >= %s;"
            % (tariff, sq(plant), month_date(from_ym)))
