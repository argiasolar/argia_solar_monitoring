# argia_weather.py
from __future__ import annotations

import os
import json
import requests
from typing import Tuple, Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build


SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")


def get_service():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def _safe_float(x, default=0.0) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return default


def _get_lat_lon_for_plant(plant_key: str) -> Tuple[float, float]:
    service = get_service()
    res = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Config_Plants!A2:F500"
    ).execute()
    rows = res.get("values", [])
    for r in rows:
        if len(r) >= 6 and str(r[0]).strip() == plant_key:
            lat = _safe_float(r[4])
            lon = _safe_float(r[5])
            return lat, lon
    return 0.0, 0.0


def get_weather_for_date(plant_key: str, date_iso: str) -> Tuple[float, float]:
    """
    Returns:
      irradiance_kwh_m2 (kWh/m2/day), cloudcover_mean (%)
    """
    lat, lon = _get_lat_lon_for_plant(plant_key)
    if not lat or not lon:
        return 0.0, 0.0

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "shortwave_radiation_sum,cloudcover_mean",
        "timezone": "auto",
        "start_date": date_iso,
        "end_date": date_iso,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        js = r.json()
        daily = js.get("daily", {})
        sw = (daily.get("shortwave_radiation_sum") or [0])[0]  # MJ/m2
        cc = (daily.get("cloudcover_mean") or [0])[0]          # %
        irr_kwh = round(_safe_float(sw) / 3.6, 3)             # MJ -> kWh
        clouds = round(_safe_float(cc), 1)
        return irr_kwh, clouds
    except Exception:
        return 0.0, 0.0
