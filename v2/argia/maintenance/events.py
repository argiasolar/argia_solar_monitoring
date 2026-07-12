"""Maintenance_Events tab — model, loader, and O&M cost rollup.

Schema (one row per event, entered manually — these are rare and
contractual, so nothing automated ever invents a billing event):

    plant_key | start_ts | end_ts | category | cost_type | cost_mxn
              | note | approved_by

Semantics
---------
* ``start_ts`` / ``end_ts``  MX-local window. Blank ``end_ts`` = ongoing
  (the GTO1-awaiting-parts case: logged now, closed when parts arrive).
* ``category``   responsibility axis, drives deemed billing:
      customer       → deemed IS billable (customer operations)
      argia          → our O&M, never billable (honest self-report)
      force_majeure  → per contract, not billable by default
  An unrecognized category is KEPT but treated as NON-billable — a typo
  must never invent income.
* ``cost_type``  cost axis so cleaning spend is reportable on its own
  (cleaning / repair / parts / inspection / other). Unknown → other.
* ``cost_mxn``   ACTUAL cost of this event, MXN. Blank = no cost (e.g. a
  pure customer-shutdown window with no work done).
* ``approved_by``  blank = draft. Deemed energy AND cost enter reports
  only when this is set — fail-closed, like Recipients.

This tab does NOT replace Cleaning_Costs: that tab keeps its own
expected-cleaning-cost as the break-even / alert input for the soiling
scheduler. Here we record the ACTUAL spend that hits the financial
report. Two numbers, two jobs.

The loader degrades to ``[]`` on a missing tab, like every other config
reader in this codebase — it never creates the tab (that is the
``maintenance_events_setup`` script's job).
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import List, Optional

from argia.core.cells import coerce_ts
from argia.core.normalize import normalize_text, safe_float
from argia.core.time_utils import MX_TZ

LOG = logging.getLogger("argia.maintenance.events")

MAINTENANCE_EVENTS_TAB = "Maintenance_Events"
MAINTENANCE_EVENTS_HEADER = [
    "plant_key", "start_ts", "end_ts", "category", "cost_type",
    "cost_mxn", "note", "approved_by",
]

# Responsibility axis.
CATEGORY_CUSTOMER = "customer"
CATEGORY_ARGIA = "argia"
CATEGORY_FORCE_MAJEURE = "force_majeure"
CATEGORIES = (CATEGORY_CUSTOMER, CATEGORY_ARGIA, CATEGORY_FORCE_MAJEURE)

#: Only these categories produce deemed (billable) energy. Kept as a set
#: so the deemed engine imports one authority for "is this billable".
BILLABLE_CATEGORIES = frozenset({CATEGORY_CUSTOMER})

# Cost axis.
COST_TYPES = ("cleaning", "repair", "parts", "inspection", "other")


def _mx_aware(when: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Coerced sheet datetimes are NAIVE local (MX) — attach MX_TZ so
    all downstream arithmetic is DST-correct and tz-aware."""
    if when is None:
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=MX_TZ)
    return when.astimezone(MX_TZ)


@dataclass(frozen=True)
class MaintenanceEvent:
    """One maintenance/compensation event, typed and normalized."""

    plant_key: str
    start_ts: dt.datetime          # MX-aware
    end_ts: Optional[dt.datetime]  # MX-aware, None = ongoing
    category: str
    cost_type: str
    cost_mxn: Optional[float]
    note: str
    approved_by: str

    @property
    def approved(self) -> bool:
        """Fail-closed gate: a draft (blank approved_by) is inert."""
        return bool(self.approved_by.strip())

    @property
    def is_ongoing(self) -> bool:
        return self.end_ts is None

    @property
    def is_billable_category(self) -> bool:
        return self.category in BILLABLE_CATEGORIES

    def effective_end(self, now: Optional[dt.datetime] = None) -> dt.datetime:
        """Window end for arithmetic: the real end, or ``now`` (MX) for an
        ongoing event so it is never treated as infinite."""
        if self.end_ts is not None:
            return self.end_ts
        n = now or dt.datetime.now(MX_TZ)
        return _mx_aware(n)

    def cost_date_iso(self) -> str:
        """Date the cost is attributed to for period membership — the day
        work started (a lump cost is realized when the event occurs)."""
        return self.start_ts.astimezone(MX_TZ).date().isoformat()


def _parse_category(value, plant_key: str) -> str:
    s = normalize_text(value).lower().replace(" ", "_")
    if s == "":
        # Blank category on a real event: cannot be billable, log it.
        LOG.warning("Maintenance_Events[%s]: blank category — treated as "
                    "non-billable", plant_key)
        return ""
    if s not in CATEGORIES:
        LOG.warning("Maintenance_Events[%s]: unknown category %r (kept, "
                    "non-billable — known: %s)", plant_key, value,
                    "/".join(CATEGORIES))
    return s


def _parse_cost_type(value, plant_key: str) -> str:
    s = normalize_text(value).lower().replace(" ", "_")
    if s == "":
        return ""
    if s not in COST_TYPES:
        LOG.warning("Maintenance_Events[%s]: unknown cost_type %r — kept as "
                    "'other'", plant_key, value)
        return "other"
    return s


def load_maintenance_events(sheets) -> List[MaintenanceEvent]:
    """Read Maintenance_Events into typed rows. Missing tab → ``[]`` with
    a warning. A row with no plant_key or an unparseable start_ts is
    skipped (it can never be a valid billing/cost basis).

    Note: reads the WHOLE row range (``A1:ZZ``) — narrow ranges silently
    drop columns (house rule).
    """
    try:
        rows = sheets.read_table(MAINTENANCE_EVENTS_TAB, "A1:ZZ")
    except Exception:  # noqa: BLE001
        LOG.warning("%s tab not found — no deemed energy or event-based "
                    "O&M available", MAINTENANCE_EVENTS_TAB)
        return []

    out: List[MaintenanceEvent] = []
    skipped = 0
    for row in rows:
        plant_key = normalize_text(row.get("plant_key")).upper()
        if not plant_key:
            continue
        start = _mx_aware(coerce_ts(row.get("start_ts")))
        if start is None:
            LOG.warning("Maintenance_Events[%s]: unparseable/blank start_ts "
                        "%r — skipping row", plant_key, row.get("start_ts"))
            skipped += 1
            continue
        end = _mx_aware(coerce_ts(row.get("end_ts")))
        if end is not None and end < start:
            LOG.warning("Maintenance_Events[%s]: end_ts %s before start_ts "
                        "%s — skipping row", plant_key, end, start)
            skipped += 1
            continue

        out.append(MaintenanceEvent(
            plant_key=plant_key,
            start_ts=start,
            end_ts=end,
            category=_parse_category(row.get("category"), plant_key),
            cost_type=_parse_cost_type(row.get("cost_type"), plant_key),
            cost_mxn=safe_float(row.get("cost_mxn")),
            note=normalize_text(row.get("note")),
            approved_by=normalize_text(row.get("approved_by")),
        ))

    if skipped:
        LOG.warning("%s: skipped %d malformed row(s)",
                    MAINTENANCE_EVENTS_TAB, skipped)
    LOG.info("Maintenance_Events loaded: %d event(s) (%d approved)",
             len(out), sum(1 for e in out if e.approved))
    return out


def om_cost_from_events(events: List[MaintenanceEvent], plant_key: str,
                        period) -> float:
    """Σ actual ``cost_mxn`` of APPROVED events for ``plant_key`` whose cost
    date falls inside ``period`` (an :class:`argia.finance.income.Period`).

    Fail-closed: drafts (no approved_by) contribute nothing. Blank cost
    contributes nothing. Every category counts toward cost — argia and
    force_majeure work is real spend even though it is not billable to
    the customer. Cost is a lump attributed to the event's start day (no
    proration — unlike a monthly retainer, a repair is not spread across
    the month).
    """
    pk = str(plant_key).upper()
    total = 0.0
    for e in events:
        if e.plant_key != pk or not e.approved or e.cost_mxn is None:
            continue
        if period.contains_iso(e.cost_date_iso()):
            total += e.cost_mxn
    return total
