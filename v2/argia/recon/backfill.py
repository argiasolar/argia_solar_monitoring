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


INVERTER_NOTE = "energy from inverter counters"
VENDOR_NOTE = "energy from vendor daily counter"


def build_fix_sql(plant_key: str, date_iso: str, vendor_kwh: float,
                  old_kpi_kwh: Optional[float],
                  basis: str = "vendor_plant_daily",
                  detail: str = "") -> str:
    """UPSERT correcting one plant-day from the day's reference.

    MISSING -> insert a new row (source v2). UNDER -> raise energy_kwh.
    The WHERE guard re-checks in SQL that we only ever fill-or-raise, so
    a concurrent sync can never make this statement lower a value.
    v206: ``basis`` names the reference — the inverters' own counters
    (INVERTER_NOTE) or the vendor plant daily (VENDOR_NOTE); both notes
    mark the row as counter-authoritative for the protected upsert.
    """
    pk = str(plant_key).strip().upper()
    head = INVERTER_NOTE if basis == "inverter_counters" else VENDOR_NOTE
    note = (head + " (" + (detail + "; " if detail else "")
            + (f"kpi had {old_kpi_kwh:.1f}" if old_kpi_kwh is not None
               else "kpi was missing") + ")")
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


def build_billable_resync_sql() -> str:
    """One idempotent UPDATE restoring the billable invariant:
    ``billable_kwh >= energy_kwh`` wherever both are stamped.

    The deemed engine only ever ADDS to measured energy (approved
    customer maintenance becomes compensated energy), so billable BELOW
    energy is always staleness: the self-heal raised ``energy_kwh``
    from the vendor counter after ``billable_kwh`` was stamped from the
    undercounted value. Found at the 2026-09-01 August close, where the
    stale column quietly shrank every PPA invoice (~17.8 MWh / ~39,600
    MXN for the month) — the exact failure the billing doctrine ("a
    collection gap can never shrink an invoice") exists to prevent.
    Same finding-class as the PR resync below, same remedy shape.

    Raise-only by construction (the WHERE is the guard), so a stamped
    deemed day (billable > energy) is never touched.
    """
    return (
        "UPDATE daily_production"
        " SET billable_kwh = energy_kwh,"
        " status_note = trim(coalesce(status_note,'') ||"
        " ' | billable lifted to corrected energy (deemed only adds)')"
        " WHERE billable_kwh IS NOT NULL AND energy_kwh IS NOT NULL"
        " AND billable_kwh < energy_kwh;"
    )


PR_RESYNC_PLAUSIBLE_MAX = 1.05
"""Recomputed PR above this is physically implausible — the day's stored
irradiance undercounts (partial collection or a sick sensor: the CDMX
Aug-24 case recomputed to 1.20). Such a day gets pr = NULL, never a lie."""


def build_pr_resync_sql(plausible_max: float = PR_RESYNC_PLAUSIBLE_MAX
                        ) -> str:
    """One idempotent UPDATE re-deriving ``pr`` on vendor-healed days.

    The self-heal raises ``energy_kwh`` from the vendor counter but the
    day's ``pr`` was stamped earlier from the UNDERCOUNTED interval
    energy — found in the 2026-08-27 due diligence: every healed day
    carried a PR that no longer matched energy/(kWp x H). This statement
    re-derives PR from the corrected energy and the stored irradiance,
    and blanks it (NULL) when the result exceeds ``plausible_max`` —
    an implausible PR means the IRRADIANCE side is broken, and an honest
    NULL beats a wrong number. pr_stc follows automatically on the next
    stamp_pr_stc pass, which always recomputes from the current pr.
    Scoped strictly to rows the self-heal touched (status_note match).
    """
    return (
        "UPDATE daily_production d SET pr = CASE"
        f" WHEN d.energy_kwh / (p.kwp_dc * d.irradiance_kwh_m2)"
        f" <= {plausible_max}"
        " THEN round((d.energy_kwh"
        " / (p.kwp_dc * d.irradiance_kwh_m2))::numeric, 4)"
        " ELSE NULL END"
        " FROM plant p"
        " WHERE p.plant_key = d.plant_key"
        " AND (d.status_note LIKE '%vendor daily counter%'"
        "      OR d.status_note LIKE '%inverter counters%')"
        " AND d.energy_kwh IS NOT NULL"
        " AND d.irradiance_kwh_m2 > 0.5"
        " AND p.kwp_dc > 0"
        " AND (d.pr IS DISTINCT FROM CASE"
        f" WHEN d.energy_kwh / (p.kwp_dc * d.irradiance_kwh_m2)"
        f" <= {plausible_max}"
        " THEN round((d.energy_kwh"
        " / (p.kwp_dc * d.irradiance_kwh_m2))::numeric, 4)"
        " ELSE NULL END);"
    )
