from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import argia_weather  # reuse the proven Growatt + Open-Meteo logic


def _latest_radiant_wm2(rows: List[Dict[str, Any]]) -> float:
    """
    Pick radiant (W/m²) from the newest row by calendar timestamp.
    Uses argia_weather._calendar_to_dt and argia_weather._safe_float (working logic).
    """
    best_ts: Optional[dt.datetime] = None
    best_rad: float = 0.0

    for r in rows:
        if not isinstance(r, dict):
            continue
        cal = r.get("calendar") or {}
        if not isinstance(cal, dict):
            continue

        ts = argia_weather._calendar_to_dt(cal)
        if not ts:
            continue

        rad = argia_weather._safe_float(r.get("radiant"), 0.0)
        if rad < 0:
            rad = 0.0

        if best_ts is None or ts > best_ts:
            best_ts = ts
            best_rad = rad

    return best_rad


def _interval_kwh_m2_from_radiant_wm2(radiant_wm2: float, interval_min: int) -> float:
    """
    Convert instantaneous irradiance (W/m²) into kWh/m² over interval minutes:
      kWh/m² = W/m² * (interval_min/60) / 1000 = W/m² * interval_min / 60000
    """
    if radiant_wm2 <= 0 or interval_min <= 0:
        return 0.0
    return float(radiant_wm2) * float(interval_min) / 60000.0


def _growatt_interval_irradiance_kwh_m2(
    plant_id: str,
    when: dt.datetime,
    interval_min: int,
    prefer_sn: Optional[str],
    prefer_addr: Optional[int],
) -> float:
    """
    Uses the SAME GrowattWebClient from argia_weather.py:
      - argia_weather._get_growatt_client()
      - argia_weather._get_or_pick_env_device()
      - cli.get_env_history(...)
    Then takes the latest "radiant" value and converts to interval kWh/m².
    """
    cli = argia_weather._get_growatt_client()
    if cli is None:
        return 0.0

    picked = argia_weather._get_or_pick_env_device(cli, plant_id, prefer_sn, prefer_addr)
    if not picked:
        return 0.0
    sn, addr = picked

    day_iso = when.date().isoformat()

    js = cli.get_env_history(plant_id, sn, addr, day_iso, start=0)
    obj = js.get("obj") or {}
    rows = obj.get("datas") or []
    if not isinstance(rows, list) or not rows:
        return 0.0

    radiant_wm2 = _latest_radiant_wm2(rows)
    return round(_interval_kwh_m2_from_radiant_wm2(radiant_wm2, interval_min), 6)


# ============================
# PUBLIC API (required by argia_sync.py)
# ============================

def get_meteo_snapshot(
    plants_config: dict,
    plant_key: str,
    siteid: str,
    when: dt.datetime,
    interval_min: int,
) -> Tuple[float, float]:
    """
    Expected by argia_sync.py

    Returns:
      (irradiance_kwh_m2_for_interval, cloud_fraction)

    Irradiance source:
      - Growatt weather station (EnvHistory radiant) via argia_weather GrowattWebClient.
      - For Huawei plants, use fallback Growatt plant/station env vars (same as argia_weather.py).

    Cloud cover:
      - Reuse argia_weather._avg_cloudcover_7_19_from_open_meteo (fraction 0..1)
        based on lat/lon from plants_config for that plant_key.
      - If lat/lon missing, returns 0.0.
    """
    conf = plants_config.get(plant_key, {}) if isinstance(plants_config, dict) else {}

    brand = str(conf.get("brand") or "").strip().upper()
    site_id = str(siteid or conf.get("site_id") or "").strip()

    # Same fallback mechanism as argia_weather.py
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

    irr = 0.0
    if str(irr_plant_id).isdigit():
        irr = _growatt_interval_irradiance_kwh_m2(
            plant_id=str(irr_plant_id),
            when=when,
            interval_min=int(interval_min),
            prefer_sn=prefer_sn,
            prefer_addr=prefer_addr,
        )

    # Cloud cover fraction (0..1), same method you already use daily
    lat = conf.get("lat")
    lon = conf.get("lon")
    cloud_frac = 0.0
    if lat and lon:
        cloud_frac = argia_weather._avg_cloudcover_7_19_from_open_meteo(
            float(lat), float(lon), when.date().isoformat()
        ) / 100.0

    return irr, cloud_frac
