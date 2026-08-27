#!/usr/bin/env python3
"""
growatt_weather_fetch.py (multi-plant aware)

Fetch Growatt (ShineServer Web UI) env/weather history:
  POST https://server.growatt.com/device/getEnvHistory

Key: avoid mixing Plant_ID and env datalogSn across different plants.
This script chooses env datalogSn/addr from a Plant Map config keyed by PLANT_ID.

REQUIRED ENVS:
  GROWATT_USERNAME
  GROWATT_PASSWORD
  GROWATT_PLANT_ID

CONFIG (choose one approach):
A) Provide a JSON plant map (recommended):
  export GROWATT_PLANT_MAP_JSON='{
    "10069072":{"name":"SMS","env_datalog_sn":"DYD1EZR007","env_addr":1},
    "9309575":{"name":"Taigene","env_datalog_sn":"DYD0E8501G","env_addr":1}
  }'

B) Or provide explicit env datalogSn/addr for the selected plant:
  GROWATT_ENV_DATALOG_SN
  GROWATT_ENV_ADDR

OPTIONAL:
  GROWATT_BASE=https://server.growatt.com
  GROWATT_TZ=America/Mexico_City
  GROWATT_DATE=YYYY-MM-DD
  GROWATT_FALLBACK_DAYS=2
  GROWATT_FETCH_ALL_PAGES=1
  GROWATT_SLEEP_BETWEEN_PAGES=0.15
  GROWATT_OUT_PREFIX=.growatt_env
  GROWATT_DEBUG=1
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, date as dt_date
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
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


def request_json(session: requests.Session, method: str, url: str, **kwargs) -> Tuple[int, Dict[str, str], Any, str]:
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


@dataclass
class GrowattWebClient:
    base: str
    username: str
    password: str
    plant_id: str

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
        st, _, _, _ = request_json(self.s, "GET", login_url, timeout=30)
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
        st, _, _, body = request_json(self.s, "POST", login_url, data=payload, headers=headers, timeout=30)

        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            snippet = (body or "").strip().replace("\n", " ")[:240]
            die(f"Login failed: assToken cookie missing. HTTP={st} body_snippet='{snippet}'")

        log("✅ Login OK (assToken present). Cookies: " + " | ".join(sorted(cookies.keys())))
        self.seed_plant_context(self.plant_id)

    def seed_plant_context(self, plant_id: str) -> None:
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    def post_get_env_history(
        self,
        datalog_sn: str,
        addr: int,
        start_date: str,
        end_date: str,
        start: int = 0,
    ) -> Tuple[int, Any]:
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
        st, _, parsed, raw = request_json(self.s, "POST", url, headers=headers, data=data, timeout=45)
        if parsed is None:
            snippet = (raw or "").strip().replace("\n", " ")[:240]
            return st, {"_parse_error": True, "_raw_snippet": snippet}
        return st, parsed


def extract_env_rows(resp: Any) -> List[Dict[str, Any]]:
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


def rows_to_csv(rows: List[Dict[str, Any]], path: str) -> None:
    if not rows:
        return
    keys = set()
    for r in rows:
        keys.update(r.keys())
    fieldnames = list(sorted(keys))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            out = dict(r)
            if "calendar" in out and isinstance(out["calendar"], dict):
                out["calendar"] = json.dumps(out["calendar"], ensure_ascii=False)
            w.writerow(out)


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


def write_normalized_csv(norm: List[Dict[str, Any]], path: str) -> None:
    if not norm:
        return
    fieldnames = list(norm[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in norm:
            w.writerow(r)


def fetch_env_for_date(
    cli: GrowattWebClient,
    datalog_sn: str,
    addr: int,
    day: str,
    fetch_all_pages: bool,
    sleep_between_pages: float,
) -> Tuple[int, List[Dict[str, Any]], Dict[str, Any], List[Any]]:
    st, resp = cli.post_get_env_history(datalog_sn, addr, day, day, start=0)
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
            st2, resp2 = cli.post_get_env_history(datalog_sn, addr, day, day, start=current_start)
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


def load_plant_map() -> Dict[str, Any]:
    raw = env("GROWATT_PLANT_MAP_JSON", "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception as e:
        die(f"GROWATT_PLANT_MAP_JSON is not valid JSON: {e}")
    return {}


def resolve_env_source(plant_id: str) -> Tuple[str, int, str]:
    """
    Returns: (env_datalog_sn, env_addr, plant_name)
    Priority:
      1) Plant map JSON (keyed by plant_id)
      2) Explicit env vars GROWATT_ENV_DATALOG_SN / GROWATT_ENV_ADDR
    """
    plant_map = load_plant_map()
    if plant_map and str(plant_id) in plant_map:
        cfg = plant_map[str(plant_id)]
        if not isinstance(cfg, dict):
            die(f"Plant map entry for {plant_id} must be an object.")
        sn = cfg.get("env_datalog_sn")
        addr = cfg.get("env_addr")
        name = cfg.get("name", f"plant_{plant_id}")
        if not sn or addr is None:
            die(f"Plant map entry for {plant_id} must include env_datalog_sn and env_addr.")
        return str(sn), int(addr), str(name)

    # fallback to explicit envs
    sn = env("GROWATT_ENV_DATALOG_SN")
    addr_s = env("GROWATT_ENV_ADDR")
    if not sn or addr_s is None:
        die(
            "Missing env source config. Provide either:\n"
            "- GROWATT_PLANT_MAP_JSON with mapping for this PLANT_ID, OR\n"
            "- GROWATT_ENV_DATALOG_SN and GROWATT_ENV_ADDR"
        )
    try:
        addr = int(addr_s)
    except Exception:
        die("GROWATT_ENV_ADDR must be an integer.")
    return sn, addr, f"plant_{plant_id}"


def main() -> None:
    base = env("GROWATT_BASE", BASE_DEFAULT) or BASE_DEFAULT
    tz_name = env("GROWATT_TZ", TZ_DEFAULT) or TZ_DEFAULT

    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    plant_id = env("GROWATT_PLANT_ID")

    if not user or not pwd:
        die("Missing GROWATT_USERNAME or GROWATT_PASSWORD.")
    if not plant_id:
        die("Missing GROWATT_PLANT_ID.")

    env_datalog_sn, env_addr, plant_name = resolve_env_source(plant_id)

    override_day = env("GROWATT_DATE")
    day0 = override_day if override_day else today_in_tz(tz_name).isoformat()

    fallback_days = env_int("GROWATT_FALLBACK_DAYS", 2)
    fetch_all_pages = env("GROWATT_FETCH_ALL_PAGES", "1") in ("1", "true", "True", "YES", "yes", "on", "ON")
    sleep_between_pages = env_float("GROWATT_SLEEP_BETWEEN_PAGES", 0.15)
    out_prefix = env("GROWATT_OUT_PREFIX", f".growatt_env_{plant_id}") or f".growatt_env_{plant_id}"

    log("=== Growatt Weather Fetch (Web UI) ===")
    log(f"🌐 Base: {base}")
    log(f"🏭 Plant: {plant_name} (ID={plant_id})")
    log(f"🧭 TZ: {tz_name}")
    log(f"📅 Date base: {day0} (fallback_days={fallback_days})")
    log(f"🌦️  Env source: datalogSn={env_datalog_sn} addr={env_addr}")
    log(f"📦 Fetch all pages: {fetch_all_pages}")

    cli = GrowattWebClient(base=base, username=user, password=pwd, plant_id=plant_id)
    cli.login()

    # Try day0..day0-fallback
    try_dates: List[str] = []
    d0 = dt_date.fromisoformat(day0)
    for i in range(0, max(fallback_days, 0) + 1):
        try_dates.append((d0 - timedelta(days=i)).isoformat())

    chosen_day: Optional[str] = None
    chosen_rows: List[Dict[str, Any]] = []
    chosen_status: int = 0
    chosen_merged: Dict[str, Any] = {}
    chosen_pages: List[Any] = []

    for d in try_dates:
        log(f"🔎 Fetching env history for {d} ...")
        st, rows, merged, pages = fetch_env_for_date(
            cli,
            datalog_sn=env_datalog_sn,
            addr=env_addr,
            day=d,
            fetch_all_pages=fetch_all_pages,
            sleep_between_pages=sleep_between_pages,
        )
        result_val = merged.get("result") if isinstance(merged, dict) else None
        log(f"   -> HTTP {st} result={result_val} rows={len(rows)} pages={len(pages)}")
        if rows:
            chosen_day = d
            chosen_rows = rows
            chosen_status = st
            chosen_merged = merged
            chosen_pages = pages
            break

    if chosen_day is None:
        die(
            "No env rows found for today or fallback days.\n"
            "Most common causes:\n"
            "- Weather station hasn't uploaded data yet today (try later)\n"
            "- Wrong mapping between PLANT_ID and env datalogSn/addr\n"
            "- Station offline\n"
            f"Tried dates: {', '.join(try_dates)}"
        )

    raw_path = f"{out_prefix}.raw.json"
    raw_pages_path = f"{out_prefix}.raw_pages.json"
    rows_csv_path = f"{out_prefix}.rows.csv"
    norm_csv_path = f"{out_prefix}.normalized.csv"

    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(chosen_merged, f, ensure_ascii=False, indent=2)
    with open(raw_pages_path, "w", encoding="utf-8") as f:
        json.dump(chosen_pages, f, ensure_ascii=False, indent=2)

    rows_to_csv(chosen_rows, rows_csv_path)
    norm = normalize_rows(chosen_rows, datalog_sn=env_datalog_sn, addr=env_addr)
    write_normalized_csv(norm, norm_csv_path)

    log("")
    log(f"✅ Success for date: {chosen_day}")
    log(f"📡 HTTP {chosen_status} rows={len(chosen_rows)} pages={len(chosen_pages)}")
    log(f"💾 Saved: {raw_path}")
    log(f"💾 Saved: {raw_pages_path}")
    log(f"💾 Saved: {rows_csv_path}")
    log(f"💾 Saved: {norm_csv_path}")

    if chosen_rows:
        r0 = chosen_rows[0]
        sample = {
            "calendar": r0.get("calendar"),
            "radiant_Wm2": r0.get("radiant"),
            "envTemp_C": r0.get("envTemp"),
            "panelTemp_C": r0.get("panelTemp"),
            "envHumidity_pct": r0.get("envHumidity"),
        }
        log("\n🧪 Sample row:")
        log(json.dumps(sample, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        die(f"Network error: {e}")
    except KeyboardInterrupt:
        die("Interrupted.", code=130)
