"""State machines, approvals and guards (scenarios 3, 4, 11–13, 23, 25,
49, 51, 59, 64).

Every transition is a table, every refusal is a RuleError with the
reason in words — the UI shows it, the audit stores it.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from argia.fin.money import D, q


class RuleError(ValueError):
    pass


# ------------------------------------------------------------ projects
PROJECT_STATUSES = ("draft", "active", "on_hold", "commissioned", "closed", "cancelled")
PROJECT_TRANSITIONS = {
    "draft": ("active", "cancelled"),
    "active": ("on_hold", "commissioned", "cancelled"),
    "on_hold": ("active", "cancelled"),
    "commissioned": ("closed", "active"),        # back to active only with authorisation (reopen)
    "closed": ("active",),                        # reopen, authorised + audited
    "cancelled": (),
}
REQUIRED_TO_ACTIVATE = ("customer", "site", "entity_id", "project_type", "contract_value", "pm_user", "budget_approved")


def project_transition(status: str, to: str, *, facts: Optional[dict] = None, authorised: bool = False) -> str:
    """Valid moves only; activation needs the commercial facts; reopening
    needs authorisation (scenario 2/3)."""
    if status not in PROJECT_TRANSITIONS:
        raise RuleError(f"unknown project status {status!r}")
    if to not in PROJECT_TRANSITIONS[status]:
        raise RuleError(f"a {status} project cannot become {to}")
    if to == "active" and status == "draft":
        missing = [k for k in REQUIRED_TO_ACTIVATE if not (facts or {}).get(k)]
        if missing:
            raise RuleError("cannot activate without " + ", ".join(missing))
    if to == "active" and status in ("closed", "commissioned") and not authorised:
        raise RuleError(f"reopening a {status} project needs authorisation")
    return to


def closure_blockers(open_pos: int, unpaid_invoices: int, unbilled_milestones: int,
                     open_change_orders: int, remaining_committed: Decimal) -> List[str]:
    """What stops a financial close (scenario 51). Empty = may close."""
    out: List[str] = []
    if open_pos:
        out.append(f"{open_pos} open purchase order(s)")
    if unpaid_invoices:
        out.append(f"{unpaid_invoices} unpaid supplier invoice(s)")
    if unbilled_milestones:
        out.append(f"{unbilled_milestones} completed milestone(s) not invoiced")
    if open_change_orders:
        out.append(f"{open_change_orders} pending change order(s)")
    if D(remaining_committed) > 0:
        out.append(f"{D(remaining_committed)} still committed")
    return out


def close_project(blockers: Sequence[str], override_reason: str = "", authorised: bool = False) -> str:
    if blockers and not (override_reason.strip() and authorised):
        raise RuleError("cannot close: " + "; ".join(blockers) + " — override needs a reason and authorisation")
    return "closed"


# ---------------------------------------------------------- milestones
@dataclass(frozen=True)
class Milestone:
    ref: str
    kind: str                    # commercial | technical
    planned: dt.date
    baseline: dt.date
    actual: Optional[dt.date] = None
    billable: bool = False
    billed_ref: str = ""         # invoice ref once billed
    amount: Decimal = Decimal("0.00")
    depends_on: Tuple[str, ...] = ()


def reschedule(m: Milestone, new_planned: dt.date) -> Milestone:
    """Planned moves; baseline never does (scenario 5)."""
    return Milestone(m.ref, m.kind, new_planned, m.baseline, m.actual, m.billable, m.billed_ref, m.amount, m.depends_on)


def complete(m: Milestone, on: dt.date, done: Iterable[str]) -> Milestone:
    """Completion needs the dependencies done first (scenario 4)."""
    missing = [d for d in m.depends_on if d not in set(done)]
    if missing:
        raise RuleError(f"{m.ref} depends on {', '.join(missing)}")
    if m.actual is not None:
        raise RuleError(f"{m.ref} already completed on {m.actual}")
    return Milestone(m.ref, m.kind, m.planned, m.baseline, on, m.billable, m.billed_ref, m.amount, m.depends_on)


def bill_milestone(m: Milestone, invoice_ref: str, remaining_contract: Decimal) -> Milestone:
    """Billing eligibility: completed, billable, not yet billed, within
    the contract balance (scenarios 4, 25, 49)."""
    if not m.billable:
        raise RuleError(f"{m.ref} is not a billing milestone")
    if m.actual is None:
        raise RuleError(f"{m.ref} is not completed")
    if m.billed_ref:
        raise RuleError(f"{m.ref} already billed on {m.billed_ref}")
    if D(m.amount) > D(remaining_contract):
        raise RuleError(f"{m.ref} amount {D(m.amount)} exceeds remaining contract {D(remaining_contract)}")
    return Milestone(m.ref, m.kind, m.planned, m.baseline, m.actual, m.billable, invoice_ref, m.amount, m.depends_on)


def late_milestones(ms: Iterable[Milestone], today: dt.date) -> List[Tuple[Milestone, int]]:
    """(milestone, days late) for open milestones past their planned date."""
    out = []
    for m in ms:
        if m.actual is None and m.planned < today:
            out.append((m, (today - m.planned).days))
    return sorted(out, key=lambda t: -t[1])


def unbilled(ms: Iterable[Milestone]) -> List[Milestone]:
    """Completed, billable, not invoiced — the exception (scenario 49)."""
    return [m for m in ms if m.billable and m.actual is not None and not m.billed_ref]


# ---------------------------------------------------- purchase orders
PO_TRANSITIONS = {
    "draft": ("submitted", "cancelled"),
    "submitted": ("approved", "draft", "cancelled"),
    "approved": ("partially_received", "closed", "cancelled"),
    "partially_received": ("closed", "cancelled"),
    "closed": (),
    "cancelled": (),
}


def po_transition(status: str, to: str) -> str:
    if status not in PO_TRANSITIONS:
        raise RuleError(f"unknown PO status {status!r}")
    if to not in PO_TRANSITIONS[status]:
        raise RuleError(f"a {status} PO cannot become {to}")
    return to


@dataclass(frozen=True)
class ApprovalLevel:
    name: str
    up_to: Optional[Decimal]     # None = unlimited


DEFAULT_LEVELS = (ApprovalLevel("pm", Decimal("50000")), ApprovalLevel("director", Decimal("500000")),
                  ApprovalLevel("board", None))


def approval_route(amount: Decimal, levels: Sequence[ApprovalLevel] = DEFAULT_LEVELS) -> List[str]:
    """Every level up to and including the first whose limit covers the
    amount (scenario 13: multi-level, by value)."""
    amt = D(amount)
    route: List[str] = []
    for lv in levels:
        route.append(lv.name)
        if lv.up_to is None or amt <= D(lv.up_to):
            return route
    return route


def can_approve(approver: str, creator: str, amount: Decimal, self_approve_limit: Decimal = Decimal("0")) -> None:
    """Segregation of duties: the creator may not approve above the
    self-approval limit (scenarios 11, 23, 59)."""
    if approver.strip().lower() == creator.strip().lower() and D(amount) > D(self_approve_limit):
        raise RuleError(f"{approver} created this and cannot approve {D(amount)} (limit {D(self_approve_limit)})")


def po_amend(status: str, old_total: Decimal, new_total: Decimal, material_pct: Decimal = Decimal("0")) -> str:
    """A change of amount after approval resets the approval — the new
    status; a draft simply changes (scenario 13)."""
    if status in ("closed", "cancelled"):
        raise RuleError(f"a {status} PO cannot change")
    if status in ("approved", "partially_received") and not _immaterial(old_total, new_total, material_pct):
        return "submitted"
    return status


def _immaterial(old: Decimal, new: Decimal, pct: Decimal) -> bool:
    old, new = D(old), D(new)
    if old == 0:
        return new == 0
    return abs(new - old) <= q(abs(old) * D(pct) / 100)


# ------------------------------------------------------------ periods
def period_guard(doc_date: dt.date, closed_through: Optional[dt.date]) -> None:
    """A document dated inside a closed period is refused; an adjustment
    goes into an open month referencing the original (scenario 64)."""
    if closed_through and doc_date <= closed_through:
        raise RuleError(f"{doc_date} is in a closed period (closed through {closed_through})")


def adjustment_date(today: dt.date, closed_through: Optional[dt.date]) -> dt.date:
    """The first open day — where a correction to a closed month is booked."""
    if closed_through and today <= closed_through:
        return closed_through + dt.timedelta(days=1)
    return today
