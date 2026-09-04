#!/usr/bin/env python3
"""The phase-3a gate: do the PostgreSQL finance tables say what the sheet
tabs say — THROUGH THE SAME PARSERS the jobs use?

Contract_Monthly, Design_Monthly, Loans, Loan_Schedule and
Maintenance_Events are read from the sheet and from PostgreSQL, each
side parsed by the production reader (parse_contract_grid,
parse_design_grid, loans_from_records, schedule_from_records,
events_from_pg_rows / the sheet loader), and the typed results are
compared field by field. Parsing both sides the same way is the point:
a serial-vs-ISO date or a Decimal-vs-float difference in the raw cells
is not a difference in what the jobs compute.

Rules:
  * Contract_Monthly: identical keys, identical fields.
  * Design: what production reads is Contract_Monthly's design_kwh
    (design.py's first candidate since v61) — that must be identical to
    PG. The legacy Design_Monthly tab is reported for information only:
    a row there that Contract_Monthly never had is not read by any job.
  * Loans / Loan_Schedule: the /setup/finance editor has been the only
    place loans change since 2026-09-01 and every change is a
    finance_audit row. A loan_id named in finance_audit is therefore
    PG-authoritative: its sheet-only rows (a deleted test loan), PG-only
    rows (a loaded credit, /finance/extend rows) and field differences
    are EXPECTED and listed. Any difference on an un-audited loan fails.
  * Maintenance_Events: every sheet event must exist in PG (the sheet
    has been empty since the /setup/ UI took over; PG may hold more).

    PYTHONPATH=. python scripts/finance_parity.py
EXIT: 0 clean   1 differences   3 config
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import fields, is_dataclass
from typing import Any, Dict, List, Tuple

TOL = 0.0051


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
            and not isinstance(a, bool) and not isinstance(b, bool):
        return abs(float(a) - float(b)) <= TOL
    return a == b


def _as_dict(obj) -> Dict[str, Any]:
    if is_dataclass(obj):
        return {f.name: getattr(obj, f.name) for f in fields(obj)}
    return {"value": obj}


def compare_maps(sheet: Dict, pg: Dict, allow_only_pg: bool = False,
                 expected=None) -> Dict:
    """Generic keyed comparison of two {key: dataclass|scalar} maps. PURE.

    ``expected(key) -> bool`` marks keys where PostgreSQL is the designed
    authority (audited loan edits): their differences are reported under
    'expected' and never fail the gate."""
    expected = expected or (lambda k: False)
    ks, kp = set(sheet), set(pg)
    only_s = sorted((k for k in ks - kp if not expected(k)), key=str)
    only_p = sorted((k for k in kp - ks if not expected(k)), key=str)
    exp: List[Tuple[str, Any]] = (
        [("only_sheet", k) for k in sorted(ks - kp, key=str) if expected(k)]
        + [("only_pg", k) for k in sorted(kp - ks, key=str) if expected(k)])
    diffs: List[Tuple[Any, str, Any, Any]] = []
    for k in sorted(ks & kp, key=str):
        a, b = _as_dict(sheet[k]), _as_dict(pg[k])
        for f in a:
            if not _same(a[f], b.get(f)):
                if expected(k):
                    exp.append(("diff", (k, f, a[f], b.get(f))))
                else:
                    diffs.append((k, f, a[f], b.get(f)))
    ok = not only_s and not diffs and (allow_only_pg or not only_p)
    return {"n_sheet": len(sheet), "n_pg": len(pg), "only_sheet": only_s,
            "only_pg": only_p, "diffs": diffs, "expected": exp, "ok": ok}


def render(name: str, rep: Dict, allow_only_pg: bool = False) -> str:
    L = [f"{name}: sheet={rep['n_sheet']} pg={rep['n_pg']}",
         f"  only in sheet: {len(rep['only_sheet'])} {rep['only_sheet'][:5]}   (must be 0)",
         f"  only in pg   : {len(rep['only_pg'])} {rep['only_pg'][:5]}"
         + ("   (allowed)" if allow_only_pg else "   (must be 0)"),
         f"  field diffs  : {len(rep['diffs'])}"]
    for k, f, a, b in rep["diffs"][:10]:
        L.append(f"     {k} {f}: sheet={a!r} pg={b!r}")
    if rep.get("expected"):
        L.append(f"  expected (PG authoritative, audited): {len(rep['expected'])}")
        for kind, what in rep["expected"][:6]:
            L.append(f"     {kind}: {what}")
    L.append("  VERDICT: " + ("CLEAN" if rep["ok"] else "DIFFERENT"))
    return "\n".join(L)


_LOAN_ID = re.compile(r"\b[A-Z]{2,6}\d{0,2}-L\d+\b")


def loan_ids_in_text(text: str) -> set:
    """Loan ids named in a free-text audit detail, e.g. the deleted
    SLP1-L2 in 'SLP1-L2 test loan deleted; ... SLP1-L3 loaded'. PURE."""
    return set(_LOAN_ID.findall(text or ""))


def audited_loan_ids() -> frozenset:
    """loan_ids touched through /setup/finance: the audit row's loan_id
    plus every loan id its detail names (a deleted loan only survives in
    the text). Server-only."""
    from argia.store.pgq import psql_rows
    ids = set()
    for r in psql_rows("SELECT coalesce(loan_id,''), coalesce(detail,'')"
                       " FROM finance_audit;"):
        if r and r[0]:
            ids.add(r[0])
        if len(r) > 1:
            ids |= loan_ids_in_text(r[1])
    return frozenset(ids)


def loan_expected(audited) -> Any:
    """Key predicate for Loans ({loan_id}) and Loan_Schedule
    ({(loan_id, ref_month)}) maps. PURE."""
    def _e(k):
        lid = k[0] if isinstance(k, tuple) else k
        return lid in audited
    return _e


def run(sheets, audited=None) -> Tuple[bool, str]:
    """All comparisons, both sources read explicitly (the switch is NOT
    consulted — this is the gate that decides it). Server-only."""
    from argia.finance import pg_source as P
    from argia.finance.contract import parse_contract_grid
    from argia.finance.loans import loans_from_records, schedule_from_records
    from argia.kpi.design import parse_design_grid
    from argia.maintenance.events import load_maintenance_events_pg
    from argia.maintenance import events as EV

    out: List[str] = []
    all_ok = True

    c_sheet = parse_contract_grid(sheets.read_range("Contract_Monthly", "A1:H"))
    c_pg = parse_contract_grid(P.read_contract_grid())
    rep = compare_maps(c_sheet, c_pg)
    all_ok &= rep["ok"]; out.append(render("Contract_Monthly", rep))

    # Design: production reads Contract_Monthly's design_kwh (v61+).
    d_sheet = parse_design_grid(sheets.read_range("Contract_Monthly", "A1:D"),
                                "Contract_Monthly")
    d_pg = parse_design_grid(P.read_design_grid(), "contract_monthly")
    rep = compare_maps(d_sheet, d_pg)
    all_ok &= rep["ok"]; out.append(render("Design (Contract_Monthly.design_kwh)", rep))
    try:
        legacy = parse_design_grid(sheets.read_range("Design_Monthly", "A1:D"),
                                   "Design_Monthly")
        extra = sorted(set(legacy) - set(d_sheet))
        out.append(f"  info: legacy Design_Monthly tab has {len(legacy)} rows; "
                   f"{len(extra)} not in Contract_Monthly (not read by any job "
                   f"since v61): {extra[:5]}")
    except Exception:  # noqa: BLE001
        out.append("  info: no Design_Monthly tab")

    if audited is None:
        audited = audited_loan_ids()
    exp = loan_expected(audited)
    l_sheet = loans_from_records(sheets.read_table("Loans"))
    l_pg = loans_from_records(P.read_loans_records())
    rep = compare_maps(l_sheet, l_pg, expected=exp)
    all_ok &= rep["ok"]; out.append(render("Loans", rep))

    s_sheet = {(r.loan_id, r.ref_month): r
               for r in schedule_from_records(sheets.read_table("Loan_Schedule"))}
    s_pg = {(r.loan_id, r.ref_month): r
            for r in schedule_from_records(P.read_schedule_records())}
    rep = compare_maps(s_sheet, s_pg, allow_only_pg=True, expected=exp)
    all_ok &= rep["ok"]; out.append(render("Loan_Schedule", rep, True))

    # Maintenance: force the sheet path explicitly (the loader is a door)
    saved = os.environ.pop(P.SOURCE_ENV, None)
    try:
        e_sheet = EV.load_maintenance_events(sheets)
    finally:
        if saved is not None:
            os.environ[P.SOURCE_ENV] = saved
    e_pg = load_maintenance_events_pg()
    key = lambda e: (e.plant_key, e.start_ts.isoformat())  # noqa: E731
    rep = compare_maps({key(e): e for e in e_sheet}, {key(e): e for e in e_pg},
                       allow_only_pg=True)
    all_ok &= rep["ok"]; out.append(render("Maintenance_Events", rep, True))

    out.append("OVERALL: " + ("CLEAN — safe to set ARGIA_FINANCE_SOURCE=pg"
                              if all_ok else "DIFFERENT — do not flip"))
    return bool(all_ok), "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.parse_args(argv)
    sid = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sid:
        print("GOOGLE_SHEET_ID_V2 not set"); return 3
    from argia.core.sheets import SheetsClient
    ok, text = run(SheetsClient(sid))
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
