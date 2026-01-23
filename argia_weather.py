import requests
from functools import lru_cache


@lru_cache(maxsize=512)
def get_weather_for_date(lat: float, lon: float, date_iso: str):
    """
    Returns:
      (irradiance_kwh_m2_day, cloud_cover_percent_mean)
    Using Open-Meteo daily:
      shortwave_radiation_sum (kWh/m²)
      cloud_cover_mean (%)
    """
    if not lat or not lon:
        return 0.0, 0.0

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "shortwave_radiation_sum,cloud_cover_mean",
        "timezone": "America/Mexico_City",
        "start_date": date_iso,
        "end_date": date_iso,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        j = r.json()

        daily = j.get("daily", {})
        irr_list = daily.get("shortwave_radiation_sum", [])
        cloud_list = daily.get("cloud_cover_mean", [])

        irr = float(irr_list[0]) if irr_list else 0.0
        clouds = float(cloud_list[0]) if cloud_list else 0.0
        return round(irr, 3), round(clouds, 1)

    except Exception as e:
        print(f"⚠️ [Weather] Failed lat={lat} lon={lon} date={date_iso}: {e}")
        return 0.0, 0.0
