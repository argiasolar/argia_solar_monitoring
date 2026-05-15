#!/usr/bin/env python3
"""Argia_Mont — SMA plant + device discovery (Stage 6.3).

Stage 6.3 improvements:
- Filters paste-ready Inverters rows to ONLY real solar inverters
  (type='Solar Inverters' AND has generatorPower field).
- Populates rated_kw from device.generatorPower / 1000.
- Lists other device types (sensors, batteries, meters, dataloggers)
  in a separate "for reference" section so you can see the full plant.

The sandbox tags charging stations as type='Solar Inverters' without a
generatorPower field — they get filtered out.

ENV VARS REQUIRED
    SMA_CLIENT_ID, SMA_CLIENT_SECRET, SMA_LOGIN_HINT, SMA_ENVIRONMENT
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List

from argia.vendors.sma import (
    SMAAPIError,
    SMAAuthError,
    SMAClient,
    SMAConsentError,
)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _build_client_from_env(log: logging.Logger) -> SMAClient:
    client_id = os.environ.get("SMA_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SMA_CLIENT_SECRET", "").strip()
    login_hint = os.environ.get(
        "SMA_LOGIN_HINT", "apiTestUser@apiSandbox.com",
    ).strip()
    environment = os.environ.get("SMA_ENVIRONMENT", "sandbox").strip()
    if not (client_id and client_secret):
        log.error("SMA_CLIENT_ID and SMA_CLIENT_SECRET must be set")
        sys.exit(3)
    log.info("Building SMAClient: env=%s loginHint=%s", environment, login_hint)
    return SMAClient(
        client_id=client_id,
        client_secret=client_secret,
        login_hint=login_hint,
        environment=environment,
    )


def _fetch_plant_list(client: SMAClient, log: logging.Logger) -> List[Dict[str, Any]]:
    try:
        response = client._get_json("/plants", {})
    except SMAAuthError as e:
        log.error("SMA auth failed: %s", e)
        return []
    except SMAConsentError as e:
        log.error("SMA consent failed: %s", e)
        return []
    except SMAAPIError as e:
        log.error("SMA /plants failed: %s", e)
        return []

    if isinstance(response, list):
        return [p for p in response if isinstance(p, dict)]
    if isinstance(response, dict):
        for key in ("plants", "list", "items", "data"):
            candidate = response.get(key)
            if isinstance(candidate, list):
                return [p for p in candidate if isinstance(p, dict)]
    log.warning("Unexpected /plants response shape: %s",
                json.dumps(response)[:200] if response else "<empty>")
    return []


def _fetch_devices(
    client: SMAClient, plant_id: str, log: logging.Logger,
) -> List[Dict[str, Any]]:
    try:
        response = client._get_json(f"/plants/{plant_id}/devices", {})
    except SMAAPIError as e:
        log.warning("SMA /plants/%s/devices failed: %s", plant_id, e)
        return []
    if isinstance(response, list):
        return [d for d in response if isinstance(d, dict)]
    if isinstance(response, dict):
        for key in ("devices", "list", "items", "data"):
            candidate = response.get(key)
            if isinstance(candidate, list):
                return [d for d in candidate if isinstance(d, dict)]
    return []


def _is_real_inverter(device: Dict[str, Any]) -> bool:
    """True if this is a real solar inverter (not a sensor/meter/battery/
    EV charger/datalogger). See sma_capture.py for the same filter."""
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


def _print_plant(
    plant: Dict[str, Any], devices: List[Dict[str, Any]],
) -> None:
    pid = plant.get("plantId") or plant.get("id") or plant.get("oid", "")
    name = plant.get("name", "")
    timezone = plant.get("timezone", "")

    real_inverters = [d for d in devices if _is_real_inverter(d)]
    other_devices = [d for d in devices if not _is_real_inverter(d)]

    print()
    print(f"=== Plant {pid} ===")
    print(f"  name:           {name}")
    if timezone:
        print(f"  timezone:       {timezone}")
    print(f"  total devices:  {len(devices)}")
    print(f"  real inverters: {len(real_inverters)}")
    print(f"  other devices:  {len(other_devices)}")

    if real_inverters:
        total_kw = sum(
            (d.get("generatorPower") or 0) / 1000.0 for d in real_inverters
        )
        print(f"  total rated kW: {total_kw:.1f}")
        print()
        print("Paste into Inverters tab (real solar inverters only):")
        print("plant_key\tinverter_sn\tinverter_label\trated_kw\tactive")
        for d in real_inverters:
            did = (
                d.get("deviceId")
                or d.get("serialNumber")
                or d.get("serial")
                or ""
            )
            label = d.get("name") or f"Inverter {did}"
            gen_kw = (d.get("generatorPower") or 0) / 1000.0
            product = d.get("product", "?")
            serial = d.get("serial", "?")
            print(
                f"SMA_SANDBOX\t{did}\t{label}\t{gen_kw:.1f}\tTRUE"
                f"\t# product={product} serial={serial}"
            )

    if other_devices:
        print()
        print("Other devices in this plant (NOT paste-worthy — for reference):")
        for d in other_devices:
            did = d.get("deviceId", "?")
            name = d.get("name", "?")
            dtype = d.get("type", "?")
            product = d.get("product", "?")
            print(f"  device {did} ({name}): type={dtype}, product={product}")


def _print_plants_tab_template(plants: List[Dict[str, Any]]) -> None:
    if not plants:
        return
    first = plants[0]
    pid = first.get("plantId") or first.get("id") or first.get("oid", "")
    print()
    print("Suggested Plants tab row (paste, edit columns to fit):")
    print("plant_key\tcustomer\tbrand\tsite_id\tkwp_dc\tkwp_ac\t"
          "lat\tlon\texpected_factor\tpr_target\tinstallation_date\t"
          "secret_api_name\tsecret_user_name\tsecret_pass_name\t"
          "weather_plant_id\tdatalogger_sn\tdatalogger_addr\tactive")
    print(f"SMA_SANDBOX\tSMA Sandbox (dev only)\tSMA\t{pid}\t0\t0\t"
          f"\t\t0\t0\t2026-05-14\t"
          f"SMA_CLIENT_ID\tSMA_CLIENT_SECRET\tSMA_LOGIN_HINT\t"
          f"\t\t0\tTRUE")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    log = logging.getLogger("argia.sma_discover")

    client = _build_client_from_env(log)

    log.info("Logging in to SMA...")
    try:
        client.login()
    except SMAAuthError as e:
        log.error("SMA login failed (auth): %s", e)
        return 3
    except SMAConsentError as e:
        log.error("SMA login failed (consent): %s", e)
        return 3
    except SMAAPIError as e:
        log.error("SMA login failed (API): %s", e)
        return 2
    log.info("Login OK")

    plants = _fetch_plant_list(client, log)
    if not plants:
        log.warning("No plants returned")
        return 2

    log.info("Found %d plant(s)", len(plants))

    failures = 0
    total_real_inverters = 0
    for plant in plants:
        plant_id = plant.get("plantId") or plant.get("id") or plant.get("oid", "")
        if not plant_id:
            log.warning("Plant has no id field; skipping: %s", plant)
            failures += 1
            continue
        devices = _fetch_devices(client, str(plant_id), log)
        real_inverters = [d for d in devices if _is_real_inverter(d)]
        total_real_inverters += len(real_inverters)
        _print_plant(plant, devices)

    _print_plants_tab_template(plants)
    print()
    print(f"Summary: {len(plants)} plant(s), {total_real_inverters} real solar inverter(s)")

    if failures:
        log.warning("DONE: %d plant(s) had errors", failures)
        return 1
    log.info("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
