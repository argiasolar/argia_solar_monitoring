#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
growatt_weather_probe.py
------------------------------------------------------------
Purpose:
  Discover whether Growatt OpenAPI exposes your weather-station telemetry,
  and which endpoint/fields are available.

How to run (locally):
  export GROWATT_API_TOKEN="..."
  python growatt_weather_probe.py

How to run (GitHub Actions):
  Add workflow_dispatch step and set env secrets.

Env:
  GROWATT_API_TOKEN (required)
  GROWATT_OPENAPI_BASE (optional) e.g. https://openapi.growatt.com
  GROWATT_PLANT_ID (optional) e.g. 10069072
  GROWATT_WEATHER_SN (optional) e.g. DYD1EZR007
  GROWATT_DEBUG_JSON (optional) 1 to print full JSON (careful)
------------------------------------------------------------
"""

from __future__ import annotations

import os
import sys
import json
import time
from typing import Any, Dict, List, Optional, Tuple
import requests


DEFAULT_BASES = [
    "https://openapi.growatt.com",
    "https://openapi-us.growatt.com",
    "https://openapi-in.growatt.com",
    "https://openapi-au.growatt.com",
    "https://openapi-oss.growatt.com",
]

CANDIDATE_ENDPOINTS = [
    # Device lists
    ("/v1/device/list", {"plant_id": None}),
    ("/v1/device/datalogger/list", {"plant_id": None}),
    # Realtime endpoints we can try on a device_sn
    ("/v1/device/inverter/last_new_data", {"device_sn": None}),
    ("/v1/device/last_data", {"device_sn": None}),
    ("/v1/device/data", {"device_sn": None}),
    ("/v1/device/datalogger/last_data", {"device_sn": None}),
    ("/v1/device/datalogger/last_new_data", {"device_sn": None}),
    # Sometimes weather is grouped as “enviroment monitor” or “sensor”
    ("/v1/device/sensor/last_data", {"device_sn": None}),
    ("/v1/device/env/last_data", {"device_sn": None}),
]

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "ArgiaGrowattProbe/1.0",
        "Accept": "application/json, text/plain, */*",
        "Connection": "keep-alive",
    }
)

TIMEOUT = 25


def jprint(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def safe_get(d: Any, key: str, default=None):
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def normalize_sn(val: Any) -> str:
    return str(val).strip()


def extract_possible_sn_records(payload: Any) -> List[Dict[str, Any]]:
    """
    Growatt responses vary. Often:
      {"data":[{...}, {...}], "msg":"", "code":0}
    or:
      {"data":{"items":[...]}, ...}
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        # common container names
        for k in ("items", "list", "rows", "devices", "deviceList"):
            if isinstance(data.get(k), list):
                return [x for x in data.get(k) if isinstance(x, dict)]
    return []


def find_sn_fields(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    """
    Return list of (field_name, value) that looks like a serial number.
    """
    sn_fields = []
    for k, v in rec.items():
        if not isinstance(v, (str, int)):
            continue
        ks = k.lower()
        if "sn" in ks or "serial" in ks:
            sn_fields.append((k, normalize_sn(v)))
    return sn_fields


def request_openapi(
    base: str,
    path: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    """
    Try multiple token placements because Growatt integrations differ:
      - Header: token
      - Header: Authorization Bearer
      - Query: token
    We do one request with headers + query token to maximize odds.
    """
    url = base.rstrip("/") + path
    params = dict(params or {})
    # include token as query param too (best-effort)
    params["token"] = token

    headers = {
        "token": token,
        "Authorization": f"Bearer {token}",
    }
    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT)
        text = r.text[:800]  # truncate
        try:
            js = r.json()
        except Exception:
            js = None
        return r.status_code, text, js
    except Exception as e:
        return 0, f"EXCEPTION: {e}", None


def pick_base_working(token: str, plant_id: str, bases: List[str]) -> Optional[str]:
    """
    Find first base that returns something sensible for device/list.
    """
    for base in bases:
        code, _, js = request_openapi(base, "/v1/device/list", token, {"plant_id": plant_id})
        ok = (code == 200) and isinstance(js, dict)
        if ok:
            # Some APIs return {"code":1,"msg":"token error"} while still 200
            api_code = js.get("code")
            msg = str(js.get("msg", ""))
            if api_code in (0, "0", None) and "token" not in msg.lower():
                return base
        print(f"❌ Base not working (or token/perm issue): {base}  HTTP={code}")
    return None


def summarize_records(records: List[Dict[str, Any]], max_items: int = 12) -> None:
    print(f"   Found {len(records)} records")
    for i, rec in enumerate(records[:max_items]):
        sn_fields = find_sn_fields(rec)
        headline = []
        for k in ("type", "deviceType", "model", "deviceModel", "name", "deviceName"):
            if k in rec:
                headline.append(f"{k}={rec.get(k)}")
        if sn_fields:
            headline.append("SNs=" + ",".join([f"{k}:{v}" for k, v in sn_fields[:3]]))
        print(f"   - [{i+1}] " + " | ".join(headline))
    if len(records) > max_items:
        print(f"   ... truncated (showing {max_items}/{len(records)})")


def locate_weather_device(
    records: List[Dict[str, Any]],
    weather_sn: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Return candidates likely to be the weather station.
    If weather_sn provided, match exact. Otherwise try to guess by type/model/name.
    """
    candidates = []
    weather_sn_norm = normalize_sn(weather_sn) if weather_sn else None

    for rec in records:
        # exact match by any SN field
        if weather_sn_norm:
            for _, v in find_sn_fields(rec):
                if normalize_sn(v) == weather_sn_norm:
                    candidates.append(rec)
                    break
        else:
            # heuristic
            blob = json.dumps(rec, ensure_ascii=False).lower()
            if any(x in blob for x in ["weather", "enviro", "environment", "meteo", "sensor", "irradiance", "radiation"]):
                candidates.append(rec)

    return candidates


def extract_device_sn(rec: Dict[str, Any]) -> Optional[str]:
    """
    Try common key names used for device serials.
    """
    for k in ("device_sn", "deviceSn", "sn", "deviceSerial", "serialNum", "dataLogSn", "datalogSn", "dataloggerSn"):
        v = rec.get(k)
        if v:
            return normalize_sn(v)
    # fallback: any SN-like field
    sn_fields = find_sn_fields(rec)
    if sn_fields:
        return sn_fields[0][1]
    return None


def probe_device_endpoints(base: str, token: str, device_sn: str) -> None:
    print(f"\n🔎 Probing telemetry endpoints for device_sn={device_sn} ...")

    # Try multiple candidate endpoints that *might* exist
    for path, params in CANDIDATE_ENDPOINTS:
        if "device_sn" not in (params or {}):
            continue
        p = dict(params)
        p["device_sn"] = device_sn
        code, text, js = request_openapi(base, path, token, p)

        # only print interesting results
        if code in (200, 400, 401, 403, 404):
            msg = ""
            api_code = None
            if isinstance(js, dict):
                api_code = js.get("code")
                msg = str(js.get("msg", ""))
            print(f" - {path}  HTTP={code}  api_code={api_code}  msg={msg[:120]}")
            if code == 200 and isinstance(js, dict):
                data = js.get("data")
                # show keys for fast mapping
                if isinstance(data, dict):
                    print(f"    data keys: {list(data.keys())[:40]}")
                elif isinstance(data, list) and data and isinstance(data[0], dict):
                    print(f"    data[0] keys: {list(data[0].keys())[:40]}")
                else:
                    print(f"    data type: {type(data).__name__}")
                if os.environ.get("GROWATT_DEBUG_JSON") == "1":
                    jprint(js)
        else:
            print(f" - {path}  HTTP={code}  (nonstandard)  body={text[:120]}")

        time.sleep(0.2)


def main() -> None:
    token = os.environ.get("GROWATT_API_TOKEN", "").strip()
    if not token:
        print("❌ Missing env var: GROWATT_API_TOKEN")
        sys.exit(2)

    plant_id = os.environ.get("GROWATT_PLANT_ID", "10069072").strip()
    weather_sn = os.environ.get("GROWATT_WEATHER_SN", "DYD1EZR007").strip()

    bases = [os.environ["GROWATT_OPENAPI_BASE"].strip()] if os.environ.get("GROWATT_OPENAPI_BASE") else DEFAULT_BASES

    print("=== Growatt Weather Probe ===")
    print(f"Plant ID: {plant_id}")
    print(f"Weather Station SN: {weather_sn}")
    print(f"Base candidates: {bases}")

    base = pick_base_working(token, plant_id, bases)
    if not base:
        print("\n❌ Could not find a working OpenAPI base for device/list.")
        print("   Likely causes:")
        print("   - token invalid / expired")
        print("   - token has no permissions for plant_id")
        print("   - your account is on a different region host not in the list")
        sys.exit(1)

    print(f"\n✅ Using OpenAPI base: {base}")

    # 1) Device list
    print("\n📌 Fetching /v1/device/list ...")
    code, _, js = request_openapi(base, "/v1/device/list", token, {"plant_id": plant_id})
    if code != 200 or not isinstance(js, dict):
        print(f"❌ device/list failed HTTP={code}")
        if js:
            jprint(js)
        sys.exit(1)

    devices = extract_possible_sn_records(js)
    summarize_records(devices)

    # 2) Datalogger list (sometimes weather station appears here)
    print("\n📌 Fetching /v1/device/datalogger/list ...")
    code, _, js2 = request_openapi(base, "/v1/device/datalogger/list", token, {"plant_id": plant_id})
    dlogs = extract_possible_sn_records(js2) if (code == 200 and isinstance(js2, dict)) else []
    summarize_records(dlogs) if dlogs else print("   (No records or endpoint not supported)")

    # 3) Locate weather station record(s)
    all_records = devices + dlogs
    candidates = locate_weather_device(all_records, weather_sn)

    print(f"\n🎯 Weather-station candidates matched: {len(candidates)}")
    if not candidates:
        print("❌ No device record matched the Weather Station SN.")
        print("   Next checks:")
        print("   - verify the SN is exactly as in Growatt UI export (case-sensitive sometimes)")
        print("   - verify plant_id really contains that weather station")
        print("   - token permission for that plant/device")
        sys.exit(1)

    # show candidate(s) and probe endpoints
    for idx, rec in enumerate(candidates[:5], start=1):
        print(f"\n--- Candidate {idx} ---")
        sn_fields = find_sn_fields(rec)
        print("SN fields:", sn_fields[:10])
        device_sn = extract_device_sn(rec)
        print("Picked device_sn:", device_sn)
        if os.environ.get("GROWATT_DEBUG_JSON") == "1":
            jprint(rec)

        if device_sn:
            probe_device_endpoints(base, token, device_sn)
        else:
            print("⚠️ Could not derive device_sn from record; enable GROWATT_DEBUG_JSON=1 and inspect fields.")

    print("\n✅ Probe completed.")


if __name__ == "__main__":
    main()
