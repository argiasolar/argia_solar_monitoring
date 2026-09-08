"""Bank statement files -> Statement (scenario 30, 66, 67).

One parser per bank format, registered by name; each is a pure function
over the file's text and is pinned by a fixture in tests. Unknown
formats are refused — a guessed column mapping is how a debit becomes
a credit. The generic format below is the portal's own CSV (what a
person exports from the bank and normalises once); bank-native
formats are added in Phase 1 from the sample files Tomasz drops.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from argia.fin.cash import BankLine, Statement
from argia.fin.money import D

Parser = Callable[[str, str], Statement]
REGISTRY: Dict[str, Parser] = {}


class BankFormatError(ValueError):
    pass


def register(name: str):
    def deco(fn: Parser) -> Parser:
        REGISTRY[name] = fn
        return fn
    return deco


def parse(fmt: str, text: str, account: str) -> Statement:
    fn = REGISTRY.get(fmt)
    if fn is None:
        raise BankFormatError(f"unknown bank format {fmt!r} — known: {sorted(REGISTRY)}")
    return fn(text, account)


def _date(s: str) -> dt.date:
    s = s.strip()
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
    raise BankFormatError(f"unreadable date {s!r}")


def _amount(s: str) -> Decimal:
    s = (s or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    if s in ("", "-"):
        return Decimal("0.00")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return D(s)


@register("argia_generic")
def parse_generic(text: str, account: str) -> Statement:
    """Header lines `opening,<amount>` / `closing,<amount>` /
    `period,<from>,<to>` then a CSV with columns
    date, description, debit, credit[, counterpart[, bank_id]]."""
    opening = closing = None
    p0 = p1 = None
    rows: List[List[str]] = []
    for row in csv.reader(io.StringIO(text)):
        if not row or not any(c.strip() for c in row):
            continue
        k = row[0].strip().lower()
        if k == "opening":
            opening = _amount(row[1])
        elif k == "closing":
            closing = _amount(row[1])
        elif k == "period":
            p0, p1 = _date(row[1]), _date(row[2])
        elif k == "date":
            continue                         # column header
        else:
            rows.append(row)
    if opening is None or closing is None or p0 is None:
        raise BankFormatError("missing opening/closing/period header lines")
    lines = []
    for r in rows:
        if len(r) < 4:
            raise BankFormatError(f"short row {r}")
        debit, credit = _amount(r[2]), _amount(r[3])
        if debit and credit:
            raise BankFormatError(f"row {r[:2]} has both debit and credit")
        if debit < 0 or credit < 0:
            raise BankFormatError(f"row {r[:2]}: debit/credit columns are unsigned")
        lines.append(BankLine(account=account, date=_date(r[0]), amount=(credit - debit),
                              description=r[1].strip(), counterpart=(r[4].strip() if len(r) > 4 else ""),
                              bank_id=(r[5].strip() if len(r) > 5 else "")))
    return Statement(account, p0, p1, opening, closing, tuple(lines))
