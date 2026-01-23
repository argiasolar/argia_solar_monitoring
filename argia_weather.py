# argia_weather.py
import requests
import time
from typing import Tuple

def _safe_float(x, default=0.0) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return default

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    conf = plants_config.get(p_key, {})
    lat = _safe_float(conf.get("lat"))
    lon = _safe_float(conf.get("lon"))

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

    for attempt in range(3):
        try:
            # Zwiększony timeout do 30s
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            js = r.json()
            daily = js.get("daily", {})
            sw = (daily.get("shortwave_radiation_sum") or [0])[0]
            irr_kwh = round(_safe_float(sw) / 3.6, 3)
            clouds = (daily.get("cloudcover_mean") or [0])[0]
            return irr_kwh, _safe_float(clouds)
        except Exception as e:
            print(f"   ⚠️ [Weather] Attempt {attempt+1} failed for {p_key}: {e}")
            time.sleep(3)
            
    return 0.0, 0.0
