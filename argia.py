# argia.py
import os, json, time, datetime as dt
from typing import Dict, List, Tuple
from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")

def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

def safe_float(value, default=0.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except:
        return default

def date_strings_for_today() -> Tuple[dt.date, str, str]:
    # Mexico City UTC-6
    now_local = dt.datetime.utcnow() + dt.timedelta(hours=-6)
    today = now_local.date()
    date_iso = today.strftime("%Y-%m-%d")
    date_slash = f"{today.month}/{today.day}/{today.year}"
    return today, date_iso, date_slash

def read_config(service) -> Tuple[Dict[str, dict], Dict[str, str], Dict[str, str]]:
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Config_Plants!A2:O200",
    ).execute()
    rows = res.get("values", [])
    plants_config, huawei_map, growatt_map = {}, {}, {}
    for row in rows:
        if len(row) < 10: continue
        p_key = str(row[0]).strip()
        brand = str(row[1]).strip().upper()
        site_id = str(row[9]).strip()
        plants_config[p_key] = {
            "brand": brand, "site_id": site_id, "kwp_dc": safe_float(row[2]),
            "lat": row[4] if len(row) > 4 else 0,
            "lon": row[5] if len(row) > 5 else 0,
            "expected_factor": safe_float(row[6], 0.8),
            "pr_target": safe_float(row[7], 0.85),
            "customer_name": str(row[8]).strip(),
        }
        if brand == "HUAWEI": huawei_map[site_id] = p_key
        elif brand == "GROWATT": growatt_map[site_id] = p_key
    return plants_config, huawei_map, growatt_map

def apply_dummy(rows: List[List], missing_keys: List[str]):
    dummy_map = {
        "SLP1": 625, "SLP2": 892, "GTO1": 2359,
        "MEX1": 1872, "NL1": 1512, "MEX2": 2400,
    }
    for r in rows:
        p_key = r[1]
        if p_key in missing_keys or safe_float(r[3]) <= 0:
            r[3] = float(dummy_map.get(p_key, 500))
            r[9] = "NO" # Flaga Transfer
            print(f"   🟠 [Dummy] Applied for {p_key}: {r[3]} kWh")

def build_rows(date_slash: str, date_iso: str, plants_config: dict, prod_map: dict) -> List[List]:
    rows = []
    for p_key, conf in plants_config.items():
        irr, clouds = weather.get_weather_for_date(p_key, date_iso, plants_config)
        kwp = conf["kwp_dc"]
        energy = float(prod_map.get(p_key, 0.0))
        possible = round(kwp * irr * conf["expected_factor"], 2) if (kwp > 0 and irr > 0) else 0.0
        pr = round(energy / (kwp * irr), 3) if (kwp > 0 and irr > 0 and energy > 0) else 0.0
        rows.append([date_slash, p_key, conf["customer_name"], energy, irr, possible, pr, conf["pr_target"], clouds, "YES"])
    return rows

def upsert_rawdata(service, rows_to_write: List[List]):
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID, range="RawData!A2",
        valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
        body={"values": rows_to_write}
    ).execute()

def main():
    print("--- 🌟 ARGIA SOLAR MONITORING (Evening Sync with Delays) ---")
    service = get_service()
    _, date_iso, date_slash = date_strings_for_today()
    print(f"📅 Target: {date_iso}")
    
    plants_config, huawei_map, growatt_map = read_config(service)
    prod_map = {}

    if huawei_map:
        prod_map.update(huawei.fetch_huawei_day_kwh(date_iso, huawei_map, plants_config))
    
    if huawei_map and growatt_map:
        print("   ⏳ Cooling down 5s between providers...")
        time.sleep(5)

    if growatt_map:
        prod_map.update(growatt.fetch_growatt_day_kwh(date_iso, growatt_map, plants_config))
    
    rows = build_rows(date_slash, date_iso, plants_config, prod_map)
    missing = [r[1] for r in rows if safe_float(r[3]) <= 0]
    if missing:
        apply_dummy(rows, missing)
    
    upsert_rawdata(service, rows)
    print(f"✅ Sync complete for {date_slash}")

if __name__ == "__main__":
    main()
