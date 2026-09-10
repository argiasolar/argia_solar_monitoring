#!/usr/bin/env python3
"""v247 — run the Savio plugin: fetch invoices + payments from Savio (the
loopback mock until the real key is in /root/.argia_savio), compare them
with the AR tracker and the books' bank deposits in PostgreSQL, store the
findings in savio_check (replaced whole), print the summary.

    fin_savio_recon.py                 # dry run: fetch, compare, print
    fin_savio_recon.py --apply         # also store the result for /finance/savio/

Env: ARGIA_SAVIO_BASE (fake | http://127.0.0.1:8530/api/v1 | https://api.savio.mx/api/v1),
ARGIA_SAVIO_KEY_FILE (/root/.argia_savio), ARGIA_FIN_ENTITY (ARGIA-MX), ARGIA_PG_DB.
Exit 0 = ran (findings are findings, not failures), 2 = Savio or the database unreachable.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runs from anywhere, PYTHONPATH or not

from argia.fin import savio as SV, savio_recon as SR   # noqa: E402
from argia.fin.ingest import _lit                       # noqa: E402

ENTITY = os.environ.get("ARGIA_FIN_ENTITY", "ARGIA-MX")


def _rows(sql: str):
    from argia.store.pgq import psql_rows
    return psql_rows("SET statement_timeout='30s'; " + sql)


def _exec(sql: str):
    from argia.store.pgq import psql_exec
    psql_exec("SET statement_timeout='60s';\n" + sql)


def tracker_rows():
    return [{"invoice": r[0], "folio_fiscal": r[1], "total": r[2], "currency": r[3], "paid_on": r[4] or "", "status": r[5], "company": r[6]}
            for r in _rows(f"SELECT invoice, folio_fiscal, total, currency, coalesce(paid_on::text, ''), status, company FROM open_item"
                           f" WHERE entity_id = {_lit(ENTITY)} AND side = 'ar';")]


def deposit_rows(days_back: int = 400):
    """Bank debits (money in) from the posted journal lines; a DLLS account's
    line is in USD, its 'Compl' twin is skipped (peso complement)."""
    since = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
    out = []
    for r in _rows(f"SELECT j.jdate::text, l.debit, l.account, coalesce(a.name, l.account_name), l.reference, j.concept FROM gl_line l"
                   f" JOIN gl_journal j ON j.entity_id = l.entity_id AND j.jkey = l.jkey AND j.posted"
                   f" LEFT JOIN gl_account a ON a.entity_id = l.entity_id AND a.account = l.account"
                   f" WHERE l.entity_id = {_lit(ENTITY)} AND l.account LIKE '102-01-%' AND l.debit > 0 AND j.jdate >= {_lit(since)} AND j.kind <> 'Diario';"):
        name = r[3] or ""
        if "compl" in name.lower():
            continue
        out.append({"date": r[0], "amount": r[1], "account": r[2], "currency": "USD" if ("DLLS" in name.upper() or "USD" in name.upper()) else "MXN",
                    "reference": r[4], "concept": r[5]})
    return out


def registered_accounts():
    return [r[0] for r in _rows(f"SELECT coalesce(clabe_last4, '') FROM bank_account WHERE entity_id = {_lit(ENTITY)} AND active;") if r and r[0]]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    base = os.environ.get("ARGIA_SAVIO_BASE", "fake")
    source = "mock" if ("127.0.0.1" in base or base == "fake") else ("sandbox" if "sandbox" in base else "live")
    try:
        client = SV.client_from_env()
        invoices = list(client.invoices())
        payments = list(client.payments())
    except Exception as e:                      # noqa: BLE001
        print(f"FAILED: Savio unreachable ({type(e).__name__}: {e})")
        return 2
    try:
        tracker, deposits, accounts = tracker_rows(), deposit_rows(), registered_accounts()
    except Exception as e:                      # noqa: BLE001
        print(f"FAILED: database ({type(e).__name__}: {str(e)[:120]})")
        return 2
    rec = SR.reconcile(invoices, payments, tracker, deposits, accounts, source=source)
    print(f"savio recon ({source}): invoices {rec.invoices_matched}/{rec.invoices} matched, payments {rec.payments_matched}/{rec.payments} with a deposit,"
          f" deposits {rec.deposits_matched}/{rec.deposits} explained ({rec.deposits_internal} internal), accounts checked {rec.accounts_checked}, findings {len(rec.findings)}")
    for f in rec.findings[:40]:
        print(f"  [{f.severity}] {f.kind} {f.savio_ref} {f.our_ref} {f.amount:,.2f} {f.currency} — {f.detail}")
    if a.apply:
        rows = SR.rows_for(rec, ENTITY)
        cols = list(rows[0].keys())
        inserts = "\n".join(f"INSERT INTO savio_check ({', '.join(cols)}) VALUES ({', '.join(_lit(r[c]) for c in cols)});" for r in rows)
        _exec(f"BEGIN;\nDELETE FROM savio_check WHERE entity_id = {_lit(ENTITY)};\n{inserts}\nCOMMIT;")
        print(f"  stored {len(rows)} row(s) in savio_check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
