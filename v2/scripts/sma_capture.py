#!/usr/bin/env python3
"""Argia_Mont — SMA response capture (Stage 6.3).

Stage 6.3 changes vs Stage 6:
- Filters to REAL solar inverters only (type='Solar Inverters' AND has
  generatorPower field) instead of grabbing the first device per plant.
- Walks SMA's actual /devices/{id}/measurements/sets list to discover what
  measurement sets exist for that device, then captures each one. No more
  guessing whether 'pvGeneration' exists — we ask SMA.
- Logs the `set` block keys for every successfully captured set so we know
  exactly what fields the parser needs to handle.

ENV VARS REQUIRED
    SMA_CLIENT_ID, SMA_CLIENT_SECRET, SMA_LOGIN_HINT, SMA_ENVIRONMENT
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from argia.vendors.sma import SMAAPIError, SMAAuthError, SMAClient, SMAConsentError


FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sma"
MAX_SETS_PER_DEVICE = 15  # safety cap; SMA typically lists <10


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _safe_name(value: str) -> str:
    """Make a string safe for use as a filename fragment."""
    return value.replace("/", "_").replace(" ", "_").replace("\\", "_")


def _save_fixture(filename: str, data: Any, log: logging.Logger) -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURE_DIR / filename
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Wrote %s (%d bytes)", path, path.stat().st_size)


def _try_get(client: SMAClient, path: str, log: logging.Logger) -> Any:
    """GET with all errors caught. Returns the JSON or None."""
    try:
        return client._get_json(path, {})
    except SMAAPIError as e:
        log.warning("Skipped %s: %s", path, e)
        return None


def _build_client(log: logging.Logger) -> SMAClient:
    client_id = os.environ.get("SMA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SMA_CLIENT_SECRET", "").strip()
    login_hint = os.environ.get(
        "SMA_LOGIN_HINT", "apiTestUser@apiSandbox.com",
    ).strip()
    environment = os.environ.get("SMA_ENVIRONMENT", "sandbox").strip()
    if not (client_id and client_secret):
        log.error("SMA_CLIENT_ID and SMA_CLIENT_SECRET must be set")
        sys.exit(3)
    return SMAClient(
        client_id=client_id,
        client_secret=client_secret,
        login_hint=login_hint,
        environment=environment,
    )


def _devices_from(response: Any) -> List[Dict[str, Any]]:
    if isinstance(response, list):
        return [d for d in response if isinstance(d, dict)]
    if isinstance(response, dict):
        for key in ("devices", "list", "items", "data"):
            candidate = response.get(key)
            if isinstance(candidate, list):
                return [d for d in candidate if isinstance(d, dict)]
    return []


def _plants_from(response: Any) -> List[Dict[str, Any]]:
    if isinstance(response, list):
        return [p for p in response if isinstance(p, dict)]
    if isinstance(response, dict):
        for key in ("plants", "list", "items", "data"):
            candidate = response.get(key)
            if isinstance(candidate, list):
                return [p for p in candidate if isinstance(p, dict)]
    return []


def _is_real_inverter(device: Dict[str, Any]) -> bool:
    """True if this device is a real solar inverter — not a sensor, meter,
    battery, charging station, or datalogger.

    Filter: type == 'Solar Inverters' AND generatorPower field is present
    and non-zero. The generatorPower field is what distinguishes real PV
    inverters from EV chargers (which sandbox tags as 'Solar Inverters'
    too but have no generatorPower)."""
    if not isinstance(device, dict):
        return False
    if device.get("type") != "Solar Inverters":
        return False
    gen_power = device.get("generatorPower")
    if gen_power is None:
        return False
    try:
        return float(gen_power) > 0
    except (TypeError, ValueError):
        return False


def _device_id_from(device: Dict[str, Any]) -> str:
    return str(
        device.get("deviceId")
        or device.get("serialNumber")
        or device.get("serial")
        or device.get("id", "")
    )


def _plant_id_from(plant: Dict[str, Any]) -> str:
    return str(
        plant.get("plantId")
        or plant.get("id")
        or plant.get("oid", "")
    )


def _set_types_from_list(response: Any) -> List[str]:
    """Extract setType names from a /devices/{id}/measurements/sets response.

    SMA returns one of two shapes:

    String array (observed in sandbox May 2026 — captured live from inverter 16):
        { "plant": {...}, "device": {...},
          "sets": ["Sensor", "EnergyAndPowerPv", "PowerDc", "PowerAc"] }

    Dict array (documented form, defensive):
        { "plant": {...}, "device": {...},
          "sets": [{"setType": "..."}, ...] }

    We handle both. Empty/garbage entries are dropped.
    """
    if not isinstance(response, dict):
        return []
    sets = response.get("sets")
    if not isinstance(sets, list):
        return []
    types: List[str] = []
    for s in sets:
        if isinstance(s, str):
            s_stripped = s.strip()
            if s_stripped:
                types.append(s_stripped)
        elif isinstance(s, dict):
            set_type = s.get("setType") or s.get("type") or s.get("name")
            if set_type and isinstance(set_type, str) and set_type.strip():
                types.append(set_type.strip())
    return types


def _capture_inverter(
    client: SMAClient,
    plant_id: str,
    device: Dict[str, Any],
    log: logging.Logger,
) -> int:
    """Capture available measurement sets for one inverter device.

    Returns count of sets successfully captured."""
    did = _device_id_from(device)
    safe_did = _safe_name(did)
    if not did:
        log.warning("[plant %s] inverter device has no id; skipping", plant_id)
        return 0

    log.info("[plant %s] capturing inverter device %s (%s, %s)",
             plant_id, did, device.get("name", "?"), device.get("product", "?"))

    # Step 1: list available sets
    dev_sets_response = _try_get(client, f"/devices/{did}/measurements/sets", log)
    if dev_sets_response is None:
        log.warning("[plant %s / device %s] cannot list sets; skipping device",
                    plant_id, did)
        return 0
    _save_fixture(f"live_inverter_sets_{safe_did}.json", dev_sets_response, log)

    set_types = _set_types_from_list(dev_sets_response)
    if not set_types:
        log.warning(
            "[plant %s / device %s] /measurements/sets returned no setTypes — "
            "this inverter has no data available in sandbox",
            plant_id, did,
        )
        return 0

    log.info("[plant %s / device %s] available setTypes: %s",
             plant_id, did, set_types)

    # Step 2: capture each set, but cap to avoid runaway
    captured = 0
    for set_type in set_types[:MAX_SETS_PER_DEVICE]:
        path = f"/devices/{did}/measurements/sets/{set_type}"
        response = _try_get(client, path, log)
        if response is None:
            continue
        safe_set = _safe_name(set_type)
        _save_fixture(f"live_inverter_{safe_did}_{safe_set}.json", response, log)
        captured += 1

        # Log keys for parser comparison — the moment of truth
        if isinstance(response, dict) and isinstance(response.get("set"), dict):
            log.info(
                "[plant %s / device %s / set %s] 'set' keys: %s",
                plant_id, did, set_type, sorted(response["set"].keys()),
            )
        elif isinstance(response, dict):
            log.info(
                "[plant %s / device %s / set %s] top-level keys: %s",
                plant_id, did, set_type, sorted(response.keys()),
            )

    log.info("[plant %s / device %s] captured %d/%d sets",
             plant_id, did, captured, len(set_types))
    return captured


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--max-plants",
        type=int,
        default=0,
        help="Stop after N plants (default: 0 = all). Useful for quota control.",
    )
    parser.add_argument(
        "--max-inverters-per-plant",
        type=int,
        default=1,
        help="Capture from at most N inverters per plant (default: 1).",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.sma_capture")

    client = _build_client(log)
    log.info("Logging in...")
    try:
        client.login()
    except (SMAAuthError, SMAConsentError, SMAAPIError) as e:
        log.error("Login failed: %s", e)
        return 3
    log.info("Login OK")

    # /plants
    plants_response = _try_get(client, "/plants", log)
    if plants_response is None:
        log.error("Could not fetch /plants — aborting")
        return 2
    _save_fixture("live_plants_list.json", plants_response, log)

    plants = _plants_from(plants_response)
    if not plants:
        log.warning("No plants in response")
        return 1

    if args.max_plants > 0:
        plants = plants[:args.max_plants]

    log.info("Capturing per-plant data for %d plant(s)", len(plants))

    total_inverters_captured = 0
    plants_with_data = 0

    for plant in plants:
        plant_id = _plant_id_from(plant)
        if not plant_id:
            log.warning("Plant has no id; skipping: %s", plant)
            continue
        safe_id = _safe_name(plant_id)

        # /plants/{id}
        detail = _try_get(client, f"/plants/{plant_id}", log)
        if detail is not None:
            _save_fixture(f"live_plant_{safe_id}.json", detail, log)

        # /plants/{id}/devices
        devices_response = _try_get(client, f"/plants/{plant_id}/devices", log)
        if devices_response is None:
            log.warning("[plant %s] no devices response; skipping", plant_id)
            continue
        _save_fixture(f"live_devices_{safe_id}.json", devices_response, log)

        devices = _devices_from(devices_response)
        real_inverters = [d for d in devices if _is_real_inverter(d)]

        if not real_inverters:
            log.warning(
                "[plant %s] no real solar inverters in this plant "
                "(filter: type='Solar Inverters' + generatorPower>0). "
                "Devices found: %s",
                plant_id,
                [(d.get("name"), d.get("type")) for d in devices],
            )
            continue

        log.info("[plant %s] found %d real inverter(s): %s",
                 plant_id, len(real_inverters),
                 [(_device_id_from(d), d.get("name")) for d in real_inverters])

        captured_this_plant = 0
        for inv in real_inverters[:args.max_inverters_per_plant]:
            n = _capture_inverter(client, plant_id, inv, log)
            if n > 0:
                captured_this_plant += 1
        total_inverters_captured += captured_this_plant
        if captured_this_plant:
            plants_with_data += 1

    log.info(
        "DONE: captured data from %d inverter(s) across %d plant(s) "
        "(out of %d total plants)",
        total_inverters_captured, plants_with_data, len(plants),
    )
    if total_inverters_captured == 0:
        log.error(
            "No inverter telemetry captured. Sandbox may not provide "
            "data for this credential, or all inverters returned empty "
            "set lists. Paste the live_inverter_sets_*.json files for review."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
