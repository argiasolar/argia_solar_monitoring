#!/usr/bin/env python3
"""Argia_Mont — one-shot SolarEdge response capture.

For each SolarEdge plant, calls the same endpoints the telemetry pipeline
uses and saves the raw JSON responses to ``tests/fixtures/solaredge/``. These
become test fixtures.

Capture is two calls per plant (1 site-level + 1 per inverter, but we limit
to 1 inverter for capture to save quota):

  GET /equipment/{siteId}/list                      → site_list_{plant_key}.json
  GET /equipment/{siteId}/{firstSn}/data?...        → equipment_data_{plant_key}.json

Total: 2 calls per plant × 2 plants = 4 calls, well within quota.

USAGE
    python scripts/solaredge_capture.py
    python scripts/solaredge_capture.py --plant-key QRO1

Output goes to ``tests/fixtures/solaredge/`` (overwrites any existing).

EXIT CODES same as discover_inverters.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from argia.core.config import PlantConfig, load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ
from argia.vendors.solaredge import (
    SolarEdgeAPIError,
    SolarEdgeAuthError,
    SolarEdgeClient,
)


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "solaredge"


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _save_fixture(filename: str, data: dict, log: logging.Logger) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s (%d bytes)", path, path.stat().st_size)


def _capture_one(
    plant: PlantConfig,
    log: logging.Logger,
) -> bool:
    """Capture site_list and equipment_data fixtures for one plant.

    Returns True on success, False on failure.
    """
    secret_name = plant.secret_api_name
    api_key = os.environ.get(secret_name, "").strip() if secret_name else ""
    if not api_key:
        log.error("[%s] env var %s is not set", plant.plant_key, secret_name)
        return False

    try:
        client = SolarEdgeClient(api_key=api_key)
    except ValueError as e:
        log.error("[%s] could not build client: %s", plant.plant_key, e)
        return False

    # Call 1: equipment list
    try:
        site_list = client._get_json(f"/equipment/{plant.site_id}/list", {})
    except (SolarEdgeAuthError, SolarEdgeAPIError) as e:
        log.error("[%s] /equipment/list failed: %s", plant.plant_key, e)
        return False

    _save_fixture(f"live_site_list_{plant.plant_key}.json", site_list, log)

    # Extract first inverter SN for the equipment_data capture
    reporters = (site_list or {}).get("reporters") or {}
    inv_list = reporters.get("list") or []
    inverters = [
        i.get("serialNumber") for i in inv_list
        if isinstance(i, dict) and i.get("serialNumber")
    ]
    if not inverters:
        log.warning("[%s] no inverters returned by /equipment/list — skipping data capture", plant.plant_key)
        return True

    first_sn = inverters[0]
    log.info("[%s] %d inverter(s): %s; capturing data for first SN %s",
             plant.plant_key, len(inverters), inverters, first_sn)

    # Call 2: equipment data for the first inverter (today's window)
    now_local = dt.datetime.now(MX_TZ)
    start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    params = {
        "startTime": start_of_day.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": now_local.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        equip_data = client._get_json(
            f"/equipment/{plant.site_id}/{first_sn}/data", params,
        )
    except (SolarEdgeAuthError, SolarEdgeAPIError) as e:
        log.error("[%s] /equipment/data failed: %s", plant.plant_key, e)
        return False

    _save_fixture(f"live_equipment_data_{plant.plant_key}.json", equip_data, log)

    # Summary of what's in the data fixture
    data = (equip_data or {}).get("data") or {}
    telemetries = data.get("telemetries") or []
    if telemetries:
        latest = telemetries[-1] if isinstance(telemetries, list) else {}
        if isinstance(latest, dict):
            log.info(
                "[%s] latest telemetry fields: %s",
                plant.plant_key, sorted(latest.keys()),
            )

    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--plant-key", default=None)
    parser.add_argument(
        "--log-level", default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.solaredge_capture")

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
        log.warning("No SolarEdge plants to capture")
        return 0

    successes = sum(1 for p in plants if _capture_one(p, log))
    failures = len(plants) - successes
    log.info("DONE: %d captured, %d failed", successes, failures)

    if failures and not successes:
        return 2
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
