"""Project cost, forecast and margin (scenarios 8, 9, 14, 31–37, 48).

Definitions (AGS-904 §5):
  actual     = Σ approved supplier invoices − credit notes, by cost code
               (a payment is never a cost; a rejected invoice never is)
  committed  = Σ approved, open POs − what is already invoiced on them
  ETC        = estimate to complete the uncommitted remainder
  EAC        = actual + committed + ETC
  contract   = baseline contract + Σ approved change orders
  margin     = contract − EAC ; baseline margin = baseline contract − baseline budget
  erosion    = baseline margin − forecast margin
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

from argia.fin.money import D, pct, q


@dataclass(frozen=True)
class CostLine:
    cost_code: str
    amount: Decimal
    kind: str                    # invoice | credit_note | po | po_invoiced
    status: str = "approved"
    ref: str = ""


@dataclass(frozen=True)
class ChangeOrder:
    ref: str
    status: str                  # pending | approved | rejected
    revenue_impact: Decimal = Decimal("0.00")
    cost_impact: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class BudgetVersion:
    version: int
    status: str                  # draft | approved | superseded | rejected
    lines: Dict[str, Decimal]    # cost_code -> amount

    @property
    def total(self) -> Decimal:
        return q(sum((D(v) for v in self.lines.values()), Decimal("0")))


def actual(lines: Iterable[CostLine]) -> Dict[str, Decimal]:
    """Approved invoices minus credit notes, per cost code."""
    out: Dict[str, Decimal] = {}
    for l in lines:
        if l.status != "approved":
            continue
        if l.kind == "invoice":
            out[l.cost_code] = q(out.get(l.cost_code, Decimal("0")) + D(l.amount))
        elif l.kind == "credit_note":
            out[l.cost_code] = q(out.get(l.cost_code, Decimal("0")) - D(l.amount))
    return out


def committed(lines: Iterable[CostLine]) -> Dict[str, Decimal]:
    """Approved open POs less what has been invoiced against them — a
    partial invoice never double-counts (scenario 14)."""
    out: Dict[str, Decimal] = {}
    for l in lines:
        if l.kind == "po" and l.status in ("approved", "partially_received"):
            out[l.cost_code] = q(out.get(l.cost_code, Decimal("0")) + D(l.amount))
        elif l.kind == "po_invoiced":
            out[l.cost_code] = q(out.get(l.cost_code, Decimal("0")) - D(l.amount))
    return {k: (v if v > 0 else Decimal("0.00")) for k, v in out.items()}


def total(d: Dict[str, Decimal]) -> Decimal:
    return q(sum((D(v) for v in d.values()), Decimal("0")))


def active_budget(versions: Sequence[BudgetVersion]) -> Optional[BudgetVersion]:
    """The approved version with the highest number; a draft or rejected
    revision never affects it (scenario 9)."""
    ok = [v for v in versions if v.status == "approved"]
    return max(ok, key=lambda v: v.version) if ok else None


def baseline_budget(versions: Sequence[BudgetVersion]) -> Optional[BudgetVersion]:
    """Version 1 once approved — immutable, kept for comparison (scenario 8)."""
    for v in versions:
        if v.version == 1 and v.status in ("approved", "superseded"):
            return v
    return None


def etc(budget: Optional[BudgetVersion], actual_by_code: Dict[str, Decimal],
        committed_by_code: Dict[str, Decimal]) -> Dict[str, Decimal]:
    """Estimate to complete per cost code = max(0, budget − actual −
    committed). A code over budget contributes 0, not a negative."""
    out: Dict[str, Decimal] = {}
    codes = set(budget.lines) if budget else set()
    codes |= set(actual_by_code) | set(committed_by_code)
    for c in codes:
        b = D(budget.lines.get(c, 0)) if budget else Decimal("0")
        left = q(b - D(actual_by_code.get(c, 0)) - D(committed_by_code.get(c, 0)))
        out[c] = left if left > 0 else Decimal("0.00")
    return out


def eac(actual_by_code: Dict[str, Decimal], committed_by_code: Dict[str, Decimal],
        etc_by_code: Dict[str, Decimal]) -> Decimal:
    return q(total(actual_by_code) + total(committed_by_code) + total(etc_by_code))


def contract_value(baseline: Decimal, change_orders: Iterable[ChangeOrder]) -> Decimal:
    """Only APPROVED change orders move the contract (scenario 31/36)."""
    return q(D(baseline) + sum((D(c.revenue_impact) for c in change_orders if c.status == "approved"), Decimal("0")))


def pending_exposure(change_orders: Iterable[ChangeOrder]) -> Dict[str, Decimal]:
    """Cost and revenue of pending change orders — visible, never booked
    (scenario 37)."""
    cost = q(sum((D(c.cost_impact) for c in change_orders if c.status == "pending"), Decimal("0")))
    rev = q(sum((D(c.revenue_impact) for c in change_orders if c.status == "pending"), Decimal("0")))
    return {"cost": cost, "revenue": rev}


@dataclass(frozen=True)
class Margin:
    contract: Decimal
    eac: Decimal
    margin: Decimal
    margin_pct: Optional[Decimal]
    baseline_contract: Decimal
    baseline_budget: Decimal
    baseline_margin: Decimal
    erosion: Decimal


def margin(baseline_contract: Decimal, change_orders: Sequence[ChangeOrder],
           versions: Sequence[BudgetVersion], lines: Sequence[CostLine]) -> Margin:
    """Forecast vs baseline margin, clearly separated (scenarios 34/35)."""
    act = actual(lines)
    com = committed(lines)
    bud = active_budget(versions)
    e = eac(act, com, etc(bud, act, com))
    contract = contract_value(baseline_contract, change_orders)
    base_b = baseline_budget(versions)
    base_budget = base_b.total if base_b else (bud.total if bud else Decimal("0.00"))
    base_margin = q(D(baseline_contract) - base_budget)
    m = q(contract - e)
    return Margin(contract=contract, eac=e, margin=m, margin_pct=pct(m, contract),
                  baseline_contract=D(baseline_contract), baseline_budget=base_budget,
                  baseline_margin=base_margin, erosion=q(base_margin - m))


def budget_variance(versions: Sequence[BudgetVersion]) -> Optional[Decimal]:
    """Active vs baseline total, signed (+ = grew)."""
    a, b = active_budget(versions), baseline_budget(versions)
    if a is None or b is None:
        return None
    return q(a.total - b.total)
