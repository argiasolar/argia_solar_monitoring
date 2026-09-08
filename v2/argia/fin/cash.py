"""Bank statements and reconciliation (scenarios 28, 29, 30, 67).

A statement is opening balance + lines = closing balance, or it is
refused whole. A line has one natural key (the bank's id when there is
one, else account + date + amount + description hash) so a re-upload
adds nothing. A line reconciles once — a split must sum to the line.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from argia.fin.money import D, q, same


@dataclass(frozen=True)
class BankLine:
    account: str
    date: dt.date
    amount: Decimal              # signed: + credit, − debit
    description: str = ""
    counterpart: str = ""        # CLABE / account / name when the bank gives it
    bank_id: str = ""            # the bank's own transaction id, when present

    @property
    def key(self) -> str:
        if self.bank_id:
            return f"{self.account}:{self.bank_id}"
        h = hashlib.sha1(f"{self.description.strip().lower()}|{self.counterpart.strip().lower()}".encode("utf-8")).hexdigest()[:12]
        return f"{self.account}:{self.date.isoformat()}:{D(self.amount)}:{h}"


@dataclass(frozen=True)
class Statement:
    account: str
    period_start: dt.date
    period_end: dt.date
    opening: Decimal
    closing: Decimal
    lines: Tuple[BankLine, ...] = ()


class CashError(ValueError):
    pass


def check_statement(st: Statement) -> List[str]:
    """opening + Σ lines = closing; lines inside the period; one account.
    Empty list = loadable (scenario 30)."""
    out: List[str] = []
    total = q(sum((D(l.amount) for l in st.lines), Decimal("0")))
    expect = q(D(st.opening) + total)
    if not same(expect, st.closing):
        out.append(f"opening {D(st.opening)} + activity {total} = {expect} != closing {D(st.closing)}")
    for l in st.lines:
        if l.account != st.account:
            out.append(f"line {l.key} belongs to account {l.account}, statement is {st.account}")
        if not (st.period_start <= l.date <= st.period_end):
            out.append(f"line {l.key} dated {l.date} outside {st.period_start}..{st.period_end}")
    keys = [l.key for l in st.lines]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        out.append(f"duplicate lines inside the statement: {dup}")
    return out


def new_lines(st: Statement, known_keys: Iterable[str]) -> List[BankLine]:
    """The lines an import may add — the rest already exist (scenario 67)."""
    known = set(known_keys)
    return [l for l in st.lines if l.key not in known]


def is_own_transfer(line: BankLine, own_accounts: Iterable[str]) -> bool:
    """Money between our own accounts is neither income nor expense
    (scenario 30)."""
    cp = (line.counterpart or "").replace(" ", "")
    return any(cp and cp.endswith(str(a).replace(" ", "")[-10:]) for a in own_accounts if a)


@dataclass(frozen=True)
class Match:
    line_key: str
    target_kind: str             # payment | customer_payment | transfer | fee | tax | other
    target_ref: str
    amount: Decimal


def reconcile(line: BankLine, existing: Sequence[Match], new: Sequence[Match]) -> List[Match]:
    """Attach matches to a line. A line reconciles once unless split, and
    a split must sum exactly to the line — never more, never a second
    full match (scenario 29)."""
    for m in list(existing) + list(new):
        if m.line_key != line.key:
            raise CashError(f"match {m.target_ref} is for line {m.line_key}, not {line.key}")
        if D(m.amount) == 0:
            raise CashError("zero match")
    all_m = list(existing) + list(new)
    total = q(sum((D(m.amount) for m in all_m), Decimal("0")))
    if abs(total) > abs(D(line.amount)) + Decimal("0.00"):
        raise CashError(f"matches {total} exceed the line {D(line.amount)}")
    if (total > 0) != (D(line.amount) > 0) and total != 0:
        raise CashError("match sign differs from the line")
    return all_m


def reconciled_state(line: BankLine, matches: Sequence[Match]) -> str:
    total = q(sum((D(m.amount) for m in matches if m.line_key == line.key), Decimal("0")))
    if total == 0:
        return "open"
    if same(total, line.amount):
        return "reconciled"
    return "partial"


def unreconcile(line: BankLine, matches: Sequence[Match], target_ref: str) -> Tuple[List[Match], Optional[Match]]:
    """Remove one match; the caller records the event (audit — scenario 29)."""
    keep, gone = [], None
    for m in matches:
        if m.line_key == line.key and m.target_ref == target_ref and gone is None:
            gone = m
        else:
            keep.append(m)
    return keep, gone


def balance(opening: Decimal, lines: Iterable[BankLine]) -> Decimal:
    return q(D(opening) + sum((D(l.amount) for l in lines), Decimal("0")))


def company_cash(balances: Dict[str, Decimal], fx: Dict[str, Decimal], home: str = "MXN") -> Decimal:
    """Σ account balances in the home currency; balances keyed
    'account|CCY'. A missing FX rate is an error, not a 1.0."""
    total = Decimal("0")
    for key, bal in balances.items():
        ccy = key.split("|")[-1] if "|" in key else home
        if ccy == home:
            total += D(bal)
        else:
            rate = fx.get(f"{ccy}/{home}")
            if rate is None:
                raise CashError(f"no FX rate for {ccy}/{home}")
            total += D(bal) * D(rate)
    return q(total)
