"""PostgreSQL-backed loan loaders (pio06 only).

Since the /setup/finance editor (2026-09-01) the PG ``loan`` and
``loan_schedule`` tables are the single authority for finance inputs —
an admin edit lands there, never in the old Loans/Loan_Schedule sheet
tabs. Jobs that run where PG lives (financial_report_publish via
run_job.sh) must therefore read loans from PG, or they would keep
reporting the pre-edit sheet copy forever.

Same shapes as ``argia.finance.loans`` returns from the sheet, so the
caller cannot tell the difference; off-server (no ARGIA_PG_MIRROR) the
sheet loaders remain the fallback.
"""

from __future__ import annotations

import logging
from typing import Dict, List

from argia.finance.loans import Loan, ScheduleRow

LOG = logging.getLogger(__name__)


def load_loans_pg() -> Dict[str, Loan]:
    from argia.store.pgq import psql_rows
    out: Dict[str, Loan] = {}
    for r in psql_rows(
            "SELECT loan_id, plant_key, project_name, bank, currency,"
            " principal_mxn, total_installments,"
            " to_char(first_month,'YYYY-MM'), to_char(last_month,'YYYY-MM')"
            " FROM loan;"):
        if len(r) < 9:
            continue
        try:
            out[r[0]] = Loan(
                loan_id=r[0], plant_key=r[1], project_name=r[2],
                bank=r[3], currency=(r[4] or "MXN").upper(),
                principal_mxn=float(r[5]),
                total_installments=int(float(r[6])),
                first_month=r[7], last_month=r[8])
        except (ValueError, TypeError):
            LOG.warning("loan %s: malformed PG row — skipped", r[0])
    LOG.info("finance: %d loan(s) from PostgreSQL", len(out))
    return out


def load_loan_schedule_pg() -> List[ScheduleRow]:
    from argia.store.pgq import psql_rows

    def _opt(v):
        return float(v) if v not in (None, "") else None

    rows: List[ScheduleRow] = []
    for r in psql_rows(
            "SELECT s.loan_id, l.plant_key,"
            " to_char(s.ref_month,'YYYY-MM'), s.installment_no,"
            " l.total_installments, s.payment_mxn, s.payment_ccy, s.xr,"
            " coalesce(s.due_after_mxn, 0)"
            " FROM loan_schedule s JOIN loan l ON l.loan_id = s.loan_id;"):
        if len(r) < 9:
            continue
        try:
            rows.append(ScheduleRow(
                loan_id=r[0], plant_key=r[1], ref_month=r[2],
                installment_no=int(float(r[3])),
                total_installments=int(float(r[4])),
                payment_mxn=float(r[5]),
                payment_ccy=_opt(r[6]), xr=_opt(r[7]),
                due_after_mxn=float(r[8])))
        except (ValueError, TypeError):
            LOG.warning("loan_schedule %s %s: malformed PG row — skipped",
                        r[0], r[2])
    LOG.info("finance: %d schedule row(s) from PostgreSQL", len(rows))
    return rows
