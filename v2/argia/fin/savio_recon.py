"""v247 — the Savio plugin: reconcile what Savio says (customer invoices,
customer payments, the bank accounts they were paid to) with what ARGIA
keeps (the AR tracker, the books' bank movements, the registered bank
accounts). Pure: inputs are plain dicts, output is a Recon with findings;
``scripts/fin_savio_recon.py`` fetches and stores.

Three checks, in the order an accountant would do them:

1. INVOICES  — every Savio invoice should be one row of the AR tracker
   (matched by CFDI UUID, else by folio + total); amount, currency and
   paid/open state must agree. Savio-only invoices are collections nobody
   is following; tracker-only rows are invoices issued outside Savio.
2. PAYMENTS  — every Savio payment ('applied') should be a deposit in the
   books: a debit on a bank account, same amount, within ±3 days. A
   payment with no deposit is money Savio believes arrived and the books
   do not show (or a booking still pending); a deposit with no payment is
   income Savio does not know about (rent, PPA billed elsewhere) — listed
   for review, not an error.
3. BANK ACCOUNTS — the account a payment landed in (when Savio carries
   it: CLABE / last digits) must be one of ARGIA's registered accounts;
   an unknown account is the classic diverted-payment fraud signal.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

from .money import D

TOL = Decimal("0.01")
DAYS = 3

# bank debits that are not customer money: transfers between ARGIA's own
# accounts (the books spell it both ways), refunds, interest, bank fees
# reversed, loans drawn, FX. They are never Savio payments, so they are
# kept out of the deposit pool instead of flooding the review list.
_INTERNAL = re.compile(r"TRA[S]?PASO|ENTRE CUENTAS|DEVOLUCION|INTERES|COMISION|PRESTAMO|CREDITO|RETORNO DEL CARGO|COMPRA DE D[OÓ]LARES|VENTA DE D[OÓ]LARES|COMPRA DE DIVISAS|VENTA DE DIVISAS", re.I)


def is_internal(concept: str) -> bool:
    """True for a deposit whose concept says it is not a customer payment."""
    return bool(_INTERNAL.search(str(concept or "")))


def _d(v) -> Optional[dt.date]:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    s = str(v or "")[:10]
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def _n(v) -> Decimal:
    try:
        return D(v if v not in (None, "") else 0)
    except Exception:            # noqa: BLE001
        return D(0)


def _uuid(inv: dict) -> str:
    for c in inv.get("cfdis") or []:
        if (c.get("type") or "I") == "I" and c.get("uuid"):
            return str(c["uuid"]).upper()
    return ""


def _folio(inv: dict) -> str:
    return str(inv.get("folio") or "").strip()


@dataclass
class Finding:
    kind: str            # SAVIO_ONLY | TRACKER_ONLY | AMOUNT | CURRENCY | STATE | NO_DEPOSIT | UNKNOWN_ACCOUNT | DEPOSIT_UNMATCHED
    savio_ref: str
    our_ref: str
    amount: Decimal
    currency: str
    detail: str
    severity: str = "warn"     # info | warn | crit


@dataclass
class Recon:
    checked_at: dt.datetime
    source: str                              # 'mock' | 'sandbox' | 'live'
    invoices: int = 0
    invoices_matched: int = 0
    payments: int = 0
    payments_matched: int = 0
    deposits: int = 0
    deposits_matched: int = 0
    deposits_internal: int = 0               # transfers/refunds/interest/loans, not customer money
    accounts_checked: int = 0
    findings: List[Finding] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for f in self.findings if f.kind == kind)

    @property
    def ok(self) -> bool:
        return not any(f.severity == "crit" for f in self.findings)


# --------------------------------------------------------------- invoices
def match_invoices(savio: Sequence[dict], tracker: Sequence[dict], rec: Recon) -> Dict[str, dict]:
    """Savio invoice → tracker row. ``tracker`` rows: {invoice, folio_fiscal,
    total, currency, paid_on, status, company}. Returns savio_id → tracker row."""
    by_uuid = {str(t.get("folio_fiscal") or "").upper(): t for t in tracker if t.get("folio_fiscal")}
    by_folio = {}
    for t in tracker:
        by_folio.setdefault(str(t.get("invoice") or "").strip(), []).append(t)
    out: Dict[str, dict] = {}
    used = set()
    rec.invoices = len(savio)
    for inv in savio:
        sid = inv.get("invoice_id") or ""
        cancelled = any((c.get("status") or "") == "cancelled" for c in inv.get("cfdis") or [])
        total, ccy = _n(inv.get("total")), (inv.get("currency") or "MXN").upper()
        t = by_uuid.get(_uuid(inv))
        how = "uuid"
        if t is None:
            cands = [x for x in by_folio.get(_folio(inv), []) if abs(_n(x.get("total")) - total) <= TOL and id(x) not in used]
            t = cands[0] if cands else None
            how = "folio+total"
        if t is None:
            if cancelled:
                continue                      # a cancelled invoice that never reached the tracker is fine
            rec.findings.append(Finding("SAVIO_ONLY", sid, "", total, ccy, f"Savio invoice {inv.get('series', '')}{_folio(inv)} ({inv.get('status')}) has no row in the AR tracker", "warn"))
            continue
        used.add(id(t))
        out[sid] = t
        rec.invoices_matched += 1
        t_total, t_ccy = _n(t.get("total")), (t.get("currency") or "MXN").upper()
        if t_ccy != ccy:
            rec.findings.append(Finding("CURRENCY", sid, t.get("invoice", ""), total, ccy, f"Savio {ccy} vs tracker {t_ccy}", "crit"))
        elif abs(t_total - total) > TOL:
            rec.findings.append(Finding("AMOUNT", sid, t.get("invoice", ""), total - t_total, ccy, f"Savio total {total:,.2f} vs tracker {t_total:,.2f} (matched by {how})", "crit"))
        s_paid = (inv.get("status") or "").lower() == "paid"
        t_paid = bool(t.get("paid_on")) or (t.get("status") or "").lower() == "paid"
        if cancelled and not (t.get("status") or "").lower().startswith("cancel"):
            rec.findings.append(Finding("STATE", sid, t.get("invoice", ""), total, ccy, "CFDI cancelled in Savio, still open in the tracker", "crit"))
        elif s_paid != t_paid:
            rec.findings.append(Finding("STATE", sid, t.get("invoice", ""), total, ccy,
                                        "paid in Savio, still open in the tracker" if s_paid else "paid in the tracker, still open in Savio", "warn"))
    matched_uuids = {_uuid(inv) for inv in savio if inv.get("invoice_id") in out}
    matched_ids = {id(t) for t in out.values()}
    for t in tracker:
        if id(t) in matched_ids or bool(t.get("paid_on")):
            continue
        rec.findings.append(Finding("TRACKER_ONLY", "", t.get("invoice", ""), _n(t.get("total")), (t.get("currency") or "MXN").upper(),
                                    f"open AR row for {t.get('company', '')} has no Savio invoice (issued outside Savio?)", "info"))
    return out


# --------------------------------------------------------------- payments
def match_payments(payments: Sequence[dict], deposits: Sequence[dict], rec: Recon, days: int = DAYS) -> Dict[str, dict]:
    """Savio payment → booked deposit. ``deposits``: {date, amount, account, reference, concept, currency}
    (bank debits from the books; USD accounts in USD). Same amount, same
    currency, within ±days; the reference wins ties."""
    pool = [dict(x, _used=False) for x in deposits if not is_internal(x.get("concept", ""))]
    rec.payments = sum(1 for p in payments if (p.get("status") or "applied") == "applied")
    rec.deposits = len(pool)
    rec.deposits_internal = len(deposits) - len(pool)
    out: Dict[str, dict] = {}
    for p in payments:
        if (p.get("status") or "applied") != "applied":
            continue
        amt, ccy, pd = _n(p.get("amount")), (p.get("currency") or "MXN").upper(), _d(p.get("date"))
        ref = str(p.get("reference") or "").strip().lower()
        cands = [x for x in pool if not x["_used"] and abs(_n(x.get("amount")) - amt) <= TOL and (x.get("currency") or "MXN").upper() == ccy
                 and pd and _d(x.get("date")) and abs((_d(x.get("date")) - pd).days) <= days]
        if ref:
            exact = [x for x in cands if ref and ref in (str(x.get("reference") or "") + " " + str(x.get("concept") or "")).lower()]
            cands = exact or cands
        if not cands:
            rec.findings.append(Finding("NO_DEPOSIT", p.get("payment_id", ""), "", amt, ccy,
                                        f"Savio payment of {amt:,.2f} {ccy} on {pd} ({p.get('method', '')} {p.get('reference', '')}) has no deposit in the books within ±{days} days", "warn"))
            continue
        cands.sort(key=lambda x: abs((_d(x.get("date")) - pd).days))
        cands[0]["_used"] = True
        out[p.get("payment_id", "")] = cands[0]
        rec.payments_matched += 1
    rec.deposits_matched = sum(1 for x in pool if x["_used"])
    for x in pool:
        if not x["_used"] and _n(x.get("amount")) > 0:
            rec.findings.append(Finding("DEPOSIT_UNMATCHED", "", f"{x.get('account', '')} {x.get('date', '')}", _n(x.get("amount")), (x.get("currency") or "MXN").upper(),
                                        f"deposit {x.get('concept', '') or x.get('reference', '')} has no Savio payment (income outside Savio, or a payment not yet registered)", "info"))
    return out


# ------------------------------------------------------------ bank accounts
def check_accounts(payments: Sequence[dict], registered: Iterable[str], rec: Recon) -> None:
    """A payment that names the receiving account (CLABE or its last
    digits) must land on one of ARGIA's registered accounts."""
    reg = [re.sub(r"\D", "", str(a)) for a in registered if a]
    for p in payments:
        acct = str(p.get("bank_account") or p.get("clabe") or p.get("destination_account") or "").strip()
        if not acct:
            continue
        rec.accounts_checked += 1
        digits = re.sub(r"\D", "", acct)
        ok = any(r.endswith(digits) or digits.endswith(r) for r in reg if len(r) >= 4 and len(digits) >= 4)
        if not ok:
            rec.findings.append(Finding("UNKNOWN_ACCOUNT", p.get("payment_id", ""), acct, _n(p.get("amount")), (p.get("currency") or "MXN").upper(),
                                        "payment reported on a bank account that is not registered for ARGIA — verify before trusting the receipt", "crit"))


def reconcile(savio_invoices: Sequence[dict], savio_payments: Sequence[dict], tracker: Sequence[dict], deposits: Sequence[dict],
              registered_accounts: Iterable[str] = (), source: str = "mock", now: Optional[dt.datetime] = None) -> Recon:
    rec = Recon(checked_at=now or dt.datetime.utcnow(), source=source)
    match_invoices(savio_invoices, tracker, rec)
    match_payments(savio_payments, deposits, rec)
    check_accounts(savio_payments, registered_accounts, rec)
    order = {"crit": 0, "warn": 1, "info": 2}
    rec.findings.sort(key=lambda f: (order[f.severity], f.kind, f.savio_ref, f.our_ref))
    return rec


def rows_for(rec: Recon, entity_id: str) -> List[dict]:
    """→ savio_check rows (replaced whole on every run)."""
    out = [{"entity_id": entity_id, "checked_at": rec.checked_at.isoformat(), "source": rec.source, "kind": "SUMMARY", "severity": "info",
            "savio_ref": "", "our_ref": "", "amount": D(0), "currency": "", "detail": f"invoices {rec.invoices_matched}/{rec.invoices} matched · payments {rec.payments_matched}/{rec.payments} "
                                                                                     f"with a deposit · deposits {rec.deposits_matched}/{rec.deposits} explained ({rec.deposits_internal} internal) · accounts checked {rec.accounts_checked}"}]
    for i, f in enumerate(rec.findings, 1):
        out.append({"entity_id": entity_id, "checked_at": rec.checked_at.isoformat(), "source": rec.source, "kind": f.kind, "severity": f.severity,
                    "savio_ref": f.savio_ref, "our_ref": f.our_ref, "amount": f.amount, "currency": f.currency, "detail": f.detail})
    return out
