"""Backfill / correction rules — vendor daily history vs daily_production.

The vendor daily counter is the billing control (see engine.py). When a
KPI day is MISSING or UNDERCOUNTED (partial collection: the 2026-08 Pi
incident), the correction fills/raises energy_kwh from the vendor value,
records provenance in status_note, and NEVER LOWERS an existing value —
a kpi ABOVE the vendor counter is flagged OVER for human review instead
of silently reduced. Pure functions; scripts do the I/O.
"""

from __future__ import annotations

from typing import Optional, Tuple

# Relative threshold for calling a stored day wrong (vendor counters are
# coarse — Growatt reports 0.1 kWh steps).
FIX_THRESHOLD_PCT = 1.0

CLASS_OK = "OK"
CLASS_MISSING = "MISSING"      # no kpi energy at all -> fill from vendor
CLASS_UNDER = "UNDER"          # kpi below vendor -> raise to vendor
CLASS_OVER = "OVER"            # kpi above vendor -> flag, never touch
CLASS_NO_VENDOR = "NO_VENDOR"  # nothing to compare against


def classify_day(kpi_kwh: Optional[float], vendor_kwh: Optional[float]
                 ) -> Tuple[str, Optional[float]]:
    """(classification, delta_pct where delta = kpi vs vendor)."""
    if vendor_kwh is None:
        return CLASS_NO_VENDOR, None
    if kpi_kwh is None:
        return CLASS_MISSING, None
    if vendor_kwh == 0:
        return (CLASS_OK, 0.0) if kpi_kwh == 0 else (CLASS_OVER, None)
    delta = (kpi_kwh - vendor_kwh) / vendor_kwh * 100.0
    if delta < -FIX_THRESHOLD_PCT:
        return CLASS_UNDER, delta
    if delta > FIX_THRESHOLD_PCT:
        return CLASS_OVER, delta
    return CLASS_OK, delta


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def build_fix_sql(plant_key: str, date_iso: str, vendor_kwh: float,
                  old_kpi_kwh: Optional[float]) -> str:
    """UPSERT correcting one plant-day from the vendor counter.

    MISSING -> insert a new row (source v2). UNDER -> raise energy_kwh.
    The WHERE guard re-checks in SQL that we only ever fill-or-raise, so
    a concurrent sheet sync can never make this statement lower a value.
    """
    pk = str(plant_key).strip().upper()
    note = (f"energy from vendor daily counter (history backfill"
            + (f"; kpi had {old_kpi_kwh:.1f}" if old_kpi_kwh is not None
               else "; kpi was missing") + ")")
    return (
        "INSERT INTO daily_production (plant_key, prod_date, energy_kwh,"
        " source, status_note) VALUES"
        f" ({_txt(pk)}, DATE '{date_iso}', {vendor_kwh:.3f}, 'v2',"
        f" {_txt(note)})"
        " ON CONFLICT (plant_key, prod_date) DO UPDATE SET"
        f" energy_kwh = {vendor_kwh:.3f}, status_note = {_txt(note)}"
        " WHERE daily_production.energy_kwh IS NULL"
        f" OR daily_production.energy_kwh < {vendor_kwh:.3f};"
    )
