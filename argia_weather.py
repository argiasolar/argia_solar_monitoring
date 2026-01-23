# argia_weather.py
import datetime
import requests

def mj_to_kwh(mj_m2: float) -> float:
    return float(mj_m2) / 3.6

def fetch_yesterday_weather(lat: float, lon: float, date_iso: str, timeout: int = 20):
    """
    Returns: (irr_kwh_m2, cloud_cover_percent)
    Uses Open-Meteo archive API for yesterday historical values.
    """
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": date_iso,
        "end_date": date_iso,
        "daily": "shortwave_radiation_sum,cloudcover_mean",
        "timezone": "America/Mexico_City",
    }
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    j = r.json()

    daily = j.get("daily", {}) or {}
    sw = (daily.get("shortwave_radiation_sum") or [None])[0]  # MJ/m2
    cc = (daily.get("cloudcover_mean") or [None])[0]          # %

    irr = round(mj_to_kwh(sw) if sw is not None else 0.0, 3)
    clouds = float(cc) if cc is not None else 0.0
    return irr, clouds
