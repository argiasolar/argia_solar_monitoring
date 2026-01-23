# argia_weather.py
import requests
import time
from typing import Tuple

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    conf = plants_config.get(p_key, {})
    lat, lon = conf.get("lat"), conf.get("lon")
    if not lat or not lon: return 0.0, 0.0

    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": lat, "longitude": lon,
        "daily": "shortwave_radiation_sum,cloudcover_mean",
        "timezone": "auto", "start_date": date_iso, "end_date": date_iso,
    }

    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 200:
                js = r.json()
                sw = (js.get("daily", {}).get("shortwave_radiation_sum") or [0])[0]
                clouds = (js.get("daily", {}).get("cloudcover_mean") or [0])[0]
                return round(float(sw)/3.6, 3), round(float(clouds), 1)
        except:
            time.sleep(2)
    return 0.0, 0.0
