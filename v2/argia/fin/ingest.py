"""Sources -> ledger rows -> idempotent SQL (v244).

Pure transforms: a Savio invoice dict, a CFDI, a statement, a PMO
snapshot become row dicts for the tables in ``schema.py``; ``upsert``
turns rows into INSERT ... ON CONFLICT statements keyed on the natural
key, so running an import twice changes nothing (scenario 67). The
scripts do the I/O.
"""
from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from argia.fin import cfdi as CF
from argia.fin.cash import BankLine, Statement, check_statement, is_own_transfer
from argia.fin.money import D

Row = Dict[str, Any]


# ------------------------------------------------------------------ SQL
def _lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        return repr(v) if not isinstance(v, Decimal) else str(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return "'" + v.isoformat() + "'"
    if isinstance(v, (list, tuple)):
        return "ARRAY[" + ", ".join(_lit(x) for x in v) + "]::text[]" if v else "ARRAY[]::text[]"
    if isinstance(v, dict):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def upsert(table: str, rows: Sequence[Row], key: Sequence[str], update: Optional[Sequence[str]] = None,
           never_update: Sequence[str] = ()) -> str:
    """One statement per row: INSERT ... ON CONFLICT (key) DO UPDATE SET
    <update cols> — only the columns named in ``update`` (default: all
    non-key columns except ``never_update``) so a re-import refreshes
    what the source owns and leaves the portal's decisions alone."""
    out: List[str] = []
    for r in rows:
        cols = list(r.keys())
        upd = [c for c in (update if update is not None else cols) if c not in key and c not in never_update]
        stmt = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(_lit(r[c]) for c in cols)})"
                f" ON CONFLICT ({', '.join(key)}) DO ")
        stmt += ("UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in upd)) if upd else "NOTHING"
        out.append(stmt + ";")
    return "\n".join(out)


# ------------------------------------------------------------ Savio (AR)
def customer_invoice_row(inv: dict, entity_id: str, customer_ids: Dict[str, int]) -> Row:
    """A Savio invoice (include=cfdis,items) -> customer_invoice row."""
    cf = inv.get("custom_fields") or {}
    cfdis = [c for c in (inv.get("cfdis") or []) if (c.get("type") or "I") == "I"]
    uuid = (cfdis[0].get("uuid") if cfdis else None) or None
    status = (inv.get("status") or "open").strip().lower()
    if any((c.get("status") or "") == "cancelled" for c in cfdis):
        status = "cancelled"
    return {
        "savio_invoice_id": inv["invoice_id"],
        "cfdi_uuid": uuid.upper() if uuid else None,
        "entity_id": entity_id,
        "customer_id": customer_ids.get(inv.get("customer_id") or ""),
        "project_id": (cf.get("argia_project_id") or None),
        "plant_key": (cf.get("argia_plant_key") or None),
        "issue_date": inv["issue_date"],
        "due_date": inv.get("due_date"),
        "currency": inv.get("currency") or "MXN",
        "subtotal": D(inv.get("subtotal")),
        "tax": D(inv.get("tax")),
        "total": D(inv.get("total")),
        "status": status,
        "savio_updated_at": inv.get("updated_at"),
    }


def customer_payment_rows(pay: dict, entity_id: str, account_id: str) -> Tuple[Row, Row]:
    """A Savio payment -> (payment row, allocation row). The bank line is
    linked later by the reconciliation, not here."""
    p = {
        "entity_id": entity_id, "account_id": account_id, "direction": "in",
        "pay_date": pay["date"], "amount": D(pay["amount"]), "currency": pay.get("currency") or "MXN",
        "counterpart": pay.get("customer_id"), "savio_payment_id": pay["payment_id"], "created_by": "savio_sync",
    }
    a = {"invoice_ref": pay["invoice_id"], "invoice_side": "ar", "amount": D(pay["amount"]), "by_user": "savio_sync",
         "savio_payment_id": pay["payment_id"]}
    return p, a


# --------------------------------------------------------- CFDI (AP)
def supplier_invoice_row(c: CF.Cfdi, entity_id: str, supplier_ids: Dict[str, int], xml_drive_id: Optional[str] = None,
                         hint: Optional[dict] = None) -> Row:
    """A parsed supplier CFDI -> supplier_invoice row. ``hint`` may carry
    project_id / po_id / cost_code found by the matcher; the row status
    starts at 'received' — the match and the approval are decisions."""
    hint = hint or {}
    terms = hint.get("terms_days", 30)
    issue = dt.date.fromisoformat(c.fecha[:10])
    return {
        "cfdi_uuid": c.uuid, "entity_id": entity_id, "supplier_id": supplier_ids.get(c.emisor_rfc),
        "emisor_rfc": c.emisor_rfc, "project_id": hint.get("project_id"), "po_id": hint.get("po_id"),
        "cost_code": hint.get("cost_code"), "issue_date": issue, "due_date": issue + dt.timedelta(days=int(terms)),
        "currency": c.moneda, "fx_rate": (c.tipo_cambio if c.moneda != "MXN" else None),
        "subtotal": c.subtotal, "tax": c.traslados, "retention": c.retenciones, "total": c.total, "tipo": c.tipo,
        "related_uuid": (c.relacionados[0] if c.relacionados else None), "status": "received", "xml_drive_id": xml_drive_id,
    }


def classify_cfdi(c: CF.Cfdi, our_rfcs: Sequence[str], known_uuids: Iterable[str]) -> Tuple[str, List[str]]:
    """What to do with a file: 'ingest' | 'skip' | 'reject', with reasons.
    Duplicates skip (nothing to say); tampered or foreign files reject
    into the exception queue (scenario 16/62/66)."""
    reasons: List[str] = []
    if c.uuid in set(known_uuids):
        return "skip", ["duplicate UUID"]
    bad = CF.check_totals(c)
    if bad:
        reasons += ["TOTALS:" + b for b in bad]
    d = CF.direction(c, list(our_rfcs))
    if d == "foreign":
        reasons.append("FOREIGN_CFDI: neither RFC is ours")
    elif d == "customer":
        reasons.append("CUSTOMER_CFDI: we are the emisor — belongs to the AR mirror, not the AP inbox")
    return ("reject" if reasons else "ingest"), reasons


def po_hint_from_concepto(text: str, projects: Iterable[str], po_numbers: Iterable[str]) -> dict:
    """The demo CFDIs carry 'cost · project · PO' in the concept; a real
    supplier writes the PO number somewhere in the description too.
    Returns whatever identifiers appear verbatim — never a guess."""
    out: dict = {}
    for p in projects:
        if p and p in text:
            out["project_id"] = p
    for po in po_numbers:
        if po and po in text:
            out["po_number"] = po
    return out


def exception_row(kind: str, ref: str, detail: str, entity_id: Optional[str] = None, project_id: Optional[str] = None,
                  owner: Optional[str] = None) -> Row:
    return {"kind": kind, "ref": ref, "entity_id": entity_id, "project_id": project_id, "owner": owner, "detail": detail[:900]}


# ------------------------------------------------------------- bank
def statement_rows(st: Statement, own_accounts: Iterable[str], file_sha: str, file_drive_id: Optional[str] = None
                   ) -> Tuple[Row, List[Row], List[str]]:
    """(bank_statement row, bank_transaction rows, problems). A statement
    with problems yields no rows — it loads whole or not at all."""
    problems = check_statement(st)
    if problems:
        return {}, [], problems
    head = {"account_id": st.account, "period_start": st.period_start, "period_end": st.period_end,
            "opening": D(st.opening), "closing": D(st.closing), "file_drive_id": file_drive_id, "file_sha256": file_sha}
    lines = [{"line_key": l.key, "account_id": l.account, "tx_date": l.date, "amount": D(l.amount),
              "description": l.description, "counterpart": l.counterpart or None,
              "own_transfer": is_own_transfer(l, own_accounts)} for l in st.lines]
    return head, lines, []


# ------------------------------------------------------------- PMO snapshot
def snapshot_rows(snap: dict, entity_of: Dict[str, str], now_iso: str) -> Tuple[List[Row], List[Row], List[Row]]:
    """(project rows to fill NULLs only, milestone rows, task rows). The
    snapshot owns schedule: baseline/planned/actual dates, tasks,
    progress. It never touches money or status — those stay the
    portal's (upsert with ``update`` limited accordingly)."""
    projects: List[Row] = []
    milestones: List[Row] = []
    tasks: List[Row] = []
    for p in snap.get("projects") or []:
        pid = p["project_id"]
        projects.append({"project_id": pid, "entity_id": entity_of.get(pid, "DEMO-MX"), "name": p.get("name") or pid,
                         "site": p.get("site"), "project_type": (p.get("type") or "OTHER").upper(), "pm_user": p.get("pm"),
                         "pmo_sheet_id": p.get("sheet_id")})
        for m in p.get("milestones") or []:
            milestones.append({"project_id": pid, "ref": m["ref"], "name": m["name"], "kind": m.get("kind") or "technical",
                               "baseline_date": m["baseline"], "planned_date": m["planned"], "actual_date": m.get("actual"),
                               "billable": bool(m.get("billable")), "amount": D(m.get("amount") or 0), "depends_on": list(m.get("depends_on") or [])})
        for t in p.get("tasks") or []:
            tasks.append({"project_id": pid, "task_id": t["task_id"], "name": t["name"], "phase": t.get("phase"),
                          "start_date": t.get("start"), "end_date": t.get("end"), "progress_pct": D(t.get("progress_pct") or 0),
                          "resource": t.get("resource"), "snapshot_at": now_iso})
    return projects, milestones, tasks


SNAPSHOT_MILESTONE_UPDATE = ("name", "kind", "planned_date", "actual_date", "depends_on")    # baseline, billable, amount, billed: portal's
SNAPSHOT_PROJECT_UPDATE = ("pmo_sheet_id",)                                                 # everything else: portal's
