"""Re-issue an invoiced month after its compensated energy changed (v342).

A customer maintenance event logged or approved AFTER the month was invoiced
(NL1 11 Oct 2026, logged on 2 Nov) changes what the customer owes. This is
never done silently by a nightly job: it is this script, run on purpose.

    PYTHONPATH=. python scripts/invoice_reissue.py --month 2026-10            # dry run: what would change
    PYTHONPATH=. python scripts/invoice_reissue.py --month 2026-10 --apply    # do it

--apply, in this order:
 1. the month's daily table follows the approved events
    (billable = energy + compensated, argia.maintenance.deemed_sync)
 2. the invoicing register, one transaction per run: penalty_kwh = Σ of the
    month's compensated days, billable_kwh = produced + penalty,
    amount_mxn = round(billable x tariff, 2). produced_kwh is NOT touched.
 3. invoice_publish --month: annexes, PDFs, zip, Drive, index (the v341
    gate checks every rule, including DEEMED: days = register)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG = logging.getLogger("argia.invoice_reissue")


def register_rows(ym: str) -> dict:
    from argia.store.pgq import psql_rows
    out = {}
    for r in psql_rows(
            "SELECT plant_key, produced_kwh, coalesce(penalty_kwh,0), tariff_mxn, amount_mxn, billable_kwh"
            f" FROM invoicing WHERE ref_month = DATE '{ym}-01';"):
        if len(r) >= 6 and r[0]:
            f = [float(x) if x not in ("", None) else None for x in r[1:6]]
            out[r[0]] = dict(zip(("produced", "penalty", "tariff", "amount", "billable"), f))
    return out


def new_register(reg: dict, deemed: dict) -> dict:
    """{plant: (penalty, billable, amount)} after the re-issue. Pure."""
    out = {}
    for pk, r in reg.items():
        if r["produced"] is None or r["tariff"] is None:
            continue
        pen = round(deemed.get(pk, 0.0), 1)
        bill = round(r["produced"] + pen, 3)
        out[pk] = (pen, bill, round(bill * r["tariff"], 2))
    return out


def update_sql(ym: str, new: dict) -> str:
    parts = ["BEGIN;"]
    for pk, (pen, bill, amt) in sorted(new.items()):
        parts.append(
            f"UPDATE invoicing SET penalty_kwh = {pen:.3f}, billable_kwh = {bill:.3f}, amount_mxn = {amt:.2f},"
            f" source = 'reissue', published_at = now() WHERE plant_key = '{pk}' AND ref_month = DATE '{ym}-01';")
    parts.append("COMMIT;")
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="re-issue an invoiced month after compensated energy changed")
    ap.add_argument("--month", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from argia.core.config import load_portfolio
    from argia.core.sheets import open_sheets
    from argia.maintenance import deemed_sync as DS
    from argia.store.pgq import psql_exec
    plants = [p.plant_key for p in load_portfolio(open_sheets()).financial_plants()]
    res = DS.sync_month(a.month, plants, apply=a.apply, include_invoiced=True)
    reg = register_rows(a.month)
    new = new_register(reg, res["month_deemed"])
    total_old = total_new = 0.0
    print(f"{a.month}: plant | compensated kWh before -> after | billable kWh | MXN before -> after")
    for pk in sorted(new):
        r = reg[pk]
        pen, bill, amt = new[pk]
        total_old += r["amount"] or 0.0
        total_new += amt
        flag = "" if abs((r["amount"] or 0) - amt) < 0.005 else "   CHANGES"
        print(f"  {pk:5s} | {r['penalty']:>9,.1f} -> {pen:>9,.1f} | {bill:>12,.3f} | "
              f"{(r['amount'] or 0):>12,.2f} -> {amt:>12,.2f}{flag}")
    print(f"  TOTAL MXN {total_old:,.2f} -> {total_new:,.2f} ({total_new - total_old:+,.2f})")
    for pk, day, d in res["orphans"]:
        print(f"  NOT APPLIED: {pk} {day} has {d:.1f} kWh compensated but no daily row")
    if not a.apply:
        print("dry run - nothing written (add --apply)")
        return 0
    changed = {pk: v for pk, v in new.items() if abs((reg[pk]["amount"] or 0) - v[2]) >= 0.005
               or abs((reg[pk]["penalty"] or 0) - v[0]) >= 0.05}
    if changed:
        psql_exec(update_sql(a.month, changed))
        print(f"register updated: {', '.join(sorted(changed))}")
    import invoice_publish
    rc = invoice_publish.main(["--month", a.month])
    print(f"invoice_publish rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
