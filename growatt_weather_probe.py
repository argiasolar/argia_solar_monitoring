#!/usr/bin/env python3
"""
growatt_weather_probe.py

Goal:
- Log into https://server.growatt.com (Growatt "ShineServer" web UI)
- Fetch environmental / weather-station history via POST /device/getEnvHistory
  (this is the endpoint you saw in DevTools returning fields like `radiant`, `envTemp`, etc.)

How to run (recommended via env vars / GitHub Actions secrets):
  export GROWATT_USERNAME="..."
  export GROWATT_PASSWORD="..."
  export GROWATT_PLANT_ID="10069072"
  export GROWATT_WEATHER_SN="DYD1EZR007"   # what you currently have (may be a datalogger SN)
  # optional overrides if you already know the real env datalogSn & addr used by getEnvHistory:
  export GROWATT_ENV_DATALOG_SN="DYD0E8501G"
  export GROWATT_ENV_ADDR="1"
  export GROWATT_DATE="2026-01-24"         # optional (default: today in plant tz not available, so local today)
  python growatt_weather_probe.py

Notes:
- Growatt web responses sometimes return JSON with content-type text/html; we parse JSON anyway.
- The web UI uses cookies like assToken, JSESSIONID and often selectedPlantId; we set selectedPlantId to PLANT_ID to help.
- If GROWATT_ENV_DATALOG_SN is not provided, the script will try to "discover" a working (datalogSn, addr)
  by probing combinations (weather_sn + different addr, and a couple of fallbacks).

Output:
- Prints a compact summary
- Saves raw JSON to: .growatt_env_history.json
- Saves CSV to: .growatt_env_history.csv
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import date as dt_date
from typing import Any, Dict, List, Optional, Tuple

import requests


BASE_DEFAULT = "https://server.growatt.com"


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def die(msg: str, code: int = 1) -> None:
    print(f"❌ {msg}", file=sys.stderr)
    raise SystemExit(code)


def safe_json_loads(text: str) -> Any:
    text = text.strip()
    # Sometimes Growatt returns JSON but wrapped in HTML or with leading junk.
    # We'll do a best-effort extraction: find first '{' or '[' and parse from there.
    if not text:
        return None
    start_candidates = [text.find("{"), text.find("[")]
    start_candidates = [i for i in start_candidates if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    trimmed = text[start:]
    return json.loads(trimmed)


def request_json(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Dict[str, str], Any, str]:
    """
    Returns: (http_status, response_headers, parsed_json_or_None, raw_text)
    """
    resp = session.request(method, url, **kwargs)
    text = resp.text or ""
    parsed = None
    try:
        parsed = resp.json()
    except Exception:
        try:
            parsed = safe_json_loads(text)
        except Exception:
            parsed = None
    return resp.status_code, dict(resp.headers), parsed, text


def login_server(session: requests.Session, username: str, password: str, base: str = BASE_DEFAULT) -> None:
    """
    Web login flow:
    - GET /login  (seed cookies)
    - POST /login (form fields: account, password)
    Expect: assToken cookie set (as you observed in DevTools)
    """
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
    )

    session.headers.update(
        {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
            "Connection": "keep-alive",
        }
    )

    # 1) seed cookies
    login_url = f"{base}/login"
    st, _, _, _ = request_json(session, "GET", login_url, timeout=30)
    if st != 200:
        die(f"GET /login failed: HTTP {st}")

    # 2) attempt login
    # Growatt web uses form-urlencoded POST
    payload = {"account": username, "password": password}
    headers = {
        "Origin": base,
        "Referer": f"{base}/login",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "text/html,application/json, text/javascript, */*; q=0.01",
    }
    st, _, _, body = request_json(session, "POST", login_url, data=payload, headers=headers, timeout=30)

    # Some variants reply 200 HTML; cookie is the true signal.
    cookies = session.cookies.get_dict()
    if "assToken" not in cookies:
        # Print some hints to help debug in CI logs
        snippet = (body or "").strip().replace("\n", " ")[:200]
        die(
            "Login failed: assToken cookie missing. "
            "Check GROWATT_USERNAME/GROWATT_PASSWORD secrets. "
            f"HTTP={st} body_snippet='{snippet}'"
        )

    print("✅ Login OK (assToken present). Cookies:", " | ".join(sorted(cookies.keys())))


def seed_plant_context(session: requests.Session, plant_id: str) -> None:
    """
    The web UI frequently relies on these cookies to know which plant is selected.
    In your browser you had selectedPlantId + selPage* cookies. We set them best-effort.
    """
    # Domain must match current base host; requests cookiejar will handle host scoping.
    session.cookies.set("selectedPlantId", str(plant_id))
    session.cookies.set("selPage", "/device")
    session.cookies.set("selPageTwo", "/device/photovoltaic")
    session.cookies.set("selPageThree", "/device/getEnvPage")


def get_device_type_tree(session: requests.Session, base: str) -> Any:
    """
    Simple sanity check endpoint (you already hit it):
    GET /masterSet/deviceTypeTree
    """
    url = f"{base}/masterSet/deviceTypeTree"
    headers = {
        "Referer": f"{base}/index",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    st, _, parsed, _ = request_json(session, "GET", url, headers=headers, timeout=30)
    if st != 200:
        print(f"⚠️  deviceTypeTree HTTP {st}")
        return None
    return parsed


def post_get_env_history(
    session: requests.Session,
    base: str,
    datalog_sn: str,
    addr: int,
    start_date: str,
    end_date: str,
    start: int = 0,
) -> Tuple[int, Any]:
    """
    POST /device/getEnvHistory
    Form fields (from your DevTools):
      datalogSn, addr, startDate, endDate, start
    """
    url = f"{base}/device/getEnvHistory"
    headers = {
        "Origin": base,
        "Referer": f"{base}/index",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    data = {
        "datalogSn": datalog_sn,
        "addr": str(addr),
        "startDate": start_date,
        "endDate": end_date,
        "start": str(start),
    }
    st, _, parsed, raw = request_json(session, "POST", url, headers=headers, data=data, timeout=45)
    if parsed is None:
        # Sometimes returns non-JSON; keep a short snippet for debugging
        snippet = (raw or "").strip().replace("\n", " ")[:200]
        return st, {"_parse_error": True, "_raw_snippet": snippet}
    return st, parsed


def extract_env_rows(resp: Any) -> List[Dict[str, Any]]:
    """
    Expected shape (from your example):
      {"result":1,"obj":{"endDate":"...","datas":[{...},{...}], "start":80,"haveNext":true}}
    """
    if not isinstance(resp, dict):
        return []
    obj = resp.get("obj")
    if not isinstance(obj, dict):
        return []
    datas = obj.get("datas")
    if not isinstance(datas, list):
        return []
    rows: List[Dict[str, Any]] = []
    for r in datas:
        if isinstance(r, dict):
            rows.append(r)
    return rows


def rows_to_csv(rows: List[Dict[str, Any]], path: str) -> None:
    if not rows:
        return
    # Flatten keys only one level deep; keep nested "calendar" as JSON string
    keys = set()
    for r in rows:
        keys.update(r.keys())
    keys = list(sorted(keys))

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            out = dict(r)
            if "calendar" in out and isinstance(out["calendar"], dict):
                out["calendar"] = json.dumps(out["calendar"], ensure_ascii=False)
            w.writerow(out)


def discover_env_datalog_and_addr(
    session: requests.Session,
    base: str,
    weather_sn_hint: str,
    day: str,
) -> Optional[Tuple[str, int]]:
    """
    Best-effort discovery:
    - Try (weather_sn_hint, addr 0..5)
    - If no luck, try a couple of common addr ranges and alternative guesses
    """
    print("🕵️  Discovering working (datalogSn, addr) for getEnvHistory...")

    candidates: List[str] = []
    if weather_sn_hint:
        candidates.append(weather_sn_hint)

    # Sometimes the "weather station SN" you store is not the datalogSn used in getEnvHistory.
    # We cannot derive DYD0E8501G reliably without another endpoint, so we keep it conservative.
    # You *can* pass the real one via GROWATT_ENV_DATALOG_SN once known.

    for datalog_sn in candidates:
        for addr in list(range(0, 6)) + list(range(6, 16)):
            st, resp = post_get_env_history(session, base, datalog_sn, addr, day, day, start=0)
            rows = extract_env_rows(resp)
            ok = isinstance(resp, dict) and resp.get("result") == 1 and len(rows) > 0
            print(f"   - try datalogSn={datalog_sn} addr={addr} -> HTTP {st} rows={len(rows)} result={getattr(resp,'get',lambda _:'?')('result') if isinstance(resp,dict) else '?'}")
            if ok:
                print(f"✅ Found working env source: datalogSn={datalog_sn} addr={addr}")
                return datalog_sn, addr

    print("⚠️  Discovery failed with provided hint. Set GROWATT_ENV_DATALOG_SN and GROWATT_ENV_ADDR explicitly.")
    return None


def main() -> None:
    base = env("GROWATT_BASE", BASE_DEFAULT) or BASE_DEFAULT

    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    plant_id = env("GROWATT_PLANT_ID")
    weather_sn = env("GROWATT_WEATHER_SN", "")

    if not user or not pwd:
        die("Missing GROWATT_USERNAME or GROWATT_PASSWORD (use GitHub Secrets / env vars).")
    if not plant_id:
        die("Missing GROWATT_PLANT_ID (env).")

    # Date selection (default: local today)
    day = env("GROWATT_DATE", dt_date.today().isoformat())  # YYYY-MM-DD

    # Optional override: if you already know the exact datalogSn/addr used by getEnvHistory
    env_datalog_sn = env("GROWATT_ENV_DATALOG_SN")
    env_addr_str = env("GROWATT_ENV_ADDR")

    try:
        env_addr = int(env_addr_str) if env_addr_str is not None else None
    except Exception:
        die("GROWATT_ENV_ADDR must be an integer if provided.")

    print("=== Growatt Weather Probe (Web UI) ===")
    print(f"🌐 Base: {base}")
    print(f"🏭 Plant ID: {plant_id}")
    print(f"🌦️  Weather SN (hint): {weather_sn}")
    print(f"📅 Date: {day}")

    s = requests.Session()
    login_server(s, user, pwd, base=base)
    seed_plant_context(s, plant_id)

    # sanity check
    tree = get_device_type_tree(s, base)
    if tree:
        # show the top-level types quickly
        top = []
        if isinstance(tree, list):
            for x in tree:
                if isinstance(x, dict):
                    top.append(x.get("value") or x.get("labelKey") or "?")
        print("🧩 deviceTypeTree:", ", ".join(top[:10]) if top else "(parsed)")

    # Determine datalogSn+addr for env history
    if env_datalog_sn and env_addr is not None:
        datalog_sn, addr = env_datalog_sn, env_addr
        print(f"🔧 Using explicit env source: datalogSn={datalog_sn} addr={addr}")
    else:
        found = discover_env_datalog_and_addr(s, base, weather_sn, day)
        if not found:
            die(
                "Could not discover env datalogSn/addr automatically.\n"
                "👉 In your DevTools you already saw working values, so set:\n"
                "   GROWATT_ENV_DATALOG_SN=DYD0E8501G\n"
                "   GROWATT_ENV_ADDR=1\n"
                "and rerun."
            )
        datalog_sn, addr = found

    # Fetch env history (first page)
    start_offset = int(env("GROWATT_START", "0") or "0")
    st, resp = post_get_env_history(s, base, datalog_sn, addr, day, day, start=start_offset)

    out_json_path = ".growatt_env_history.json"
    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(resp, f, ensure_ascii=False, indent=2)

    rows = extract_env_rows(resp)
    out_csv_path = ".growatt_env_history.csv"
    if rows:
        rows_to_csv(rows, out_csv_path)

    print(f"\n📡 POST /device/getEnvHistory -> HTTP {st}")
    if isinstance(resp, dict):
        print(f"   result={resp.get('result')}")
        if isinstance(resp.get("obj"), dict):
            obj = resp["obj"]
            print(f"   haveNext={obj.get('haveNext')} start={obj.get('start')} endDate={obj.get('endDate')}")
    print(f"   rows={len(rows)}")
    print(f"💾 Saved: {out_json_path}")
    if rows:
        print(f"💾 Saved: {out_csv_path}")

    # Print a compact sample of the newest row (Growatt list is usually newest-first; if not, it’s still useful)
    if rows:
        r0 = rows[0]
        # Fields you care about most:
        sample = {
            "calendar": r0.get("calendar"),
            "dataLogSn": r0.get("dataLogSn"),
            "addr": r0.get("addr"),
            "radiant_Wm2": r0.get("radiant"),
            "envTemp_C": r0.get("envTemp"),
            "panelTemp_C": r0.get("panelTemp"),
            "windSpeed": r0.get("windSpeed"),
            "windAngle": r0.get("windAngle"),
            "envHumidity_pct": r0.get("envHumidity"),
            "rainfallIntensity": r0.get("rainfallIntensity"),
        }
        print("\n✅ Sample row:")
        print(json.dumps(sample, ensure_ascii=False, indent=2))

    # Helpful final hint for your use-case:
    print("\n🎯 Irradiance field:")
    print("   - Use `radiant` (W/m²) from each row. This is your irradiance time series.\n")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        die(f"Network error: {e}")
    except KeyboardInterrupt:
        die("Interrupted.", code=130)
