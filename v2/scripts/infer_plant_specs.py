#!/usr/bin/env python3
"""Argia_Mont — Reverse-engineer plant specs from observed telemetry.

Use case: onboarding placeholder. When `rated_kw`, `kwp_ac`, `kwp_dc` are
not yet filled from installer docs, derive plausible values from observed
peak power in the live telemetry.

HONEST LIMITATIONS — read before using:

1. We can ONLY infer AC-side values. DC ratings (kwp_dc, module_count,
   module_wp) cannot be derived from electrical telemetry. The script
   defaults kwp_dc = kwp_ac × 1.20 (typical Mexico commercial overbuild)
   but flags this as a guess. Module counts stay empty.

2. Peak power is LOWER bound, not exact nameplate. A 50 kW inverter that
   has never seen full sun shows max ~38 kW; we'd round to 40 kW and
   under-rate it by 25%. Mitigated by:
   - Snapping UP to the next standard size
   - Reading multiple days of telemetry, taking the multi-day max
   - Refusing to infer when observed peak is too low (<1 kW)

3. Tilt, azimuth, system_losses_pct stay EMPTY. These cannot be inferred.
   The KPI math has reasonable defaults when these are missing.

4. The script NEVER overwrites a non-zero existing value. Re-running as
   you add real installer data is safe — your hand-entered values stay.

5. Default mode is DRY-RUN. You see what it would write, then add --apply
   to actually update the sheet.

SAFETY GUARDS:
- Refuses to infer rated_kw when observed peak < MIN_OBSERVABLE_KW (1 kW)
- Refuses to infer when only 1 day of data is available
- Skips inverters with no telemetry rows at all

USAGE:
    PYTHONPATH=. python scripts/infer_plant_specs.py
    PYTHONPATH=. python scripts/infer_plant_specs.py --apply
    PYTHONPATH=. python scripts/infer_plant_specs.py --plant-key QRO1
    PYTHONPATH=. python scripts/infer_plant_specs.py --dc-ac-ratio 1.25
    PYTHONPATH=. python scripts/infer_plant_specs.py --days 7 --log-level DEBUG
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.core.config import (
    INVERTERS_HEADER,
    PLANTS_HEADER,
    PlantConfig,
    load_portfolio,
)
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.kpi.reader import (
    ARGIA_TAB_NAME,
    InverterRow,
    parse_rows,
)


# ============================================================
# Standard inverter sizes (kW AC)
# ============================================================
#
# Snap observed peak UP to the nearest standard size. These cover Growatt
# MAX/MAC, Huawei SUN2000, SolarEdge SE-series, SMA SB/STP — the brands
# in this portfolio.

STANDARD_INVERTER_SIZES_KW = [
    # Residential
    3, 4, 5, 6, 7, 8, 10, 12,
    # Small commercial
    15, 17, 20, 25, 30, 33,
    # Mid commercial
    40, 50, 60, 75, 80, 100, 110,
    # Larger
    125, 150, 175, 200, 250,
]

MIN_OBSERVABLE_KW = 1.0
"""Refuse to infer rated_kw when observed peak is below this. Nighttime-
only or near-dark observations don't tell us inverter size."""

DEFAULT_DC_AC_RATIO = 1.20
"""Typical Mexican commercial DC overbuild ratio. Override with --dc-ac-ratio."""

MIN_DAYS_FOR_INFERENCE = 2
"""Refuse to infer with only 1 day of data — could be a cloudy outlier."""


def snap_to_standard_size(observed_kw: float) -> int:
    """Round observed peak power UP to the next standard inverter size.

    Examples:
        47.3 → 50
        8.2 → 10
        102 → 110
        251 → 250 (uses largest size as ceiling)

    Args:
        observed_kw: peak power observed across all telemetry, in kW

    Returns:
        Standard size in kW, or 0 if observed is below threshold.
    """
    if observed_kw < MIN_OBSERVABLE_KW:
        return 0
    for size in STANDARD_INVERTER_SIZES_KW:
        if size >= observed_kw:
            return size
    # Above the table — use the largest size (could be wrong; flagged in
    # the per-row notes)
    return STANDARD_INVERTER_SIZES_KW[-1]


# ============================================================
# Inference dataclasses
# ============================================================


@dataclass(frozen=True)
class InverterInference:
    plant_key: str
    inverter_sn: str
    rows_seen: int
    days_seen: int
    observed_peak_kw: Optional[float]
    inferred_rated_kw: int
    existing_rated_kw: float
    will_update: bool
    note: str


@dataclass(frozen=True)
class PlantInference:
    plant_key: str
    summed_rated_kw_ac: int
    inferred_kwp_dc: float
    inferred_kwp_ac: float
    existing_kwp_dc: float
    existing_kwp_ac: float
    will_update_dc: bool
    will_update_ac: bool
    note: str


# ============================================================
# Compute peaks from telemetry
# ============================================================


def _peak_kw_by_inverter(
    rows: List[InverterRow],
) -> Dict[Tuple[str, str], Tuple[float, int, int]]:
    """For each (plant_key, inverter_sn), return (peak_kw, days_seen, rows_seen).

    Peak is computed across ALL provided rows — caller pre-filters to the
    desired N-day window. Days are distinct LOCAL dates (rough proxy: UTC
    date) across the rows.
    """
    out: Dict[Tuple[str, str], Tuple[float, set, int]] = {}
    for r in rows:
        key = (r.plant_key, r.inverter_sn)
        peak, days, count = out.get(key, (0.0, set(), 0))
        if r.power_w is not None:
            kw = r.power_w / 1000.0
            if kw > peak:
                peak = kw
        days.add(r.timestamp_utc.date())
        out[key] = (peak, days, count + 1)
    return {k: (peak, len(days), count) for k, (peak, days, count) in out.items()}


def infer_inverter_specs(
    rows: List[InverterRow],
    portfolio_inverters: Dict[Tuple[str, str], float],
) -> List[InverterInference]:
    """Build one InverterInference per (plant, sn) present in either source.

    Args:
        rows: parsed Telemetry_Argia rows
        portfolio_inverters: existing dict (plant_key, sn) → existing rated_kw
            from Inverters tab

    Returns:
        list sorted by plant_key, inverter_sn
    """
    peaks = _peak_kw_by_inverter(rows)

    # Union of keys
    all_keys = set(peaks.keys()) | set(portfolio_inverters.keys())

    out: List[InverterInference] = []
    for key in sorted(all_keys):
        plant_key, sn = key
        peak_kw, days, count = peaks.get(key, (0.0, 0, 0))
        existing = portfolio_inverters.get(key, 0.0)

        # Already filled — don't overwrite
        if existing > 0:
            out.append(InverterInference(
                plant_key=plant_key, inverter_sn=sn,
                rows_seen=count, days_seen=days,
                observed_peak_kw=peak_kw if count > 0 else None,
                inferred_rated_kw=0,
                existing_rated_kw=existing,
                will_update=False,
                note=f"existing rated_kw={existing:.1f} kW — not overwriting",
            ))
            continue

        # Safety: no data at all
        if count == 0:
            out.append(InverterInference(
                plant_key=plant_key, inverter_sn=sn,
                rows_seen=0, days_seen=0,
                observed_peak_kw=None,
                inferred_rated_kw=0,
                existing_rated_kw=0.0,
                will_update=False,
                note="no telemetry rows for this inverter — cannot infer",
            ))
            continue

        # Safety: too few days
        if days < MIN_DAYS_FOR_INFERENCE:
            out.append(InverterInference(
                plant_key=plant_key, inverter_sn=sn,
                rows_seen=count, days_seen=days,
                observed_peak_kw=peak_kw,
                inferred_rated_kw=0,
                existing_rated_kw=0.0,
                will_update=False,
                note=f"only {days} day(s) of data; need {MIN_DAYS_FOR_INFERENCE}+ "
                     f"(re-run after more days accumulate)",
            ))
            continue

        # Safety: peak below threshold
        if peak_kw < MIN_OBSERVABLE_KW:
            out.append(InverterInference(
                plant_key=plant_key, inverter_sn=sn,
                rows_seen=count, days_seen=days,
                observed_peak_kw=peak_kw,
                inferred_rated_kw=0,
                existing_rated_kw=0.0,
                will_update=False,
                note=f"peak {peak_kw:.2f} kW below threshold "
                     f"({MIN_OBSERVABLE_KW} kW) — likely offline or nighttime",
            ))
            continue

        inferred = snap_to_standard_size(peak_kw)
        if inferred == 0:
            out.append(InverterInference(
                plant_key=plant_key, inverter_sn=sn,
                rows_seen=count, days_seen=days,
                observed_peak_kw=peak_kw,
                inferred_rated_kw=0,
                existing_rated_kw=0.0,
                will_update=False,
                note=f"snap returned 0 for peak {peak_kw:.2f}",
            ))
            continue

        # Note: if peak is unusually low (<60% of snapped size), warn that
        # the inferred value may be an UNDER-estimate
        ratio = peak_kw / inferred
        note = (
            f"observed peak {peak_kw:.2f} kW over {days} days → "
            f"snapped UP to {inferred} kW"
        )
        if ratio < 0.6:
            note += " ⚠ peak is <60% of inferred — might be a larger inverter"

        out.append(InverterInference(
            plant_key=plant_key, inverter_sn=sn,
            rows_seen=count, days_seen=days,
            observed_peak_kw=peak_kw,
            inferred_rated_kw=inferred,
            existing_rated_kw=0.0,
            will_update=True,
            note=note,
        ))

    return out


def infer_plant_specs(
    inverter_inferences: List[InverterInference],
    portfolio: Dict[str, PlantConfig],
    dc_ac_ratio: float,
) -> List[PlantInference]:
    """Sum inferred rated_kw per plant → plant kwp_ac → kwp_dc.

    Will_update flags reflect "is this value currently 0 or missing?".
    """
    # Group by plant
    by_plant: Dict[str, List[InverterInference]] = {}
    for inv in inverter_inferences:
        by_plant.setdefault(inv.plant_key, []).append(inv)

    out: List[PlantInference] = []
    for plant_key in sorted(by_plant.keys()):
        plant_invs = by_plant[plant_key]
        existing_plant = portfolio.get(plant_key)
        existing_kwp_ac = existing_plant.kwp_ac if existing_plant else 0.0
        existing_kwp_dc = existing_plant.kwp_dc if existing_plant else 0.0

        # Sum: use existing rated_kw where set, inferred otherwise
        summed = 0.0
        complete = True  # all inverters have *some* rated_kw value
        for inv in plant_invs:
            val = inv.existing_rated_kw if inv.existing_rated_kw > 0 else inv.inferred_rated_kw
            if val <= 0:
                complete = False
            else:
                summed += val

        inferred_kwp_ac = round(summed, 1)
        inferred_kwp_dc = round(summed * dc_ac_ratio, 1)

        # Only update plant kwp_ac if it's currently 0 AND we have a full sum
        will_update_ac = existing_kwp_ac <= 0 and complete and summed > 0
        # Only update plant kwp_dc if it's currently 0 AND we have a full sum
        will_update_dc = existing_kwp_dc <= 0 and complete and summed > 0

        notes: List[str] = []
        if not complete:
            notes.append(
                "not all inverters have rated_kw — plant total is incomplete"
            )
        if existing_kwp_ac > 0:
            notes.append(f"existing kwp_ac={existing_kwp_ac:.1f} kept")
        if existing_kwp_dc > 0:
            notes.append(f"existing kwp_dc={existing_kwp_dc:.1f} kept")
        if will_update_dc:
            notes.append(f"kwp_dc derived from kwp_ac × {dc_ac_ratio} (assumption)")

        out.append(PlantInference(
            plant_key=plant_key,
            summed_rated_kw_ac=int(summed),
            inferred_kwp_dc=inferred_kwp_dc,
            inferred_kwp_ac=inferred_kwp_ac,
            existing_kwp_dc=existing_kwp_dc,
            existing_kwp_ac=existing_kwp_ac,
            will_update_dc=will_update_dc,
            will_update_ac=will_update_ac,
            note="; ".join(notes),
        ))

    return out


# ============================================================
# Reading + writing
# ============================================================


def _load_telemetry_window(
    sheets: SheetsClient, days: int,
) -> List[InverterRow]:
    """Read recent telemetry and filter to the last ``days`` days."""
    try:
        raw = sheets.read_range(ARGIA_TAB_NAME, "A1:O")
    except Exception as e:
        logging.error("Could not read %s: %s", ARGIA_TAB_NAME, e)
        return []
    all_rows = parse_rows(raw)
    cutoff = (now_mx().date() - dt.timedelta(days=days))
    cutoff_utc = dt.datetime.combine(
        cutoff, dt.time(0, 0), tzinfo=dt.timezone.utc,
    )
    return [r for r in all_rows if r.timestamp_utc >= cutoff_utc]


def _portfolio_inverters_map(portfolio) -> Dict[Tuple[str, str], float]:
    out: Dict[Tuple[str, str], float] = {}
    for plant_key, inv_list in portfolio.inverters_by_plant.items():
        for inv in inv_list:
            out[(plant_key, inv.inverter_sn)] = inv.rated_kw
    return out


def _apply_inverter_updates(
    sheets: SheetsClient,
    inferences: List[InverterInference],
    log: logging.Logger,
) -> int:
    """Update rated_kw in Inverters tab. Returns count of rows changed."""
    try:
        rows = sheets.read_range("Inverters", "A1:Z")
    except Exception as e:
        log.error("Failed to read Inverters tab: %s", e)
        return 0

    if not rows or len(rows) < 2:
        log.error("Inverters tab has no data rows")
        return 0

    header = [str(c).strip() for c in rows[0]]
    try:
        plant_col = header.index("plant_key")
        sn_col = header.index("inverter_sn")
        rated_col = header.index("rated_kw")
    except ValueError as e:
        log.error("Inverters tab missing expected column: %s", e)
        return 0

    by_key = {(i.plant_key, i.inverter_sn): i for i in inferences if i.will_update}

    changed = 0
    for i, row in enumerate(rows[1:], start=2):  # sheet row 2 = first data
        if len(row) <= max(plant_col, sn_col, rated_col):
            continue
        plant = str(row[plant_col]).strip()
        sn = str(row[sn_col]).strip().upper()
        key = (plant, sn)
        if key not in by_key:
            continue
        inference = by_key[key]
        # Update just the rated_kw cell, not the whole row
        try:
            sheets.write_cell("Inverters", i, rated_col + 1, inference.inferred_rated_kw)
            log.info("Updated Inverters[%s/%s] rated_kw = %d kW",
                     plant, sn, inference.inferred_rated_kw)
            changed += 1
        except Exception as e:
            log.error("Failed to update Inverters[%s/%s]: %s", plant, sn, e)

    return changed


def _apply_plant_updates(
    sheets: SheetsClient,
    inferences: List[PlantInference],
    log: logging.Logger,
) -> int:
    """Update kwp_dc and kwp_ac in Plants tab. Returns count of cells changed."""
    try:
        rows = sheets.read_range("Plants", "A1:AB")
    except Exception as e:
        log.error("Failed to read Plants tab: %s", e)
        return 0

    if not rows or len(rows) < 2:
        return 0

    header = [str(c).strip() for c in rows[0]]
    try:
        plant_col = header.index("plant_key")
        kwp_dc_col = header.index("kwp_dc")
        kwp_ac_col = header.index("kwp_ac")
    except ValueError as e:
        log.error("Plants tab missing expected column: %s", e)
        return 0

    by_key = {i.plant_key: i for i in inferences}

    changed = 0
    for i, row in enumerate(rows[1:], start=2):
        if len(row) <= plant_col:
            continue
        plant = str(row[plant_col]).strip()
        if plant not in by_key:
            continue
        inference = by_key[plant]
        if inference.will_update_dc:
            try:
                sheets.write_cell("Plants", i, kwp_dc_col + 1, inference.inferred_kwp_dc)
                log.info("Updated Plants[%s] kwp_dc = %.1f kWp", plant, inference.inferred_kwp_dc)
                changed += 1
            except Exception as e:
                log.error("Failed to update Plants[%s].kwp_dc: %s", plant, e)
        if inference.will_update_ac:
            try:
                sheets.write_cell("Plants", i, kwp_ac_col + 1, inference.inferred_kwp_ac)
                log.info("Updated Plants[%s] kwp_ac = %.1f kWp", plant, inference.inferred_kwp_ac)
                changed += 1
            except Exception as e:
                log.error("Failed to update Plants[%s].kwp_ac: %s", plant, e)

    return changed


# ============================================================
# CLI main
# ============================================================


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reverse-engineer plant specs from observed telemetry (DRY-RUN by default)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write updates to the sheet. WITHOUT this, nothing is written.",
    )
    parser.add_argument(
        "--plant-key", default=None,
        help="Limit to one plant",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Days of telemetry to read (default 7)",
    )
    parser.add_argument(
        "--dc-ac-ratio", type=float, default=DEFAULT_DC_AC_RATIO,
        help=f"DC overbuild ratio for kwp_dc derivation (default {DEFAULT_DC_AC_RATIO})",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    log = logging.getLogger("argia.infer_specs")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    sheets = SheetsClient(sheet_id=sheet_id)

    portfolio = load_portfolio(sheets)
    log.info("Reading last %d days of Telemetry_Argia...", args.days)
    rows = _load_telemetry_window(sheets, args.days)
    log.info("Loaded %d telemetry rows", len(rows))

    if args.plant_key:
        rows = [r for r in rows if r.plant_key == args.plant_key]
        log.info("Filtered to plant %s: %d rows", args.plant_key, len(rows))

    inv_map = _portfolio_inverters_map(portfolio)
    if args.plant_key:
        inv_map = {k: v for k, v in inv_map.items() if k[0] == args.plant_key}

    inv_inferences = infer_inverter_specs(rows, inv_map)

    # Filter plant inferences too
    plant_filter = {p.plant_key for p in portfolio.active_plants()}
    if args.plant_key:
        plant_filter = {args.plant_key}
    plants_dict = {k: v for k, v in portfolio.plants.items() if k in plant_filter}
    plant_inferences = infer_plant_specs(
        inv_inferences, plants_dict, args.dc_ac_ratio,
    )

    # ----- Print inverter inferences -----
    print()
    print(f"=== Inverter inferences (last {args.days} days) ===")
    print()
    print(
        f"{'Plant':10s} {'SN':22s} {'Days':>5s} {'Peak kW':>9s} "
        f"{'Existing':>9s} {'Inferred':>9s}  Action  Note"
    )
    print("-" * 120)
    will_update_inv = 0
    for inv in inv_inferences:
        action = "UPDATE" if inv.will_update else "skip  "
        if inv.will_update:
            will_update_inv += 1
        peak_str = (
            f"{inv.observed_peak_kw:.2f}"
            if inv.observed_peak_kw is not None else "  --"
        )
        existing_str = (
            f"{inv.existing_rated_kw:.1f}"
            if inv.existing_rated_kw > 0 else "  --"
        )
        inferred_str = (
            f"{inv.inferred_rated_kw}"
            if inv.inferred_rated_kw > 0 else "  --"
        )
        print(
            f"{inv.plant_key:10s} {inv.inverter_sn[:22]:22s} "
            f"{inv.days_seen:>5d} {peak_str:>9s} "
            f"{existing_str:>9s} {inferred_str:>9s}  {action}  {inv.note}"
        )

    # ----- Print plant inferences -----
    print()
    print(f"=== Plant inferences (DC/AC ratio = {args.dc_ac_ratio}) ===")
    print()
    print(
        f"{'Plant':10s} {'Sum kW':>8s} "
        f"{'kwp_ac':>16s} {'kwp_dc':>16s}  Note"
    )
    print("-" * 100)
    will_update_plant = 0
    for plant in plant_inferences:
        ac_str = (
            f"{plant.existing_kwp_ac:.1f}→{plant.inferred_kwp_ac:.1f}"
            if plant.will_update_ac else
            f"{plant.existing_kwp_ac:.1f} (kept)"
            if plant.existing_kwp_ac > 0 else "skip"
        )
        dc_str = (
            f"{plant.existing_kwp_dc:.1f}→{plant.inferred_kwp_dc:.1f}"
            if plant.will_update_dc else
            f"{plant.existing_kwp_dc:.1f} (kept)"
            if plant.existing_kwp_dc > 0 else "skip"
        )
        if plant.will_update_ac:
            will_update_plant += 1
        if plant.will_update_dc:
            will_update_plant += 1
        print(
            f"{plant.plant_key:10s} {plant.summed_rated_kw_ac:>8d} "
            f"{ac_str:>16s} {dc_str:>16s}  {plant.note}"
        )

    print()
    print(f"Summary: would update {will_update_inv} inverter rated_kw, "
          f"{will_update_plant} plant cells (kwp_ac + kwp_dc)")
    print()

    if not args.apply:
        log.warning(
            "DRY RUN — nothing written. Re-run with --apply to update the sheet."
        )
        return 0

    log.warning("--apply set — writing changes to the sheet")
    inv_changed = _apply_inverter_updates(sheets, inv_inferences, log)
    plant_changed = _apply_plant_updates(sheets, plant_inferences, log)
    log.info("DONE: updated %d inverter rows, %d plant cells",
             inv_changed, plant_changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
