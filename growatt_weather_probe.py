#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
growatt_weather_probe.py
------------------------------------------------------------
Purpose:
  Discover whether Growatt OpenAPI exposes your weather-station telemetry,
  and which endpoint/fields are available.

What this probe does:
  1) Finds working OpenAPI base URL.
  2) Calls /v1/device/list for a plant_id.
  3) Locates the weather station by datalogger_sn (your DYD...).
     (In your case the record is usually: device_sn="env", datalogger_sn="DYDxxxx")
  4) Probes multiple telemetry endpoints for:
       - device_sn="env"
       - inverter device_sn(s) found in list (e.g., JGM7DY500G)
       - optionally meter device_sn(s)
  5) If a response contains data as a string, it tries to parse nested JSON.

Env:
  Required:
    GROWATT_API_TOKEN
  Optional:
    GROWATT_OPENAPI_BASE     # if you already know the correct base
    GROWATT_PLANT_ID         # default 10069072
    GROWATT_WEATHER_SN       # default DYD1EZR007
    GROWATT_DEBUG_JSON       # "1" prints full JSON payloads (careful with logs)
    GROWATT_PROBE_ALL_SNS    # "1" probes also meter + any other device_sn
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

# Candidate endpoints worth probing. Many will 404 (that’s OK).
CANDIDATE_ENDPOINTS = [
    # Lists
    ("/v1/device/list", {"plant_id": None}),
    ("/v1/device/datalogger/list", {"plant_id": None}),
    # Telemetry (device_sn)
    ("/v1/device/inverter/last_new_data", {"device_sn": None}),
    ("/v1/device/inverter/invs_data", {"device_sn": None}),
    ("/v1/device/last_data", {"device_sn": None}),
    ("/v1/device/data", {"device_sn": None}),
    ("/v1/device/datalogger/last_data", {"device_sn": None}),
    ("/v1/device/datalogger/last_new_data", {"device_sn": None}),
    ("/v1/device/sensor/last_data", {"device_sn": None}),
    ("/v1/device/env/last_data", {"device_sn": None}),
    # Some undocumented variants people encounter
    ("/v1/device/last_new_data", {"device_sn": None}),
    ("/v1/device/realtime", {"device_sn": None}),
]

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "ArgiaGrowattProbe/1.1",
        "Accept": "application/json, text/plain, */*",
        "Connection": "keep-alive",
    }
)

TIMEOUT = 25


def jprint(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def normalize_str(v: Any) -> str:
    return str(v).strip()


def safe_get(d: Any, key: str, default=None):
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def request_openapi(
    base: str,
    path: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    """
    Try token in:
      - Header "token"
      - Header "Authorization: Bearer"
      - Query param "token"
    """
    url = base.rstrip("/") + path
    params = dict(params or {})
    params["token"] = token  # best-effort

    headers = {
        "token": token,
        "Authorization": f"Bearer {token}",
    }
    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT)
        text = r.text[:1200]  # truncate
        try:
            js = r.json()
        except Exception:
            js = None
        return r.status_code, text, js
    except Exception as e:
        return 0, f"EXCEPTION: {e}", None


def extract_records(payload: Any) -> List[Dict[str, Any]]:
    """
    Growatt responses vary. Common:
      {"data":[{...}, ...], "code":0}
      {"data":{"items":[...]},"code":0}
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("items", "list", "rows", "devices", "deviceList", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def find_sn_fields(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    sn_fields: List[Tuple[str, str]] = []
    for k, v in rec.items():
        if not isinstance(v, (str, int)):
            continue
        ks = k.lower()
        if "sn" in ks or "serial" in ks:
            sn_fields.append((k, normalize_str(v)))
    return sn_fields


def summarize_records(records: List[Dict[str, Any]], max_items: int = 20) -> None:
    print(f"   Found {len(records)} records")
    for i, rec in enumerate(records[:max_items]):
        parts: List[str] = []
        for k in ("type", "deviceType", "model", "deviceModel", "name", "deviceName"):
            if k in rec and rec.get(k) not in ("", None):
                parts.append(f"{k}={rec.get(k)}")
        sn_fields = find_sn_fields(rec)
        if sn_fields:
            parts.append("SNs=" + ",".join([f"{k}:{v}" for k, v in sn_fields[:4]]))
        if not parts:
            parts.append("(no headline fields)")
        print(f"   - [{i+1}] " + " | ".join(parts))
    if len(records) > max_items:
        print(f"   ... truncated (showing {max_items}/{len(records)})")


def locate_weather_candidates(records: List[Dict[str, Any]], weather_sn: Optional[str]) -> List[Dict[str, Any]]:
    weather_sn_norm = normalize_str(weather_sn) if weather_sn else None
    candidates: List[Dict[str, Any]] = []
    for rec in records:
        # Exact match by datalogger_sn or any SN field
        if weather_sn_norm:
            for _, v in find_sn_fields(rec):
                if normalize_str(v) == weather_sn_norm:
                    candidates.append(rec)
                    break
        else:
            # heuristic
            blob = json.dumps(rec, ensure_ascii=False).lower()
            if any(x in blob for x in ["weather", "enviro", "environment", "meteo", "sensor", "irradiance", "radiation"]):
                candidates.append(rec)
    return candidates


def extract_device_sn(rec: Dict[str, Any]) -> Optional[str]:
    for k in ("device_sn", "deviceSn", "sn", "deviceSerial", "serialNum"):
        v = rec.get(k)
        if v:
            return normalize_str(v)
    # fallback: first SN-like field
    sn_fields = find_sn_fields(rec)
    if sn_fields:
        return sn_fields[0][1]
    return None


def try_parse_nested_json(value: Any) -> Any:
    """
    If data is returned as a JSON string, decode it.
    Handles:
      - plain JSON string
      - JSON string inside JSON string (rare but happens)
    """
    if not isinstance(value, str):
        return value

    s = value.strip()
    if not s:
        return value

    # quick guard: if it doesn't look like JSON, return as-is
    if not (s.startswith("{") or s.startswith("[") or (s.startswith('"') and s.endswith('"'))):
        return value

    # attempt 1
    try:
        v1 = json.loads(s)
    except Exception:
        return value

    # attempt 2: sometimes it becomes another JSON string
    if isinstance(v1, str):
        s2 = v1.strip()
        if s2.startswith("{") or s2.startswith("["):
            try:
                v2 = json.loads(s2)
                return v2
            except Exception:
                return v1
    return v1


def print_data_shape(js: Dict[str, Any]) -> None:
    data = js.get("data")
    parsed = try_parse_nested_json(data)

    if parsed is not data:
        print("    ✅ data was a string; parsed nested JSON successfully.")

    if isinstance(parsed, dict):
        keys = list(parsed.keys())
        print(f"    data keys ({len(keys)}): {keys[:60]}")
    elif isinstance(parsed, list):
        if parsed and isinstance(parsed[0], dict):
            keys = list(parsed[0].keys())
            print(f"    data[0] keys ({len(keys)}): {keys[:60]}")
        else:
            print(f"    data list length: {len(parsed)}; element type: {type(parsed[0]).__name__ if parsed else 'n/a'}")
    else:
        # string or number etc.
        sample = str(parsed)
        print(f"    data type: {type(parsed).__name__}  sample: {sample[:180]}")


def probe_device_endpoints(base: str, token: str, device_sn: str) -> None:
    print(f"\n🔎 Probing telemetry endpoints for device_sn={device_sn} ...")

    for path, params in CANDIDATE_ENDPOINTS:
        if not params or "device_sn" not in params:
            continue

        p = dict(params)
        p["device_sn"] = device_sn

        code, text, js = request_openapi(base, path, token, p)

        api_code = None
        msg = ""
        if isinstance(js, dict):
            api_code = js.get("code")
            msg = str(js.get("msg", ""))

        # Print response summary for common HTTPs, including 404 to learn what's supported
        if code in (200, 400, 401, 403, 404):
            print(f" - {path}  HTTP={code}  api_code={api_code}  msg={msg[:140]}")
            if code == 200 and isinstance(js, dict):
                print_data_shape(js)
                if os.environ.get("GROWATT_DEBUG_JSON") == "1":
                    jprint(js)
            elif os.environ.get("GROWATT_DEBUG_JSON") == "1" and isinstance(js, dict):
                # print error json too (useful)
                jprint(js)
        else:
            print(f" - {path}  HTTP={code}  body={text[:140]}")

        time.sleep(0.15)


def pick_base_working(token: str, plant_id: str, bases: List[str]) -> Optional[str]:
    for base in bases:
        code, _, js = request_openapi(base, "/v1/device/list", token, {"plant_id": plant_id})
        if code == 200 and isinstance(js, dict):
            api_code = js.get("code")
            msg = str(js.get("msg", ""))
            # Some implementations return code=0 or omit "code"
            if api_code in (0, "0", None) and "token" not in msg.lower():
                return base
        print(f"❌ Base not working (or token/perm issue): {base}  HTTP={code}")
    return None


def unique_preserve_order(items: List[str]) -> List[str]:
    out = []
    seen = set()
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def main() -> None:
    token = os.environ.get("GROWATT_API_TOKEN", "").strip()
    if not token:
        print("❌ Missing env var: GROWATT_API_TOKEN")
        sys.exit(2)

    plant_id = os.environ.get("GROWATT_PLANT_ID", "10069072").strip()
    weather_sn = os.environ.get("GROWATT_WEATHER_SN", "DYD1EZR007").strip()

    bases = (
        [os.environ["GROWATT_OPENAPI_BASE"].strip()]
        if os.environ.get("GROWATT_OPENAPI_BASE")
        else DEFAULT_BASES
    )

    print("=== Growatt Weather Probe ===")
    print(f"Plant ID: {plant_id}")
    print(f"Weather Station SN: {weather_sn}")
    print(f"Base candidates: {bases}")
    print(f"DEBUG_JSON: {os.environ.get('GROWATT_DEBUG_JSON','0')}")
    print(f"PROBE_ALL_SNS: {os.environ.get('GROWATT_PROBE_ALL_SNS','0')}")

    base = pick_base_working(token, plant_id, bases)
    if not base:
        print("\n❌ Could not find a working OpenAPI base for /v1/device/list.")
        print("Likely causes: invalid token, missing permissions, or different regional host.")
        sys.exit(1)

    print(f"\n✅ Using OpenAPI base: {base}")

    # 1) device/list
    print("\n📌 Fetching /v1/device/list ...")
    code, _, js = request_openapi(base, "/v1/device/list", token, {"plant_id": plant_id})
    if code != 200 or not isinstance(js, dict):
        print(f"❌ device/list failed HTTP={code}")
        if js:
            jprint(js)
        sys.exit(1)

    devices = extract_records(js)
    summarize_records(devices)

    if os.environ.get("GROWATT_DEBUG_JSON") == "1":
        print("\n[DEBUG] Full device/list JSON:")
        jprint(js)

    # 2) datalogger/list (optional)
    print("\n📌 Fetching /v1/device/datalogger/list ...")
    code2, _, js2 = request_openapi(base, "/v1/device/datalogger/list", token, {"plant_id": plant_id})
    dlogs: List[Dict[str, Any]] = []
    if code2 == 200 and isinstance(js2, dict):
        dlogs = extract_records(js2)
        if dlogs:
            summarize_records(dlogs)
        else:
            print("   (No records returned)")
        if os.environ.get("GROWATT_DEBUG_JSON") == "1":
            print("\n[DEBUG] Full datalogger/list JSON:")
            jprint(js2)
    else:
        print("   (No records or endpoint not supported)")

    all_records = devices + dlogs

    # 3) locate weather station record
    candidates = locate_weather_candidates(all_records, weather_sn)

    print(f"\n🎯 Weather-station candidates matched: {len(candidates)}")
    if not candidates:
        print("❌ No device record matched the Weather Station SN.")
        print("Next checks: verify SN exact, verify plant_id contains it, verify token permission.")
        sys.exit(1)

    # probe weather candidate device_sn(s)
    probed_sns: List[str] = []
    for idx, rec in enumerate(candidates[:5], start=1):
        print(f"\n--- Weather Candidate {idx} ---")
        print("SN fields:", find_sn_fields(rec)[:12])
        device_sn = extract_device_sn(rec)
        print("Picked device_sn:", device_sn)
        if os.environ.get("GROWATT_DEBUG_JSON") == "1":
            print("[DEBUG] Candidate record:")
            jprint(rec)

        if device_sn:
            probe_device_endpoints(base, token, device_sn)
            probed_sns.append(device_sn)

    # 4) ALSO probe inverter SN(s) from device list (common place where env fields appear)
    inverter_like: List[str] = []
    meter_like: List[str] = []
    other: List[str] = []

    for rec in devices:
        sn = rec.get("device_sn")
        if not sn:
            continue
        sn = normalize_str(sn)
        t = rec.get("type")
        # Heuristic from your output:
        # - env -> weather logical device
        # - meter -> meter logical device
        # - type 1/4 often inverter variants
        if sn.lower() == "env":
            continue
        if sn.lower() == "meter":
            meter_like.append(sn)
            continue
        if str(t) in ("1", "4"):
            inverter_like.append(sn)
        else:
            other.append(sn)

    inverter_like = unique_preserve_order(inverter_like)
    meter_like = unique_preserve_order(meter_like)
    other = unique_preserve_order(other)

    print("\n📌 Additional device_sn discovered in device/list:")
    print(f"   inverter_like: {inverter_like}")
    print(f"   meter_like: {meter_like}")
    print(f"   other: {other}")

    # Probe inverter_like first (most promising)
    for sn in inverter_like[:5]:
        if sn in probed_sns:
            continue
        probe_device_endpoints(base, token, sn)
        probed_sns.append(sn)

    # Optionally probe meter + other (can be noisy)
    if os.environ.get("GROWATT_PROBE_ALL_SNS") == "1":
        for sn in (meter_like + other)[:8]:
            if sn in probed_sns:
                continue
            probe_device_endpoints(base, token, sn)
            probed_sns.append(sn)

    print("\n✅ Probe completed.")
    print("Next action:")
    print("- Look for any endpoint with HTTP=200 where parsed data keys include irradiance/temp/humidity/wind etc.")
    print("- Paste that JSON or at least the 'data keys' output, and we map it into argia_weather.py.")


if __name__ == "__main__":
    main()
