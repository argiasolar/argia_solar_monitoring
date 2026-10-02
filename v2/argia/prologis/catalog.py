"""Service catalogue of the Prologis platform - the "shop" (v293).

Tomasz, 2026-10-02: "a class of tickets as a service order for things like
cleaning, thermal imaging, corrective maintenance - where the Prologis team
can literally order it like in a shop; and a section where we can add /
remove services and provide pricing for all additional costs".

A catalogue item is priced per unit:

    site     one per selected site                (thermal imaging)
    kwp      the selected site's kWp, automatic   (cleaning, MXN/kWp)
    unit / module / string / sensor / m / visit / session / hour
             a quantity the customer types

``price_mxn`` None means "on quote": ARGIA prices the line after the order
and Prologis accepts the quote before work starts. ``orderable`` False
items are the rate card (labour rates, mark-ups) - shown, not ordered.
``published`` False items are drafts that only ARGIA sees. ``basis`` says
where a price comes from: the MSA (contract), ARGIA's list (catalog), or
none yet (quote).

Prices are commercial data: they are never in this public repository. The
server seeds them once from a server-only JSON file
(prologis_app.py --seed-catalog FILE). Pure module - no I/O.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

IVA = 0.16
CATEGORIES = [
    ("cleaning", "Cleaning", "Limpieza"),
    ("inspection", "Inspection & testing", "Inspección y pruebas"),
    ("corrective", "Corrective & repairs", "Correctivo y reparaciones"),
    ("monitoring", "Monitoring & data", "Monitoreo y datos"),
    ("safety", "Safety & compliance", "Seguridad y cumplimiento"),
    ("advisory", "Reports & advisory", "Reportes y asesoría"),
    ("rates", "Rate card & additional costs", "Tarifas y costos adicionales"),
]
CAT_KEYS = [k for k, _, _ in CATEGORIES]
UNITS = {
    "site": ("per site", "por sitio"),
    "kwp": ("per kWp", "por kWp"),
    "unit": ("per unit", "por unidad"),
    "module": ("per module", "por módulo"),
    "string": ("per string", "por string"),
    "sensor": ("per sensor", "por sensor"),
    "m": ("per metre", "por metro"),
    "visit": ("per visit", "por visita"),
    "session": ("per session", "por sesión"),
    "hour": ("per hour", "por hora"),
    "percent": ("mark-up %", "margen %"),
}
AUTO_QTY = {"site", "kwp"}       # quantity comes from the site, not the customer
BASES = ("contract", "catalog", "quote")
RESERVED = {"NEW", "SAVE", "CART", "ADD", "CHECKOUT"}   # words used in the shop and admin URLs


@dataclass
class Item:
    code: str
    category: str
    name_en: str
    name_es: str
    unit: str
    price_mxn: Optional[float] = None
    desc_en: str = ""
    desc_es: str = ""
    includes_en: str = ""
    includes_es: str = ""
    lead_days: int = 10
    orderable: bool = True
    published: bool = False
    basis: str = "catalog"           # contract (MSA price) | catalog (ARGIA list price) | quote
    sort: int = 100
    active: bool = True

    @property
    def on_quote(self) -> bool:
        return self.price_mxn is None


def validate(it: Item) -> List[str]:
    errs = []
    if not it.code or not all(ch.isalnum() or ch in "-_" for ch in it.code):
        errs.append("code: letters, digits, - and _ only")
    elif it.code.upper() in RESERVED:
        errs.append(f"code {it.code} is reserved")
    if it.category not in CAT_KEYS:
        errs.append(f"category {it.category!r} unknown")
    if it.unit not in UNITS:
        errs.append(f"unit {it.unit!r} unknown")
    if not it.name_en.strip():
        errs.append("English name required")
    if it.price_mxn is not None and (it.price_mxn < 0 or it.price_mxn > 50_000_000):
        errs.append("price out of range")
    if it.lead_days < 0 or it.lead_days > 365:
        errs.append("lead time 0-365 days")
    if it.basis not in BASES:
        errs.append("basis must be contract, catalog or quote")
    if it.basis == "quote" and it.price_mxn is not None and it.unit != "percent":
        errs.append("a quote-basis service has no list price (leave the price empty)")
    if it.unit == "percent" and it.orderable:
        errs.append("a mark-up is not orderable")
    return errs


def from_dict(d: dict) -> Item:
    p = d.get("price_mxn")
    return Item(code=str(d["code"]).strip().upper(), category=d["category"], name_en=d["name_en"],
                name_es=d.get("name_es") or d["name_en"], unit=d["unit"],
                price_mxn=(None if p in (None, "") else float(p)),
                desc_en=d.get("desc_en", ""), desc_es=d.get("desc_es", ""),
                includes_en=d.get("includes_en", ""), includes_es=d.get("includes_es", ""),
                lead_days=int(d.get("lead_days", 10)), orderable=bool(d.get("orderable", True)),
                published=bool(d.get("published", False)), basis=d.get("basis", "catalog"),
                sort=int(d.get("sort", 100)), active=bool(d.get("active", True)))


@dataclass
class Line:
    item_code: str
    name: str
    unit: str
    site_code: str
    qty: float
    unit_price: Optional[float]
    lead_days: int = 10

    @property
    def total(self) -> Optional[float]:
        return None if self.unit_price is None else round(self.unit_price * self.qty, 2)


def qty_for(unit: str, site_kwp: Optional[float], typed_qty: Optional[float]) -> float:
    """The billable quantity of one line (pure)."""
    if unit == "kwp":
        if not site_kwp or site_kwp <= 0:
            raise ValueError("a per-kWp service needs a site with its kWp")
        return round(float(site_kwp), 2)
    if unit == "site":
        return 1.0
    q = float(typed_qty or 0)
    if q <= 0:
        raise ValueError("quantity must be above zero")
    if q > 100000:
        raise ValueError("quantity too large")
    return q


def build_lines(item: Item, sites: Sequence, typed_qty: Optional[float] = None) -> List[Line]:
    """One line per selected site (a portfolio-wide service with no site
    takes ``sites`` empty and gets one line without a site)."""
    if not item.orderable or not item.active:
        raise ValueError("not orderable")
    if not sites:
        if item.unit in AUTO_QTY:
            raise ValueError("choose at least one site")
        return [Line(item.code, item.name_en, item.unit, "", qty_for(item.unit, None, typed_qty),
                     item.price_mxn, item.lead_days)]
    return [Line(item.code, item.name_en, item.unit, s.code, qty_for(item.unit, s.kwp, typed_qty),
                 item.price_mxn, item.lead_days) for s in sites]


def totals(lines: Iterable[Line], iva: float = IVA) -> Dict[str, Optional[float]]:
    """Subtotal of the priced lines, IVA, total, and how many lines wait for a quote."""
    ls = list(lines)
    priced = [ln.total for ln in ls if ln.total is not None]
    sub = round(sum(priced), 2)
    return {"subtotal": sub, "iva": round(sub * iva, 2), "total": round(sub * (1 + iva), 2),
            "on_quote": sum(ln.unit_price is None for ln in ls), "lines": len(ls)}


def target_date(ordered: dt.date, lines: Iterable[Line], preferred: Optional[dt.date] = None) -> dt.date:
    """Earliest date ARGIA commits to: order date + the longest lead time,
    or the customer's preferred date when that is later."""
    lead = max((ln.lead_days for ln in lines), default=0)
    d = ordered + dt.timedelta(days=lead)
    return max(d, preferred) if preferred else d


def price_text(item: Item, t=lambda en, es: en) -> str:
    """'MXN 150 per kWp' / 'On quote' / '+10 %'."""
    if item.unit == "percent":
        return f"+{item.price_mxn:g} %" if item.price_mxn is not None else t("On quote", "Bajo cotización")
    if item.price_mxn is None:
        return t("On quote", "Bajo cotización")
    en, es = UNITS[item.unit]
    return f"MXN {item.price_mxn:,.0f} {t(en, es)}" if item.price_mxn >= 100 else f"MXN {item.price_mxn:,.2f} {t(en, es)}"


# Service-order lifecycle
ORDER_STATUSES = [("ORDERED", "Ordered", "Pedido"), ("CONFIRMED", "Confirmed", "Confirmado"),
                  ("SCHEDULED", "Scheduled", "Programado"), ("IN_PROGRESS", "In progress", "En curso"),
                  ("COMPLETED", "Completed", "Completado"), ("INVOICED", "Invoiced", "Facturado"),
                  ("CANCELLED", "Cancelled", "Cancelado")]
ORDER_OPEN = ("ORDERED", "CONFIRMED", "SCHEDULED", "IN_PROGRESS")
ORDER_TRANSITIONS = {"ORDERED": ("CONFIRMED", "CANCELLED"),
                     "CONFIRMED": ("SCHEDULED", "CANCELLED"),
                     "SCHEDULED": ("IN_PROGRESS", "CANCELLED"),
                     "IN_PROGRESS": ("COMPLETED",),
                     "COMPLETED": ("INVOICED",),
                     "INVOICED": (), "CANCELLED": ()}
CUSTOMER_MAY = {"CANCELLED"}          # Prologis may cancel; ARGIA works the rest
