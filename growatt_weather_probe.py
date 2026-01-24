#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import os, sys, json, time, random
from typing import Any, Dict, List, Optional, Tuple
import requests

DEFAULT_BASES = [
    "https://openapi.growatt.com",
    "https://openapi-us.growatt.com",
    "https://openapi-in.growatt.com",
    "https://openapi-au.growatt.com",
    "https://openapi-oss.growatt.com",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ArgiaGrowattProbe/1.3",
    "Accept": "application/json, text/plain, */*",
    "Connection": "keep-alive",
})
TIMEOUT = 25

CACHE_FILE = os.environ.get("GROWATT_PROBE_CACHE", ".growatt_probe_cache.json")
CACHE_TTL_SECONDS = int(os.environ.get("GROWATT_PROBE_CACHE_TTL", "3600"))

# Device-level probes (we already know inverter/last_new_data works)
DEVICE_ENDPOINTS = [
    ("/v1/device/inverter/last_new_data", {"device_sn": None}),
    # keep a couple of generic guesses (some regions have these)
    ("/v1/device/overview", {"device_sn": None}),
    ("/v1/device/detail", {"device_sn": None}),
]

# Plant-level probes (often where weather/sensor aggregates live)
PLANT_ENDPOINTS = [
    "/v1/plant/detail",
    "/v1/plant/overview",
    "/v1/plant/weather",
    "/v1/plant/env",
    "/v1/plant/sensor",
    "/v1/plant/power",
    "/v1/plant/data",
    "/v1/plant/last_data",
    "/v1/plant/last_new_data",
]


def jprint(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def is_rate_limited(js: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(js, dict):
        return False
    return js.get("error_code") == 10012 or str(js.get("error_msg", "")).lower() == "error_frequently_access"


def request_openapi(base: str, path: str, token: str, params: Optional[Dict[str, Any]] = None) -> Tuple[int, str, Optional[Dict[str, Any]]]:
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


def request_with_backoff(base: str, path: str, token: str, params: Dict[str, Any], max_attempts: int = 6, base_sleep: float = 2.0) -> Tuple[int, str, Optional[Dict[str, Any]]]:
    last = (0, "", None)
    for attempt in range(1, max_attempts + 1):
        last = request_openapi(base, path, token, params)
        code, text, js = last
        if not is_rate_limited(js):
            return last
        sleep_s = (base_sleep * (2 ** (attempt - 1))) + random.uniform(0.0, 1.5)
        print(f"⏳ Rate-limited (10012) on {path}. Backing off {sleep_s:.1f}s (attempt {attempt}/{max_attempts})")
        time.sleep(sleep_s)
    return last


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
        for k in ("devices", "items", "list", "rows", "data"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def find_sn_fields(rec: Dict[str, Any]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, v in rec.items():
        if not isinstance(v, (str, int)):
            continue
        if "sn" in k.lower() or "serial" in k.lower():
            out.append((k, str(v).strip()))
    return out


def summarize_records(records: List[Dict[str, Any]], max_items: int = 20) -> None:
    print(f"   Found {len(records)} records")
    for i, rec in enumerate(records[:max_items]):
        parts = []
        for k in ("type", "model", "manufacturer", "status", "lost"):
            if k in rec and rec.get(k) not in ("", None):
                parts.append(f"{k}={rec.get(k)}")
        sn_fields = find_sn_fields(rec)
        if sn_fields:
            parts.append("SNs=" + ",".join([f"{k}:{v}" for k, v in sn_fields[:4]]))
        print(f"   - [{i+1}] " + " | ".join(parts))
    if len(records) > max_items:
        print(f"   ... truncated (showing {max_items}/{len(records)})")


def locate_weather_candidates(records: List[Dict[str, Any]], weather_sn: str) -> List[Dict[str, Any]]:
    ws = weather_sn.strip()
    out = []
    for rec in records:
        for _, v in find_sn_fields(rec):
            if v.strip() == ws:
                out.append(rec)
                break
    return out


def extract_device_sn(rec: Dict[str, Any]) -> Optional[str]:
    for k in ("device_sn", "deviceSn", "sn"):
        v = rec.get(k)
        if v:
            return str(v).strip()
    sn_fields = find_sn_fields(rec)
    return sn_fields[0][1] if sn_fields else None


def try_parse_nested_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value
    if not (s.startswith("{") or s.startswith("[")):
        return value
    try:
        v = json.loads(s)
    except Exception:
        return value
    if isinstance(v, str):
        s2 = v.strip()
        if s2.startswith("{") or s2.startswith("["):
            try:
                return json.loads(s2)
            except Exception:
                return v
    return v


def print_data_shape(js: Dict[str, Any]) -> None:
    data = js.get("data")
    parsed = try_parse_nested_json(data)

    if parsed is not data:
        print("    ✅ data was a string; parsed nested JSON.")

    if isinstance(parsed, dict):
        keys = list(parsed.keys())
        print(f"    data keys ({len(keys)}): {keys[:80]}")
    elif isinstance(parsed, list):
        print(f"    data list length: {len(parsed)}")
        if parsed and isinstance(parsed[0], dict):
            keys = list(parsed[0].keys())
            print(f"    data[0] keys ({len(keys)}): {keys[:80]}")
    else:
        print(f"    data type: {type(parsed).__name__} sample: {str(parsed)[:180]}")


def probe_device(base: str, token: str, device_sn: str) -> None:
    print(f"\n🔎 Device probe for device_sn={device_sn} ...")
    for path, params in DEVICE_ENDPOINTS:
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

        time.sleep(0.25)


def probe_plant(base: str, token: str, plant_id: str) -> None:
    print(f"\n🌿 Plant-level probe for plant_id={plant_id} ...")
    for path in PLANT_ENDPOINTS:
        code, text, js = request_with_backoff(base, path, token, {"plant_id": plant_id}, max_attempts=5, base_sleep=1.5)

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

        time.sleep(0.25)


def main() -> None:
    token = os.environ.get("GROWATT_API_TOKEN", "").strip()
    if not token:
        print("❌ Missing env var: GROWATT_API_TOKEN")
        sys.exit(2)

    plant_id = os.environ.get("GROWATT_PLANT_ID", "10069072").strip()
    weather_sn = os.environ.get("GROWATT_WEATHER_SN", "DYD1EZR007").strip()

    bases = [os.environ["GROWATT_OPENAPI_BASE"].strip()] if os.environ.get("GROWATT_OPENAPI_BASE") else DEFAULT_BASES
    base = bases[0]

    print("=== Growatt Weather Probe ===")
    print(f"Plant ID: {plant_id}")
    print(f"Weather Station SN: {weather_sn}")
    print(f"Base candidates: {bases}")
    print(f"DEBUG_JSON: {os.environ.get('GROWATT_DEBUG_JSON','0')}")

    # Fetch device list with cache/backoff
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
        if is_rate_limited(js):
            print("❌ Still rate-limited after retries (10012). Try again later.")
            if os.environ.get("GROWATT_DEBUG_JSON") == "1":
                jprint(js)
            sys.exit(1)
        save_cache(js)

    if os.environ.get("GROWATT_DEBUG_JSON") == "1":
        print("\n[DEBUG] Full device/list JSON:")
        jprint(js)

    devices = extract_records(js)
    summarize_records(devices)

    # 1) Plant-level probe first (highest chance to contain env aggregates)
    probe_plant(base, token, plant_id)

    # 2) Weather station candidate (datalogger_sn match)
    candidates = locate_weather_candidates(devices, weather_sn)
    print(f"\n🎯 Weather-station candidates matched: {len(candidates)}")
    if candidates:
        rec = candidates[0]
        print("\n--- Weather Candidate 1 ---")
        print("SN fields:", find_sn_fields(rec)[:12])
        device_sn = extract_device_sn(rec)
        print("Picked device_sn:", device_sn)
        if device_sn:
            probe_device(base, token, device_sn)

    # 3) Inverter probe (we already saw it works)
    inverter_sns = []
    for rec in devices:
        sn = str(rec.get("device_sn", "")).strip()
        t = str(rec.get("type", "")).strip()
        if not sn or sn.lower() in ("env", "meter"):
            continue
        if t in ("1", "4"):
            inverter_sns.append(sn)

    inverter_sns = list(dict.fromkeys(inverter_sns))
    if inverter_sns:
        print(f"\n📌 Probing inverter-like SNs too: {inverter_sns}")
        for sn in inverter_sns[:2]:
            probe_device(base, token, sn)

    print("\n✅ Probe completed.")


if __name__ == "__main__":
    main()
