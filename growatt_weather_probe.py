#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Growatt Weather Probe (ShineServer Web endpoints)

Goal:
- Login to https://server.growatt.com (web)
- Use assToken cookie + headers (makeToken/permissionsKey) like the UI
- Probe likely endpoints and print JSON + attempt to extract irradiance-like fields

Env:
  GROWATT_USERNAME
  GROWATT_PASSWORD
  GROWATT_PLANT_ID        (e.g. 10069072)
  GROWATT_WEATHER_SN      (e.g. DYD1EZR007)  # datalogger_sn shown in OpenAPI device/list
Optional:
  GROWATT_SERVER_BASE     default https://server.growatt.com
  DEBUG_JSON              1/0
  TIMEOUT_SEC             default 25
  CACHE_TTL_SEC           default 1800
"""

import os
import sys
import json
import time
import re
from urllib.parse import urljoin

import requests


DEFAULT_BASE = os.getenv("GROWATT_SERVER_BASE", "https://server.growatt.com").rstrip("/")
DEBUG_JSON = os.getenv("DEBUG_JSON", "1") == "1"
TIMEOUT = int(os.getenv("TIMEOUT_SEC", "25"))
CACHE_TTL = int(os.getenv("CACHE_TTL_SEC", "1800"))

USERNAME = os.getenv("GROWATT_USERNAME", "")
PASSWORD = os.getenv("GROWATT_PASSWORD", "")
PLANT_ID = os.getenv("GROWATT_PLANT_ID", "")
WEATHER_SN = os.getenv("GROWATT_WEATHER_SN", "")

CACHE_FILE = ".growatt_server_probe_cache.json"


def _now() -> float:
    return time.time()


def _print_json(title: str, obj):
    if not DEBUG_JSON:
        return
    print(f"\n[DEBUG] {title}:")
    try:
        print(json.dumps(obj, indent=2, ensure_ascii=False))
    except Exception:
        print(str(obj))


def _load_cache():
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved = float(data.get("_saved_at", 0))
        if _now() - saved <= CACHE_TTL:
            return data
    except Exception:
        return None
    return None


def _save_cache(payload: dict):
    try:
        payload["_saved_at"] = _now()
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _req(sess: requests.Session, method: str, path: str, **kwargs):
    url = urljoin(DEFAULT_BASE + "/", path.lstrip("/"))
    kwargs.setdefault("timeout", TIMEOUT)
    return sess.request(method, url, **kwargs)


def login_server_web(sess: requests.Session) -> dict:
    """
    We try a few known/likely login endpoints because Growatt has variants by region/version.
    We first GET /login to obtain cookies (JSESSIONID), then POST candidates.
    """
    if not USERNAME or not PASSWORD:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    print(f"🌐 Server base: {DEFAULT_BASE}")
    print("🔐 Fetching login page to seed cookies...")
    r = _req(sess, "GET", "/login")
    # Some deployments redirect /login -> /login or /index; accept anything 200/302
    print(f"   GET /login -> HTTP {r.status_code}")

    # Candidate POST endpoints and payload formats.
    # We don’t assume; we try and detect success by presence of assToken cookie or JSON success=true.
    candidates = [
        ("/login", {"account": USERNAME, "password": PASSWORD}),
        ("/login", {"userName": USERNAME, "password": PASSWORD}),
        ("/login/doLogin", {"account": USERNAME, "password": PASSWORD}),
        ("/login/doLogin", {"userName": USERNAME, "password": PASSWORD}),
        ("/user/login", {"account": USERNAME, "password": PASSWORD}),
        ("/user/login", {"userName": USERNAME, "password": PASSWORD}),
    ]

    headers = {
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "user-agent": "Mozilla/5.0",
        "accept": "application/json, text/javascript, */*; q=0.01",
    }

    for path, form in candidates:
        try:
            print(f"➡️  Trying POST {path} with keys={list(form.keys())} ...")
            rr = _req(sess, "POST", path, data=form, headers=headers, allow_redirects=False)
            ct = rr.headers.get("content-type", "")
            ass = sess.cookies.get("assToken")
            jsid = sess.cookies.get("JSESSIONID")
            print(f"   HTTP {rr.status_code} | ct={ct.split(';')[0]} | JSESSIONID={'Y' if jsid else 'N'} | assToken={'Y' if ass else 'N'}")

            # If JSON, inspect success markers.
            if "application/json" in ct:
                try:
                    j = rr.json()
                    _print_json(f"login response {path}", j)
                    if j.get("success") is True or j.get("result") in ("1", 1) or (ass is not None):
                        print("✅ Login looks successful (JSON markers / assToken present).")
                        return {"ok": True, "path": path, "json": j}
                except Exception:
                    pass

            # If redirect and we got assToken -> good
            if rr.status_code in (301, 302, 303, 307, 308) and ass:
                print("✅ Login looks successful (redirect + assToken cookie).")
                return {"ok": True, "path": path, "json": None}

            # If HTML but assToken exists -> also ok
            if ass:
                print("✅ Login looks successful (assToken cookie present).")
                return {"ok": True, "path": path, "json": None}

        except Exception as e:
            print(f"   ⚠️  Login attempt failed: {e}")

    raise RuntimeError("Login failed on all candidate endpoints. (No assToken, no success JSON)")


def get_permissions_like_ui(sess: requests.Session) -> dict:
    """
    In UI iframe code you pasted, the child page receives:
      permissionsKey, commonMakeToken
    In your network capture, deviceTypeTree works with cookies only.
    But masterSet/* calls often expect request headers:
      makeToken: <assToken or commonMakeToken>
      permissionsKey: <string>
    We’ll try to discover a permissionsKey by probing a couple endpoints and parsing JSON.
    If not found, we fall back to empty permissionsKey (sometimes tolerated).
    """
    ass = sess.cookies.get("assToken") or ""
    if not ass:
        raise RuntimeError("No assToken cookie; cannot proceed")

    # Some builds expose current user/permission payload somewhere.
    candidates = [
        ("/masterSet/deviceTypeTree", "GET", None),
        ("/user/getUserRight", "GET", None),
        ("/user/getUserInfo", "GET", None),
        ("/index/getUserInfo", "GET", None),
    ]

    headers = {
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "user-agent": "Mozilla/5.0",
        # mimic UI (may be optional)
        "makeToken": ass,
        "permissionsKey": "",
    }

    found_key = ""
    for path, method, payload in candidates:
        try:
            r = _req(sess, method, path, headers=headers, data=payload)
            ct = r.headers.get("content-type", "")
            print(f"🧩 Perm probe {method} {path} -> HTTP {r.status_code} ct={ct.split(';')[0]}")
            if "application/json" in ct:
                j = r.json()
                _print_json(f"perm probe {path}", j)

                # Heuristic: look for permissionsKey-ish fields anywhere
                blob = json.dumps(j, ensure_ascii=False)
                m = re.search(r"(permissionsKey|permissionKey|permKey)[\"']?\s*[:=]\s*[\"']([^\"']+)[\"']", blob, re.IGNORECASE)
                if m:
                    found_key = m.group(2)
                    break
        except Exception as e:
            print(f"   ⚠️  perm probe error: {e}")

    return {"assToken": ass, "permissionsKey": found_key}


def masterset_read_device(sess: requests.Session, datalog_sn: str, perm: dict) -> dict:
    """
    Calls the same endpoint the UI uses:
      POST /masterSet/readDevice
    with headers makeToken + permissionsKey.
    """
    ass = perm.get("assToken", "") or ""
    pkey = perm.get("permissionsKey", "") or ""

    headers = {
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "user-agent": "Mozilla/5.0",
        "makeToken": ass,
        "permissionsKey": pkey,
    }

    data = {"datalogSn": datalog_sn}
    r = _req(sess, "POST", "/masterSet/readDevice", headers=headers, data=data)
    ct = r.headers.get("content-type", "")
    print(f"📡 POST /masterSet/readDevice datalogSn={datalog_sn} -> HTTP {r.status_code} ct={ct.split(';')[0]}")
    if "application/json" not in ct:
        print("   ⚠️  Non-JSON response (blocked / changed endpoint). Showing first 200 chars:")
        print(r.text[:200])
        return {"ok": False, "raw": r.text[:200], "status": r.status_code}

    j = r.json()
    _print_json("readDevice response", j)
    return j


def guess_irradiance_fields(device_list: list) -> dict:
    """
    We don’t know field names yet. We scan for common weather keys.
    """
    if not device_list:
        return {"found": []}

    keys_of_interest = [
        "irr", "irradiance", "radiation", "solar", "poa", "ghi",
        "cloud", "cloudy", "cover",
        "temp", "temperature",
        "wind", "humidity", "rain",
        "light", "lux",
    ]

    found = []
    for dev in device_list:
        flat = json.dumps(dev, ensure_ascii=False).lower()
        if any(k in flat for k in keys_of_interest):
            found.append(dev)

    return {"found": found, "count": len(found)}


def main():
    print("=== Growatt Weather Probe (Web API) ===")
    print(f"Plant ID: {PLANT_ID}")
    print(f"Weather Station SN: {WEATHER_SN}")
    if not WEATHER_SN:
        print("❌ Missing GROWATT_WEATHER_SN (datalogger_sn).")
        sys.exit(2)

    cache = _load_cache()
    if cache:
        print(f"🧠 Using cached probe result from {CACHE_FILE} (TTL={CACHE_TTL}s)")
        _print_json("cached", cache)
        sys.exit(0)

    with requests.Session() as sess:
        sess.headers.update({"accept-language": "en-US,en;q=0.9"})
        # Step 1: login (get assToken cookie)
        login_server_web(sess)

        ass = sess.cookies.get("assToken")
        jsid = sess.cookies.get("JSESSIONID")
        print(f"🍪 Cookies: JSESSIONID={'Y' if jsid else 'N'} | assToken={'Y' if ass else 'N'}")
        if not ass:
            print("❌ Login did not yield assToken cookie.")
            sys.exit(1)

        # Step 2: discover permissionsKey if possible
        perm = get_permissions_like_ui(sess)
        print(f"🔑 permissionsKey: {'(none)' if not perm.get('permissionsKey') else perm.get('permissionsKey')}")

        # Step 3: call masterSet/readDevice for the datalogger SN (weather station)
        j = masterset_read_device(sess, WEATHER_SN, perm)

        # Step 4: attempt to parse device list / fields
        result = {
            "server_base": DEFAULT_BASE,
            "weather_sn": WEATHER_SN,
            "plant_id": PLANT_ID,
            "permissionsKey": perm.get("permissionsKey", ""),
            "assToken_present": True,
            "readDevice": j,
        }

        # Common shapes: {success:true, obj:[...]} OR {data:{...}} OR something else
        device_list = None
        if isinstance(j, dict):
            if isinstance(j.get("obj"), list):
                device_list = j["obj"]
            elif isinstance(j.get("data"), dict) and isinstance(j["data"].get("devices"), list):
                device_list = j["data"]["devices"]

        if device_list is None:
            print("⚠️ Could not locate a device list in response. (Check JSON above.)")
        else:
            scan = guess_irradiance_fields(device_list)
            print(f"🔎 Devices returned: {len(device_list)} | Weather-ish matches: {scan.get('count', 0)}")
            _print_json("weather-ish matches", scan)
            result["device_list_count"] = len(device_list)
            result["weatherish_matches"] = scan

        _save_cache(result)
        print(f"✅ Probe finished. Saved cache: {CACHE_FILE}")


if __name__ == "__main__":
    main()
