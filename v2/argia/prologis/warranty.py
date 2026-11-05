"""Warranty claims of the LED programme for Prologis (v354).

The data is a server-only file, /opt/argia/prologis/lighting/warranty.json, built on ARGIA's laptop from
ARGIA's two warranty trackers (build_warranty_json.py). It holds business data and never enters this public
repository; the tests use invented claims. Pure functions only, no Flask.

A claim: one report of failed fixtures at one building (a W- ticket in ARGIA's sheet since Aug 2025, a row
of the 2022-2025 overview before that).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

STATUSES = {"open": ("Open", "Abierto", "#e0a100"), "closed": ("Closed", "Cerrado", "#23B2A9")}
CODE_RE = re.compile(r"^[A-Z]{3}\d{5}$")
REF_RE = re.compile(r"^[A-Za-z0-9_.:# -]{1,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Claim:
    ref: str
    ticket: Optional[str]
    code: Optional[str]
    market: Optional[str]
    park: Optional[str]
    group: Optional[str]
    customer: Optional[str]
    qty: Optional[int]
    models: Tuple[str, ...]
    cause: Optional[str]
    reported: Optional[str]
    closed: Optional[str]
    status: str
    days: Optional[int]
    source: str

    def days_open(self, today: dt.date) -> Optional[int]:
        """Days to close; for an open claim, days since it was reported."""
        if self.days is not None:
            return self.days
        if self.status == "open" and self.reported:
            return (today - dt.date.fromisoformat(self.reported)).days
        return None


@dataclass(frozen=True)
class Register:
    generated: str
    years: int
    claims: List[Claim]
    excluded: Dict[str, int]


def _date(s) -> Optional[str]:
    if s is None:
        return None
    if not isinstance(s, str) or not DATE_RE.match(s):
        raise ValueError(f"warranty.json: bad date {s!r}")
    dt.date.fromisoformat(s)
    return s


def parse(d: dict) -> Register:
    """Fail closed: a file that does not look right is refused as a whole."""
    if not isinstance(d, dict) or d.get("meta", {}).get("schema") != 1 or not isinstance(d.get("claims"), list):
        raise ValueError("warranty.json: unknown schema")
    out, seen = [], set()
    for r in d["claims"]:
        ref = str(r.get("ref") or "")
        if not REF_RE.match(ref):
            raise ValueError(f"warranty.json: bad reference {ref!r}")
        code = r.get("code")
        if code is not None and not CODE_RE.match(str(code)):
            raise ValueError(f"warranty.json: {ref} has a bad building code")
        st = r.get("status")
        if st not in STATUSES:
            raise ValueError(f"warranty.json: {ref} has unknown status {st!r}")
        qty = r.get("qty")
        if qty is not None and (not isinstance(qty, int) or not 0 < qty < 10000):
            raise ValueError(f"warranty.json: {ref} has a bad quantity")
        rep, clo = _date(r.get("reported")), _date(r.get("closed"))
        if rep and clo and clo < rep:
            raise ValueError(f"warranty.json: {ref} closed before it was reported")
        days = r.get("days")
        if days is not None and (not isinstance(days, int) or days < 0):
            raise ValueError(f"warranty.json: {ref} has bad days")
        key = (ref, rep, code)
        if key in seen:
            raise ValueError(f"warranty.json: duplicate claim {key}")
        seen.add(key)
        cust = r.get("customer")
        out.append(Claim(ref, r.get("ticket"), code, r.get("market"), r.get("park"), r.get("group"),
                         (cust[:1].upper() + cust[1:]) if cust else cust, qty,
                         tuple(str(m) for m in (r.get("models") or [])), r.get("cause"), rep, clo, st, days,
                         str(r.get("source") or "")))
    meta = d["meta"]
    return Register(str(meta.get("generated", "")), int(meta.get("warranty_years") or 5), out,
                    {str(k): int(v) for k, v in (meta.get("excluded") or {}).items()})


def load(path: str) -> Register:
    with open(path, encoding="utf-8") as fh:
        return parse(json.load(fh))


@dataclass(frozen=True)
class Summary:
    claims: int
    fixtures: int
    open: int
    buildings: int
    median_days: Optional[float]
    within_30: Optional[float]                 # share of closed claims closed within 30 days


def summary(claims: Iterable[Claim]) -> Summary:
    cl = list(claims)
    days = [c.days for c in cl if c.status == "closed" and c.days is not None]
    return Summary(len(cl), sum(c.qty or 0 for c in cl), sum(1 for c in cl if c.status == "open"),
                   len({c.code for c in cl if c.code}), statistics.median(days) if days else None,
                   (sum(1 for x in days if x <= 30) / len(days)) if days else None)


def per_year(claims: Iterable[Claim]) -> List[Tuple[int, int, int]]:
    """(year, claims, fixtures) by the year a claim was reported."""
    agg: Dict[int, List[int]] = {}
    for c in claims:
        if c.reported:
            a = agg.setdefault(int(c.reported[:4]), [0, 0])
            a[0] += 1
            a[1] += c.qty or 0
    return [(y, a[0], a[1]) for y, a in sorted(agg.items())]


def for_building(claims: Iterable[Claim], code: Optional[str]) -> List[Claim]:
    return [c for c in claims if code and c.code == code]


def warranty_until(handover: Optional[str], years: int = 5) -> Optional[str]:
    """The end of the warranty: `years` after the handover (29 Feb rolls to 28 Feb)."""
    if not handover or not DATE_RE.match(handover):
        return None
    d = dt.date.fromisoformat(handover)
    try:
        return d.replace(year=d.year + years).isoformat()
    except ValueError:
        return d.replace(year=d.year + years, day=28).isoformat()


def filtered(claims: Iterable[Claim], status: str = "", market: str = "", q: str = "") -> List[Claim]:
    qn = q.strip().lower()
    out = []
    for c in claims:
        if status and c.status != status:
            continue
        if market and c.market != market:
            continue
        if qn and qn not in " ".join(str(x or "") for x in (c.ref, c.code, c.park, c.customer, " ".join(c.models), c.cause)).lower():
            continue
        out.append(c)
    return out
