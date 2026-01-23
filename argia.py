import os
import json
import time
import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
TZ_NAME = os.environ.get("TZ_NAME", "America/Mexico_City")

RAW_RANGE = "RawData!A2:J5000"       # A..J (J = Transfer)
CONFIG_RANGE = "Config_Plants!A1:O200"

# Gdy nie ma historii dla dummy:
DUMMY_FALLBACK = {
    "SLP1": 609,
    "SLP2": 986,
    "GTO1": 2259,
    "MEX1": 2174,
    "NL1": 2463,
    "MEX2": 2448,
}

def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS env var")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def safe_float(value, default=0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return default

def fmt_mdy(d: datetime.date) -> str:
    # 1/22/2026 (bez zer wiodących)
    return f"{d.month}/{d.day}/{d.year}"

def get_yesterday_dates() -> Tuple[str, str]:
    tz = ZoneInfo(TZ_NAME)
    now = datetime.datetime.now(tz=tz)
    yday = (now - datetime.timedelta(days=1)).date()
    return yday.isoformat(), fmt_mdy(yday)

def read_sheet(service, rng: str) -> List[List[str]]:
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=rng
    ).execute()
    return res.get("values", [])

def batch_update(service, data: List[Dict[str, Any]]):
    if not data:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SHEET_ID,
        body={"valueInputOption": "USER_ENTERED", "data": data}
    ).execute()

def append_rows(service, rng: str, rows: List[List[Any]]):
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=SHEET_ID,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": rows}
    ).execute()

def parse_config(config_rows: List[List[str]]) -> Dict[str, Dict[str, Any]]:
    """
    Zwraca dict:
      {Plantkey: {brand, kwp_dc, expected_factor, pr_target, customer, site_id, lat, lon, secret_user, secret_pass}}
    """
    if not config_rows:
        return {}

    header = config_rows[0]
    idx = {name.strip(): i for i, name in enumerate(header)}

    def get(row, col, default=""):
        i = idx.get(col)
        if i is None or i >= len(row):
            return default
        return row[i]

    plants = {}
    for row in config_rows[1:]:
        plantkey = str(get(row, "Plantkey", "")).strip()
        if not plantkey:
            continue

        brand = str(get(row, "Brand", "")).strip().upper()
        site_id = str(get(row, "SiteID", "")).strip()
        lat = safe_float(get(row, "Latitude", None), 0.0)
        lon = safe_float(get(row, "Longtitude", None), 0.0)

        plants[plantkey] = {
            "brand": brand,
            "site_id": site_id,
            "kwp_dc": safe_float(get(row, "kWp_DC", 0)),
            "expected_factor": safe_float(get(row, "ExpectedFactor", 0.8), 0.8),
            "pr_target": safe_float(get(row, "PR_Target", 0.85), 0.85),
            "customer": str(get(row, "CustomerName", "")).strip(),
            "lat": lat,
            "lon": lon,
            "secret_user": str(get(row, "SecretUser_Name", "")).strip(),
            "secret_pass": str(get(row, "SecretPass_Name", "")).strip(),
        }

    return plants

def load_existing_index(service, date_slash: str) -> Dict[str, int]:
    """
    Mapuje Plantkey -> numer wiersza w RawData (1-indexed w Sheets), dla danej daty.
    """
    rows = read_sheet(service, RAW_RANGE)
    index = {}
    base_row = 2  # bo A2 start
    for i, r in enumerate(rows):
        if len(r) < 2:
            continue
        if str(r[0]).strip() == date_slash:
            pk = str(r[1]).strip()
            if pk:
                index[pk] = base_row + i
    return index

def compute_dummy_kwh(service, plantkey: str, date_slash: str) -> float:
    """
    Dummy = mediana z ostatnich ~20 wpisów dla plantkey (przed dzisiejszą datą),
    a jak nie ma historii -> fallback stały.
    """
    rows = read_sheet(service, "RawData!A2:D5000")
    vals = []
    for r in rows:
        if len(r) < 4:
            continue
        d = str(r[0]).strip()
        pk = str(r[1]).strip()
        if pk != plantkey or d == date_slash:
            continue
        kwh = safe_float(r[3], 0)
        if kwh > 0:
            vals.append(kwh)

    vals = vals[-20:]
    if vals:
        vals_sorted = sorted(vals)
        mid = len(vals_sorted) // 2
        if len(vals_sorted) % 2 == 1:
            return float(vals_sorted[mid])
        return float((vals_sorted[mid - 1] + vals_sorted[mid]) / 2.0)

    return float(DUMMY_FALLBACK.get(plantkey, 500))

def build_row(date_slash: str, plantkey: str, conf: Dict[str, Any], energy_kwh: float,
              irr_kwh_m2: float, cloud_pct: float, transfer_flag: str) -> List[Any]:
    kwp = conf["kwp_dc"]
    expected_factor = conf["expected_factor"]

    possible = round(kwp * irr_kwh_m2 * expected_factor, 2) if (kwp > 0 and irr_kwh_m2 > 0) else 0
    pr = round(energy_kwh / (kwp * irr_kwh_m2), 3) if (kwp > 0 and irr_kwh_m2 > 0 and energy_kwh > 0) else 0

    return [
        date_slash,
        plantkey,
        conf["customer"],
        round(float(energy_kwh or 0), 2),
        round(float(irr_kwh_m2 or 0), 3),
        possible,
        pr,
        conf["pr_target"],
        round(float(cloud_pct or 0), 1),
        transfer_flag,  # J = Transfer (YES/NO)
    ]

def main():
    if not SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID env var")

    date_iso, date_slash = get_yesterday_dates()
    print(f"--- 🌟 ARGIA SOLAR MONITORING (Daily) ---")
    print(f"📅 Target date: {date_iso} / {date_slash} ({TZ_NAME})")

    service = get_service()

    # 1) Config
    config = read_sheet(service, CONFIG_RANGE)
    plants = parse_config(config)
    if not plants:
        print("❌ No plants found in Config_Plants")
        return

    # 2) Weather (cache per plant)
    weather_map: Dict[str, Tuple[float, float]] = {}
    for pk, conf in plants.items():
        irr, clouds = weather.get_weather_for_date(conf["lat"], conf["lon"], date_iso, TZ_NAME)
        weather_map[pk] = (irr, clouds)

    # 3) Production (group by brand + creds)
    prod_map: Dict[str, float] = {}

    # Huawei groups by (user_env, pass_env)
    huawei_groups: Dict[Tuple[str, str], Dict[str, str]] = {}
    growatt_groups: Dict[Tuple[str, str], Dict[str, str]] = {}

    for pk, conf in plants.items():
        brand = conf["brand"]
        sid = conf["site_id"]
        if not sid:
            continue

        user_env = conf["secret_user"] or (f"{brand}_USERNAME" if brand in ("HUAWEI", "GROWATT") else "")
        pass_env = conf["secret_pass"] or (f"{brand}_PASSWORD" if brand in ("HUAWEI", "GROWATT") else "")

        if brand == "HUAWEI":
            huawei_groups.setdefault((user_env, pass_env), {})[sid] = pk
        elif brand == "GROWATT":
            growatt_groups.setdefault((user_env, pass_env), {})[sid] = pk

    # Fetch Huawei
    for (uenv, penv), mapping in huawei_groups.items():
        user = os.environ.get(uenv) if uenv else os.environ.get("HUAWEI_USERNAME")
        pwd = os.environ.get(penv) if penv else os.environ.get("HUAWEI_PASSWORD")
        prod_map.update(huawei.fetch_huawei_data(date_iso, mapping, user=user, password=pwd))

    # Fetch Growatt
    for (uenv, penv), mapping in growatt_groups.items():
        user = os.environ.get(uenv) if uenv else os.environ.get("GROWATT_USERNAME")
        pwd = os.environ.get(penv) if penv else os.environ.get("GROWATT_PASSWORD")
        prod_map.update(growatt.fetch_growatt_data(date_iso, mapping, user=user, password=pwd))

    # 4) Retry for missing productions
    def missing_keys() -> List[str]:
        miss = []
        for pk, conf in plants.items():
            if conf["brand"] in ("HUAWEI", "GROWATT"):
                if safe_float(prod_map.get(pk, 0), 0) <= 0:
                    miss.append(pk)
        return miss

    misses = missing_keys()
    if misses:
        print(f"⚠️ Missing/zero production for: {misses}. Retrying only those plants...")
        for attempt in range(1, 3):  # 2 dodatkowe próby
            time.sleep(5 * attempt)

            # Rebuild per brand maps just for missing
            h_map = {}
            g_map = {}
            for pk in misses:
                conf = plants[pk]
                sid = conf["site_id"]
                if not sid:
                    continue
                if conf["brand"] == "HUAWEI":
                    h_map[sid] = pk
                elif conf["brand"] == "GROWATT":
                    g_map[sid] = pk

            if h_map:
                prod_map.update(huawei.fetch_huawei_data(date_iso, h_map))
            if g_map:
                prod_map.update(growatt.fetch_growatt_data(date_iso, g_map))

            misses = missing_keys()
            print(f"🔁 After retry {attempt}: still missing = {misses}")
            if not misses:
                break

    # 5) UPSERT rows
    existing_idx = load_existing_index(service, date_slash)

    updates = []
    appends = []

    dummy_used = []

    for pk, conf in plants.items():
        energy = safe_float(prod_map.get(pk, 0), 0)
        irr, clouds = weather_map.get(pk, (0.0, 0.0))

        transfer = "YES"

        if conf["brand"] in ("HUAWEI", "GROWATT") and energy <= 0:
            # dummy fallback
            energy = compute_dummy_kwh(service, pk, date_slash)
            transfer = "NO"
            dummy_used.append(pk)

        row = build_row(date_slash, pk, conf, energy, irr, clouds, transfer)

        if pk in existing_idx:
            rnum = existing_idx[pk]
            updates.append({"range": f"RawData!A{rnum}:J{rnum}", "values": [row]})
        else:
            appends.append(row)

    batch_update(service, updates)
    append_rows(service, "RawData!A2", appends)

    if dummy_used:
        print(f"🟠 Dummy used for plants: {dummy_used} (Transfer=NO)")
    print(f"✅ Sync complete for {date_slash}. Updated: {len(updates)}, Appended: {len(appends)}")

if __name__ == "__main__":
    main()
