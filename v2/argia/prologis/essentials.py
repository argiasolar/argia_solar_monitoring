"""Prologis Essentials offer simulator (v359) - the numbers of ARGIA's proforma, recalculated with other conditions.

The proforma (sheets "PROLOGIS Essentials" and "SAVING CALCULATOR") prices a LED retrofit that Prologis pays up
front and recovers through a monthly Essentials fee per m2 from the customer:

  investment  = direct costs + corporate overhead + ARGIA profit + Prologis fee            (paid by Prologis at start)
  monthly fee = sum over area types of m2 x price per m2                                    (paid by the customer)
  customer    = lighting energy cost before - after - monthly fee                           (what the customer keeps)

The simulator lets a Prologis manager change the price per m2, the contract length, the Prologis fee, a customer
contribution, the electricity price and ARGIA's profit, and shows Prologis's IRR and payback and the customer's net
saving, plus the price range between "Prologis reaches its target IRR" and "the customer still saves".

Differences from the workbook, on purpose:
  * IRR from the monthly cash flows, annualised. The workbook's IRR cell uses yearly sums AND per-ft2 USD prices
    (0.02 / 0.01 / 0.02 USD) that are not the MXN prices it charges, so its IRR does not match its own fee.
  * Everything in MXN.

Data: /opt/argia/prologis/lighting/offers.json (built on ARGIA's laptop by build_offers_json.py). Business data,
never in this public repository; the tests use invented offers. Pure functions only, no Flask.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

AREAS = ("production", "warehouse", "office")
AREA_NAMES = {"production": ("Production", "Producción"), "warehouse": ("Warehouse", "Almacén"), "office": ("Office", "Oficinas")}
ID_RE = re.compile(r"^[OP]\d{3}-\d{1,2}$")
KEY_RE = re.compile(r"^[OP]\d{3}$")
CODE_RE = re.compile(r"^[A-Z]{3}\d{5}$")

# what a scenario may be set to (anything else is clamped); the standard values come from the proforma
LIMITS = {"rate": (0.0, 30.0), "months": (12, 120), "prologis_fee": (0.0, 0.10), "profit": (0.0, 0.15),
          "kwh_price": (0.5, 10.0), "kwh_before": (0.0, 1e8), "target_irr": (0.0, 0.60), "contribution": (0.0, 1e9)}
DEFAULT_TARGET_IRR = 0.12
CUSTOMER_SHARE = 0.30                     # suggestion: the customer keeps 30 % of the energy saving


class Invalid(ValueError):
    pass


@dataclass(frozen=True)
class Offer:
    id: str
    key: str                              # O### opportunity / P### project of ARGIA's register
    file: str
    date: Optional[str]
    code: Optional[str]                   # Prologis building code
    park: str
    building: str
    tenant: str
    areas: Dict[str, float]               # m2 per area type
    rates: Dict[str, float]               # MXN per m2 per month
    lines: Tuple[Tuple[str, float], ...]  # direct cost lines, MXN
    oh: float
    profit: float
    prologis_fee: float
    fx: Optional[float]
    months: int
    kwh_price: float
    hours_before: Optional[float]
    kw_before: Optional[float]
    hours_after: Optional[float]
    kw_after: Optional[float]
    total_mxn: float

    @property
    def direct(self) -> float:
        return sum(v for _, v in self.lines)

    @property
    def kwh_before(self) -> Optional[float]:
        return self.hours_before * self.kw_before if self.hours_before and self.kw_before else None

    @property
    def kwh_after(self) -> Optional[float]:
        return self.hours_after * self.kw_after if self.hours_after and self.kw_after else None


def _f(x, what) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        raise Invalid(f"offers.json: {what} is not a number")
    return float(x)


def parse(d: dict) -> List[Offer]:
    """Fail closed: a file that does not look right is refused as a whole."""
    if not isinstance(d, dict) or d.get("schema") != 1 or not isinstance(d.get("offers"), list):
        raise Invalid("offers.json: unknown schema")
    out, seen = [], set()
    for o in d["offers"]:
        oid = str(o.get("id") or "")
        if not ID_RE.match(oid) or oid in seen:
            raise Invalid(f"offers.json: bad or duplicate id {oid!r}")
        seen.add(oid)
        if not KEY_RE.match(str(o.get("key") or "")) or not oid.startswith(o["key"] + "-"):
            raise Invalid(f"offers.json: {oid} has a bad key")
        code = o.get("code")
        if code is not None and not CODE_RE.match(str(code)):
            raise Invalid(f"offers.json: {oid} has a bad building code")
        ar = o.get("areas") or {}
        if set(ar) != set(AREAS):
            raise Invalid(f"offers.json: {oid} areas must be {AREAS}")
        areas = {a: _f(ar[a].get("m2"), f"{oid} {a} m2") or 0.0 for a in AREAS}
        rates = {a: _f(ar[a].get("rate"), f"{oid} {a} rate") or 0.0 for a in AREAS}
        if any(v < 0 for v in areas.values()) or any(v < 0 for v in rates.values()) or sum(areas.values()) <= 0:
            raise Invalid(f"offers.json: {oid} has no area")
        lines = tuple((str(x.get("label") or "")[:60], _f(x.get("mxn"), f"{oid} cost") or 0.0) for x in (o.get("lines") or []))
        if not lines or any(v < 0 for _, v in lines):
            raise Invalid(f"offers.json: {oid} has no cost lines")
        oh, pr, pf = (_f(o.get(k), f"{oid} {k}") for k in ("oh", "profit", "prologis_fee"))
        if oh is None or pr is None or pf is None or not (0 <= oh < 1 and 0 <= pr < 1 and 0 <= pf < 1):
            raise Invalid(f"offers.json: {oid} has bad percentages")
        months = o.get("months")
        if not isinstance(months, int) or not 1 <= months <= 240:
            raise Invalid(f"offers.json: {oid} has a bad contract length")
        price = _f(o.get("kwh_price"), f"{oid} kWh price")
        if not price or price <= 0:
            raise Invalid(f"offers.json: {oid} has no electricity price")
        en = o.get("energy") or {}
        b, a = en.get("before") or {}, en.get("after") or {}
        total = _f(o.get("total_mxn"), f"{oid} total") or 0.0
        off = Offer(oid, o["key"], str(o.get("file") or "")[:120], o.get("date"), code, str(o.get("park") or ""),
                    str(o.get("building") or ""), str(o.get("tenant") or ""), areas, rates, lines, oh, pr, pf,
                    _f(o.get("fx"), f"{oid} fx"), months, price, _f(b.get("hours"), oid), _f(b.get("kw"), oid),
                    _f(a.get("hours"), oid), _f(a.get("kw"), oid), total)
        if total <= 0 or abs(investment(off.direct, oh, pr, pf) - total) > 0.005 * total:
            raise Invalid(f"offers.json: {oid} cost lines do not add up to its total")
        out.append(off)
    return out


def load(path: str) -> List[Offer]:
    with open(path, encoding="utf-8") as fh:
        return parse(json.load(fh))


# ----------------------------------------------------------------------------------------------------------- money

def investment(direct: float, oh: float, profit: float, prologis_fee: float) -> float:
    """The work order total, built up as in the proforma: OH on direct, profit on direct+OH, fee on all three."""
    return direct * (1 + oh) * (1 + profit) * (1 + prologis_fee)


def irr_monthly(outlay: float, payment: float, months: int) -> Optional[float]:
    """Monthly rate r with outlay = payment x (1 - (1+r)^-months) / r; None when the payments never repay it."""
    if outlay <= 0 or payment <= 0 or months <= 0:
        return None
    if payment * months <= outlay:
        return None if payment * months < outlay else 0.0

    def pv(r):
        return payment * months if r == 0 else payment * (1 - (1 + r) ** -months) / r
    lo, hi = 0.0, 1.0
    while pv(hi) > outlay:
        hi *= 2
        if hi > 1e6:
            return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if pv(mid) > outlay:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def annual(r_month: Optional[float]) -> Optional[float]:
    return None if r_month is None else (1 + r_month) ** 12 - 1


def payment_for_irr(outlay: float, irr_year: float, months: int) -> float:
    """The monthly payment that gives `irr_year` (annualised) over `months` on `outlay`."""
    if outlay <= 0 or months <= 0:
        return 0.0
    r = (1 + irr_year) ** (1 / 12) - 1
    return outlay / months if r == 0 else outlay * r / (1 - (1 + r) ** -months)


@dataclass(frozen=True)
class Scenario:
    rates: Dict[str, float]
    months: int
    prologis_fee: float
    profit: float
    kwh_price: float
    kwh_before: Optional[float]           # annual kWh of the current lighting (None = the proforma's)
    contribution: float                   # customer pays this part of the investment at the start, MXN
    target_irr: float = DEFAULT_TARGET_IRR


def standard(o: Offer) -> Scenario:
    return Scenario(dict(o.rates), o.months, o.prologis_fee, o.profit, o.kwh_price, None, 0.0)


def _clamp(v, k):
    lo, hi = LIMITS[k]
    return max(lo, min(hi, v))


def from_args(o: Offer, args) -> Scenario:
    """A scenario from URL parameters (missing or unreadable ones keep the proforma's value; all are clamped).

    r_production / r_warehouse / r_office  MXN per m2 per month    m  months       pf  Prologis fee %
    pr  ARGIA profit %    c  customer contribution MXN    kp  MXN per kWh    kb  kWh/yr now    t  target IRR %"""
    def num(name):
        try:
            v = float(str(args.get(name, "")).replace(",", ""))
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None
    s = standard(o)
    rates = {a: _clamp(num(f"r_{a}") if num(f"r_{a}") is not None else s.rates[a], "rate") for a in AREAS}
    months = int(_clamp(round(num("m")) if num("m") is not None else s.months, "months"))
    pf = _clamp(num("pf") / 100 if num("pf") is not None else s.prologis_fee, "prologis_fee")
    pr = _clamp(num("pr") / 100 if num("pr") is not None else s.profit, "profit")
    kp = _clamp(num("kp") if num("kp") is not None else s.kwh_price, "kwh_price")
    kb = _clamp(num("kb"), "kwh_before") if num("kb") else None
    c = _clamp(num("c") or 0.0, "contribution")
    t = _clamp(num("t") / 100 if num("t") is not None else DEFAULT_TARGET_IRR, "target_irr")
    return Scenario(rates, months, pf, pr, kp, kb, c, t)


def to_args(s: Scenario) -> Dict[str, str]:
    d = {f"r_{a}": f"{s.rates[a]:g}" for a in AREAS}
    d.update(m=str(s.months), pf=f"{100 * s.prologis_fee:g}", pr=f"{100 * s.profit:g}", kp=f"{s.kwh_price:g}",
             c=f"{s.contribution:g}", t=f"{100 * s.target_irr:g}")
    if s.kwh_before:
        d["kb"] = f"{s.kwh_before:g}"
    return d


@dataclass(frozen=True)
class Result:
    direct: float
    oh_mxn: float
    profit_mxn: float
    fee_mxn: float
    investment: float
    outlay: float                         # what Prologis pays (investment - customer contribution)
    monthly_fee: float
    fees_total: float
    irr: Optional[float]                  # annualised
    payback_months: Optional[float]
    kwh_before: Optional[float]
    kwh_after: Optional[float]
    cost_before_m: Optional[float]        # customer's lighting electricity per month, MXN
    cost_after_m: Optional[float]
    gross_saving_m: Optional[float]
    net_saving_m: Optional[float]         # gross saving - monthly fee
    customer_keeps: Optional[float]       # net / gross
    fee_for_target: float                 # monthly fee that gives the target IRR
    fee_breakeven: Optional[float]        # monthly fee at which the customer saves nothing
    warnings: Tuple[str, ...] = field(default_factory=tuple)


def compute(o: Offer, s: Scenario) -> Result:
    oh_mxn = o.direct * o.oh
    profit_mxn = (o.direct + oh_mxn) * s.profit
    fee_mxn = (o.direct + oh_mxn + profit_mxn) * s.prologis_fee
    inv = o.direct + oh_mxn + profit_mxn + fee_mxn
    contribution = min(s.contribution, inv)
    outlay = inv - contribution
    fee = sum(o.areas[a] * s.rates[a] for a in AREAS)
    r = irr_monthly(outlay, fee, s.months)
    kb = s.kwh_before or o.kwh_before
    ka = o.kwh_after
    cb = kb * s.kwh_price / 12 if kb else None
    ca = ka * s.kwh_price / 12 if ka else None
    gross = cb - ca if cb is not None and ca is not None else None
    net = gross - fee if gross is not None else None
    warn = []
    if not kb:
        warn.append("no_before")
    if gross is not None and gross <= 0:
        warn.append("no_saving")
    if net is not None and gross and gross > 0 and net < 0:
        warn.append("customer_pays_more")
    if outlay > 0 and fee * s.months < outlay:
        warn.append("not_repaid")
    if r is not None and annual(r) > 1.0:
        warn.append("check_data")             # > 100 % a year: the proforma's cost or area is not right
    return Result(o.direct, oh_mxn, profit_mxn, fee_mxn, inv, outlay, fee, fee * s.months, annual(r),
                  outlay / fee if fee > 0 and outlay > 0 else (0.0 if outlay <= 0 else None), kb, ka, cb, ca, gross, net,
                  (net / gross) if gross and gross > 0 and net is not None else None,
                  payment_for_irr(outlay, s.target_irr, s.months), gross if gross and gross > 0 else None, tuple(warn))


def scale_rates(o: Offer, s: Scenario, monthly_fee: float) -> Dict[str, float]:
    """Rates in the same proportion as the scenario's, scaled to give `monthly_fee` (rounded to 0.01 MXN)."""
    cur = sum(o.areas[a] * s.rates[a] for a in AREAS)
    base = s.rates if cur > 0 else {a: 1.0 for a in AREAS}
    cur = cur if cur > 0 else sum(o.areas.values())
    k = monthly_fee / cur if cur else 0
    return {a: round(base[a] * k, 2) for a in AREAS}


def suggestions(o: Offer, s: Scenario) -> List[Tuple[str, Scenario]]:
    """Meaningful alternatives to the scenario: each moves ONE thing and says what it is."""
    res = compute(o, s)
    out = [("target", replace(s, rates=scale_rates(o, s, res.fee_for_target)))]
    if res.gross_saving_m and res.gross_saving_m > 0:
        out.append(("share", replace(s, rates=scale_rates(o, s, res.gross_saving_m * (1 - CUSTOMER_SHARE)))))
    longer = min(LIMITS["months"][1], max(s.months + 24, 84))
    if longer > s.months:
        out.append(("longer", replace(s, months=longer, rates=scale_rates(o, s, payment_for_irr(res.outlay, s.target_irr, longer)))))
    if s.prologis_fee > 0:
        out.append(("nofee", replace(s, prologis_fee=0.0)))
    return out
