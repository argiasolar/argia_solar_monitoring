#!/usr/bin/env python3
"""Argia_Mont — Seed physical-spec PLACEHOLDERS on Plants + Inverters tabs.

This is the second half of the spec inference pair:
  - scripts/infer_plant_specs.py     → electrical (rated_kw, kwp_ac, kwp_dc)
                                        derived from observed peak power
  - scripts/seed_plant_physicals.py  → THIS SCRIPT — physical placeholders
                                        (module count, MPPT count, tilt, etc.)
                                        derived from assumptions

HONEST DISCLAIMERS:

1. Physical specs CANNOT be derived from electrical telemetry. This script
   fills in *defaults* based on brand, installation date, and rated capacity.
   It is a placeholder generator, not an inference tool.

2. The defaults are biased toward modern (2022+) Mexican commercial installs.
   Older installs and residential plants may be off by significant amounts.

3. The math `module_count = kwp_dc / module_wp` means once you fill these in,
   the Stage 7.3 sanity warning ("kwp_dc disagrees with module_count×module_wp")
   will be silent — because we made them agree by assumption. The warning
   only becomes useful again after you replace these with real installer data.

4. DRY-RUN by default. Never overwrites a non-zero existing value.

5. Leaves these fields EMPTY:
   - string_count          → varies wildly with combiner boxes; needs docs
   - strings_per_mppt      → vendor doc
   - rated_kw_dc           → vendor doc
   - commissioning_date    → unknown to us
   - notes                 → human field

WHAT IT FILLS:

   Plants tab:
     module_wp           → 540 (modern panels) or 330 (older if before 2020)
     module_count        → ceiling(kwp_dc × 1000 / module_wp)
     tilt_deg            → 15  (Mexico latitude rule of thumb)
     azimuth_deg         → 180 (south)
     system_losses_pct   → 14  (industry standard for commercial PV)

   Inverters tab:
     mppt_count          → guess from rated_kw + brand:
                           <10 kW           → 2 MPPTs
                           10-50 kW         → 4 MPPTs
                           50-100 kW        → 6 MPPTs
                           100-200 kW       → 12 MPPTs
                           >200 kW          → 16 MPPTs

USAGE:
   PYTHONPATH=. python scripts/seed_plant_physicals.py
   PYTHONPATH=. python scripts/seed_plant_physicals.py --apply
   PYTHONPATH=. python scripts/seed_plant_physicals.py --plant-key QRO1

EXIT CODES:
   0  ran cleanly (or dry-run)
   3  config error
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

from argia.core.config import PlantConfig, load_portfolio
from argia.core.sheets import SheetsClient


# ============================================================
# Defaults / heuristics
# ============================================================

DEFAULT_MODULE_WP_MODERN = 540
"""Most common panel wattage in Mexican commercial installs from ~2022 onward."""

DEFAULT_MODULE_WP_OLDER = 330
"""Common panel wattage in installs from ~2017-2020."""

MODERN_PANEL_CUTOFF_YEAR = 2021
"""Installations from this year or later default to MODERN module_wp."""

DEFAULT_TILT_DEG = 15
"""Mexico central latitude rule of thumb: lat - 5° ≈ 15° for ~lat 20°N."""

DEFAULT_AZIMUTH_DEG = 180
"""South-facing. Almost universal in Mexico (northern hemisphere)."""

DEFAULT_SYSTEM_LOSSES_PCT = 14
"""NREL PVWatts default — covers wiring, soiling, mismatch, inverter,
   age-related losses. Conservative."""


def guess_module_wp(installation_date: str) -> int:
    """Pick 540W or 330W based on install year."""
    if installation_date and len(installation_date) >= 4:
        try:
            year = int(installation_date[:4])
            if year < MODERN_PANEL_CUTOFF_YEAR:
                return DEFAULT_MODULE_WP_OLDER
        except (ValueError, TypeError):
            pass
    return DEFAULT_MODULE_WP_MODERN


def guess_mppt_count(rated_kw: float) -> int:
    """Map AC rating to a typical MPPT count.

    These are MEDIAN ranges. Vendor variation is significant — e.g. some
    100kW Growatt MAX units have 10 MPPTs, some have 6. This is a
    placeholder; replace with vendor doc when available.
    """
    if rated_kw <= 0:
        return 2  # safe default
    if rated_kw < 10:
        return 2
    if rated_kw < 50:
        return 4
    if rated_kw < 100:
        return 6
    if rated_kw < 200:
        return 12
    return 16


# ============================================================
# Inference dataclasses
# ============================================================


@dataclass(frozen=True)
class PlantPhysicalInference:
    plant_key: str
    # module_wp
    existing_module_wp: Optional[float]
    inferred_module_wp: int
    will_update_module_wp: bool
    # module_count
    existing_module_count: Optional[int]
    inferred_module_count: int
    will_update_module_count: bool
    # tilt
    existing_tilt_deg: Optional[float]
    inferred_tilt_deg: int
    will_update_tilt_deg: bool
    # azimuth
    existing_azimuth_deg: Optional[float]
    inferred_azimuth_deg: int
    will_update_azimuth_deg: bool
    # losses
    existing_losses_pct: Optional[float]
    inferred_losses_pct: int
    will_update_losses_pct: bool

    note: str = ""


@dataclass(frozen=True)
class InverterPhysicalInference:
    plant_key: str
    inverter_sn: str
    rated_kw: float
    existing_mppt_count: Optional[int]
    inferred_mppt_count: int
    will_update_mppt_count: bool
    note: str = ""


# ============================================================
# Pure inference functions (testable)
# ============================================================


def infer_plant_physicals(plant: PlantConfig) -> PlantPhysicalInference:
    """Compute placeholder physicals for one plant. No I/O."""
    notes: List[str] = []

    # module_wp
    inferred_module_wp = guess_module_wp(plant.installation_date)
    will_update_wp = plant.module_wp is None or plant.module_wp <= 0
    if not will_update_wp:
        notes.append(f"existing module_wp={plant.module_wp}")

    # module_count: derive from kwp_dc / module_wp_to_use
    # If module_wp is already set, use that; else use what we'd infer
    wp_to_use = plant.module_wp if (plant.module_wp and plant.module_wp > 0) else inferred_module_wp

    inferred_module_count = 0
    if plant.kwp_dc > 0 and wp_to_use > 0:
        # Round to nearest int — ceiling is wrong (would always over-count by 1)
        inferred_module_count = int(round((plant.kwp_dc * 1000) / wp_to_use))
    will_update_mc = (plant.module_count is None or plant.module_count <= 0) and inferred_module_count > 0
    if plant.module_count and plant.module_count > 0:
        notes.append(f"existing module_count={plant.module_count}")
    if plant.kwp_dc <= 0:
        notes.append("kwp_dc=0; cannot derive module_count")

    # Tilt
    will_update_tilt = plant.tilt_deg is None or plant.tilt_deg <= 0
    # Azimuth
    will_update_azim = plant.azimuth_deg is None
    # Note: azimuth=0 is technically valid (north) but in Mexico it almost
    # never is. We treat None-only-as-empty so an explicit 0 stays.
    if plant.azimuth_deg is not None:
        will_update_azim = False
    # Losses
    will_update_losses = plant.system_losses_pct is None or plant.system_losses_pct <= 0

    return PlantPhysicalInference(
        plant_key=plant.plant_key,
        existing_module_wp=plant.module_wp,
        inferred_module_wp=inferred_module_wp,
        will_update_module_wp=will_update_wp,
        existing_module_count=plant.module_count,
        inferred_module_count=inferred_module_count,
        will_update_module_count=will_update_mc,
        existing_tilt_deg=plant.tilt_deg,
        inferred_tilt_deg=DEFAULT_TILT_DEG,
        will_update_tilt_deg=will_update_tilt,
        existing_azimuth_deg=plant.azimuth_deg,
        inferred_azimuth_deg=DEFAULT_AZIMUTH_DEG,
        will_update_azimuth_deg=will_update_azim,
        existing_losses_pct=plant.system_losses_pct,
        inferred_losses_pct=DEFAULT_SYSTEM_LOSSES_PCT,
        will_update_losses_pct=will_update_losses,
        note="; ".join(notes),
    )


def infer_inverter_physicals(plant_key: str, inverter) -> InverterPhysicalInference:
    """Compute placeholder physicals for one inverter."""
    notes: List[str] = []
    inferred = guess_mppt_count(inverter.rated_kw)
    will_update = inverter.mppt_count is None or inverter.mppt_count <= 0
    if not will_update:
        notes.append(f"existing mppt_count={inverter.mppt_count}")
    if inverter.rated_kw <= 0:
        notes.append("rated_kw=0; mppt_count guess may be wrong")
    return InverterPhysicalInference(
        plant_key=plant_key,
        inverter_sn=inverter.inverter_sn,
        rated_kw=inverter.rated_kw,
        existing_mppt_count=inverter.mppt_count,
        inferred_mppt_count=inferred,
        will_update_mppt_count=will_update,
        note="; ".join(notes),
    )


# ============================================================
# Sheet writers
# ============================================================


def _find_col(header: List, name: str) -> Optional[int]:
    """1-indexed column number for a header name, or None if not found."""
    for i, h in enumerate(header):
        if str(h).strip() == name:
            return i + 1
    return None


def _apply_plant_updates(
    sheets: SheetsClient,
    inferences: List[PlantPhysicalInference],
    log: logging.Logger,
) -> int:
    rows = sheets.read_range("Plants", "A1:AB")
    if not rows or len(rows) < 2:
        log.error("Plants tab is empty")
        return 0

    header = [str(c).strip() for c in rows[0]]
    plant_col = _find_col(header, "plant_key")
    if plant_col is None:
        log.error("Plants tab has no 'plant_key' column")
        return 0

    targets = {
        "module_wp": _find_col(header, "module_wp"),
        "module_count": _find_col(header, "module_count"),
        "tilt_deg": _find_col(header, "tilt_deg"),
        "azimuth_deg": _find_col(header, "azimuth_deg"),
        "system_losses_pct": _find_col(header, "system_losses_pct"),
    }
    missing = [k for k, v in targets.items() if v is None]
    if missing:
        log.error(
            "Plants tab is missing columns: %s. "
            "Add them to row 1 of the Plants tab first (see docs/stage7_3c).",
            missing,
        )
        return 0

    by_key = {i.plant_key: i for i in inferences}
    changed = 0
    for sheet_i, row in enumerate(rows[1:], start=2):
        if len(row) < plant_col:
            continue
        pk = str(row[plant_col - 1]).strip()
        if pk not in by_key:
            continue
        inf = by_key[pk]

        plan = [
            ("module_wp", inf.inferred_module_wp, inf.will_update_module_wp),
            ("module_count", inf.inferred_module_count, inf.will_update_module_count),
            ("tilt_deg", inf.inferred_tilt_deg, inf.will_update_tilt_deg),
            ("azimuth_deg", inf.inferred_azimuth_deg, inf.will_update_azimuth_deg),
            ("system_losses_pct", inf.inferred_losses_pct, inf.will_update_losses_pct),
        ]
        for field_name, value, will in plan:
            if not will or value is None:
                continue
            col = targets[field_name]
            try:
                sheets.write_cell("Plants", sheet_i, col, value)
                log.info("Plants[%s].%s = %s", pk, field_name, value)
                changed += 1
            except Exception as e:
                log.error("Plants[%s].%s write failed: %s", pk, field_name, e)
    return changed


def _apply_inverter_updates(
    sheets: SheetsClient,
    inferences: List[InverterPhysicalInference],
    log: logging.Logger,
) -> int:
    rows = sheets.read_range("Inverters", "A1:Z")
    if not rows or len(rows) < 2:
        return 0

    header = [str(c).strip() for c in rows[0]]
    plant_col = _find_col(header, "plant_key")
    sn_col = _find_col(header, "inverter_sn")
    mppt_col = _find_col(header, "mppt_count")
    if plant_col is None or sn_col is None or mppt_col is None:
        log.error(
            "Inverters tab missing required columns "
            "(plant_key, inverter_sn, mppt_count)"
        )
        return 0

    by_key = {(i.plant_key, i.inverter_sn): i for i in inferences if i.will_update_mppt_count}

    changed = 0
    for sheet_i, row in enumerate(rows[1:], start=2):
        if len(row) < max(plant_col, sn_col):
            continue
        pk = str(row[plant_col - 1]).strip()
        sn = str(row[sn_col - 1]).strip().upper()
        key = (pk, sn)
        if key not in by_key:
            continue
        inf = by_key[key]
        try:
            sheets.write_cell("Inverters", sheet_i, mppt_col, inf.inferred_mppt_count)
            log.info("Inverters[%s/%s].mppt_count = %d",
                     pk, sn, inf.inferred_mppt_count)
            changed += 1
        except Exception as e:
            log.error("Inverters[%s/%s].mppt_count write failed: %s", pk, sn, e)
    return changed


# ============================================================
# CLI
# ============================================================


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plant-key", default=None)
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)
    log = logging.getLogger("argia.seed_physicals")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    sheets = SheetsClient(sheet_id=sheet_id)

    portfolio = load_portfolio(sheets)
    plants_to_process = portfolio.active_plants()
    if args.plant_key:
        plants_to_process = [p for p in plants_to_process if p.plant_key == args.plant_key]
        if not plants_to_process:
            log.error("Plant %s not active or not found", args.plant_key)
            return 1

    # Compute all inferences
    plant_inferences = [infer_plant_physicals(p) for p in plants_to_process]
    inverter_inferences: List[InverterPhysicalInference] = []
    for plant in plants_to_process:
        for inv in portfolio.inverters_for(plant.plant_key):
            inverter_inferences.append(infer_inverter_physicals(plant.plant_key, inv))

    # ---- Print plant table ----
    print()
    print("=== Plant physical inferences ===")
    print()
    print(f"{'Plant':10s} {'module_wp':>10s} {'module_count':>13s} "
          f"{'tilt':>6s} {'azim':>6s} {'losses%':>8s}  Action")
    print("-" * 96)
    plant_updates = 0
    for inf in plant_inferences:
        actions: List[str] = []
        if inf.will_update_module_wp:
            actions.append("module_wp")
            plant_updates += 1
        if inf.will_update_module_count:
            actions.append("module_count")
            plant_updates += 1
        if inf.will_update_tilt_deg:
            actions.append("tilt_deg")
            plant_updates += 1
        if inf.will_update_azimuth_deg:
            actions.append("azimuth_deg")
            plant_updates += 1
        if inf.will_update_losses_pct:
            actions.append("system_losses_pct")
            plant_updates += 1
        action_str = "+".join(actions) if actions else "(all set)"

        def _fmt(existing, inferred, will):
            if not will:
                return f"{existing}" if existing is not None else "--"
            return f"→{inferred}"

        print(
            f"{inf.plant_key:10s} "
            f"{_fmt(inf.existing_module_wp, inf.inferred_module_wp, inf.will_update_module_wp):>10s} "
            f"{_fmt(inf.existing_module_count, inf.inferred_module_count, inf.will_update_module_count):>13s} "
            f"{_fmt(inf.existing_tilt_deg, inf.inferred_tilt_deg, inf.will_update_tilt_deg):>6s} "
            f"{_fmt(inf.existing_azimuth_deg, inf.inferred_azimuth_deg, inf.will_update_azimuth_deg):>6s} "
            f"{_fmt(inf.existing_losses_pct, inf.inferred_losses_pct, inf.will_update_losses_pct):>8s}  "
            f"{action_str}"
        )

    # ---- Print inverter table ----
    print()
    print("=== Inverter physical inferences ===")
    print()
    print(f"{'Plant':10s} {'SN':22s} {'rated_kw':>9s} "
          f"{'mppt_count':>11s}  Action")
    print("-" * 80)
    inv_updates = 0
    for inf in inverter_inferences:
        rated_str = f"{inf.rated_kw:.1f}" if inf.rated_kw > 0 else "--"
        if inf.will_update_mppt_count:
            mppt_str = f"→{inf.inferred_mppt_count}"
            action = "UPDATE"
            inv_updates += 1
        else:
            mppt_str = f"{inf.existing_mppt_count}" if inf.existing_mppt_count else "--"
            action = "skip"
        print(
            f"{inf.plant_key:10s} {inf.inverter_sn[:22]:22s} "
            f"{rated_str:>9s} {mppt_str:>11s}  {action}"
        )

    print()
    print(f"Summary: would update {plant_updates} plant cells, "
          f"{inv_updates} inverter cells")
    print()

    if not args.apply:
        log.warning(
            "DRY RUN — nothing written. Re-run with --apply to update the sheet."
        )
        return 0

    log.warning("--apply set — writing placeholder values to the sheet")
    p_changed = _apply_plant_updates(sheets, plant_inferences, log)
    i_changed = _apply_inverter_updates(sheets, inverter_inferences, log)
    log.info("DONE: wrote %d plant cells, %d inverter cells", p_changed, i_changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
