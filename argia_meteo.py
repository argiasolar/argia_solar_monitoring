from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Optional, Tuple, List

import argia_weather  # <-- reuse your proven Growatt logic


def _latest_radiant_wm2_from_history_rows(rows: List[Dict[str, Any]]) -> float:
    """
    Pick the 'radiant' value from the newest row by calendar timestamp.
    Uses the SAME calendar parsing helper from argia_weather.py.
    """
    best_ts: Optional[dt.datetime] = None
    best_rad: float = 0.0

    for r in rows:
        if not isinstance(r, dict):
            continue

        cal = r.get("calendar") or {}
        if not isinstance(cal, dict):
            continue

        ts = argia_weather._calendar_to_dt(cal)  # uses your existing 0-based month logic
        if not ts:
            continue

        rad = r.get("radiant", None)
        rad_f = argia_weather._safe_float(rad, 0.0)
        if rad_f < 0:
            rad_f = 0.0

        if best_ts is None or ts > best_ts:
            best_ts = ts
            best_rad = rad_f

    return best_rad


def get_growatt_irradiance_kwh_m2_10min(
    plant_id: str,
    prefer_sn: Optional[str] = None,
    prefer_addr: Optional[int] = None,
) -> float:
    """
    10-min irradiation (kWh/m²) using Growatt weather station data,
    via the SAME working GrowattWebClient flow in argia_weather.py.

    Steps:
      1) reuse argia_weather._get_growatt_client() (login/cookies)
      2) reuse argia_weather._get_or_pick_env_device() (sn/addr selection)
      3) call getEnvHistory for today (single page)
      4) take latest 'radiant' (W/m²)
      5) convert to 10-min kWh/m²: W/m² / 6000
    """
    cli = argia_weather._get_growatt_client()
    if cli is None:
        return 0.0

    picked = argia_weather._get_or_pick_env_device(cli, plant_id, prefer_sn, prefer_addr)
    if not picked:
        return 0.0

    sn, addr = picked
    day_iso = dt.date.today().isoformat()

    # One page is enough for "latest reading" (no pagination complications)
    # If Growatt returns rows out of order, we still choose newest by calendar timestamp.
    js = cli.get_env_history(plant_id, sn, addr, day_iso, start=0)
    obj = js.get("obj") or {}
    rows = obj.get("datas") or []
    if not isinstance(rows, list) or not rows:
        return 0.0

    radiant_wm2 = _latest_radiant_wm2_from_history_rows(rows)

    # Convert instantaneous W/m² to 10-min kWh/m²
    return round(radiant_wm2 / 6000.0, 6)


def get_meteo_for_plant_10min(p_key: str, plants_config: dict) -> Tuple[float, float]:
    """
    Drop-in style helper if you want to keep a similar signature as before.

    Returns: (irradiance_kwh_m2_10min, clouds_fraction)

    - Irradiance now comes from Growatt weather station (via argia_weather pipeline).
    - Cloud cover: keep existing behavior (if you still want it during the day),
      or return 0.0 if you don't need clouds in 10-min sync.
    """
    conf = plants_config.get(p_key, {}) if isinstance(plants_config, dict) else {}
    brand = str(conf.get("brand") or "").strip().upper()
    site_id = str(conf.get("site_id") or "").strip()

    fallback_plant = argia_weather._env("GROWATT_WEATHER_FALLBACK_PLANT_ID", "10069072")

    irr_plant_id = site_id if (brand == "GROWATT" and site_id) else str(fallback_plant)

    prefer_sn: Optional[str] = None
    prefer_addr: Optional[int] = None
    if irr_plant_id == str(fallback_plant):
        prefer_sn = argia_weather._env("GROWATT_WEATHER_FALLBACK_DATALOG_SN", "DYD1EZR007")
        try:
            prefer_addr = int(argia_weather._env("GROWATT_WEATHER_FALLBACK_ADDR", "32") or "32")
        except Exception:
            prefer_addr = None

    irr_10m = 0.0
    if str(irr_plant_id).isdigit():
        irr_10m = get_growatt_irradiance_kwh_m2_10min(str(irr_plant_id), prefer_sn, prefer_addr)

    # Optional: keep clouds calculation or set to 0.0 for simplicity
    lat = conf.get("lat")
    lon = conf.get("lon")
    clouds = 0.0
    if lat and lon:
        # If you want clouds during the day too, reuse argia_weather's existing Open-Meteo function
        clouds = argia_weather._avg_cloudcover_7_19_from_open_meteo(float(lat), float(lon), dt.date.today().isoformat()) / 100.0

    return irr_10m, clouds
