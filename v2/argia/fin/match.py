"""Three-way match: purchase order = receipt = supplier invoice
(scenarios 18, 19, 20). Returns a verdict plus exception codes the
queue can own; the decision to override belongs to a person and is
audited elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from argia.fin.money import D, q

DEFAULT_TOL_PCT = Decimal("1.0")      # 1 %
DEFAULT_TOL_ABS = Decimal("500.00")   # 500 MXN


@dataclass(frozen=True)
class PO:
    number: str
    supplier_rfc: str
    currency: str
    total: Decimal
    status: str = "approved"
    invoiced_so_far: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class Receipt:
    po_number: str
    value: Decimal               # value received so far on the PO


@dataclass(frozen=True)
class SupplierInvoice:
    uuid: str
    supplier_rfc: str
    currency: str
    total: Decimal
    po_number: Optional[str] = None


@dataclass(frozen=True)
class MatchResult:
    verdict: str                 # matched | exception | no_po
    codes: List[str] = field(default_factory=list)
    detail: str = ""


def within_tolerance(a: Decimal, b: Decimal, tol_pct: Decimal = DEFAULT_TOL_PCT,
                     tol_abs: Decimal = DEFAULT_TOL_ABS) -> bool:
    """|a − b| ≤ max(tol_pct % of b, tol_abs)."""
    a, b = D(a), D(b)
    allowed = max(q(abs(b) * D(tol_pct) / 100), D(tol_abs))
    return abs(a - b) <= allowed


def three_way(inv: SupplierInvoice, po: Optional[PO], receipt: Optional[Receipt],
              tol_pct: Decimal = DEFAULT_TOL_PCT, tol_abs: Decimal = DEFAULT_TOL_ABS) -> MatchResult:
    """Exact and within-tolerance matches pass; anything else is an
    exception with every reason listed (a human fixes them all at once)."""
    if po is None:
        return MatchResult("no_po", ["MISSING_PO"], "invoice carries no purchase order")
    codes: List[str] = []
    if po.status not in ("approved", "partially_received", "closed"):
        codes.append("PO_NOT_APPROVED")
    if inv.supplier_rfc.strip().upper() != po.supplier_rfc.strip().upper():
        codes.append("WRONG_SUPPLIER")
    if inv.currency != po.currency:
        codes.append("WRONG_CURRENCY")
    remaining_po = q(D(po.total) - D(po.invoiced_so_far))
    if D(inv.total) > remaining_po and not within_tolerance(inv.total, remaining_po, tol_pct, tol_abs):
        codes.append("OVER_PO")
    if receipt is None:
        codes.append("NOT_RECEIVED")
    else:
        received_unbilled = q(D(receipt.value) - D(po.invoiced_so_far))
        if D(inv.total) > received_unbilled and not within_tolerance(inv.total, received_unbilled, tol_pct, tol_abs):
            codes.append("OVER_RECEIVED")
    if codes:
        return MatchResult("exception", codes, f"remaining on PO {remaining_po}")
    return MatchResult("matched", [], f"remaining on PO after this invoice {q(remaining_po - D(inv.total))}")


def receive(po_total: Decimal, received_so_far: Decimal, qty_value: Decimal) -> Decimal:
    """Add a receipt; over-receipt is blocked (scenario 15)."""
    new = q(D(received_so_far) + D(qty_value))
    if D(qty_value) <= 0:
        raise ValueError("receipt must be positive")
    if new > D(po_total):
        raise ValueError(f"over-receipt: {new} > PO total {D(po_total)}")
    return new
