#!/usr/bin/env python3
"""
growatt_weather_fetch.py (multi-site, multi-weather-station)

Implements the SAME flow as Growatt Web UI (ShineServer):
  1) POST /device/getEnvList   (to list all env/weather devices for a plant)
  2) POST /device/getEnvHistory for each (datalogSn, addr)

Why:
- Plant datalogger SN (WeatherStation column) is NOT guaranteed to be the env device used by getEnvHistory.
- The web UI always uses getEnvList first.

INPUT CONFIG:
- Sites are provided via env var JSON:
    GROWATT_SITES_JSON='[
      {"customer":"TAIGENE MEXICO","site_id":"9309575","type":"growatt","weather_station_hint":"DYD0E8501G"},
      {"customer":"SMS","site_id":"10069072","type":"growatt","weather_station_hint":"DYD1EZR007"},
      {"customer":"SAG-MEXICO","site_id":"NE=35314736","type":"huawei"}
    ]'

REQUIRED ENVS:
  GROWATT_USERNAME
  GROWATT_PASSWORD
  GROWATT_SITES_JSON

OPTIONAL ENVS:
  GROWATT_BASE=https://server.growatt.com
  GROWATT_TZ=America/Mexico_City
  GROWATT_DATE=YYYY-MM-DD              # override "today"
  GROWATT_FALLBACK_DAYS=2              # try today, today-1, ... today-N (default 2)
  GROWATT_FETCH_ALL_PAGES=1            # fetch all pages for env history (default 1)
  GROWATT_SLEEP_BETWEEN_PAGES=0.15
  GROWATT_ENVLIST_MAX_PAGES=50         # pagination for getEnvList (default 50)
  GROWATT_OUT_DIR=out                  # default "out"
  GROWATT_DEBUG=1                      # verbose logs
  GROWATT_FAIL_ON_NO_DATA=0            # if 1 -> exit 1 if ALL growatt sites yield no rows

OUTPUTS (per site_id):
  out/<site_id>__env_list.json
  out/<site_id>__envpage.html
  out/<site_id>__history__<datalogSn>__addr<addr>__<date>.raw.json
  out/<site_id>__history__<datalogSn>__addr<addr>__<date>.normalized.csv

Also writes a summary:
  out/summary.json
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, date as dt_date
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore


BASE_DEFAULT = "https://server.growatt.com"
TZ_DEFAULT = "America/Mexico_City"


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_int(name: str, default: int) -> int:
    v = env(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    v = env(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default


def debug_enabled() -> bool:
    return env("GROWATT_DEBUG", "") in ("1", "true", "True", "YES", "yes", "on", "ON")


def fail_on_no_data() -> bool:
    return env("GROWATT_FAIL_ON_NO_DATA", "0") in ("1", "true", "True", "YES", "yes", "on", "ON")


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> None:
    print(f"❌ {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def safe_json_loads(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    start_candidates = [text.find("{"), text.find("[")]
    start_candidates = [i for i in start_candidates if i >= 0]
    if not start_candidates:
        return None
    start = min(start_candidates)
    return json.loads(text[start:])


def request_any(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Dict[str, str], Any, str]:
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


def today_in_tz(tz_name: str) -> dt_date:
    if ZoneInfo is None:
        return dt_date.today()
    try:
        tz = ZoneInfo(tz_name)
        return datetime.now(tz=tz).date()
    except Exception:
        return dt_date.today()


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


@dataclass
class GrowattWebClient:
    base: str
    username: str
    password: str

    def __post_init__(self) -> None:
        self.s = requests.Session()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        )
        self.s.headers.update(
            {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
                "Connection": "keep-alive",
            }
        )

    def login(self) -> None:
        login_url = f"{self.base}/login"
        st, _, _, _ = request_any(self.s, "GET", login_url, timeout=30)
        if st != 200:
            die(f"GET /login failed: HTTP {st}")

        payload = {"account": self.username, "password": self.password}
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/login",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        st, _, _, body = request_any(self.s, "POST", login_url, data=payload, headers=headers, timeout=30)
        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            snippet = (body or "").strip().replace("\n", " ")[:240]
            die(f"Login failed: assToken cookie missing. HTTP={st} body_snippet='{snippet}'")

        log("✅ Login OK (assToken present). Cookies: " + " | ".join(sorted(cookies.keys())))

    def seed_plant_context(self, plant_id: str) -> None:
        # Web UI relies on selectedPlantId cookie
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    def get_env_page_html(self, plant_id: str) -> str:
        self.seed_plant_context(plant_id)
        url = f"{self.base}/device/getEnvPage"
        headers = {
            "Referer": f"{self.base}/index",
            "Accept": "text/html, */*",
        }
        st, _, _, raw = request_any(self.s, "GET", url, headers=headers, timeout=30)
        if debug_enabled():
            log(f"🧭 GET /device/getEnvPage (plant={plant_id}) -> HTTP {st} (len={len(raw or '')})")
        return raw or ""

    def post_get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        """
        Matches web UI JS:
          POST /device/getEnvList
            plantId=<PLANT_ID>
            currPage=<n>
            alias=<search>
        """
        self.seed_plant_context(plant_id)

        url = f"{self.base}/device/getEnvList"
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/device/getEnvPage",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        data = {
            "plantId": str(plant_id),
            "currPage": str(curr_page),
            "alias": alias,
        }
        st, _, parsed, raw = request_any(self.s, "POST", url, headers=headers, data=data, timeout=45)
        if debug_enabled():
            log(f"🧭 POST /device/getEnvList plantId={plant_id} currPage={curr_page} -> HTTP {st}")
        if parsed is None:
            snippet = (raw or "").strip().replace("\n", " ")[:240]
            return {"_parse_error": True, "_http": st, "_raw_snippet": snippet}
        if isinstance(parsed, dict):
            parsed["_http"] = st
        return parsed

    def post_get_env_history(
        self, plant_id: str, datalog_sn: str, addr: int, start_date: str, end_date: str, start: int = 0
    ) -> Tuple[int, Any]:
        """
        POST /device/getEnvHistory with:
          datalogSn, addr, startDate, endDate, start
        """
        self.seed_plant_context(plant_id)

        url = f"{self.base}/device/getEnvHistory"
        headers = {
            "Origin": self.base,
            "Referer": f"{self.base}/device/getEnvPage",
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
        st, _, parsed, raw = request_any(self.s, "POST", url, headers=headers, data=data, timeout=45)
        if parsed is None:
            snippet = (raw or "").strip().replace("\n", " ")[:240]
            return st, {"_parse_error": True, "_raw_snippet": snippet}
        return st, parsed


def extract_env_rows(resp: Any) -> List[Dict[str, Any]]:
    """
    Expected:
      {"result":1,"obj":{"datas":[...], "haveNext": true/false, "start": 80, ...}}
    """
    if not isinstance(resp, dict):
        return []
    obj = resp.get("obj")
    if not isinstance(obj, dict):
        return []
    datas = obj.get("datas")
    if not isinstance(datas, list):
        return []
    return [r for r in datas if isinstance(r, dict)]


def resp_have_next(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    obj = resp.get("obj")
    if not isinstance(obj, dict):
        return False
    return bool(obj.get("haveNext"))


def resp_next_start(resp: Any, current_start: int, page_rows: int) -> int:
    if isinstance(resp, dict):
        obj = resp.get("obj")
        if isinstance(obj, dict):
            nxt = obj.get("start")
            try:
                if nxt is not None:
                    return int(nxt)
            except Exception:
                pass
    return current_start + max(page_rows, 0)


def calendar_to_iso(cal: Any) -> Optional[str]:
    if not isinstance(cal, dict):
        return None

    def g(*keys: str) -> Optional[int]:
        for k in keys:
            if k in cal:
                try:
                    return int(cal[k])
                except Exception:
                    return None
        return None

    y = g("year")
    m = g("month")
    d = g("day", "dayOfMonth")
    hh = g("hour", "hourOfDay") or 0
    mm = g("minute") or 0
    ss = g("second") or 0

    if y and m and d:
        try:
            return datetime(y, m, d, hh, mm, ss).isoformat()
        except Exception:
            return None
    return None


def normalize_rows(rows: List[Dict[str, Any]], datalog_sn: str, addr: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "ts_iso": calendar_to_iso(r.get("calendar")),
                "datalog_sn": datalog_sn,
                "addr": addr,
                "radiant_wm2": r.get("radiant"),
                "env_temp_c": r.get("envTemp"),
                "panel_temp_c": r.get("panelTemp"),
                "env_humidity_pct": r.get("envHumidity"),
                "wind_speed": r.get("windSpeed"),
                "wind_angle": r.get("windAngle"),
                "rainfall_intensity": r.get("rainfallIntensity"),
            }
        )
    return out


def write_csv_dicts(rows: List[Dict[str, Any]], path: str) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def load_sites() -> List[Dict[str, Any]]:
    raw = env("GROWATT_SITES_JSON")
    if not raw:
        die("Missing GROWATT_SITES_JSON (JSON list of sites).")
    try:
        data = json.loads(raw)
    except Exception as e:
        die(f"GROWATT_SITES_JSON is not valid JSON: {e}")
    if not isinstance(data, list):
        die("GROWATT_SITES_JSON must be a JSON array/list.")
    out: List[Dict[str, Any]] = []
    for x in data:
        if isinstance(x, dict):
            out.append(x)
    if not out:
        die("GROWATT_SITES_JSON contains no site objects.")
    return out


def get_env_devices_for_plant(cli: GrowattWebClient, plant_id: str, max_pages: int) -> Tuple[List[Dict[str, Any]], List[Any]]:
    """
    Paginate /device/getEnvList to collect all env devices.
    Returns (devices, pages_raw)
    """
    devices: List[Dict[str, Any]] = []
    pages: List[Any] = []
    seen = set()

    for page in range(1, max_pages + 1):
        resp = cli.post_get_env_list(plant_id=plant_id, curr_page=page, alias="")
        pages.append(resp)

        if not isinstance(resp, dict):
            break

        datas = resp.get("datas")
        if not isinstance(datas, list) or len(datas) == 0:
            # no more data
            break

        for d in datas:
            if not isinstance(d, dict):
                continue
            sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn")
            addr = d.get("addr")
            if not sn or addr is None:
                continue
            try:
                a = int(addr)
            except Exception:
                continue
            key = (str(sn), a)
            if key not in seen:
                seen.add(key)
                devices.append(d)

    return devices, pages


def fetch_env_history_for_device(
    cli: GrowattWebClient,
    plant_id: str,
    datalog_sn: str,
    addr: int,
    day: str,
    fetch_all_pages: bool,
    sleep_between_pages: float,
) -> Tuple[int, List[Dict[str, Any]], Dict[str, Any], List[Any]]:
    st, resp = cli.post_get_env_history(plant_id, datalog_sn, addr, day, day, start=0)
    pages: List[Any] = [resp]
    rows = extract_env_rows(resp)
    all_rows = list(rows)

    if fetch_all_pages:
        current_start = 0
        guard = 0
        while resp_have_next(resp):
            guard += 1
            if guard > 500:
                break

            nxt_start = resp_next_start(resp, current_start, len(rows))
            if nxt_start == current_start:
                nxt_start = current_start + max(len(rows), 1)

            time.sleep(max(sleep_between_pages, 0.0))

            current_start = nxt_start
            st2, resp2 = cli.post_get_env_history(plant_id, datalog_sn, addr, day, day, start=current_start)
            pages.append(resp2)

            rows = extract_env_rows(resp2)
            if not rows:
                break

            all_rows.extend(rows)
            resp = resp2
            if st2 != 200:
                break

    merged = pages[0] if isinstance(pages[0], dict) else {"resp": pages[0]}
    if isinstance(merged, dict) and isinstance(merged.get("obj"), dict):
        merged["obj"]["datas"] = all_rows
        merged["obj"]["_pages_fetched"] = len(pages)

    return st, all_rows, merged, pages


def slug(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9_.=-]+", "", s)
    return s[:120] if len(s) > 120 else s


def main() -> None:
    base = env("GROWATT_BASE", BASE_DEFAULT) or BASE_DEFAULT
    tz_name = env("GROWATT_TZ", TZ_DEFAULT) or TZ_DEFAULT

    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    if not user or not pwd:
        die("Missing GROWATT_USERNAME or GROWATT_PASSWORD.")

    sites = load_sites()

    out_dir = env("GROWATT_OUT_DIR", "out") or "out"
    ensure_dir(out_dir)

    override_day = env("GROWATT_DATE")
    day0 = override_day if override_day else today_in_tz(tz_name).isoformat()
    fallback_days = env_int("GROWATT_FALLBACK_DAYS", 2)
    fetch_all_pages = env("GROWATT_FETCH_ALL_PAGES", "1") in ("1", "true", "True", "YES", "yes", "on", "ON")
    sleep_between_pages = env_float("GROWATT_SLEEP_BETWEEN_PAGES", 0.15)
    max_envlist_pages = env_int("GROWATT_ENVLIST_MAX_PAGES", 50)

    log("=== Growatt Weather Fetch (Web UI) - Multi Site ===")
    log(f"🌐 Base: {base}")
    log(f"🧭 TZ: {tz_name}")
    log(f"📅 Date base: {day0} (fallback_days={fallback_days})")
    log(f"📦 Fetch all pages: {fetch_all_pages}")
    log(f"📁 Out dir: {out_dir}")
    log(f"🏷️  Sites in config: {len(sites)}")

    cli = GrowattWebClient(base=base, username=user, password=pwd)
    cli.login()

    try_dates: List[str] = []
    d0 = dt_date.fromisoformat(day0)
    for i in range(0, max(fallback_days, 0) + 1):
        try_dates.append((d0 - timedelta(days=i)).isoformat())

    summary: Dict[str, Any] = {
        "base": base,
        "tz": tz_name,
        "date_base": day0,
        "try_dates": try_dates,
        "sites_total": len(sites),
        "sites": [],
    }

    any_growatt_rows = False

    for site in sites:
        customer = str(site.get("customer") or site.get("CustomerName") or site.get("name") or "UNKNOWN")
        site_id = str(site.get("site_id") or site.get("SiteID") or "")
        typ = str(site.get("type") or site.get("Type") or "growatt").lower().strip()
        hint = str(site.get("weather_station_hint") or site.get("WeatherStation") or "")

        if not site_id:
            log(f"⚠️  SKIP (missing site_id) customer={customer}")
            continue

        entry: Dict[str, Any] = {
            "customer": customer,
            "site_id": site_id,
            "type": typ,
            "weather_station_hint": hint,
            "status": "unknown",
            "env_devices": [],
            "results": [],
        }

        if typ != "growatt":
            entry["status"] = "skipped_non_growatt"
            log(f"⏭️  SKIP non-growatt: {customer} site_id={site_id} type={typ}")
            summary["sites"].append(entry)
            continue

        if not site_id.isdigit():
            entry["status"] = "skipped_invalid_growatt_site_id"
            log(f"⏭️  SKIP growatt site_id not numeric: {customer} site_id={site_id}")
            summary["sites"].append(entry)
            continue

        log("")
        log(f"🏭 SITE: {customer} | plantId={site_id} | hint={hint}")

        # 1) dump env page html for debugging (always)
        html = cli.get_env_page_html(site_id)
        envpage_path = os.path.join(out_dir, f"{site_id}__envpage.html")
        write_text(envpage_path, html)

        # 2) get env list (all env devices on the plant)
        devices, pages_raw = get_env_devices_for_plant(cli, site_id, max_pages=max_envlist_pages)
        envlist_path = os.path.join(out_dir, f"{site_id}__env_list.json")
        write_json(envlist_path, {"plantId": site_id, "devices": devices, "pages": pages_raw})

        # record devices (as compact list)
        compact_devices: List[Dict[str, Any]] = []
        for d in devices:
            sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn")
            addr = d.get("addr")
            dev_type = d.get("deviceType")
            alias = d.get("alias") or d.get("deviceAilas") or d.get("name")
            compact_devices.append(
                {"datalogSn": sn, "addr": addr, "deviceType": dev_type, "alias": alias}
            )
        entry["env_devices"] = compact_devices

        if not devices:
            entry["status"] = "no_env_devices"
            log(f"⚠️  No ENV devices found for plantId={site_id} (weather station not connected in Growatt).")
            summary["sites"].append(entry)
            continue

        log(f"✅ ENV devices found: {len(devices)}")
        # For each device, try dates until we get rows
        for d in devices:
            sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn")
            addr = d.get("addr")
            if not sn or addr is None:
                continue
            try:
                addr_i = int(addr)
            except Exception:
                continue

            sn_s = str(sn)
            device_tag = f"{sn_s}__addr{addr_i}"

            dev_result: Dict[str, Any] = {
                "datalogSn": sn_s,
                "addr": addr_i,
                "chosen_date": None,
                "rows": 0,
                "http": None,
                "files": {},
            }

            for day in try_dates:
                log(f"   🔎 history device={sn_s} addr={addr_i} date={day}")
                st, rows, merged, pages = fetch_env_history_for_device(
                    cli=cli,
                    plant_id=site_id,
                    datalog_sn=sn_s,
                    addr=addr_i,
                    day=day,
                    fetch_all_pages=fetch_all_pages,
                    sleep_between_pages=sleep_between_pages,
                )
                result_val = merged.get("result") if isinstance(merged, dict) else None
                log(f"      -> HTTP {st} result={result_val} rows={len(rows)} pages={len(pages)}")

                # save raw regardless (use the day we tried)
                raw_path = os.path.join(out_dir, f"{site_id}__history__{device_tag}__{day}.raw.json")
                write_json(raw_path, merged)

                if rows:
                    # normalize + save
                    norm = normalize_rows(rows, datalog_sn=sn_s, addr=addr_i)
                    norm_path = os.path.join(out_dir, f"{site_id}__history__{device_tag}__{day}.normalized.csv")
                    write_csv_dicts(norm, norm_path)

                    dev_result["chosen_date"] = day
                    dev_result["rows"] = len(rows)
                    dev_result["http"] = st
                    dev_result["files"] = {"raw": raw_path, "normalized": norm_path}
                    any_growatt_rows = True
                    break

            entry["results"].append(dev_result)

        # status for site
        rows_total = sum(int(r.get("rows") or 0) for r in entry["results"])
        if rows_total > 0:
            entry["status"] = "ok"
        else:
            entry["status"] = "no_rows_for_any_device"

        summary["sites"].append(entry)

    # Write summary
    summary_path = os.path.join(out_dir, "summary.json")
    write_json(summary_path, summary)
    log("")
    log(f"🧾 Summary saved: {summary_path}")

    # Exit behavior
    if fail_on_no_data() and not any_growatt_rows:
        die("No data for any Growatt site/device (GROWATT_FAIL_ON_NO_DATA=1).", code=2)

    log("✅ Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        die(f"Network error: {e}")
    except KeyboardInterrupt:
        die("Interrupted.", code=130)
