#!/usr/bin/env python3
"""Argia_Mont — one-shot SolarEdge inverter discovery.

Calls ``/equipment/{siteId}/list`` for every active SolarEdge plant in the
portfolio and prints the inverters (SN, name, manufacturer, model). Use the
output to populate the ``Inverters`` tab in the v2 sheet.

This burns 1 API call per SolarEdge site (2 total for QRO1+GTO2), well within
the 300/day quota.

USAGE
    python scripts/solaredge_discover_inverters.py
    python scripts/solaredge_discover_inverters.py --plant-key QRO1

ENV VARS REQUIRED
    GOOGLE_SHEET_ID_V2     v2 sheet (for reading Plants tab)
    GOOGLE_CREDENTIALS     service account JSON
    SOLAREDGE_API_KEY      api key for plants whose secret_api_name = 'SOLAREDGE_API_KEY'
    SOLAREDGE_API_KEY2     api key for plants whose secret_api_name = 'SOLAREDGE_API_KEY2'
    (and so on per the Plants tab)

EXIT CODES
    0 success
    1 partial failure (some plants worked, some didn't)
    2 nothing worked
    3 config error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import List, Optional

from argia.core.config import PlantConfig, load_portfolio
from argia.core.sheets import SheetsClient
from argia.vendors.solaredge import (
    SolarEdgeAPIError,
    SolarEdgeAuthError,
    SolarEdgeClient,
)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _discover_one(
    plant: PlantConfig,
    log: logging.Logger,
) -> Optional[List[dict]]:
    """Call /equipment/{siteId}/list for one plant.

    Returns the list of inverter dicts on success, None on failure.
    """
    secret_name = plant.secret_api_name
    if not secret_name:
        log.error("[%s] plant has no secret_api_name configured", plant.plant_key)
        return None

    api_key = os.environ.get(secret_name, "").strip()
    if not api_key:
        log.error("[%s] env var %s is not set", plant.plant_key, secret_name)
        return None

    try:
        client = SolarEdgeClient(api_key=api_key)
    except ValueError as e:
        log.error("[%s] could not build SolarEdgeClient: %s", plant.plant_key, e)
        return None

    try:
        response = client._get_json(
            f"/equipment/{plant.site_id}/list", {},
        )
    except SolarEdgeAuthError as e:
        log.error("[%s] auth error: %s", plant.plant_key, e)
        return None
    except SolarEdgeAPIError as e:
        log.error("[%s] API error: %s", plant.plant_key, e)
        return None

    # Expected shape per SolarEdge docs:
    # { "reporters": { "count": N, "list": [ {...inverter info...} ] } }
    reporters = (response or {}).get("reporters") or {}
    inv_list = reporters.get("list")
    if not isinstance(inv_list, list):
        log.warning(
            "[%s] unexpected response shape: %s",
            plant.plant_key, json.dumps(response)[:200],
        )
        return None

    return inv_list


def _print_inverters(
    plant: PlantConfig,
    inverters: List[dict],
) -> None:
    """Pretty-print the inverter list for one plant, ready for copy/paste."""
    print()
    print(f"=== {plant.plant_key} (site_id={plant.site_id}) ===")
    print(f"Found {len(inverters)} inverter(s):")
    print()

    # Print header for easy paste into Inverters tab
    # Columns expected: plant_key, inverter_sn, inverter_label, capacity_kwp_dc, active
    print("plant_key\tinverter_sn\tinverter_label\tcapacity_kwp_dc\tactive")
    for i, inv in enumerate(inverters, 1):
        sn = inv.get("serialNumber") or inv.get("serialnumber") or ""
        name = inv.get("name", f"Inverter {i}")
        manufacturer = inv.get("manufacturer", "")
        model = inv.get("model", "")
        # capacity_kwp_dc not exposed by /equipment/list — leave blank for human fill
        print(f"{plant.plant_key}\t{sn}\t{name}\t\tTRUE\t[{manufacturer} {model}]")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--plant-key", default=None,
        help="Run only this one plant (otherwise all SolarEdge plants)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.solaredge_discover")

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        log.error("GOOGLE_SHEET_ID_V2 is not set")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to load portfolio: %s", e)
        return 3

    plants = portfolio.plants_by_brand("SOLAREDGE")
    if args.plant_key:
        plants = [p for p in plants if p.plant_key == args.plant_key]
    if not plants:
        log.warning("No SolarEdge plants to discover")
        return 0

    log.info("Discovering %d SolarEdge plant(s): %s",
             len(plants), [p.plant_key for p in plants])

    successes = 0
    failures = 0
    for plant in plants:
        inverters = _discover_one(plant, log)
        if inverters is None:
            failures += 1
            continue
        _print_inverters(plant, inverters)
        successes += 1

    print()
    log.info("DONE: %d succeeded, %d failed", successes, failures)
    if failures and not successes:
        return 2
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
