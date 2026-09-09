"""v245 — readers for the CONTPAQi® exports the accountants drop in
``ACCOUNTING/Accounting Reporting/<year>/<n>.- <Month>/`` every month.

Three prints, all "report as a spreadsheet" (a header block, then rows
whose meaning depends on their shape, then totals):

* ``MMYY Polizas Argia.xlsx``    — *Diarios y Pólizas*: every journal
  entry of the year to date. A póliza header row (date, type, number,
  concept) is followed by its lines (line no., reference, account, name,
  journal, segment, debit, credit). The SEGMENT is the business-case
  number (701 = operation costs, 1473 = the Quijote roof, …) — the
  join key to the project overview and the PMO sheets.
* ``MMYY Auxiliares Argia.xlsx`` — *Movimientos auxiliares*: the same
  lines regrouped per account with the account's opening balance and a
  running balance. This is where each BANK ACCOUNT's statement lives
  (102-01-xxx) and where AR/AP per counterparty come from.
* ``Balanza`` (a sheet of the accountants' workbook) — trial balance:
  opening, debits, credits, closing per account.

Everything here is pure: the callers hand in ``rows`` (a list of tuples,
one per spreadsheet row, as openpyxl yields them) and get dataclasses
back. No file I/O, no dependency on openpyxl, so the unit tests run on a
synthetic print with the same shape (``tests/fixtures/fin/contpaq``) and
never on the real books — the repository is public.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .money import D

ACCOUNT_RE = re.compile(r"^\d{3}-\d{2}-\d{3}$")
DATE_RE = re.compile(r"^(\d{2})/([A-Za-z]{3})/(\d{4})$")
_MONTHS = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12,
           "jan": 1, "apr": 4, "aug": 8, "dec": 12}

def class_nature(account: str) -> str:
    """Default nature by account class (Mexican chart: 1 activo, 2 pasivo,
    3 capital, 4 ingresos, 5 costos, 6 gastos, 7 otros): liabilities,
    equity and revenue show credit-positive."""
    return "credit" if account[:1] in ("2", "3", "4") else "debit"


BANK_PREFIX = "102-01-"        # Bancos nacionales — one sub-account per bank account
AR_PREFIX = "105-01-"          # Clientes
AP_PREFIX = "201-01-"          # Proveedores


def parse_date(s) -> Optional[dt.date]:
    """'13/Ene/2026' -> date; anything else -> None."""
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    m = DATE_RE.match(str(s or "").strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    return dt.date(int(m.group(3)), mon, int(m.group(1)))


def _amt(v) -> Decimal:
    if v is None or v == "":
        return D(0)
    if isinstance(v, str):
        v = v.replace(",", "").strip() or "0"
    return D(v)


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _cell(row: Sequence, i: int):
    return row[i] if i < len(row) else None


# ----------------------------------------------------------------- pólizas
@dataclass
class JournalLine:
    line_no: int
    account: str
    account_name: str
    reference: str
    segment: Optional[int]          # business-case number, None when blank
    debit: Decimal
    credit: Decimal

    @property
    def amount(self) -> Decimal:     # signed: debit positive, credit negative
        return self.debit - self.credit


@dataclass
class Journal:
    date: dt.date
    kind: str                       # Ingresos / Egresos / Diario
    number: int
    concept: str
    lines: List[JournalLine] = field(default_factory=list)
    control: str = ""               # "Cifra de Control"

    @property
    def key(self) -> str:
        """Natural key inside one entity's books: the print restarts the
        number every month per type, so date + type + number is unique."""
        return f"{self.date.isoformat()}:{self.kind}:{self.number}"

    @property
    def debit(self) -> Decimal:
        return sum((l.debit for l in self.lines), D(0))

    @property
    def credit(self) -> Decimal:
        return sum((l.credit for l in self.lines), D(0))

    @property
    def balanced(self) -> bool:
        return self.debit == self.credit


@dataclass
class PolizasPrint:
    entity: str
    rfc: str
    period_from: Optional[dt.date]
    period_to: Optional[dt.date]
    printed: Optional[dt.date]
    journals: List[Journal]

    @property
    def lines(self) -> int:
        return sum(len(j.lines) for j in self.journals)


_PRINT_HDR = re.compile(r"del (\d{2}/\w{3}/\d{4}) al (\d{2}/\w{3}/\d{4})")


def _header(rows: Iterable[Sequence]) -> Tuple[str, str, Optional[dt.date], Optional[dt.date], Optional[dt.date]]:
    entity = rfc = ""
    p_from = p_to = printed = None
    for i, r in enumerate(rows):
        if i > 12:
            break
        text = " ".join(_s(c) for c in r if c is not None)
        if i == 0:
            cells = [_s(c) for c in r if _s(c)]
            if len(cells) >= 2:
                entity = cells[1]
        m = _PRINT_HDR.search(text)
        if m:
            p_from, p_to = parse_date(m.group(1)), parse_date(m.group(2))
        m = re.search(r"Fecha:\s*(\d{2}/\w{3}/\d{4})", text)
        if m:
            printed = parse_date(m.group(1))
        m = re.search(r"Reg\. Fed\.:\s*([A-Z&Ñ]{3,4}\d{6}[A-Z0-9]{3})", text)
        if m:
            rfc = m.group(1)
    return entity, rfc, p_from, p_to, printed


def parse_polizas(rows: Sequence[Sequence]) -> PolizasPrint:
    """The whole *Diarios y Pólizas* print → journals with their lines.

    Row shapes (CONTPAQi is stable about them):
      header : date | kind | number | concept | class | journal
      line   : line_no | reference | account | name | journal | segment | debit | credit
      control: '' | 'Cifra de Control' | n | … | 'Total póliza :' | debit | credit
    Everything else (day totals, page breaks, blanks) is skipped.
    """
    entity, rfc, p_from, p_to, printed = _header(rows)
    journals: List[Journal] = []
    cur: Optional[Journal] = None
    for r in rows:
        c0 = _cell(r, 0)
        d = parse_date(c0)
        if d and _s(_cell(r, 1)) and _s(_cell(r, 2)):
            try:
                num = int(float(_cell(r, 2)))
            except (TypeError, ValueError):
                continue
            cur = Journal(date=d, kind=_s(_cell(r, 1)), number=num, concept=_s(_cell(r, 3)))
            journals.append(cur)
            continue
        acct = _s(_cell(r, 2))
        if cur is not None and ACCOUNT_RE.match(acct) and isinstance(c0, (int, float)):
            seg = _cell(r, 5)
            try:
                seg_i = int(float(seg)) if seg not in (None, "") else None
            except (TypeError, ValueError):
                seg_i = None
            cur.lines.append(JournalLine(line_no=int(c0), account=acct, account_name=_s(_cell(r, 3)),
                                         reference=_s(_cell(r, 1)), segment=seg_i,
                                         debit=_amt(_cell(r, 6)), credit=_amt(_cell(r, 7))))
            continue
        if cur is not None and _s(_cell(r, 1)) == "Cifra de Control":
            cur.control = _s(_cell(r, 2))
    return PolizasPrint(entity, rfc, p_from, p_to, printed, journals)


# --------------------------------------------------------------- auxiliares
@dataclass
class Movement:
    date: dt.date
    kind: str
    number: int
    concept: str
    reference: str
    debit: Decimal
    credit: Decimal
    balance: Decimal

    @property
    def amount(self) -> Decimal:
        return self.debit - self.credit


@dataclass
class AccountLedger:
    account: str
    name: str
    opening: Decimal
    movements: List[Movement] = field(default_factory=list)

    @property
    def debits(self) -> Decimal:
        return sum((m.debit for m in self.movements), D(0))

    @property
    def credits(self) -> Decimal:
        return sum((m.credit for m in self.movements), D(0))

    @property
    def closing_shown(self) -> Decimal:
        """Closing balance the way the print shows it: opening ± movements
        in the account's nature (a supplier in credit reads positive)."""
        sign = 1 if self.nature == "debit" else -1
        return self.opening + sign * (self.debits - self.credits)

    @property
    def closing(self) -> Decimal:
        """Closing balance normalised debit-positive (a liability in credit
        is negative), so sums across accounts of both natures add up."""
        return self.closing_shown if self.nature == "debit" else -self.closing_shown

    @property
    def nature(self) -> str:
        """'debit' (assets, expenses: balance = opening + Dr - Cr) or
        'credit' (liabilities, equity, revenue: the print shows the balance
        as opening + Cr - Dr). Decided from the first movement whose two
        readings differ; an account with no movements is 'debit'."""
        bal_d = bal_c = self.opening
        for m in self.movements:
            bal_d = bal_d + m.debit - m.credit
            bal_c = bal_c - m.debit + m.credit
            if abs(bal_d - m.balance) <= Decimal("0.01") and abs(bal_c - m.balance) > Decimal("0.01"):
                return "debit"
            if abs(bal_c - m.balance) <= Decimal("0.01") and abs(bal_d - m.balance) > Decimal("0.01"):
                return "credit"
        return class_nature(self.account)

    @property
    def running_ok(self) -> bool:
        """The print's own running balance agrees with opening ± movements
        in the account's nature (a mis-parsed row shows up here before it
        reaches the database)."""
        sign = 1 if self.nature == "debit" else -1
        bal = self.opening
        for m in self.movements:
            bal = bal + sign * (m.debit - m.credit)
            if abs(bal - m.balance) > Decimal("0.01"):
                return False
        return True


    def closing_at(self, day: dt.date) -> Decimal:
        """Balance as shown by the print at the end of ``day``."""
        sign = 1 if self.nature == "debit" else -1
        bal = self.opening
        for m in self.movements:
            if m.date <= day:
                bal = bal + sign * (m.debit - m.credit)
        return bal


@dataclass
class AuxiliaresPrint:
    entity: str
    period_from: Optional[dt.date]
    period_to: Optional[dt.date]
    printed: Optional[dt.date]
    accounts: Dict[str, AccountLedger]

    def by_prefix(self, prefix: str) -> List[AccountLedger]:
        return [a for k, a in sorted(self.accounts.items()) if k.startswith(prefix)]


def parse_auxiliares(rows: Sequence[Sequence]) -> AuxiliaresPrint:
    """The *Movimientos auxiliares* print → one ledger per DETAIL account.

    Row shapes:
      account: '102-01-001' | name | … | 'Saldo inicial :' | opening
      movement: date | kind | number | concept | reference | debit | credit | balance
      totals : 'Total …' rows and blanks — skipped.
    Summary accounts (102-00-000) also carry an opening row; they are kept
    (with no movements) so callers can read group openings.
    """
    entity, _rfc, p_from, p_to, printed = _header(rows)
    accounts: Dict[str, AccountLedger] = {}
    cur: Optional[AccountLedger] = None
    for r in rows:
        c0 = _s(_cell(r, 0))
        if ACCOUNT_RE.match(c0):
            opening = D(0)
            for i, c in enumerate(r):
                if _s(c).startswith("Saldo inicial"):
                    opening = _amt(_cell(r, i + 1))
                    break
            cur = AccountLedger(account=c0, name=_s(_cell(r, 1)), opening=opening)
            accounts[c0] = cur
            continue
        d = parse_date(c0)
        if cur is not None and d:
            try:
                num = int(float(_cell(r, 2) or 0))
            except (TypeError, ValueError):
                num = 0
            cur.movements.append(Movement(date=d, kind=_s(_cell(r, 1)), number=num, concept=_s(_cell(r, 3)),
                                          reference=_s(_cell(r, 4)), debit=_amt(_cell(r, 5)), credit=_amt(_cell(r, 6)),
                                          balance=_amt(_cell(r, 7))))
    return AuxiliaresPrint(entity, p_from, p_to, printed, accounts)


# ------------------------------------------------------------------ balanza
@dataclass
class BalanceRow:
    account: str            # CONTPAQi 8-digit code ('10201001') or dashed
    name: str
    open_debit: Decimal
    open_credit: Decimal
    debits: Decimal
    credits: Decimal
    close_debit: Decimal
    close_credit: Decimal

    @property
    def opening(self) -> Decimal:
        return self.open_debit - self.open_credit

    @property
    def closing(self) -> Decimal:
        return self.close_debit - self.close_credit

    @property
    def level(self) -> int:
        """0 = class (10000000), 1 = group (10001000), 2 = account (10200000),
        3 = sub (10201000), 4 = detail (10201001)."""
        a = self.account.replace("-", "")
        if len(a) != 8:
            return 4
        if a[1:] == "0000000":
            return 0
        if a[4:] == "0000":
            return 1 if a[2:4] == "00" else 2
        if a[5:] == "000":
            return 3
        return 4

    @property
    def dashed(self) -> str:
        """'10201001' -> '102-01-001' (the form the pólizas use)."""
        a = self.account.replace("-", "")
        return f"{a[:3]}-{a[3:5]}-{a[5:8]}" if len(a) == 8 and a.isdigit() else self.account


def parse_balanza(rows: Sequence[Sequence]) -> List[BalanceRow]:
    out: List[BalanceRow] = []
    for r in rows:
        c0 = _s(_cell(r, 0))
        if not (re.match(r"^\d{8}$", c0) or ACCOUNT_RE.match(c0)):
            continue
        out.append(BalanceRow(account=c0, name=_s(_cell(r, 1)),
                              open_debit=_amt(_cell(r, 2)), open_credit=_amt(_cell(r, 3)),
                              debits=_amt(_cell(r, 4)), credits=_amt(_cell(r, 5)),
                              close_debit=_amt(_cell(r, 6)), close_credit=_amt(_cell(r, 7))))
    return out


# -------------------------------------------------------------- consistency
def cross_check(pol: PolizasPrint, aux: AuxiliaresPrint) -> List[str]:
    """Pólizas and Auxiliares are two prints of the same books: per detail
    account, Σdebits and Σcredits must agree. Returns the findings (empty =
    consistent) — a mismatch means a truncated export, not a parser bug."""
    sums: Dict[str, List[Decimal]] = {}
    for j in pol.journals:
        for l in j.lines:
            s = sums.setdefault(l.account, [D(0), D(0)])
            s[0] += l.debit
            s[1] += l.credit
    findings = []
    # the pólizas print can carry entries the auxiliares (and the balanza) do not — an
    # unposted or later-deleted póliza; balances come from the auxiliares, so this is a
    # finding to read, never a reason to stop the import
    for acct, (dr, cr) in sorted(sums.items()):
        led = aux.accounts.get(acct)
        if led is None:
            findings.append(f"{acct}: in pólizas, not in auxiliares")
            continue
        if abs(led.debits - dr) > Decimal("0.01") or abs(led.credits - cr) > Decimal("0.01"):
            findings.append(f"{acct}: pólizas {dr}/{cr} vs auxiliares {led.debits}/{led.credits}")
    unbalanced = [j.key for j in pol.journals if not j.balanced]
    if unbalanced:
        findings.append(f"{len(unbalanced)} unbalanced póliza(s): {', '.join(unbalanced[:5])}")
    return findings
