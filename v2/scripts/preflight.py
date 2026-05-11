#!/usr/bin/env python3
"""
Argia_Mont — preflight check.

Connects to every service the orchestrator will use and reports OK / FAIL
per service. Writes NOTHING anywhere. Run this first.

Checks (in order):
  1. GOOGLE_SHEET_ID_V2 + GOOGLE_CREDENTIALS env vars present
  2. Sheets API: open the sheet, read Plants and Inverters tabs
  3. Portfolio parses without errors
  4. For each active plant: vendor credentials exist
  5. For each active plant: vendor login succeeds

USAGE
    python scripts/preflight.py

EXIT CODE
    0  all checks pass
    1  any check failed
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.vendors.factory import VendorCredentialsMissing, build_client_for


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
    )


GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"


def _ok(msg: str) -> None:
    print(f"  {GREEN}OK{NC}     {msg}")


def _fail(msg: str) -> None:
    print(f"  {RED}FAIL{NC}   {msg}")


def _warn(msg: str) -> None:
    print(f"  {YELLOW}WARN{NC}   {msg}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read-only check before first run")
    parser.add_argument(
        "--skip-vendor-login",
        action="store_true",
        help="Don't actually log into vendor APIs (faster, but less coverage)",
    )
    args = parser.parse_args(argv)

    _setup_logging("INFO")

    print("=" * 64)
    print("Argia_Mont — preflight check (READ-ONLY)")
    print("=" * 64)

    failures = 0

    # --- 1. Env vars ---
    print("\n[1] Environment variables:")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if sheet_id:
        _ok(f"GOOGLE_SHEET_ID_V2 set (len={len(sheet_id)})")
    else:
        _fail("GOOGLE_SHEET_ID_V2 is empty")
        failures += 1

    creds = os.environ.get("GOOGLE_CREDENTIALS", "").strip()
    if creds:
        _ok(f"GOOGLE_CREDENTIALS set (len={len(creds)})")
    else:
        _fail("GOOGLE_CREDENTIALS is empty")
        failures += 1

    if failures:
        print(f"\n{RED}Aborting: required env vars missing.{NC}")
        return 1

    # --- 2. Sheets ---
    print("\n[2] Google Sheets:")
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
        _ok("Service account credentials parsed")
    except Exception as e:
        _fail(f"Could not construct SheetsClient: {e}")
        return 1

    try:
        plants_raw = sheets.read_range("Plants", "A1:A2")
        _ok(f"Plants tab readable (got {len(plants_raw)} rows in A1:A2)")
    except Exception as e:
        _fail(f"Could not read Plants tab: {e}")
        return 1

    # --- 3. Portfolio ---
    print("\n[3] Portfolio:")
    try:
        portfolio = load_portfolio(sheets)
    except Exception as e:
        _fail(f"load_portfolio failed: {e}")
        return 1

    total_plants = len(portfolio.plants)
    active = [p for p in portfolio.plants.values() if p.active]
    _ok(f"{total_plants} plants total, {len(active)} active")

    total_inverters = sum(len(v) for v in portfolio.inverters_by_plant.values())
    _ok(f"{total_inverters} inverter rows loaded")

    # Sanity check: do active plants have at least one inverter?
    plants_without_inverters = [
        p.plant_key for p in active
        if not portfolio.inverters_for(p.plant_key)
    ]
    if plants_without_inverters:
        _warn(
            f"Active plants with NO active inverters: {plants_without_inverters} "
            f"— 10-min snapshot will skip them"
        )

    # --- 4. Vendor credentials ---
    print("\n[4] Vendor credentials:")
    creds_failures = 0
    clients = {}
    for plant in active:
        try:
            client = build_client_for(plant)
            clients[plant.plant_key] = client
            _ok(f"{plant.plant_key:6s} {plant.brand:10s} credentials present")
        except VendorCredentialsMissing as e:
            _fail(f"{plant.plant_key:6s} {plant.brand:10s} {e}")
            creds_failures += 1
        except Exception as e:
            _fail(f"{plant.plant_key:6s} {plant.brand:10s} unexpected: {e}")
            creds_failures += 1

    failures += creds_failures

    # --- 5. Vendor login ---
    if not args.skip_vendor_login:
        print("\n[5] Vendor login (real API calls):")
        for plant_key, client in clients.items():
            try:
                client.login()
                _ok(f"{plant_key:6s} login OK")
            except Exception as e:
                _fail(f"{plant_key:6s} login failed: {e}")
                failures += 1
    else:
        print("\n[5] Vendor login: SKIPPED (--skip-vendor-login)")

    # --- summary ---
    print("\n" + "=" * 64)
    if failures == 0:
        print(f"{GREEN}All checks passed.{NC} Safe to do a --dry-run next.")
        return 0
    print(f"{RED}{failures} check(s) failed.{NC} Fix before running for real.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
