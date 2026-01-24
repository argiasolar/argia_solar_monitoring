#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import sys
import json
import time
import random
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
    ("/v1/device/inverter/last_new_data", {"device_sn": None}),
    ("/v1/device/last_new_data", {"device_sn": None}),
    ("/v1/device/last_data", {"device_sn": None}),
    ("/v1/device/data", {"device_sn": None}),
    ("/v1/device/env/last_data", {"device_sn": None}),
    ("/v1/device/sensor/last_data", {"device_sn": None}),
]

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "ArgiaGrowattProbe/1.2",
        "Accept": "application/json, text/plain, */*",
        "Connection": "keep-alive",
    }
)

TIMEOUT = 25

# Cache (best effort) – avoids slamming device/list repeatedly
CACHE_FILE = os.environ.get("GROWATT_PROBE_CACHE", ".growatt_probe_cache.json")
CACHE_TTL_SECONDS = int(os.environ.get("GROWATT_PROBE_CACHE_TTL", "3600"))  # 1h default


def jprint(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def normalize_str(v: Any) -> str:
    return str(v).strip()


def is_rate_limited(js: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(js, dict):
        return False
    # Growatt OpenAPI rate limit pattern
    return js.get("error_code") == 10012 or str(js.get("error_msg", "")).lower() == "error_frequently_access"


def request_openapi(
    base: str,
    path: str,
    token: str,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    url = base.rstrip("/") + path
    params = dict(params or {})
    params["token"] = token

    headers = {"token": token, "Authorization": f"Bearer {token}"}

    try:
        r = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT)
        text = r.text[:1200]
        try:
            js = r.json()
        except Exception:
            js = None
        return r.status_code, text, js
    except Exception as e:
        return 0, f"EXCEPTION: {e}", None


def request_with_backoff(
    base: str,
    path: str,
    token: str,
    params: Dict[str, Any],
    max_attempts: int = 6,
    base_sleep: float = 2.0,
) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    """
    Retries on Growatt 10012 rate limit with exponential backoff + jitter.
    """
    for attempt in range(1, max_attempts + 1):
        code, text, js = request_openapi(base, path, token, params)
        if not is_rate_limited(js):
            return code, text, js

        sleep_s = (base_sleep * (2 ** (attempt - 1))) + random.uniform(0.0, 1.5)
        print(f"⏳ Rate-limited (10012) on {path}. Backing off {sleep_s:.1f}s (attempt {attempt}/{max_attempts})")
        time.sleep(sleep_s)

    return code, text, js  # last response


def load_cache() -> Optional[Dict[str, Any]]:
    try:
        if not os.path.exists(CACHE_FILE):
            return None
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        ts = obj.get("_saved_at", 0)
        if time.time() - ts > CACHE_TTL_SECONDS:
            return None
        return obj
    except Exception:
        return None


def save_cache(payload: Dict[str, Any]) -> None:
    try:
        payload = dict(payload)
        payload["_saved_at"] = time.time()
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def extract_records(payload: Any) -> List[Dict[str, Any]]:
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
    out: List[Tuple[str, str]] = []
    for k, v in rec.items():
        if not isinstance(v, (str, int)):
            continue
        ks = k.lower()
        if "sn" in ks or "serial" in ks:
            out.append((k, normalize_str(v)))
    return out


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
        print(f"   - [{i+1}] " + " | ".join(parts))
    if len(records) > max_items:
        print(f"   ... truncated (showing {max_items}/{len(records)})")


def locate_weather_candidates(records: List[Dict[str, Any]], weather_sn: str) -> List[Dict[str, Any]]:
    weather_sn_norm = normalize_str(weather_sn)
    candidates: List[Dict[str, Any]] = []
    for rec in records:
        for _, v in find_sn_fields(rec):
            if normalize_str(v) == weather_sn_norm:
                candidates.append(rec)
                break
    return candidates


def extract_device_sn(rec: Dict[str, Any]) -> Optional[str]:
    for k in ("device_sn", "deviceSn", "sn", "deviceSerial", "serialNum"):
        v = rec.get(k)
        if v:
            return normalize_str(v)
    sn_fields = find_sn_fields(rec)
    if sn_fields:
        return sn_fields[0][1]
    return None


def try_parse_nested_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value
    if not (s.startswith("{") or s.startswith("[")):
        return value
    try:
        v1 = json.loads(s)
    except Exception:
        return value
    if isinstance(v1, str):
        s2 = v1.strip()
        if s2.startswith("{") or s2.startswith("["):
            try:
                return json.loads(s2)
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
            print(f"    data list length: {len(parsed)}")
    else:
        print(f"    data type: {type(parsed).__name__}  sample: {str(parsed)[:180]}")


def probe_device_endpoints(base: str, token: str, device_sn: str) -> None:
    print(f"\n🔎 Probing telemetry endpoints for device_sn={device_sn} ...")
    for path, params in CANDIDATE_ENDPOINTS:
        p = dict(params)
        p["device_sn"] = device_sn

        code, text, js = request_with_backoff(base, path, token, p, max_attempts=5, base_sleep=1.5)

        api_code = js.get("code") if isinstance(js, dict) else None
        err_code = js.get("error_code") if isinstance(js, dict) else None
        msg = ""
        if isinstance(js, dict):
            msg = str(js.get("msg", "")) or str(js.get("error_msg", ""))

        print(f" - {path}  HTTP={code}  api_code={api_code}  error_code={err_code}  msg={msg[:140]}")
        if code == 200 and isinstance(js, dict):
            print_data_shape(js)
            if os.environ.get("GROWATT_DEBUG_JSON") == "1":
                jprint(js)
        elif os.environ.get("GROWATT_DEBUG_JSON") == "1" and isinstance(js, dict):
            jprint(js)
        elif code != 200 and os.environ.get("GROWATT_DEBUG_JSON") == "1":
            print("    body:", text[:200])

        time.sleep(0.25)  # gentle pacing


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
    print(f"DEBUG_JSON: {os.environ.get('GROWATT_DEBUG_JSON','0')}")

    base = bases[0]  # your probe already confirmed openapi.growatt.com works
    # If you want auto-pick again, we can add it back, but it costs extra calls.

    # Try cache first to avoid rate-limit
    cached = load_cache()
    if cached:
        print(f"\n🧠 Using cached device/list from {CACHE_FILE} (TTL={CACHE_TTL_SECONDS}s)")
        js = cached
    else:
        print("\n📌 Fetching /v1/device/list ...")
        code, text, js = request_with_backoff(base, "/v1/device/list", token, {"plant_id": plant_id}, max_attempts=6, base_sleep=2.0)
        if code != 200 or not isinstance(js, dict):
            print(f"❌ device/list failed HTTP={code} body={text[:200]}")
            sys.exit(1)

        # If rate limited even after backoff, show and exit
        if is_rate_limited(js):
            print("❌ Still rate-limited after retries. Try again later (or reduce frequency).")
            if os.environ.get("GROWATT_DEBUG_JSON") == "1":
                jprint(js)
            sys.exit(1)

        save_cache(js)

    if os.environ.get("GROWATT_DEBUG_JSON") == "1":
        print("\n[DEBUG] Full device/list JSON:")
        jprint(js)

    devices = extract_records(js)
    summarize_records(devices)

    if not devices:
        # If we got here with 0 records, it’s usually rate limiting or permission weirdness
        print("\n⚠️ No records returned. If JSON shows error_code=10012 then it's rate limiting.")
        sys.exit(1)

    candidates = locate_weather_candidates(devices, weather_sn)
    print(f"\n🎯 Weather-station candidates matched: {len(candidates)}")
    if not candidates:
        print("❌ No device record matched the Weather Station SN (check plant_id / SN).")
        sys.exit(1)

    # Probe weather candidate device_sn
    for idx, rec in enumerate(candidates[:3], start=1):
        print(f"\n--- Weather Candidate {idx} ---")
        print("SN fields:", find_sn_fields(rec)[:12])
        device_sn = extract_device_sn(rec)
        print("Picked device_sn:", device_sn)
        if device_sn:
            probe_device_endpoints(base, token, device_sn)

    # ALSO probe inverter-like SNs (often contains env data)
    inverter_sns = []
    for rec in devices:
        sn = normalize_str(rec.get("device_sn", ""))
        t = str(rec.get("type", "")).strip()
        if not sn or sn.lower() in ("env", "meter"):
            continue
        if t in ("1", "4"):
            inverter_sns.append(sn)

    inverter_sns = list(dict.fromkeys(inverter_sns))
    if inverter_sns:
        print(f"\n📌 Probing inverter-like SNs too: {inverter_sns}")
        for sn in inverter_sns[:3]:
            probe_device_endpoints(base, token, sn)

    print("\n✅ Probe completed.")


if __name__ == "__main__":
    main()
