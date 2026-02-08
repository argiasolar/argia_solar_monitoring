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

import datetime as dt
from typing import Any, Dict, Optional, Tuple

import argia_weather


def get_meteo_snapshot(*args: Any, **kwargs: Any) -> Tuple[float, float]:
    """
    Compatibility adapter for argia_sync.py.

    Supports BOTH call styles:
      A) get_meteo_snapshot(plants_config, plant_key, siteid, when, interval_min, ...)
      B) get_meteo_snapshot(plants=..., plant_key=..., siteid=..., when=..., interval_min=..., plant_id_for_weather=..., ...)
         and many variants (site_id vs siteid, interval_minutes vs interval_min, ts vs when, etc.)

    Returns:
      (irradiance_kwh_m2_for_interval, cloud_fraction)
    """

    # ----------------------------
    # 1) Collect core inputs
    # ----------------------------
    plants_config: Dict[str, Any] = {}
    plant_key: str = ""
    siteid: str = ""
    when: dt.datetime = dt.datetime.utcnow()
    interval_min: int = 10

    # Positional form (old)
    if len(args) >= 5:
        plants_config = args[0] or {}
        plant_key = str(args[1] or "")
        siteid = str(args[2] or "")
        when = args[3] if isinstance(args[3], dt.datetime) else dt.datetime.utcnow()
        try:
            interval_min = int(args[4])
        except Exception:
            interval_min = 10
    else:
        # Keyword form (current)
        plants_config = kwargs.get("plants_config") or kwargs.get("plants") or kwargs.get("config") or {}
        plant_key = str(kwargs.get("plant_key") or kwargs.get("p_key") or kwargs.get("key") or "")
        siteid = str(kwargs.get("siteid") or kwargs.get("site_id") or kwargs.get("siteId") or "")

        w = kwargs.get("when") or kwargs.get("ts") or kwargs.get("dt") or kwargs.get("timestamp")
        if isinstance(w, dt.datetime):
            when = w
        else:
            # If they pass a date string like '2026-02-08', keep it simple: use today's date at current UTC time
            # (We prefer not to invent complexity here.)
            when = dt.datetime.utcnow()

        try:
            interval_min = int(kwargs.get("interval_min") or kwargs.get("interval_minutes") or kwargs.get("interval") or 10)
        except Exception:
            interval_min = 10

    # Optional override for which Growatt plant to use for weather station
    plant_id_for_weather = kwargs.get("plant_id_for_weather") or kwargs.get("weather_plant_id") or kwargs.get("plant_id")

    # ----------------------------
    # 2) Compute irradiance using SAME Growatt pipeline as argia_weather.py
    # ----------------------------
    conf = plants_config.get(plant_key, {}) if isinstance(plants_config, dict) else {}

    brand = str(conf.get("brand") or "").strip().upper()
    fallback_plant = argia_weather._env("GROWATT_WEATHER_FALLBACK_PLANT_ID", "10069072")

    if plant_id_for_weather:
        irr_plant_id = str(plant_id_for_weather).strip()
    else:
        # Same rule as argia_weather.py
        irr_plant_id = siteid if (brand == "GROWATT" and siteid) else str(fallback_plant)

    # Only pin SN/ADDR for the fallback plant (same as argia_weather.py)
    prefer_sn: Optional[str] = None
    prefer_addr: Optional[int] = None
    if irr_plant_id == str(fallback_plant):
        prefer_sn = argia_weather._env("GROWATT_WEATHER_FALLBACK_DATALOG_SN", "DYD1EZR007")
        try:
            prefer_addr = int(argia_weather._env("GROWATT_WEATHER_FALLBACK_ADDR", "32") or "32")
        except Exception:
            prefer_addr = None

    irr_kwh_m2_interval = 0.0
    if str(irr_plant_id).isdigit():
        # Reuse GrowattWebClient + env device selection from argia_weather.py
        cli = argia_weather._get_growatt_client()
        if cli is not None:
            picked = argia_weather._get_or_pick_env_device(cli, str(irr_plant_id), prefer_sn, prefer_addr)
            if picked:
                sn, addr = picked
                day_iso = when.date().isoformat()

                js = cli.get_env_history(str(irr_plant_id), sn, addr, day_iso, start=0)
                obj = js.get("obj") or {}
                rows = obj.get("datas") or []

                # Find newest radiant (W/m²) using argia_weather calendar parsing
                best_ts = None
                best_rad = 0.0
                if isinstance(rows, list):
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

                # Convert W/m² to kWh/m² over interval minutes:
                # kWh/m² = W/m² * interval_min / 60000
                if best_rad > 0 and interval_min > 0:
                    irr_kwh_m2_interval = round(best_rad * float(interval_min) / 60000.0, 6)

    # ----------------------------
    # 3) Clouds (keep same daily method; return fraction 0..1)
    # ----------------------------
    cloud_frac = 0.0
    lat = conf.get("lat") or kwargs.get("lat")
    lon = conf.get("lon") or kwargs.get("lon")
    if lat and lon:
        cloud_frac = argia_weather._avg_cloudcover_7_19_from_open_meteo(float(lat), float(lon), when.date().isoformat()) / 100.0

    return irr_kwh_m2_interval, cloud_frac
