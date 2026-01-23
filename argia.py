# argia.py
from __future__ import annotations

import os
import json
import datetime as dt
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build

import argia_weather as weather
import argia_huawei as huawei
import argia_growatt as growatt

TZ = ZoneInfo("America/Mexico_City")

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
RAW_RANGE_READ = "RawData!A2:J2000"          # A..J (J = Transfer)
RAW_APPEND_RANGE = "RawData!A2"
CONFIG_RANGE = "Config_Plants!A2:O200"       # jak u Ciebie


def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS")
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


def _env_by_name(name: str | None) -> str | None:
    if not name:
        return None
    return os.environ.get(name)


def _yesterday_dates() -> Tuple[str, str, dt.date]:
    now_local = dt.datetime.now(TZ)
    y = (now_local - dt.timedelta(days=1)).date()
    date_iso = y.strftime("%Y-%m-%d")
    # Google Sheets u Ciebie ma format M/D/YYYY
    date_slash = f"{y.month}/{y.day}/{y.year}"
    return date_iso, date_slash, y


def load_config(service) -> Tuple[Dict[str, Any], Dict[str, str], Dict[str, str]]:
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=CONFIG_RANGE
    ).execute()
    rows = res.get("values", [])

    plants_config: Dict[str, Any] = {}
    huawei_map: Dict[str, str] = {}
    growatt_map: Dict[str, str] = {}

    # kolumny wg Twojej tabeli:
    # A Plantkey
    # B Brand
    # C kWp_DC
    # G ExpectedFactor
    # H PR_Target
    # I CustomerName
    # J SiteID
    # K SecretName_API
    # L SecretUser_Name
    # M SecretPass_Name
    # E Latitude
    # F Longitude
    for row in rows:
        if len(row) < 10:
            continue

        plantkey = str(row[0]).strip()
        brand = str(row[1]).strip().upper()
        kwp_dc = safe_float(row[2])
        lat = safe_float(row[4], None) if len(row) > 4 else None
        lon = safe_float(row[5], None) if len(row) > 5 else None
        expected_factor = safe_float(row[6], 0.8) if len(row) > 6 else 0.8
        pr_target = safe_float(row[7], 0.85) if len(row) > 7 else 0.85
        customer = str(row[8]).strip() if len(row) > 8 else ""
        site_id = str(row[9]).strip()

        secret_api = str(row[10]).strip() if len(row) > 10 and row[10] else ""
        secret_user = str(row[11]).strip() if len(row) > 11 and row[11] else ""
        secret_pass = str(row[12]).strip() if len(row) > 12 and row[12] else ""

        plants_config[plantkey] = {
            "brand": brand,
            "kwp": kwp_dc,
            "lat": lat,
            "lon": lon,
            "expected_factor": expected_factor,
            "target_pr": pr_target,
            "customer": customer,
            "site_id": site_id,
            "secret_api": secret_api,
            "secret_user": secret_user,
            "secret_pass": secret_pass,
        }

        if brand == "HUAWEI":
            huawei_map[site_id] = plantkey
        elif brand == "GROWATT":
            growatt_map[site_id] = plantkey

    return plants_config, huawei_map, growatt_map


def read_raw_rows(service, date_slash: str) -> Tuple[Dict[str, int], List[List[str]]]:
    """
    Returns:
      existing_row_index_by_plantkey: {PlantKey: sheet_row_number}
      all_rows: list of rows from A2:J...
    """
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=RAW_RANGE_READ
    ).execute()
    rows = res.get("values", [])

    index: Dict[str, int] = {}
    # rows[0] corresponds to sheet row 2
    for i, row in enumerate(rows):
        if len(row) < 2:
            continue
        if row[0] == date_slash:
            plantkey = row[1]
            index[plantkey] = i + 2
    return index, rows


def write_rows(service, updates: List[Dict[str, Any]], appends: List[List[Any]]) -> None:
    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": updates},
        ).execute()

    if appends:
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=RAW_APPEND_RANGE,
            valueInputOption="USER_ENTERED",
            body={"values": appends},
        ).execute()


def main():
    print("--- 🌟 ARGIA SOLAR MONITORING (Daily) ---")
    if not SHEET_ID:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    service = get_service()

    date_iso, date_slash, _ = _yesterday_dates()
    print(f"📅 Target date: {date_iso} / {date_slash} (America/Mexico_City)")

    plants_config, huawei_map, growatt_map = load_config(service)

    # --- 1) Produkcja: pierwsze pobranie
    prod: Dict[str, float] = {}

    # Huawei creds z configu: bierzemy pierwszą sensowną parę sekretów (u Ciebie wspólne)
    h_user = None
    h_pass = None
    for pk, c in plants_config.items():
        if c["brand"] == "HUAWEI":
            h_user = _env_by_name(c["secret_user"]) or os.environ.get("HUAWEI_USERNAME")
            h_pass = _env_by_name(c["secret_pass"]) or os.environ.get("HUAWEI_PASSWORD")
            break

    # Growatt token (preferowany) wg configu: SecretName_API albo env default
    g_token = None
    for pk, c in plants_config.items():
        if c["brand"] == "GROWATT":
            g_token = _env_by_name(c["secret_api"]) or os.environ.get("GROWATT_API_TOKEN")
            break

    g_user = os.environ.get("GROWATT_USERNAME")
    g_pass = os.environ.get("GROWATT_PASSWORD")

    if huawei_map:
        prod.update(huawei.fetch_huawei_day_kwh(date_iso, huawei_map, h_user, h_pass))

    if growatt_map:
        prod.update(growatt.fetch_growatt_day_kwh(
            date_iso,
            growatt_map,
            token=g_token,
            username=g_user,
            password=g_pass,
        ))

    # --- 2) Retry logic: jeśli braki/zera, próbujemy max 2 razy jeszcze, tylko brakujące
    def missing_plants(p: Dict[str, float]) -> List[str]:
        out = []
        for plantkey in plants_config.keys():
            v = p.get(plantkey, 0.0)
            if v is None or float(v) <= 0:
                out.append(plantkey)
        return out

    missing = missing_plants(prod)
    if missing:
        print(f"⚠️ Missing/zero production for: {missing}. Retrying only those plants...")

    retries = 2
    for attempt in range(1, retries + 1):
        if not missing:
            break

        # budujemy mapy SiteID->PlantKey tylko dla missing
        h_map2 = {sid: pk for sid, pk in huawei_map.items() if pk in missing}
        g_map2 = {sid: pk for sid, pk in growatt_map.items() if pk in missing}

        if h_map2:
            prod.update(huawei.fetch_huawei_day_kwh(date_iso, h_map2, h_user, h_pass, attempt=attempt))
        if g_map2:
            prod.update(growatt.fetch_growatt_day_kwh(
                date_iso,
                g_map2,
                token=g_token,
                username=g_user,
                password=g_pass,
                attempt=attempt,
            ))

        missing = missing_plants(prod)
        print(f"🔁 After retry {attempt}: still missing = {missing}")

    # --- 3) Pogoda per plant (lat/lon z Config_Plants)
    wx_cache: Dict[str, Tuple[float, float]] = {}
    for pk, conf in plants_config.items():
        lat, lon = conf.get("lat"), conf.get("lon")
        if lat is None or lon is None:
            wx_cache[pk] = (0.0, 0.0)
        else:
            irr_kwh_m2, cloud_mean = weather.get_daily_irradiance_and_clouds(
                date_iso=date_iso,
                latitude=lat,
                longitude=lon,
                tz="America/Mexico_City",
            )
            wx_cache[pk] = (irr_kwh_m2, cloud_mean)

    # --- 4) Zapis do RawData: update jeśli już istnieje, append jeśli nie
    existing_index, _ = read_raw_rows(service, date_slash)

    updates = []
    appends = []

    # mapka dummy (ostatnia deska ratunku)
    dummy_map = {
        "SLP1": 609,
        "SLP2": 986,
        "GTO1": 2259,
        "MEX1": 2174,
        "NL1": 2463,
        "MEX2": 2448,
    }

    updated_count = 0
    appended_count = 0

    for pk, conf in plants_config.items():
        energy = float(prod.get(pk, 0.0) or 0.0)
        irr, clouds = wx_cache.get(pk, (0.0, 0.0))

        # forecast: kWp * irr(kWh/m2) * expected_factor
        forecast = round(conf["kwp"] * irr * conf["expected_factor"], 2) if conf["kwp"] and irr else 0.0
        pr = round(energy / (conf["kwp"] * irr), 3) if conf["kwp"] and irr and energy > 0 else 0.0

        transfer = "YES"
        if energy <= 0:
            # jeśli po retry dalej 0 -> dummy
            energy = float(dummy_map.get(pk, 500))
            pr = round(energy / (conf["kwp"] * irr), 3) if conf["kwp"] and irr else 0.0
            transfer = "NO"

        row_values = [
            date_slash,             # A
            pk,                     # B
            conf["customer"],       # C
            round(energy, 2),        # D
            round(irr, 3),           # E
            forecast,               # F
            pr,                     # G
            conf["target_pr"],      # H
            round(clouds, 1),        # I
            transfer,               # J
        ]

        if pk in existing_index:
            r = existing_index[pk]
            # update całego A..J w wierszu (prosto i stabilnie)
            updates.append({"range": f"RawData!A{r}:J{r}", "values": [row_values]})
            updated_count += 1
        else:
            appends.append(row_values)
            appended_count += 1

    if any(v and float(v) <= 0 for v in prod.values()):
        print("🟠 Dummy used for some plants (Transfer=NO)")

    write_rows(service, updates, appends)
    print(f"✅ Sync complete for {date_slash}. Updated: {updated_count}, Appended: {appended_count}")


if __name__ == "__main__":
    main()
