"""v245 — from the parsed sources (contpaq / acctbook / portfolio /
pmo_sheet) to table rows and idempotent SQL. Pure: no I/O, no database;
``scripts/fin_drive_ingest.py`` fetches the files and runs the SQL.

Every row carries ``source_sha`` — the content hash of the file it came
from (``fin_source_file``). Re-importing the same file is a no-op; a new
monthly print replaces what the previous one said (same natural keys).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

from . import acctbook as AB
from . import contpaq as CP
from . import costcenter as CC
from . import pmo_sheet as PS
from . import portfolio as PF
from .ingest import Row, _lit, upsert

ENTITY_MX = {"entity_id": "ARGIA-MX", "name": "ARGIA México S.A. de C.V.", "rfc": "AME1407113A7", "currency": "MXN"}


def entity_sql(entity: Dict = ENTITY_MX) -> str:
    return upsert("entity", [dict(entity, active=True)], ("entity_id",), update=("name", "rfc", "currency"))


def source_file_row(sha: str, entity_id: str, kind: str, name: str, drive_id: str = "", modified: Optional[dt.datetime] = None,
                    period: str = "", rows: int = 0, notes: str = "", mime: str = "") -> Row:
    return {"sha256": sha, "entity_id": entity_id, "kind": kind, "name": name, "drive_id": drive_id or None,
            "modified": modified.isoformat() if modified else None, "period": period or None, "rows": rows, "notes": notes, "mime": mime}


def source_file_sql(row: Row) -> str:
    return upsert("fin_source_file", [row], ("sha256",), update=("name", "drive_id", "modified", "period", "rows", "notes", "mime"))


def source_touch_sql(sha: str, drive_id: str = "", mime: str = "", modified: Optional[dt.datetime] = None) -> str:
    """v250: a file whose content is unchanged is skipped by hash — but its
    Drive id may have only just become readable (the folders were shared).
    Refresh the pointer without re-importing anything."""
    sets = [f"drive_id = coalesce({_lit(drive_id or None)}, drive_id)", f"mime = CASE WHEN {_lit(mime)} <> '' THEN {_lit(mime)} ELSE mime END"]
    if modified:
        sets.append(f"modified = {_lit(modified.isoformat())}")
    return f"UPDATE fin_source_file SET {', '.join(sets)} WHERE sha256 = {_lit(sha)};"


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------- books
def gl_account_rows(entity_id: str, mapping: Dict[str, AB.AccountMap], aux: Optional[CP.AuxiliaresPrint] = None) -> List[Row]:
    natures = {a: l.nature for a, l in (aux.accounts.items() if aux else [])}
    out = []
    for acct, m in sorted(mapping.items()):
        out.append({"entity_id": entity_id, "account": acct, "name": m.name, "name_en": m.name_en, "bs_pl": m.bs_pl,
                    "a_p": m.a_p, "report_code": m.report_code, "report_account": m.report_account,
                    "nature": natures.get(acct, "credit" if m.a_p in ("P", "R") else "debit")})
    # accounts the print knows but the mapping does not (new this month) — keep them, unmapped
    for acct, l in sorted((aux.accounts.items() if aux else [])):
        if acct not in mapping and l.movements:
            out.append({"entity_id": entity_id, "account": acct, "name": l.name, "name_en": l.name, "bs_pl": "", "a_p": "",
                        "report_code": "", "report_account": "", "nature": l.nature})
    return out


def gl_account_sql(rows: Sequence[Row]) -> str:
    return upsert("gl_account", rows, ("entity_id", "account"))


def journal_rows(entity_id: str, pol: CP.PolizasPrint, sha: str, posted_keys: Optional[set] = None):
    """→ (journal rows, line rows). ``posted_keys``: journal keys the
    auxiliares print confirms; when given, the others are flagged
    ``posted = false`` (an unposted / later-deleted póliza)."""
    jr, lr = [], []
    for j in pol.journals:
        if not j.lines:                    # a header without lines (a cancelled póliza) carries nothing
            continue
        jr.append({"entity_id": entity_id, "jkey": j.key, "jdate": j.date.isoformat(), "kind": j.kind, "number": j.number,
                   "concept": j.concept, "control": j.control, "posted": (j.key in posted_keys) if posted_keys is not None else True,
                   "source_sha": sha})
        for l in j.lines:
            lr.append({"entity_id": entity_id, "jkey": j.key, "line_no": l.line_no, "account": l.account, "account_name": l.account_name,
                       "reference": l.reference, "segment": l.segment, "debit": l.debit, "credit": l.credit})
    return jr, lr


def journal_sql(entity_id: str, jr: Sequence[Row], lr: Sequence[Row], period_from: Optional[dt.date], period_to: Optional[dt.date]) -> str:
    """The print is the whole year to date: journals in the window that the
    new print no longer carries are deleted (their lines cascade), then
    everything is upserted."""
    parts = []
    if period_from and period_to and jr:
        keys = ", ".join(_lit(r["jkey"]) for r in jr)
        parts.append(f"DELETE FROM gl_journal WHERE entity_id = {_lit(entity_id)} AND jdate BETWEEN {_lit(period_from.isoformat())} AND "
                     f"{_lit(period_to.isoformat())} AND jkey NOT IN ({keys});")
    parts.append(upsert("gl_journal", jr, ("entity_id", "jkey")))
    parts.append(upsert("gl_line", lr, ("entity_id", "jkey", "line_no")))
    return "\n".join(p for p in parts if p)


def posted_keys(aux: CP.AuxiliaresPrint) -> set:
    """Journal keys the auxiliares print carries (date:kind:number)."""
    out = set()
    for l in aux.accounts.values():
        for m in l.movements:
            out.add(f"{m.date.isoformat()}:{m.kind}:{m.number}")
    return out


def balance_rows(entity_id: str, aux: CP.AuxiliaresPrint, sha: str, period: str) -> List[Row]:
    out = []
    for acct, l in sorted(aux.accounts.items()):
        if not l.movements and not l.opening:
            continue
        out.append({"entity_id": entity_id, "period": period, "account": acct, "name": l.name, "opening": l.opening,
                    "debits": l.debits, "credits": l.credits, "closing": l.closing_shown, "nature": l.nature,
                    "movements": len(l.movements), "source_sha": sha})
    return out


def balance_sql(rows: Sequence[Row]) -> str:
    return upsert("gl_balance", rows, ("entity_id", "period", "account"))


# ---------------------------------------------------------------- workbook
def report_rows(entity_id: str, period: str, sheet: str, lines: Sequence[AB.ReportLine], sha: str) -> List[Row]:
    out = []
    for l in lines:
        r: Row = {"entity_id": entity_id, "period": period, "sheet": sheet, "code": l.code, "label": l.label, "ord": l.order,
                  "ytd": l.ytd, "source_sha": sha}
        for i, v in enumerate(l.months, 1):
            r[f"m{i:02d}"] = v
        out.append(r)
    return out


def report_sql(rows: Sequence[Row]) -> str:
    return upsert("fin_report_line", rows, ("entity_id", "period", "sheet", "code"))


def biz_case_rows(entity_id: str, projects: Dict[int, AB.ProjectCode]) -> List[Row]:
    return [{"entity_id": entity_id, "code": p.code, "name": p.name, "project_type": p.project_type, "business_manager": p.business_manager}
            for p in sorted(projects.values(), key=lambda x: x.code)]


def biz_case_sql(rows: Sequence[Row]) -> str:
    return upsert("biz_case", rows, ("entity_id", "code"))


def cost_center_rows(entity_id: str, projects: Dict[int, AB.ProjectCode], project_codes: Iterable[int] = ()) -> List[Row]:
    """v248: the cost-centre catalogue from the accountants' project list."""
    return [{"entity_id": entity_id, "code": c.code, "name": c.name, "kind": c.kind, "grp": c.grp, "manual": False}
            for c in CC.build(projects.values(), None, project_codes)]


def cost_center_sql(rows: Sequence[Row]) -> str:
    """Upsert that respects a manual kind/group (set with fin_cost_centers.py --set)."""
    out = []
    for r in rows:
        cols = list(r.keys())
        out.append(f"INSERT INTO cost_center ({', '.join(cols)}) VALUES ({', '.join(_lit(r[c]) for c in cols)})"
                   f" ON CONFLICT (entity_id, code) DO UPDATE SET name = EXCLUDED.name,"
                   f" kind = CASE WHEN cost_center.manual THEN cost_center.kind ELSE EXCLUDED.kind END,"
                   f" grp = CASE WHEN cost_center.manual THEN cost_center.grp ELSE EXCLUDED.grp END;")
    return "\n".join(out)


def margin_rows(entity_id: str, period: str, gm: Dict[int, AB.ProjectMargin], sha: str) -> List[Row]:
    return [{"entity_id": entity_id, "period": period, "code": m.code, "name": m.name, "revenue_prior": m.revenue_prior, "cos_prior": m.cos_prior,
             "revenue_ytd": m.revenue_ytd, "cos_ytd": m.cos_ytd, "revenue_total": m.revenue_total, "cos_total": m.cos_total, "gm": m.gm,
             "gm_pct": m.gm_pct, "planned_value": m.planned_value, "planned_cost": m.planned_cost, "planned_margin": m.planned_margin,
             "planned_margin_pct": m.planned_margin_pct, "business_manager": m.business_manager, "source_sha": sha}
            for m in sorted(gm.values(), key=lambda x: x.code)]


def margin_sql(rows: Sequence[Row]) -> str:
    return upsert("project_margin", rows, ("entity_id", "period", "code"))


# ------------------------------------------------------------- portfolio
def portfolio_rows(entity_id: str, rows_in: Sequence[PF.OverviewRow], sha: str) -> List[Row]:
    out = []
    for o in rows_in:
        out.append({"entity_id": entity_id, "code": o.code, "project_id": o.project_id, "name": o.name, "phase": o.phase, "status": o.status,
                    "country": o.country, "business_manager": o.business_manager, "project_manager": o.project_manager,
                    "value_usd": o.value_usd, "value_mxn": o.value_mxn, "planned_cost_mxn": o.planned_cost_mxn,
                    "margin_planned_pct": o.margin_planned_pct, "contract_start": o.contract_start, "contract_end": o.contract_end,
                    "planned_start": o.planned_start, "planned_finish": o.planned_finish, "subcontractor": o.subcontractor,
                    "progress": o.progress, "handover": o.handover, "invoiced_mxn": o.invoiced_mxn, "paid_mxn": o.paid_mxn,
                    "po": o.po, "comment": o.comment, "source_sha": sha, "src_sheet": o.sheet, "src_row": o.row, "src_gid": ""})
    return out


def portfolio_sql(entity_id: str, rows: Sequence[Row]) -> str:
    """The overview is the whole list: rows it no longer carries go."""
    parts = [f"DELETE FROM portfolio_project WHERE entity_id = {_lit(entity_id)};"]
    if rows:
        parts.append(upsert("portfolio_project", rows, ("entity_id", "code", "name")))
    return "\n".join(parts)


def open_item_rows(entity_id: str, items: Sequence[PF.OpenItem], sha: str) -> List[Row]:
    out = []
    seen: Dict[str, int] = {}
    for it in items:
        base = "|".join([it.side, it.company, it.invoice, it.folio_fiscal, it.issued.isoformat() if it.issued else "", str(it.total), it.po])
        seen[base] = seen.get(base, 0) + 1          # identical rows (monthly leasing lines) get an ordinal
        key = sha_of(f"{base}#{seen[base]}".encode("utf-8"))[:24]
        out.append({"entity_id": entity_id, "item_key": key, "side": it.side, "status": it.status, "invoice": it.invoice, "company": it.company,
                    "project_code": it.project_code, "project_name": it.project_name, "po": it.po, "issued": it.issued, "due": it.due,
                    "new_due": it.new_due, "final_due": it.final_due, "days_to_due": it.days_to_due, "currency": it.currency,
                    "total": it.total, "net": it.net_usd if it.currency == "USD" else it.net_mxn, "mxn_equiv_net": it.mxn_equiv_net,
                    "folio_fiscal": it.folio_fiscal, "paid_on": it.paid_on, "kind": it.kind, "comment": it.comment, "source_sha": sha,
                    "src_sheet": it.sheet, "src_row": it.row, "src_gid": ""})
    return out


def open_item_sql(entity_id: str, rows: Sequence[Row]) -> str:
    """The tracker is a living list: rows that left the sheet leave the table."""
    parts = [f"DELETE FROM open_item WHERE entity_id = {_lit(entity_id)};"]
    if rows:
        parts.append(upsert("open_item", rows, ("entity_id", "item_key")))
    return "\n".join(parts)


# --------------------------------------------------------------------- PMO
def pmo_rows(entity_id: str, p: PS.PmoProject, sha: str, sheet_id: str = "", modified: Optional[dt.datetime] = None,
             gids: Optional[Dict[str, str]] = None):
    proj = {"project_id": p.project_id, "entity_id": entity_id, "code": p.code, "name": p.name, "customer": p.customer, "location": p.location,
            "status": p.status, "phase": p.phase, "contract_type": p.contract_type, "manager": p.manager, "supervisor": p.supervisor,
            "start_date": p.start, "end_date": p.end, "value": p.value, "cost": p.cost, "progress": p.progress, "sheet_id": sheet_id,
            "modified": modified.isoformat() if modified else None, "source_sha": sha}
    tasks = [{"project_id": p.project_id, "task_id": t.task_id, "wbs": t.wbs, "name": t.name, "is_phase": t.is_phase, "is_milestone": t.is_milestone,
              "resource": t.resource, "start_date": t.start, "end_date": t.end, "duration_days": t.duration_days, "priority": t.priority,
              "status": t.status, "progress": t.progress, "src_sheet": t.sheet, "src_row": t.row, "src_gid": (gids or {}).get(t.sheet, "")} for t in p.tasks]
    costs = [{"project_id": p.project_id, "cost_id": c.cost_id, "cost_date": c.date, "category": c.category, "vendor": c.vendor,
              "description": c.description, "net": c.net, "vat": c.vat, "total": c.total, "cost_status": c.cost_status, "approval": c.approval,
              "approved_by": c.approved_by, "paid": c.paid, "payment_status": c.payment_status,
              "src_sheet": c.sheet, "src_row": c.row, "src_gid": (gids or {}).get(c.sheet, "")} for c in p.costs]
    invs = [{"project_id": p.project_id, "invoice_id": i.invoice_id, "customer": i.customer, "milestone": i.milestone, "number": i.number,
             "inv_date": i.date, "due": i.due, "net": i.net, "vat": i.vat, "total": i.total, "status": i.status, "payment_status": i.payment_status,
             "paid_on": i.paid_on, "received": i.received,
             "src_sheet": i.sheet, "src_row": i.row, "src_gid": (gids or {}).get(i.sheet, "")} for i in p.invoices]
    return proj, tasks, costs, invs


def pmo_sql(proj: Row, tasks: Sequence[Row], costs: Sequence[Row], invs: Sequence[Row]) -> str:
    """A workbook is re-read whole: its children are replaced, the project
    row upserted (cascade keeps the children of an unchanged project)."""
    pid = _lit(proj["project_id"])
    parts = [upsert("pmo_project", [proj], ("project_id",)),
             f"DELETE FROM pmo_task WHERE project_id = {pid};", f"DELETE FROM pmo_cost WHERE project_id = {pid};",
             f"DELETE FROM pmo_invoice WHERE project_id = {pid};"]
    if tasks:
        parts.append(upsert("pmo_task", tasks, ("project_id", "task_id")))
    if costs:
        parts.append(upsert("pmo_cost", costs, ("project_id", "cost_id")))
    if invs:
        parts.append(upsert("pmo_invoice", invs, ("project_id", "invoice_id")))
    return "\n".join(parts)


# --------------------------------------------------------------- helpers
def period_of(d: Optional[dt.date]) -> str:
    return f"{d.year}-{d.month:02d}" if d else ""


def month_prefix_period(name: str) -> str:
    """'0726 Polizas Argia.xlsx' → '2026-07' (MMYY, the usual prefix);
    '122023 Auxiliares …' → '2023-12' (MMYYYY, the 2023 close);
    '2025 Polizas Argia.xlsx' → '2025-12' (a whole-year print)."""
    import re
    m = re.match(r"^(\d{2})(20\d{2})\s", name)
    if m:
        return f"{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(20\d{2})\s", name)
    if m:
        return f"{m.group(1)}-12"
    m = re.match(r"^(\d{2})(\d{2})\s", name)
    return f"20{m.group(2)}-{m.group(1)}" if m else ""


def acctbook_period(name: str) -> str:
    """'Argia_Accounting_Data_07_26_V1.xlsx' → '2026-07'; '…_12_2023_cambio saldos' → '2023-12'."""
    import re
    m = re.search(r"_(\d{2})_(20\d{2})", name)
    if m:
        return f"{m.group(2)}-{m.group(1)}"
    m = re.search(r"_(\d{2})_(\d{2})", name)
    return f"20{m.group(2)}-{m.group(1)}" if m else ""
