import requests
from typing import Tuple

def get_weather_for_date(lat: float, lon: float, date_iso: str, tz_name: str) -> Tuple[float, float]:
    """
    Zwraca:
      (Irradiance_kWh_m2, CloudCover_%)

    Używa Open-Meteo Forecast API z past_days=1 (żeby działało dla "wczoraj").
    Jeśli API nie zwróci danych -> fallback (0 + 0).
    """
    if not lat or not lon:
        return 0.0, 0.0

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "shortwave_radiation_sum,cloud_cover_mean",
        "timezone": tz_name,
        "past_days": 1,
        "forecast_days": 1,
    }

    try:
        r = requests.get(url, params=params, timeout=20)
        j = r.json()

        daily = j.get("daily", {})
        times = daily.get("time", [])
        if date_iso not in times:
            # czasem API zwróci inny zakres – po prostu spróbuj znaleźć pierwszy element
            if times:
                idx = 0
            else:
                return 0.0, 0.0
        else:
            idx = times.index(date_iso)

        rad = daily.get("shortwave_radiation_sum", [0])[idx]
        cloud = daily.get("cloud_cover_mean", [0])[idx]

        # Unit-safe: Open-Meteo zwraca daily_units – jeżeli MJ/m² to przeliczamy na kWh/m²
        units = j.get("daily_units", {})
        rad_unit = units.get("shortwave_radiation_sum", "")

        rad_val = float(rad or 0)
        if "mj" in rad_unit.lower():
            rad_val = rad_val / 3.6  # MJ/m² -> kWh/m²

        return round(rad_val, 3), round(float(cloud or 0), 1)

    except Exception:
        return 0.0, 0.0
