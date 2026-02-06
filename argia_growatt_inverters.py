#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ARGIA – Growatt Inverter 30-min Snapshot
---------------------------------------
• Reads Plant IDs from SNAP tab
• Fetches inverter list from /device/getInverterList
• Extracts real-time power + today energy
• Appends time-series rows to Google Sheets

SAFE:
✓ Read-only endpoints
✓ No device configuration calls
✓ Suitable for 30-min cron
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# --------------------------------------------------
# Logging
# --------------------------------------------------
LOG = logging.getLogger("argia.growatt.inverters")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def pick(d: Dict[str, Any], *keys) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in ("", None, "null"):
            return d[k]
    return None


def to_float(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


# --------------------------------------------------
# Google Sheets
# --------------------------------------------------
def sheets_service():
    raw = os.getenv("GOOGLE_CREDENTIALS", "")
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def read_siteids(sheet_id: str, snap_range: str) -> List[str]:
    svc = sheets_service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=snap_range,
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()

    ids = set()
    for row in resp.get("values", []):
        for cell in row:
            if re.fullmatch(r"\d{6,12}", str(cell)):
                ids.add(str(cell))

    return sorted(ids)


def ensure_header(sheet_id: str, tab: str):
    header = [
        "ExtractedAtUTC",
        "PlantId",
        "InverterSN",
        "Alias",
        "Status",
        "Power_W",
        "EToday_kWh",
    ]

    svc = sheets_service()
    rng = f"{tab}!A1:G1"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng
    ).execute()

    if not resp.get("values"):
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [header]},
        ).execute()


def append_rows(sheet_id: str, tab: str, rows: List[List[Any]]):
    if not rows:
        return
    svc = sheets_service()
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


# --------------------------------------------------
# Growatt Client
# --------------------------------------------------
class GrowattClient:
    BASE = "https://server.growatt.com"

    def __init__(self, user: str, password: str):
        self.s = requests.Session()
        self.user = user
        self.password = password
        self.s.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (ARGIA Growatt)",
                "X-Requested-With": "XMLHttpRequest",
            }
        )

    def login(self):
        self.s.get(f"{self.BASE}/login", timeout=30)
        r = self.s.post(
            f"{self.BASE}/login",
            data={"account": self.user, "password": self.password},
            timeout=30,
        )
        if "assToken" not in self.s.cookies.get_dict():
            raise RuntimeError("Growatt login failed")
        LOG.info("✅ Growatt login OK")

    def warm(self, plant_id: str):
        self.s.get(f"{self.BASE}/device", timeout=30)
        self.s.get(
            f"{self.BASE}/device/photovoltaic",
            params={"plantId": plant_id},
            timeout=30,
        )

    def get_inverters(self, plant_id: str) -> List[Dict[str, Any]]:
        r = self.s.post(
            f"{self.BASE}/device/getInverterList",
            data={"plantId": plant_id, "currPage": 1, "pageSize": 50},
            timeout=30,
        )
        data = r.json()
        return data.get("datas", [])


# --------------------------------------------------
# Main
# --------------------------------------------------
def main():
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    user = os.getenv("GROWATT_USER")
    password = os.getenv("GROWATT_PASS")

    if not all([sheet_id, user, password]):
        raise RuntimeError("Missing required env vars")

    snap_range = os.getenv("SNAP_RANGE", "SNAP!A1:Z")
    tab = os.getenv("INVERTER_TAB", "InverterData")

    ensure_header(sheet_id, tab)

    siteids = read_siteids(sheet_id, snap_range)
    LOG.info("Loaded %s plants", len(siteids))

    cli = GrowattClient(user, password)
    cli.login()

    extracted = now_utc_iso()
    rows = []

    for pid in siteids:
        LOG.info("🏭 Plant %s", pid)
        cli.warm(pid)

        invs = cli.get_inverters(pid)
        LOG.info(" → %s inverters", len(invs))

        for inv in invs:
            rows.append([
                extracted,
                pid,
                pick(inv, "sn", "serialNum"),
                pick(inv, "alias", "name"),
                pick(inv, "status", "deviceStatus"),
                to_float(pick(inv, "pac", "power")),
                to_float(pick(inv, "eToday", "EToday")),
            ])

        time.sleep(1)

    append_rows(sheet_id, tab, rows)
    LOG.info("✅ Written %s rows", len(rows))


if __name__ == "__main__":
    main()
