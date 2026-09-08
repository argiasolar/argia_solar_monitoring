"""Project health score (scenario 72): deterministic, explained, with
versioned thresholds. 100 = on plan; the three risks subtract.

  schedule  worst open-milestone slip vs baseline, days
  budget    EAC vs active budget, %
  cash      unpaid supplier invoices past due + unbilled completed
            milestones, as % of contract
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional

from argia.fin.money import D, pct, q

THRESHOLDS_VERSION = "2026-09-08.1"

SCHEDULE_STEPS = ((7, 0), (30, 10), (60, 25), (None, 40))     # days late -> penalty
BUDGET_STEPS = ((Decimal("2"), 0), (Decimal("5"), 10), (Decimal("10"), 25), (None, 40))   # % over budget
CASH_STEPS = ((Decimal("2"), 0), (Decimal("5"), 10), (Decimal("15"), 20), (None, 30))     # % of contract


def _step(value, steps):
    for limit, penalty in steps:
        if limit is None or value <= limit:
            return penalty
    return steps[-1][1]


@dataclass(frozen=True)
class Health:
    score: int
    band: str                    # green | amber | red
    schedule_penalty: int
    budget_penalty: int
    cash_penalty: int
    reasons: List[str]
    thresholds_version: str = THRESHOLDS_VERSION


def score(days_late: int, eac: Decimal, budget: Decimal, cash_exposure: Decimal, contract: Decimal) -> Health:
    reasons: List[str] = []
    sp = _step(max(0, int(days_late)), SCHEDULE_STEPS)
    if sp:
        reasons.append(f"schedule: {days_late} days behind baseline")
    over = pct(D(eac) - D(budget), budget) if D(budget) > 0 else None
    bp = _step(over, BUDGET_STEPS) if over is not None and over > 0 else 0
    if bp:
        reasons.append(f"budget: EAC {D(eac)} is {over}% over budget {D(budget)}")
    exp = pct(cash_exposure, contract) if D(contract) > 0 else None
    cp = _step(exp, CASH_STEPS) if exp is not None and exp > 0 else 0
    if cp:
        reasons.append(f"cash: exposure {D(cash_exposure)} = {exp}% of contract")
    s = max(0, 100 - sp - bp - cp)
    band = "green" if s >= 80 else "amber" if s >= 60 else "red"
    if not reasons:
        reasons.append("on plan")
    return Health(s, band, sp, bp, cp, reasons)
