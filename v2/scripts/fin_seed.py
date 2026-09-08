#!/usr/bin/env python3
"""Load the DEMO world (tests/fixtures/fin/world.json) into the finance
tables — v244. Everything lands under the DEMO-* entities; nothing here
touches a real entity, and --wipe removes exactly what --apply loaded.

    fin_seed.py              # dry run: statement counts
    fin_seed.py --apply      # idempotent upserts (natural keys)
    fin_seed.py --wipe       # delete every DEMO-* row (entities included)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from argia.fin import ingest as I
from argia.fin.money import D

V2 = Path(__file__).resolve().parents[1]
WORLD = V2 / "tests" / "fixtures" / "fin" / "world.json"
DEMO_ENTITIES = ("DEMO-MX", "DEMO-CZ")


def seed_sql(world: dict, supplier_ids: dict, customer_ids: dict) -> list:
    """The upserts, in dependency order. supplier_ids / customer_ids map
    RFC -> serial id when the rows already exist (second run); on a
    first run the POs are inserted in a second pass by the caller."""
    out = []
    out.append(I.upsert("entity", [{"entity_id": e["entity_id"], "name": e["name"], "rfc": e["rfc"], "currency": e["currency"]} for e in world["entities"]], ("entity_id",)))
    out.append(I.upsert("bank_account", [{"account_id": a["account_id"], "entity_id": a["entity_id"], "bank": a["bank"], "currency": a["currency"],
                                          "clabe_last4": a["clabe_last4"], "purpose": a["purpose"], "statement_format": "argia_generic"} for a in world["accounts"]], ("account_id",)))
    codes = world["cost_codes"]
    out.append(I.upsert("cost_code", [{"code": c["code"], "parent": None, "name_en": c["name_en"], "name_es": c["name_es"]} for c in codes if not c["parent"]], ("code",)))
    out.append(I.upsert("cost_code", [{"code": c["code"], "parent": c["parent"], "name_en": c["name_en"], "name_es": c["name_es"]} for c in codes if c["parent"]], ("code",)))
    out.append(I.upsert("supplier", [{"entity_id": s["entity_id"], "rfc": s["rfc"], "name": s["name"], "bank_clabe": s["clabe"], "terms_days": s["terms_days"]}
                                     for s in world["suppliers"]], ("entity_id", "rfc")))
    out.append(I.upsert("customer_master", [{"rfc": c["rfc"], "name": c["name"], "savio_customer_id": c["savio_customer_id"], "terms_days": c["terms_days"]}
                                            for c in world["customers"]], ("rfc",)))
    out.append(I.upsert("fx_rate", [{"rate_date": "2026-09-01", "pair": k, "rate": D(v), "source": "demo"} for k, v in world["fx"].items()], ("rate_date", "pair")))
    out.append(I.upsert("project", [{"project_id": p["project_id"], "entity_id": p["entity_id"], "customer_id": customer_ids.get(p["customer_rfc"] or ""),
                                     "name": p["name"], "site": p["site"], "project_type": p["project_type"], "kwp_dc": p["kwp_dc"], "status": p["status"],
                                     "pm_user": p["pm_user"], "offer_ref": p["offer_ref"], "contract_value": D(p["contract_value"]), "contract_ccy": p["contract_ccy"],
                                     "created_by": "fin_seed"} for p in world["projects"]], ("project_id",)))
    bv, bl = [], []
    for pid, versions in world["budgets"].items():
        for v in versions:
            bv.append({"project_id": pid, "version": v["version"], "status": v["status"], "approved_by": "demo" if v["status"] != "draft" else None, "note": "demo seed"})
            for code, amt in v["lines"].items():
                bl.append({"project_id": pid, "version": v["version"], "cost_code": code, "amount": D(amt)})
    out.append(I.upsert("budget_version", bv, ("project_id", "version")))
    out.append(I.upsert("budget_line", bl, ("project_id", "version", "cost_code")))
    out.append(I.upsert("change_order", [{"project_id": c["project_id"], "ref": c["ref"], "status": c["status"], "revenue_impact": D(c["revenue_impact"]),
                                          "cost_impact": D(c["cost_impact"]), "approved_by": "demo" if c["status"] == "approved" else None} for c in world["change_orders"]],
                        ("project_id", "ref")))
    if supplier_ids:
        pos = []
        for po in world["purchase_orders"]:
            pos.append({"entity_id": "DEMO-MX", "po_number": po["po_number"], "supplier_id": supplier_ids[po["supplier_rfc"]], "project_id": po["project_id"],
                        "status": po["status"], "currency": po["currency"], "fx_rate": (D(world["fx"]["USD/MXN"]) if po["currency"] == "USD" else None),
                        "subtotal": D(po["subtotal"]), "tax": D(po["tax"]), "total": D(po["total"]), "terms_days": 30,
                        "expected_delivery": po["expected_delivery"], "created_by": po["created_by"]})
        out.append(I.upsert("purchase_order", pos, ("entity_id", "po_number")))
        # lines, approvals and receipts hang off the serial po_id -> guarded plain SQL
        extra = []
        for po in world["purchase_orders"]:
            pid = f"(SELECT po_id FROM purchase_order WHERE entity_id = 'DEMO-MX' AND po_number = '{po['po_number']}')"
            extra.append(f"INSERT INTO po_line (po_id, line_no, cost_code, description, qty, unit_price) SELECT {pid}, 1, '{po['cost_code']}', 'demo line', 1, {D(po['subtotal'])}"
                         f" WHERE NOT EXISTS (SELECT 1 FROM po_line WHERE po_id = {pid} AND line_no = 1);")
            for level, who in po["approvals"]:
                extra.append(f"INSERT INTO po_approval (po_id, level_name, approver, decision, total_at) SELECT {pid}, '{level}', '{who}', 'approved', {D(po['total'])}"
                             f" WHERE NOT EXISTS (SELECT 1 FROM po_approval WHERE po_id = {pid} AND level_name = '{level}');")
            if D(po["received"]) > 0:
                extra.append(f"INSERT INTO receipt (po_id, line_no, value, received_on, received_by) SELECT {pid}, 1, {D(po['received'])}, '{po['expected_delivery']}', 'demo'"
                             f" WHERE NOT EXISTS (SELECT 1 FROM receipt WHERE po_id = {pid});")
        out.append("\n".join(extra))
    return out


WIPE_SQL = """
DELETE FROM allocation WHERE payment_id IN (SELECT payment_id FROM payment WHERE entity_id IN ({e})) OR invoice_ref IN (SELECT cfdi_uuid FROM supplier_invoice WHERE entity_id IN ({e})) OR invoice_ref IN (SELECT savio_invoice_id FROM customer_invoice WHERE entity_id IN ({e}));
DELETE FROM bank_match WHERE line_key IN (SELECT line_key FROM bank_transaction WHERE account_id IN (SELECT account_id FROM bank_account WHERE entity_id IN ({e})));
DELETE FROM bank_transaction WHERE account_id IN (SELECT account_id FROM bank_account WHERE entity_id IN ({e}));
DELETE FROM bank_statement WHERE account_id IN (SELECT account_id FROM bank_account WHERE entity_id IN ({e}));
DELETE FROM payment WHERE entity_id IN ({e});
DELETE FROM supplier_invoice WHERE entity_id IN ({e});
DELETE FROM customer_invoice WHERE entity_id IN ({e});
DELETE FROM receipt WHERE po_id IN (SELECT po_id FROM purchase_order WHERE entity_id IN ({e}));
DELETE FROM po_approval WHERE po_id IN (SELECT po_id FROM purchase_order WHERE entity_id IN ({e}));
DELETE FROM po_line WHERE po_id IN (SELECT po_id FROM purchase_order WHERE entity_id IN ({e}));
DELETE FROM purchase_order WHERE entity_id IN ({e});
DELETE FROM fin_exception WHERE entity_id IN ({e}) OR project_id IN (SELECT project_id FROM project WHERE entity_id IN ({e}));
DELETE FROM change_order WHERE project_id IN (SELECT project_id FROM project WHERE entity_id IN ({e}));
DELETE FROM budget_line WHERE project_id IN (SELECT project_id FROM project WHERE entity_id IN ({e}));
DELETE FROM budget_version WHERE project_id IN (SELECT project_id FROM project WHERE entity_id IN ({e}));
DELETE FROM project_task WHERE project_id IN (SELECT project_id FROM project WHERE entity_id IN ({e}));
DELETE FROM project_milestone WHERE project_id IN (SELECT project_id FROM project WHERE entity_id IN ({e}));
DELETE FROM project WHERE entity_id IN ({e});
DELETE FROM supplier WHERE entity_id IN ({e});
DELETE FROM customer_master WHERE savio_customer_id LIKE 'cus_demo_%';
DELETE FROM bank_account WHERE entity_id IN ({e});
DELETE FROM period_close WHERE entity_id IN ({e});
DELETE FROM fx_rate WHERE source = 'demo';
DELETE FROM savio_event WHERE event_id LIKE 'evt_demo_%';
DELETE FROM savio_cursor WHERE resource LIKE 'demo:%';
DELETE FROM entity WHERE entity_id IN ({e});
""".format(e=", ".join(f"'{x}'" for x in DEMO_ENTITIES))


def decisions_sql(uuids: dict) -> str:
    """The people's decisions of the demo story, after fin_ingest --all:
    approvals, our payments (bank lines -> payment -> allocation ->
    bank_match), the credit note applied. Guarded, re-runnable."""
    u = uuids
    def pay(line_ref: str, account: str, amount: str, ccy: str, date: str, counterpart: str, inv_uuid: str) -> str:
        key = f"{account}:{line_ref}"
        return (f"INSERT INTO payment (entity_id, account_id, direction, pay_date, amount, currency, counterpart, bank_line_key, created_by)"
                f" SELECT 'DEMO-MX', '{account}', 'out', '{date}', {amount}, '{ccy}', '{counterpart}', '{key}', 'demo'"
                f" WHERE NOT EXISTS (SELECT 1 FROM payment WHERE bank_line_key = '{key}');\n"
                f"INSERT INTO allocation (payment_id, invoice_ref, invoice_side, amount, by_user)"
                f" SELECT payment_id, '{inv_uuid}', 'ap', {amount}, 'demo' FROM payment WHERE bank_line_key = '{key}'"
                f" AND NOT EXISTS (SELECT 1 FROM allocation a WHERE a.invoice_ref = '{inv_uuid}' AND a.payment_id = payment.payment_id);\n"
                f"INSERT INTO bank_match (line_key, target_kind, target_ref, amount, by_user)"
                f" SELECT '{key}', 'payment', payment_id::text, -{amount}, 'demo' FROM payment WHERE bank_line_key = '{key}'"
                f" AND EXISTS (SELECT 1 FROM bank_transaction t WHERE t.line_key = '{key}')"
                f" AND NOT EXISTS (SELECT 1 FROM bank_match m WHERE m.line_key = '{key}' AND m.reversed_at IS NULL);\n")
    out = []
    for name in ("ELE-A-1021", "EST-B-77", "MOD-M-310", "ING-C-5", "ELE-NC-12"):
        out.append(f"UPDATE supplier_invoice SET status = 'approved' WHERE cfdi_uuid = '{u[name]}' AND status IN ('received', 'matched');")
    out.append(pay("DEMO-TX-0830A", "DEMO-BBVA-MXN", "58000.00", "MXN", "2026-08-30", "014180000011112222", u["ELE-A-1021"]))
    out.append(pay("DEMO-TX-0821A", "DEMO-BBVA-MXN", "556800.00", "MXN", "2026-08-21", "021180000033334444", u["EST-B-77"]))
    out.append(pay("DEMO-TX-0728A", "DEMO-BBVA-MXN", "63200.00", "MXN", "2026-07-28", "058180000077778888", u["ING-C-5"]))
    out.append(pay("DEMO-TX-U0808", "DEMO-BANORTE-USD", "121800.00", "USD", "2026-08-08", "044180000055556666", u["MOD-M-310"]))
    # the credit note reduces its original (applied as a credit allocation)
    out.append(f"INSERT INTO allocation (credit_uuid, invoice_ref, invoice_side, amount, by_user) SELECT '{u['ELE-NC-12']}', '{u['ELE-A-1021']}', 'ap', 11600.00, 'demo'"
               f" WHERE NOT EXISTS (SELECT 1 FROM allocation WHERE credit_uuid = '{u['ELE-NC-12']}');")
    # own transfers and fees reconciled as such
    out.append("INSERT INTO bank_match (line_key, target_kind, target_ref, amount, by_user) SELECT line_key, 'transfer', 'own', amount, 'demo' FROM bank_transaction t"
               " WHERE own_transfer AND NOT EXISTS (SELECT 1 FROM bank_match m WHERE m.line_key = t.line_key AND m.reversed_at IS NULL);")
    out.append("INSERT INTO bank_match (line_key, target_kind, target_ref, amount, by_user) SELECT line_key, 'fee', 'bank fee', amount, 'demo' FROM bank_transaction t"
               " WHERE (description ILIKE '%COMISION%' OR description ILIKE '%WIRE FEE%') AND NOT EXISTS (SELECT 1 FROM bank_match m WHERE m.line_key = t.line_key AND m.reversed_at IS NULL);")
    # statuses follow the money
    out.append("UPDATE supplier_invoice i SET status = CASE WHEN coalesce((SELECT sum(amount) FROM allocation a WHERE a.invoice_ref = i.cfdi_uuid AND a.invoice_side = 'ap'), 0) >= i.total THEN 'paid'"
               " WHEN coalesce((SELECT sum(amount) FROM allocation a WHERE a.invoice_ref = i.cfdi_uuid AND a.invoice_side = 'ap'), 0) > 0 THEN 'partially_paid' ELSE status END"
               " WHERE i.entity_id = 'DEMO-MX' AND i.tipo = 'I' AND i.status IN ('approved', 'partially_paid', 'paid');")
    out.append("UPDATE customer_invoice i SET status = CASE WHEN coalesce((SELECT sum(amount) FROM allocation a WHERE a.invoice_ref = i.savio_invoice_id AND a.invoice_side = 'ar'), 0) >= i.total THEN 'paid'"
               " WHEN coalesce((SELECT sum(amount) FROM allocation a WHERE a.invoice_ref = i.savio_invoice_id AND a.invoice_side = 'ar'), 0) > 0 THEN 'partially_paid' ELSE status END"
               " WHERE i.entity_id = 'DEMO-MX' AND i.status NOT IN ('cancelled');")
    # the milestone the paid Savio invoice billed
    out.append("UPDATE project_milestone SET billed_invoice = 'inv_demo_0101' WHERE project_id = 'ARG9001' AND ref = 'M2' AND billed_invoice IS NULL;")
    out.append("UPDATE project_milestone SET billed_invoice = 'inv_demo_0102' WHERE project_id = 'ARG9003' AND ref = 'M2' AND billed_invoice IS NULL;")
    out.append("UPDATE project_milestone SET billed_invoice = 'inv_demo_0103' WHERE project_id = 'ARG9002' AND ref = 'M1' AND billed_invoice IS NULL;")
    out.append("UPDATE project_milestone SET billed_invoice = 'inv_demo_0104' WHERE project_id = 'ARG9005' AND ref = 'M3' AND billed_invoice IS NULL;")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--wipe", action="store_true")
    ap.add_argument("--decisions", action="store_true", help="the demo story's approvals, payments and matches (after fin_ingest --all --apply)")
    a = ap.parse_args(argv)
    from argia.store.pgq import psql_exec, psql_rows
    if a.decisions:
        world = json.loads(WORLD.read_text(encoding="utf-8"))
        psql_exec("SET statement_timeout='60s';\n" + decisions_sql(world["uuids"]))
        print("demo decisions applied (approvals, payments, matches)")
        return 0
    if a.wipe:
        psql_exec("SET statement_timeout='60s';\n" + WIPE_SQL)
        print("wiped every DEMO-* row")
        return 0
    world = json.loads(WORLD.read_text(encoding="utf-8"))

    def ids(sql):
        return {r[0]: int(r[1]) for r in psql_rows("SET statement_timeout='10s'; " + sql) if len(r) >= 2}
    sup = ids("SELECT rfc, supplier_id FROM supplier WHERE entity_id IN ('DEMO-MX','DEMO-CZ');")
    cus = ids("SELECT rfc, customer_id FROM customer_master WHERE savio_customer_id LIKE 'cus_demo_%';")
    stmts = seed_sql(world, sup, cus)
    n = sum(s.count("INSERT INTO") for s in stmts)
    if not a.apply:
        print(f"dry run: {n} upserts in {len(stmts)} groups (suppliers known: {len(sup)}, customers known: {len(cus)}) — add --apply")
        return 0
    psql_exec("SET statement_timeout='60s';\n" + "\n".join(stmts))
    # second pass: serial ids now exist -> POs and customer links
    sup = ids("SELECT rfc, supplier_id FROM supplier WHERE entity_id IN ('DEMO-MX','DEMO-CZ');")
    cus = ids("SELECT rfc, customer_id FROM customer_master WHERE savio_customer_id LIKE 'cus_demo_%';")
    stmts2 = seed_sql(world, sup, cus)
    psql_exec("SET statement_timeout='60s';\n" + "\n".join(stmts2))
    counts = psql_rows("SET statement_timeout='10s'; SELECT 'project', count(*) FROM project WHERE entity_id LIKE 'DEMO-%'"
                       " UNION ALL SELECT 'purchase_order', count(*) FROM purchase_order WHERE entity_id LIKE 'DEMO-%'"
                       " UNION ALL SELECT 'budget_line', count(*) FROM budget_line WHERE project_id LIKE 'ARG90%'"
                       " UNION ALL SELECT 'supplier', count(*) FROM supplier WHERE entity_id LIKE 'DEMO-%';")
    print("applied: " + ", ".join(f"{r[0]}={r[1]}" for r in counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
