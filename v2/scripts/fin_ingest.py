#!/usr/bin/env python3
"""Bring the sources into the ledger — v244. Idempotent by natural keys:
run it twice, the second run changes nothing.

    fin_ingest.py --savio                 # customer invoices + payments (ARGIA_SAVIO_BASE: fake | URL)
    fin_ingest.py --cfdi DIR              # supplier CFDI XML files in DIR (the Drive inbox mirror)
    fin_ingest.py --bank DIR              # DIR/<account_id>/*.csv statements
    fin_ingest.py --pmo FILE              # the PMO portfolio snapshot JSON
    fin_ingest.py --all                   # the demo world: fixtures for every source
    ... --entity DEMO-MX --apply          # default is a dry run

Rejected files become fin_exception rows (STATEMENT_REJECTED,
FOREIGN_CFDI, CFDI_TOTALS); an invoice without a PO gets MISSING_PO.
Nothing is ever updated on a supplier invoice from a re-imported file;
Savio rows refresh the columns Savio owns; the snapshot refreshes
schedule columns only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List

from argia.fin import bank_csv, cfdi as CF, ingest as I, match as MT, savio as SV
from argia.fin.money import D

V2 = Path(__file__).resolve().parents[1]
FIX = V2 / "tests" / "fixtures" / "fin"


class Ctx:
    def __init__(self, entity: str, apply: bool):
        self.entity = entity
        self.apply = apply
        self.sql: List[str] = []
        self.notes: List[str] = []

    def add(self, sql: str, note: str = ""):
        if sql.strip():
            self.sql.append(sql)
        if note:
            self.notes.append(note)


def _rows(sql: str):
    from argia.store.pgq import psql_rows
    return psql_rows("SET statement_timeout='20s'; " + sql)


def _exec(sql: str):
    from argia.store.pgq import psql_exec
    psql_exec("SET statement_timeout='120s';\n" + sql)


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# ---------------------------------------------------------------- savio
def ingest_savio(ctx: Ctx, client: SV.SavioClient, default_account: str):
    cus = {r[0]: int(r[1]) for r in _rows("SELECT savio_customer_id, customer_id FROM customer_master WHERE savio_customer_id IS NOT NULL;") if len(r) >= 2}
    plants = {r[0] for r in _rows("SELECT plant_key FROM plant;")}
    projects = {r[0] for r in _rows("SELECT project_id FROM project;")}
    inv_rows = []
    for inv in client.invoices():
        row = I.customer_invoice_row(inv, ctx.entity, cus)
        # a link Savio carries that we do not know stays out of the row and into the queue — never a broken reference
        for col, known, kind in (("plant_key", plants, "UNKNOWN_PLANT"), ("project_id", projects, "UNKNOWN_PROJECT")):
            if row.get(col) and row[col] not in known:
                ctx.add(I.upsert("fin_exception", [I.exception_row(kind, row["savio_invoice_id"], f"Savio invoice {row['savio_invoice_id']} names {col} {row[col]!r}, unknown here", ctx.entity)],
                                 ("kind", "ref"), update=("detail",)))
                row[col] = None
        inv_rows.append(row)
    ctx.add(I.upsert("customer_invoice", inv_rows, ("savio_invoice_id",), never_update=("entity_id",)), f"savio: {len(inv_rows)} invoice(s)")
    pays = list(client.payments())
    prow = []
    for p in pays:
        pr, _ = I.customer_payment_rows(p, ctx.entity, default_account)
        prow.append(pr)
    ctx.add(I.upsert("payment", prow, ("savio_payment_id",), update=("pay_date", "amount", "currency", "counterpart")), f"savio: {len(pays)} payment(s)")
    # allocations + bank links need the serial payment ids -> plain SQL guarded by NOT EXISTS
    for p in pays:
        pid = f"(SELECT payment_id FROM payment WHERE savio_payment_id = {_q(p['payment_id'])})"
        ctx.add(f"INSERT INTO allocation (payment_id, invoice_ref, invoice_side, amount, by_user)"
                f" SELECT {pid}, {_q(p['invoice_id'])}, 'ar', {D(p['amount'])}, 'savio_sync'"
                f" WHERE NOT EXISTS (SELECT 1 FROM allocation WHERE payment_id = {pid} AND invoice_ref = {_q(p['invoice_id'])});")
        ref = (p.get("reference") or "").strip()
        if ref:
            ctx.add(f"INSERT INTO bank_match (line_key, target_kind, target_ref, amount, by_user)"
                    f" SELECT t.line_key, 'customer_payment', {_q(p['payment_id'])}, {D(p['amount'])}, 'savio_sync'"
                    f" FROM bank_transaction t WHERE t.line_key LIKE {_q('%:' + ref)} AND t.amount = {D(p['amount'])}"
                    f" AND NOT EXISTS (SELECT 1 FROM bank_match m WHERE m.line_key = t.line_key AND m.reversed_at IS NULL);")
    ctx.add(I.upsert("savio_cursor", [{"resource": f"demo:invoice:{ctx.entity}", "cursor": None, "updated_at": dt.datetime.utcnow().isoformat()}], ("resource",), update=("cursor", "updated_at")))


# ----------------------------------------------------------------- cfdi
def ingest_cfdi(ctx: Ctx, folder: Path):
    our = [r[0] for r in _rows("SELECT rfc FROM entity WHERE rfc IS NOT NULL;")]
    known = {r[0] for r in _rows("SELECT cfdi_uuid FROM supplier_invoice;")}
    sup = {r[0]: (int(r[1]), int(r[2] or 30)) for r in _rows(f"SELECT rfc, supplier_id, terms_days FROM supplier WHERE entity_id = {_q(ctx.entity)};") if len(r) >= 2}
    pos = {}
    for r in _rows(f"SELECT po_number, po_id, coalesce(project_id, ''), coalesce((SELECT cost_code FROM po_line l WHERE l.po_id = p.po_id ORDER BY line_no LIMIT 1), ''),"
                   f" coalesce((SELECT rfc FROM supplier s WHERE s.supplier_id = p.supplier_id), ''), currency, total, status,"
                   f" coalesce((SELECT sum(total) FROM supplier_invoice i WHERE i.po_id = p.po_id AND i.tipo = 'I' AND i.status <> 'rejected'), 0),"
                   f" coalesce((SELECT sum(value) FROM receipt x WHERE x.po_id = p.po_id), 0)"
                   f" FROM purchase_order p WHERE entity_id = {_q(ctx.entity)};"):
        if len(r) >= 10:
            pos[r[0]] = {"po_id": int(r[1]), "project_id": r[2] or None, "cost_code": r[3] or None, "rfc": r[4], "currency": r[5],
                         "total": D(r[6]), "status": r[7], "invoiced": D(r[8]), "received": D(r[9])}
    projects = [r[0] for r in _rows("SELECT project_id FROM project;")]
    n_new = n_skip = n_rej = 0
    seen_now: Dict[str, str] = {}
    for f in sorted(folder.glob("*.xml")):
        text = f.read_text(encoding="utf-8")
        try:
            c = CF.parse(text)
        except CF.CfdiError as e:
            ctx.add(I.upsert("fin_exception", [I.exception_row("CFDI_UNREADABLE", f.name, str(e), ctx.entity)], ("kind", "ref"), update=("detail",)))
            n_rej += 1
            continue
        verdict, reasons = I.classify_cfdi(c, our, set(known) | set(seen_now))
        if verdict == "skip":
            n_skip += 1
            continue
        if verdict == "reject":
            kind = "FOREIGN_CFDI" if any(r.startswith("FOREIGN") or r.startswith("CUSTOMER") for r in reasons) else "CFDI_TOTALS"
            ctx.add(I.upsert("fin_exception", [I.exception_row(kind, c.uuid, f"{f.name}: " + "; ".join(reasons), ctx.entity)], ("kind", "ref"), update=("detail",)))
            n_rej += 1
            continue
        concepto = text  # the demo writes 'cost · project · PO' into the concept; a real supplier puts the PO in the description
        hint = I.po_hint_from_concepto(concepto, projects, list(pos))
        h = {"terms_days": sup.get(c.emisor_rfc, (None, 30))[1]}
        po = pos.get(hint.get("po_number") or "")
        if po:
            h.update({"po_id": po["po_id"], "project_id": po["project_id"], "cost_code": po["cost_code"]})
        elif hint.get("project_id"):
            h["project_id"] = hint["project_id"]
        row = I.supplier_invoice_row(c, ctx.entity, {k: v[0] for k, v in sup.items()}, hint=h)
        # three-way match at arrival: matched rows wait for approval, exceptions wait for a person
        if c.tipo == "I" and po:
            verdict = MT.three_way(MT.SupplierInvoice(c.uuid, c.emisor_rfc, c.moneda, c.total, po_number=hint.get("po_number")),
                                   MT.PO(hint["po_number"], po["rfc"], po["currency"], po["total"], po["status"], po["invoiced"]),
                                   MT.Receipt(hint["po_number"], po["received"]) if po["received"] > 0 else None)
            row["status"] = "matched" if verdict.verdict == "matched" else "exception"
            for code in verdict.codes:
                ctx.add(I.upsert("fin_exception", [I.exception_row(code, c.uuid, f"{f.name}: {c.emisor_nombre} {c.total} {c.moneda} vs {hint['po_number']} — {verdict.detail}", ctx.entity, po["project_id"])],
                                 ("kind", "ref"), update=()))
            if verdict.verdict == "matched":
                po["invoiced"] = po["invoiced"] + c.total          # a second invoice on the same PO sees the first
        ctx.add(I.upsert("supplier_invoice", [row], ("cfdi_uuid",), update=()))          # never update from a file
        seen_now[c.uuid] = f.name
        n_new += 1
        if c.tipo == "I" and not po:
            ctx.add(I.upsert("fin_exception", [I.exception_row("MISSING_PO", c.uuid, f"{f.name}: {c.emisor_nombre} {c.total} {c.moneda} — no purchase order", ctx.entity, h.get("project_id"))],
                             ("kind", "ref"), update=()))
    ctx.notes.append(f"cfdi: {n_new} new, {n_skip} duplicate(s) skipped, {n_rej} rejected -> exceptions")


# ----------------------------------------------------------------- bank
def ingest_bank(ctx: Ctx, folder: Path):
    accounts = {r[0]: r[1] for r in _rows(f"SELECT account_id, statement_format FROM bank_account WHERE entity_id = {_q(ctx.entity)};") if len(r) >= 2}
    own = [r[0] for r in _rows("SELECT clabe_last4 FROM bank_account;")]
    # own accounts: match by the tail the statement shows; the demo world carries full CLABEs
    try:
        world = json.loads((FIX / "world.json").read_text(encoding="utf-8"))
        own = [a["clabe"] for a in world["accounts"]] + own
    except OSError:
        pass
    known_sha = {r[0] for r in _rows("SELECT file_sha256 FROM bank_statement WHERE file_sha256 IS NOT NULL;")}
    n_new = n_skip = n_rej = 0
    for acc_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        acc = acc_dir.name
        if acc not in accounts:
            ctx.notes.append(f"bank: {acc} is not a configured account of {ctx.entity} — skipped")
            continue
        for f in sorted(acc_dir.glob("*.csv")):
            text = f.read_text(encoding="utf-8")
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if sha in known_sha:
                n_skip += 1
                continue
            try:
                st = bank_csv.parse(accounts[acc], text, acc)
            except bank_csv.BankFormatError as e:
                ctx.add(I.upsert("fin_exception", [I.exception_row("STATEMENT_REJECTED", f"{acc}/{f.name}", str(e), ctx.entity)], ("kind", "ref"), update=("detail",)))
                n_rej += 1
                continue
            head, lines, problems = I.statement_rows(st, own, sha)
            if problems:
                ctx.add(I.upsert("fin_exception", [I.exception_row("STATEMENT_REJECTED", f"{acc}/{f.name}", "; ".join(problems), ctx.entity)], ("kind", "ref"), update=("detail",)))
                n_rej += 1
                continue
            ctx.add(I.upsert("bank_statement", [head], ("account_id", "period_start", "period_end"), update=()))
            sid = f"(SELECT statement_id FROM bank_statement WHERE account_id = {_q(acc)} AND period_start = '{st.period_start}' AND period_end = '{st.period_end}')"
            for l in lines:
                ctx.add(f"INSERT INTO bank_transaction (line_key, statement_id, account_id, tx_date, amount, description, counterpart, own_transfer)"
                        f" VALUES ({I._lit(l['line_key'])}, {sid}, {_q(acc)}, '{l['tx_date']}', {l['amount']}, {I._lit(l['description'])}, {I._lit(l['counterpart'])}, {I._lit(l['own_transfer'])})"
                        f" ON CONFLICT (line_key) DO NOTHING;")
            n_new += 1
    ctx.notes.append(f"bank: {n_new} statement(s) loaded, {n_skip} already known, {n_rej} rejected -> exceptions")


# ------------------------------------------------------------------ pmo
def ingest_pmo(ctx: Ctx, snapshot: Path):
    snap = json.loads(snapshot.read_text(encoding="utf-8"))
    entity_of = {r[0]: r[1] for r in _rows("SELECT project_id, entity_id FROM project;") if len(r) >= 2}
    projects, ms, tasks = I.snapshot_rows(snap, entity_of, snap.get("generated_at") or dt.datetime.utcnow().isoformat())
    projects = [dict(p, created_by="pmo_snapshot") for p in projects]
    ctx.add(I.upsert("project", projects, ("project_id",), update=I.SNAPSHOT_PROJECT_UPDATE))
    ctx.add(I.upsert("project_milestone", ms, ("project_id", "ref"), update=I.SNAPSHOT_MILESTONE_UPDATE))
    ctx.add(I.upsert("project_task", tasks, ("project_id", "task_id")))
    ctx.notes.append(f"pmo: {len(projects)} project(s), {len(ms)} milestone(s), {len(tasks)} task(s) from {snap.get('generated_at')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", default="DEMO-MX")
    ap.add_argument("--account", default=None, help="bank account for Savio receipts (default: the entity's first account)")
    ap.add_argument("--savio", action="store_true")
    ap.add_argument("--cfdi", default=None)
    ap.add_argument("--bank", default=None)
    ap.add_argument("--pmo", default=None)
    ap.add_argument("--all", action="store_true", help="the demo fixtures for every source")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.all:
        a.savio, a.cfdi, a.bank, a.pmo = True, str(FIX / "cfdi"), str(FIX / "bank"), str(FIX / "pmo" / "portfolio_snapshot.json")
    ctx = Ctx(a.entity, a.apply)
    if a.bank:
        ingest_bank(ctx, Path(a.bank))
    if a.savio:
        acc = a.account or next((r[0] for r in _rows(f"SELECT account_id FROM bank_account WHERE entity_id = {_q(a.entity)} ORDER BY account_id;")), None)
        if not acc:
            print(f"{a.entity}: no bank account configured — seed first")
            return 2
        ingest_savio(ctx, SV.client_from_env(), acc)
    if a.cfdi:
        ingest_cfdi(ctx, Path(a.cfdi))
    if a.pmo:
        ingest_pmo(ctx, Path(a.pmo))
    for n in ctx.notes:
        print("  " + n)
    n = sum(s.count("INSERT INTO") for s in ctx.sql)
    if not a.apply:
        print(f"dry run: {n} statement(s) — add --apply")
        return 0
    if ctx.sql:
        _exec("\n".join(ctx.sql))
    print(f"applied: {n} statement(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
