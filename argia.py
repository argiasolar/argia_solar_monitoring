import os
import json
import time
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt


TZ = ZoneInfo("America/Mexico_City")
SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")


def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS env var")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def safe_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default


def target_dates() -> Tuple[str, str]:
    """returns (YYYY-MM-DD, M/D/YYYY) for 'yesterday' in Mexico City timezone"""
    now = datetime.datetime.now(TZ)
    y = (now - datetime.timedelta(days=1)).date()
    date_iso = y.strftime("%Y-%m-%d")
    date_slash = f"{y.month}/{y.day}/{y.year}"
    return date_iso, date_slash


def load_config(service) -> Tuple[Dict[str, dict], dict, dict]:
    """
    Reads Config_Plants A2:O (15 cols)
    Returns:
      plants_config[p_key] = {brand, kwp, target, name, lat, lon, expected_factor, secrets...}
      huawei_map[stationCode] = p_key
      growatt_map[plantId] = p_key
    """
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="Config_Plants!A2:O200"
    ).execute()
    rows = res.get("values", [])

    plants_config = {}
    huawei_map = {}
    growatt_map = {}

    for row in rows:
        if len(row) < 10:
            continue

        p_key = str(row[0]).strip()
        if not p_key:
            continue

        brand = str(row[1]).strip().upper()

        # Config_Plants columns:
        # C kWp_DC
        # E Latitude
        # F Longtitude
        # G ExpectedFactor
        # H PR_Target
        # I CustomerName
        # J SiteID
        kwp = safe_float(row[2])
        lat = safe_float(row[4])
        lon = safe_float(row[5])
        expected_factor = safe_float(row[6], 0.8)
        target_pr = safe_float(row[7])
        name = str(row[8]).strip()
        site_id = str(row[9]).strip()

        # Secret name columns (K,L,M in your sheet)
        secret_api = str(row[10]).strip() if len(row) > 10 else ""
        secret_user = str(row[11]).strip() if len(row) > 11 else ""
        secret_pass = str(row[12]).strip() if len(row) > 12 else ""

        plants_config[p_key] = {
            "brand": brand,
            "kwp": kwp,
            "expected_factor": expected_factor,   # ✅ NOW USED
            "target_pr": target_pr,
            "name": name,
            "lat": lat,
            "lon": lon,
            "site_id": site_id,
            "secret_api": secret_api,
            "secret_user": secret_user,
            "secret_pass": secret_pass,
        }

        if brand == "HUAWEI" and site_id:
            huawei_map[site_id] = p_key
        elif brand == "GROWATT" and site_id:
            growatt_map[site_id] = p_key

    return plants_config, huawei_map, growatt_map


def read_rawdata_index(service, date_slash: str) -> Dict[str, int]:
    """
    Build map: Plantkey -> row_number in sheet (1-based)
    Reads RawData A:J (expects col A date, col B plantkey)
    """
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range="RawData!A2:J2000"
    ).execute()
    rows = res.get("values", [])

    idx = {}
    for i, row in enumerate(rows, start=2):
        if len(row) >= 2 and row[0] == date_slash:
            idx[str(row[1]).strip()] = i
    return idx


def write_rows(service, rows_to_write: List[List], raw_idx: Dict[str, int]) -> Tuple[int, int]:
    """
    Updates existing rows by date+plantkey, appends missing.
    Returns (updated_count, appended_count)
    """
    updates = []
    appends = []

    for r in rows_to_write:
        p_key = r[1]
        if p_key in raw_idx:
            rn = raw_idx[p_key]
            updates.append({"range": f"RawData!A{rn}:J{rn}", "values": [r]})
        else:
            appends.append(r)

    updated = 0
    appended = 0

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()
        updated = len(updates)

    if appends:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="RawData!A2",
            valueInputOption="USER_ENTERED",
            body={"values": appends},
        ).execute()
        appended = len(appends)

    return updated, appended


def compute_row(date_slash: str, p_key: str, conf: dict, energy_kwh: float, irr_kwh_m2: float, clouds_pct: float, transfer: str) -> List:
    kwp = conf["kwp"]
    expected_factor = conf.get("expected_factor", 0.8)  # ✅ per plant
    possible = round(kwp * irr_kwh_m2 * expected_factor, 2) if (kwp > 0 and irr_kwh_m2 > 0) else 0.0
    pr = round(energy_kwh / (kwp * irr_kwh_m2), 3) if (kwp > 0 and irr_kwh_m2 > 0) else 0.0

    return [
        date_slash,                  # A Data
        p_key,                       # B Plantkey
        conf["name"],                # C CustomerName
        round(float(energy_kwh), 2), # D Real_kWh
        round(float(irr_kwh_m2), 3), # E Irradiance_kWh_m2
        possible,                    # F Possible_Gen_kWh
        pr,                          # G Real_PR
        conf["target_pr"],           # H Target_PR
        round(float(clouds_pct), 1), # I Cloud Cover (%)
        transfer                     # J Transfer (YES/NO)
    ]


def main():
    print("--- 🌟 ARGIA SOLAR MONITORING (Daily) ---")
    date_iso, date_slash = target_dates()
    print(f"📅 Target date: {date_iso} / {date_slash} (America/Mexico_City)")

    service = get_service()
    plants_config, huawei_map, growatt_map = load_config(service)

    # ---- Weather for each plant (real API)
    weather_map = {}
    for p_key, conf in plants_config.items():
        irr, clouds = weather.get_weather_for_date(conf["lat"], conf["lon"], date_iso)
        weather_map[p_key] = (irr, clouds)

    # ---- Inverter production (first pass)
    prod_map: Dict[str, float] = {}

    if huawei_map:
        prod_map.update(huawei.fetch_huawei_day_kwh(date_iso, huawei_map, plants_config))

    if growatt_map:
        prod_map.update(growatt.fetch_growatt_data(date_iso, growatt_map, plants_config))

    missing = [k for k in plants_config.keys() if safe_float(prod_map.get(k, 0.0)) <= 0]
    if missing:
        print(f"⚠️ Missing/zero production for: {missing}. Retrying only those plants...")

        for attempt in (1, 2):
            time.sleep(3 * attempt)

            h_map2 = {conf["site_id"]: p for p, conf in plants_config.items() if p in missing and conf["brand"] == "HUAWEI"}
            g_map2 = {conf["site_id"]: p for p, conf in plants_config.items() if p in missing and conf["brand"] == "GROWATT"}

            if h_map2:
                prod_map.update(huawei.fetch_huawei_day_kwh(date_iso, h_map2, plants_config))
            if g_map2:
                prod_map.update(growatt.fetch_growatt_data(date_iso, g_map2, plants_config))

            missing = [k for k in plants_config.keys() if safe_float(prod_map.get(k, 0.0)) <= 0]
            print(f"🔁 After retry {attempt}: still missing = {missing}")
            if not missing:
                break

    # ---- Dummy fallback (final)
    dummy_used = []
    if missing:
        dummy_used = missing[:]
        for p in missing:
            prod_map[p] = growatt.dummy_kwh_for(p)
        print(f"🟠 Dummy used for plants: {dummy_used} (Transfer=NO)")

    # ---- Build rows to write
    rows_to_write = []
    for p_key, conf in plants_config.items():
        energy = safe_float(prod_map.get(p_key, 0.0))
        irr, clouds = weather_map.get(p_key, (0.0, 0.0))
        transfer = "NO" if p_key in dummy_used else "YES"
        rows_to_write.append(compute_row(date_slash, p_key, conf, energy, irr, clouds, transfer))

    # ---- Write (update existing or append)
    raw_idx = read_rawdata_index(service, date_slash)
    updated, appended = write_rows(service, rows_to_write, raw_idx)
    print(f"✅ Sync complete for {date_slash}. Updated: {updated}, Appended: {appended}")


if __name__ == "__main__":
    main()
