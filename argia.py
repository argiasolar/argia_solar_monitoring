# argia.py
from __future__ import annotations

import os
import json
import datetime as dt
from typing import Dict, List, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt


SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
TZ = "America/Mexico_City"


def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def safe_float(value, default=0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def date_strings_for_yesterday() -> Tuple[dt.date, str, str]:
    # Mexico City UTC-6 logic
    today_local = dt.datetime.utcnow() + dt.timedelta(hours=-6)
    yesterday = (today_local.date() - dt.timedelta(days=1))
    date_iso = yesterday.strftime("%Y-%m-%d")
    date_slash = f"{yesterday.month}/{yesterday.day}/{yesterday.year}"
    return yesterday, date_iso, date_slash


def read_config(service) -> Tuple[Dict[str, dict], Dict[str, str], Dict[str, str]]:
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="Config_Plants!A2:O200",
    ).execute()
    rows = res.get("values", [])

    plants_config: Dict[str, dict] = {}
    huawei_map: Dict[str, str] = {}
    growatt_map: Dict[str, str] = {}

    for row in rows:
        if len(row) < 10:
            continue

        p_key = str(row[0]).strip()
        brand = str(row[1]).strip().upper()
        site_id = str(row[9]).strip()

        plants_config[p_key] = {
            "brand": brand,
            "site_id": site_id,
            "kwp_dc": safe_float(row[2]),
            "lat": row[4] if len(row) > 4 else 0,  # Czytamy Lat
            "lon": row[5] if len(row) > 5 else 0,  # Czytamy Lon
            "expected_factor": safe_float(row[6], 0.8),
            "pr_target": safe_float(row[7], 0.85),
            "customer_name": str(row[8]).strip(),
        }

        if brand == "HUAWEI":
            huawei_map[site_id] = p_key
        elif brand == "GROWATT":
            growatt_map[site_id] = p_key

    return plants_config, huawei_map, growatt_map


def build_rows(
    date_slash: str,
    date_iso: str,
    plants_config: Dict[str, dict],
    prod_map: Dict[str, float],
) -> List[List]:
    rows = []
    for p_key, conf in plants_config.items():
        # PRZEKAZUJEMY plants_config, aby weather nie musiał sam czytać Sheets
        irr, clouds = weather.get_weather_for_date(p_key, date_iso, plants_config)

        kwp = conf["kwp_dc"]
        expected_factor = conf["expected_factor"]
        target_pr = conf["pr_target"]
        energy = float(prod_map.get(p_key, 0.0) or 0.0)

        # Obliczenia Performance Ratio
        possible = round(kwp * irr * expected_factor, 2) if (kwp > 0 and irr > 0) else 0.0
        pr = round(energy / (kwp * irr), 3) if (kwp > 0 and irr > 0 and energy > 0) else 0.0

        rows.append([
            date_slash,
            p_key,
            conf["customer_name"],
            energy,
            irr,
            possible,
            pr,
            target_pr,
            clouds,
            "YES",  # Transfer domyślnie YES
        ])
    return rows


def upsert_rawdata(service, date_slash: str, rows_to_write: List[List]) -> Tuple[int, int]:
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="RawData!A2:J5000",
    ).execute()
    existing = res.get("values", [])

    idx = {}
    for i, r in enumerate(existing, start=2):
        if len(r) >= 2:
            idx[(r[0], r[1])] = i

    updates = []
    appends = []

    for row in rows_to_write:
        key = (row[0], row[1])
        if key in idx:
            rnum = idx[key]
            updates.append((rnum, row))
        else:
            appends.append(row)

    updated = 0
    appended = 0

    if updates:
        data = []
        for rnum, row in updates:
            data.append({"range": f"RawData!A{rnum}:J{rnum}", "values": [row]})
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
        updated = len(updates)

    if appends:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="RawData!A2",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": appends},
        ).execute()
        appended = len(appends)

    return updated, appended


def apply_dummy(rows: List[List], plants: List[str]) -> None:
    dummy_map = {
        "SLP1": 609, "SLP2": 848, "GTO1": 2177,
        "MEX1": 2174, "NL1": 1394, "MEX2": 2448,
    }
    for r in rows:
        if r[1] in plants and safe_float(r[3]) <= 0:
            r[3] = float(dummy_map.get(r[1], 500))
            r[9] = "NO"


def main():
    print("--- 🌟 ARGIA SOLAR MONITORING (Daily) ---")
    service = get_service()

    yesterday, date_iso, date_slash = date_strings_for_yesterday()
    print(f"📅 Target date: {date_iso} / {date_slash} ({TZ})")

    plants_config, huawei_map, growatt_map = read_config(service)

    prod_map: Dict[str, float] = {}

    # Huawei
    if huawei_map:
        prod_map.update(huawei.fetch_huawei_day_kwh(date_iso, huawei_map, plants_config))

    # Growatt
    if growatt_map:
        prod_map.update(growatt.fetch_growatt_day_kwh(date_iso, growatt_map, plants_config))

    rows = build_rows(date_slash, date_iso, plants_config, prod_map)

    # Retry logic
    missing = [r[1] for r in rows if safe_float(r[3]) <= 0]
    if missing:
        print(f"⚠️ Missing production for: {missing}. Retrying...")
        for attempt in range(1, 3):
            subset_huawei = {k: v for k, v in huawei_map.items() if v in missing}
            subset_growatt = {k: v for k, v in growatt_map.items() if v in missing}
            
            if subset_huawei:
                prod_map.update(huawei.fetch_huawei_day_kwh(date_iso, subset_huawei, plants_config))
            if subset_growatt:
                prod_map.update(growatt.fetch_growatt_day_kwh(date_iso, subset_growatt, plants_config))

            rows = build_rows(date_slash, date_iso, plants_config, prod_map)
            missing = [r[1] for r in rows if safe_float(r[3]) <= 0]
            if not missing: break

    if missing:
        apply_dummy(rows, missing)

    updated, appended = upsert_rawdata(service, date_slash, rows)
    print(f"✅ Sync complete for {date_slash}. Updated: {updated}, Appended: {appended}")


if __name__ == "__main__":
    main()
