"""AP / AR ledger rules (scenarios 21, 22, 24, 27, 28, 45, 46, 62).

An *invoice* here is any receivable or payable: total, currency, issue
date, payment terms, status. *Allocations* are the money applied to it
(payments, credit notes). Everything is Decimal; nothing here knows
about PostgreSQL.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from argia.fin.money import D, q

AGING_BUCKETS = ((0, 30, "0-30"), (31, 60, "31-60"), (61, 90, "61-90"), (91, None, "90+"))
OPEN_STATUSES = ("received", "matched", "exception", "approved", "issued", "partially_paid")
DEAD_STATUSES = ("cancelled", "rejected", "paid")


@dataclass(frozen=True)
class Invoice:
    ref: str                     # UUID or Savio id
    total: Decimal
    issue_date: dt.date
    terms_days: int = 30
    currency: str = "MXN"
    status: str = "approved"
    due_date: Optional[dt.date] = None


@dataclass(frozen=True)
class Allocation:
    invoice_ref: str
    amount: Decimal
    kind: str = "payment"        # payment | credit_note | advance
    ref: str = ""                # payment id / credit note UUID
    date: Optional[dt.date] = None


class LedgerError(ValueError):
    pass


def due_date(inv: Invoice) -> dt.date:
    """Explicit due date wins; else issue + terms (scenario 21)."""
    return inv.due_date or (inv.issue_date + dt.timedelta(days=int(inv.terms_days)))


def outstanding(inv: Invoice, allocations: Iterable[Allocation]) -> Decimal:
    """total − Σ allocations for this invoice; a cancelled or rejected
    invoice owes nothing (scenario 21/46)."""
    if inv.status in ("cancelled", "rejected"):
        return Decimal("0.00")
    applied = sum((D(a.amount) for a in allocations if a.invoice_ref == inv.ref), Decimal("0"))
    return q(D(inv.total) - applied)


def allocate(inv: Invoice, existing: Sequence[Allocation], new: Allocation) -> List[Allocation]:
    """Apply one more allocation. Refused when it would exceed the
    outstanding balance, is not positive, or the invoice is not payable
    (scenario 22/24 — an invoice cannot be paid twice)."""
    if new.invoice_ref != inv.ref:
        raise LedgerError("allocation is for another invoice")
    if inv.status in ("cancelled", "rejected", "draft", "exception"):
        raise LedgerError(f"invoice {inv.ref} is {inv.status} — not payable")
    amt = D(new.amount)
    if amt <= 0:
        raise LedgerError("allocation must be positive")
    left = outstanding(inv, existing)
    if amt > left:
        raise LedgerError(f"allocation {amt} exceeds outstanding {left} on {inv.ref}")
    return list(existing) + [new]


def status_after(inv: Invoice, allocations: Iterable[Allocation]) -> str:
    """paid / partially_paid / the invoice's own status."""
    if inv.status in ("cancelled", "rejected"):
        return inv.status
    left = outstanding(inv, allocations)
    if left == 0:
        return "paid"
    if left < D(inv.total):
        return "partially_paid"
    return inv.status


def age_days(inv: Invoice, today: dt.date) -> int:
    """Days past due; ≤ 0 = not yet due."""
    return (today - due_date(inv)).days


def bucket_of(days_past_due: int) -> str:
    if days_past_due <= 0:
        return "current"
    for lo, hi, name in AGING_BUCKETS:
        if hi is None or days_past_due <= hi:
            if days_past_due >= lo:
                return name
    return "90+"


def aging(invoices: Iterable[Invoice], allocations: Sequence[Allocation], today: dt.date,
          currency: Optional[str] = None) -> Dict[str, Decimal]:
    """Outstanding per bucket (current, 0-30, 31-60, 61-90, 90+) plus
    'total'. Cancelled/paid invoices contribute nothing (scenario 21/27).
    One currency per call — mixing MXN and USD in one bucket is a lie."""
    out: Dict[str, Decimal] = {k: Decimal("0.00") for k in ("current", "0-30", "31-60", "61-90", "90+", "total")}
    for inv in invoices:
        if currency and inv.currency != currency:
            continue
        left = outstanding(inv, allocations)
        if left <= 0:
            continue
        b = bucket_of(age_days(inv, today))
        out[b] = q(out[b] + left)
        out["total"] = q(out["total"] + left)
    return out


def overdue(invoices: Iterable[Invoice], allocations: Sequence[Allocation], today: dt.date,
            limit: int = 10) -> List[Tuple[Invoice, Decimal, int]]:
    """(invoice, outstanding, days past due) sorted oldest first."""
    rows = []
    for inv in invoices:
        left = outstanding(inv, allocations)
        d = age_days(inv, today)
        if left > 0 and d > 0:
            rows.append((inv, left, d))
    rows.sort(key=lambda r: (-r[2], -r[1]))
    return rows[:limit]


def duplicate_payment(candidate: dict, recent: Iterable[dict], window_hours: int = 24) -> Optional[dict]:
    """Same account, same counterpart, same amount within the window ->
    the earlier one (scenario 24/62). Keys: account, counterpart, amount,
    ts (datetime)."""
    amt = D(candidate["amount"])
    for p in recent:
        if p.get("account") != candidate.get("account"):
            continue
        if (p.get("counterpart") or "").strip().upper() != (candidate.get("counterpart") or "").strip().upper():
            continue
        if D(p["amount"]) != amt:
            continue
        gap = abs((candidate["ts"] - p["ts"]).total_seconds()) / 3600.0
        if gap <= window_hours:
            return p
    return None


def apply_credit_note(inv: Invoice, allocations: Sequence[Allocation], credit_ref: str,
                      amount: Decimal, date: Optional[dt.date] = None) -> List[Allocation]:
    """A credit note reduces the payable/receivable like a payment does,
    tagged so cost/revenue reports can tell them apart (scenario 45)."""
    return allocate(inv, allocations, Allocation(inv.ref, D(amount), "credit_note", credit_ref, date))


def cancel(inv: Invoice, allocations: Sequence[Allocation]) -> Invoice:
    """A cancelled CFDI leaves the ledger; if money was already applied
    the caller must first move it (an advance or a refund) — refusing
    here keeps cash and the ledger consistent (scenario 46)."""
    if outstanding(inv, allocations) != D(inv.total) and inv.status not in ("cancelled",):
        raise LedgerError(f"{inv.ref} has allocations — reverse or reassign them before cancelling")
    return Invoice(inv.ref, inv.total, inv.issue_date, inv.terms_days, inv.currency, "cancelled", inv.due_date)
