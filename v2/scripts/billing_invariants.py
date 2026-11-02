"""Check published invoices against the billing invariants (v341). READ ONLY.

Runs every rule of argia.finance.invariants against the invoice annexes as
they are published (the payload each HTML embeds) and the books in
PostgreSQL (reconciliation close, daily table). Prints one line per plant
and month, and every broken rule. Exit 1 when any rule is broken.

    PYTHONPATH=. python scripts/billing_invariants.py --months 2026-08,2026-09,2026-10
    PYTHONPATH=. python scripts/billing_invariants.py --last-month
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import invoice_publish as IP            # noqa: E402
import report_invoice_annex as RIA      # noqa: E402
from argia.finance import invariants as INV  # noqa: E402
from argia.finance.annex import rollup_month  # noqa: E402

LOG = logging.getLogger("argia.billing_invariants")


def check_month(out_root: str, ym: str, books) -> list:
    """[(factura_name, kWh, MXN, [violations])] for one published month."""
    rows = []
    by_name = {n: pk for n, (pk, _d) in IP.FACTURA_CLIENT.items()}
    for path in sorted(glob.glob(os.path.join(out_root, ym, "factura_*_%s.html" % ym.replace("-", "")))):
        name = os.path.basename(path)[len("factura_"):-len("_%s.html" % ym.replace("-", ""))]
        pk = by_name.get(name, name)
        with open(path, encoding="utf-8") as f:
            m = IP.ANNEX_DATA.search(f.read())
        if not m:
            rows.append((name, None, None, [INV.Violation("ERROR", "no embedded payload")]))
            continue
        payload = json.loads(m.group(1))
        r = rollup_month(payload, ym)
        try:
            bad = RIA.invoice_violations(ym, payload, books, pk)
        except Exception as e:  # noqa: BLE001
            bad = [INV.Violation("ERROR", repr(e))]
        rows.append((name, r.get("billable_kwh"), r.get("amount_mxn"), bad))
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="billing invariants over published invoices (read only)")
    ap.add_argument("--months", default=None, help="comma list YYYY-MM")
    ap.add_argument("--last-month", action="store_true")
    ap.add_argument("--out-root", default=os.path.join(IP.WEBROOT, "invoices"))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if a.last_month:
        from argia.core.time_utils import now_mx
        yms = [RIA.last_complete_month(now_mx())]
    elif a.months:
        yms = [m.strip() for m in a.months.split(",") if m.strip()]
    else:
        ap.error("give --months or --last-month")
    broken = 0
    for ym in yms:
        books = RIA.month_books(ym)
        rows = check_month(a.out_root, ym, books)
        if not rows:
            print(f"{ym}: no published invoices")
        for name, kwh, mxn, bad in rows:
            k = "-" if kwh is None else f"{kwh:,.1f}"
            mx = "-" if mxn is None else f"{mxn:,.2f}"
            print(f"{ym} {name:18s} {k:>12} kWh  MXN {mx:>13}  {'OK' if not bad else 'BROKEN'}")
            for v in bad:
                print(f"      {v}")
            broken += bool(bad)
    print("RESULT:", "every rule holds" if broken == 0 else f"{broken} invoice(s) break a rule")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
