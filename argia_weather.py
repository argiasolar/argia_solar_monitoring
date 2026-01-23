# argia_weather.py
import requests
from typing import Tuple

def _safe_float(x, default=0.0) -> float:
    try:
        return float(str(x).strip().replace(",", "."))
    except Exception:
        return default

def get_weather_for_date(p_key: str, date_iso: str, plants_config: dict) -> Tuple[float, float]:
    """
    Pobiera irradiancję (kWh/m2) i chmury (%) z Open-Meteo.
    Współrzędne bierze z przekazanego słownika plants_config.
    """
    conf = plants_config.get(p_key, {})
    lat = _safe_float(conf.get("lat"))
    lon = _safe_float(conf.get("lon"))

    if not lat or not lon:
        print(f"   ⚠️ [Weather] No Lat/Lon for {p_key}")
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
        
        # Konwersja MJ/m2 -> kWh/m2 (dzielenie przez 3.6)
        sw = (daily.get("shortwave_radiation_sum") or [0])[0]
        irr_kwh = round(_safe_float(sw) / 3.6, 3)
        
        cc = (daily.get("cloudcover_mean") or [0])[0]
        clouds = round(_safe_float(cc), 1)
        
        return irr_kwh, clouds
    except Exception as e:
        print(f"   ❌ [Weather] Error for {p_key}: {e}")
        return 0.0, 0.0
