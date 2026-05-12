#!/usr/bin/env python3
"""
Stage 0: Capture Growatt web UI API responses as test fixtures.

This is a ONE-OFF script. Run it locally (NOT in CI) with your real Growatt
web credentials. It logs in, hits each documented endpoint for one chosen
plant (default: TAIGENE = plant_id 9309575, 4 inverters), and saves every
raw response to v2/tests/fixtures/growatt_web/.

Captured fixtures power the unit tests for the v2 Growatt web client. Once
captured, the test suite runs entirely offline.

USAGE
    cd v2/
    export GROWATT_USERNAME=<your-user>
    export GROWATT_PASSWORD=<your-pass>
    python scripts/growatt_capture_fixtures.py

    # or specify a different plant:
    python scripts/growatt_capture_fixtures.py --plant-id 9275498 --plant-name SLP1

OUTPUTS
    tests/fixtures/growatt_web/login.json
    tests/fixtures/growatt_web/<plant_key>_getDevicesByPlant.json
    tests/fixtures/growatt_web/<plant_key>_getPlantData.json
    tests/fixtures/growatt_web/<plant_key>_alertPlantEvent.json
    tests/fixtures/growatt_web/<plant_key>_getWeatherByPlantId.json
    tests/fixtures/growatt_web/<plant_key>_getInvHisData_<sn>_<date>.json
    tests/fixtures/growatt_web/listDevice.json

SAFETY
- Read-only: no POST that modifies state. All POSTs we use are read-only API
  queries that Growatt happens to require POST for.
- Credentials NEVER end up in fixtures. We strip Set-Cookie headers.
- Fixtures contain real plant data (production values, inverter SNs) but no
  passwords, tokens, or session cookies.

EXIT CODES
    0  all fixtures captured
    1  partial — some endpoints failed (fixtures written for the ones that worked)
    2  could not log in
    3  config error (missing credentials, etc.)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(3)


# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------

WEB_BASE = "https://server.growatt.com"
LOGIN_PATH = "/login"  # Common Growatt login endpoint; adjust if account is on a different region

# Plant defaults (TAIGENE = good variety: 4 inverters, GTO1)
DEFAULT_PLANT_ID = "9309575"
DEFAULT_PLANT_KEY = "GTO1"

# All Growatt plants in the Argia account (discovered live via window.PLANTS)
# Used by --all-alerts loop to capture alertPlantEvent for every plant
# so we have a chance of seeing a real alert response in the fixtures.
ALL_GROWATT_PLANTS = [
    # (plant_id, plant_key)
    ("9275498",  "SLP1"),       # Química Coyoacán
    ("9309589",  "SLP2"),       # Turística Arizona
    ("9275469",  "NL2"),        # Budenheim
    ("9309575",  "GTO1"),       # Taigene
    ("10078094", "NL1"),        # Plastic Omnium NL
    ("10069072", "MEX3"),       # SMS
    ("10593332", "OECHSLER"),   # Weather-station-only plant (no inverters)
]

# Hardcoded inverter SN fallback per plant.
# getDevicesByPlant returns only one representative SN per device-type bucket,
# not the full inverter list. Until we discover the right endpoint, we fall back
# to a hardcoded list when getDevicesByPlant returns fewer than expected SNs.
# Add entries here as we confirm SNs for each plant.
HARDCODED_INVERTER_SNS = {
    "9309575": ["JFM7DXN00T", "JFM7DXN00U", "JFM5D8900B", "JFMCE9D014"],  # TAIGENE / GTO1
    # Add more plants here as needed:
    # "9275469": ["JJM4D4P01C", "JJM4D4P017"],  # Budenheim / NL2
    # "10069072": ["JGM7DY500G"],               # SMS / MEX3
}

DEFAULT_TIMEOUT = 30  # seconds per request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# -------------------------------------------------------------------------
# Logging
# -------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("capture")


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def _redact(obj: Any) -> Any:
    """Recursively scrub anything that looks like a credential or session token."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lower = str(k).lower()
            if any(x in lower for x in ("password", "passwd", "token", "session",
                                          "cookie", "authorization", "auth")):
                out[k] = "***REDACTED***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def save_fixture(out_dir: Path, name: str, response_obj: Any, status: int,
                  url: str, request_body: Optional[Dict] = None) -> Path:
    """
    Write a fixture file. Includes the URL, status, request body (for replay),
    and the parsed response. Strips anything credential-shaped.
    """
    fixture = {
        "_meta": {
            "url": url,
            "status": status,
            "request_body": _redact(request_body or {}),
        },
        "response": _redact(response_obj),
    }
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("  saved %s (%d bytes)", path.name, path.stat().st_size)
    return path


def safe_parse(resp: requests.Response) -> Any:
    """Try JSON; fall back to text snippet."""
    ct = resp.headers.get("Content-Type", "")
    if "json" in ct.lower():
        try:
            return resp.json()
        except ValueError:
            pass
    text = resp.text
    if len(text) > 100_000:
        text = text[:100_000] + f"\n...[truncated, total {len(text)} chars]"
    return {"_raw_text": text}


# -------------------------------------------------------------------------
# Login
# -------------------------------------------------------------------------

def login(session: requests.Session, username: str, password: str) -> bool:
    """
    Log into the Growatt web UI. Sets the session cookie. Returns True on success.

    Growatt's login is form-encoded POST to /login (or /newLogin in some accounts).
    Successful login redirects to /index or returns a JSON {result: 1, msg: ""}.
    """
    log.info("Logging in as %s...", username)

    # Most accounts: form-encoded POST to /login
    resp = session.post(
        f"{WEB_BASE}{LOGIN_PATH}",
        data={"account": username, "password": password, "validateCode": ""},
        timeout=DEFAULT_TIMEOUT,
        allow_redirects=False,
    )

    if resp.status_code == 302:
        # Redirect to /index = logged in
        loc = resp.headers.get("Location", "")
        if "index" in loc or "panel" in loc:
            log.info("  login OK (302 → %s)", loc)
            return True
        log.error("  login redirect to unexpected location: %s", loc)
        return False

    if resp.status_code == 200:
        try:
            j = resp.json()
            if j.get("result") in (1, "1", True) or "success" in str(j).lower():
                log.info("  login OK (JSON response)")
                return True
            log.error("  login JSON did not indicate success: %s", j)
            return False
        except ValueError:
            if "logout" in resp.text.lower() or "index" in resp.url:
                log.info("  login OK (HTML, likely landed on dashboard)")
                return True
            log.error("  login response not parseable as JSON, status=%d", resp.status_code)
            return False

    log.error("  login failed with status %d", resp.status_code)
    return False


# -------------------------------------------------------------------------
# Capture functions — one per endpoint
# -------------------------------------------------------------------------

def capture_get_devices_by_plant(session: requests.Session, out_dir: Path,
                                  plant_id: str, plant_key: str) -> List[str]:
    """
    Capture and return list of inverter SNs for the plant.

    The actual response shape (observed live):
        {"result":1, "obj": {
            "max":[[sn, label, deviceTypeCode], ...],
            "env":[[sn, "ENV_DEVICE", code], ...],
            "multipleBackflow":[],
            "singleBackflow":[]
        }}

    where "max" is the MAX-series inverter list. Each inverter is a
    3-element list [sn, label, deviceType]. Other type buckets may exist
    on other plants (e.g. "tlx", "mix", "inv").
    """
    log.info("Capturing getDevicesByPlant for plant %s (%s)...", plant_key, plant_id)
    url = f"{WEB_BASE}/panel/getDevicesByPlant?plantId={plant_id}"
    resp = session.post(
        url,
        data={},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=DEFAULT_TIMEOUT,
    )
    body = safe_parse(resp)
    save_fixture(out_dir, f"{plant_key}_getDevicesByPlant", body, resp.status_code, url)

    # Body may be {"_raw_text": "..."} if Content-Type wasn't JSON; re-parse it
    obj = None
    if isinstance(body, dict):
        if "_raw_text" in body:
            try:
                inner = json.loads(body["_raw_text"])
                obj = inner.get("obj")
            except (json.JSONDecodeError, AttributeError):
                pass
        elif "obj" in body:
            obj = body.get("obj")
        elif "response" in body and isinstance(body["response"], dict):
            obj = body["response"].get("obj")

    sns: List[str] = []
    if isinstance(obj, dict):
        # Inverter type buckets we know about
        for bucket_name in ("max", "tlx", "mix", "inv", "spa", "sph", "min", "mod"):
            bucket = obj.get(bucket_name, [])
            if not isinstance(bucket, list):
                continue
            for item in bucket:
                # Each item is [sn, label, device_type_numeric]
                if isinstance(item, list) and len(item) >= 1 and isinstance(item[0], str):
                    sns.append(item[0])
                elif isinstance(item, dict):
                    # Future-proof: if Growatt switches to dicts
                    for key in ("sn", "deviceSn"):
                        if isinstance(item.get(key), str):
                            sns.append(item[key])
                            break
    return sns


def capture_get_plant_data(session: requests.Session, out_dir: Path,
                            plant_id: str, plant_key: str) -> None:
    log.info("Capturing getPlantData for plant %s...", plant_key)
    url = f"{WEB_BASE}/panel/getPlantData?plantId={plant_id}"
    resp = session.post(
        url, data={}, headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=DEFAULT_TIMEOUT,
    )
    save_fixture(out_dir, f"{plant_key}_getPlantData", safe_parse(resp),
                  resp.status_code, url)


def capture_alert_plant_event(session: requests.Session, out_dir: Path,
                                plant_id: str, plant_key: str) -> None:
    """The unknown endpoint — the alert/event feed. Most interesting capture."""
    log.info("Capturing alertPlantEvent for plant %s...", plant_key)
    url = f"{WEB_BASE}/panel/alertPlantEvent?plantId={plant_id}"
    resp = session.get(url, timeout=DEFAULT_TIMEOUT)
    save_fixture(out_dir, f"{plant_key}_alertPlantEvent", safe_parse(resp),
                  resp.status_code, url)


def capture_weather(session: requests.Session, out_dir: Path,
                     plant_id: str, plant_key: str) -> None:
    log.info("Capturing getWeatherByPlantId for plant %s...", plant_key)
    url = f"{WEB_BASE}/index/getWeatherByPlantId?plantId={plant_id}"
    resp = session.post(
        url, data={}, headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=DEFAULT_TIMEOUT,
    )
    save_fixture(out_dir, f"{plant_key}_getWeatherByPlantId", safe_parse(resp),
                  resp.status_code, url)


def capture_inverter_history(session: requests.Session, out_dir: Path,
                               plant_id: str, plant_key: str,
                               inverter_sn: str, date_iso: str) -> None:
    """
    Capture per-inverter history at 5-minute granularity.

    Endpoint VERIFIED LIVE via DevTools Network inspection on 2026-05-12:
        POST /device/getMAXHistory
        Content-Type: application/x-www-form-urlencoded
        Form Data:
            maxSn=<inverter_sn>      (NOTE: "maxSn", not "deviceSn")
            startDate=YYYY-MM-DD
            endDate=YYYY-MM-DD
            start=0                  (pagination offset, 0 returns whole day)

    Response: JSON with ~150 rows per day × 155 columns per row (~34 KB).
    "MAX" refers to Growatt's MAX-series commercial inverters; if Argia ever
    adds non-MAX inverters (e.g. MID/MOD/TLX), the endpoint name and payload
    field will need to be adjusted (e.g. /device/getTLXHistory, tlxSn=...).
    """
    log.info("Capturing getMAXHistory for %s on %s...", inverter_sn, date_iso)
    url = f"{WEB_BASE}/device/getMAXHistory"
    body = {
        "maxSn": inverter_sn,
        "startDate": date_iso,
        "endDate": date_iso,
        "start": "0",
    }
    resp = session.post(
        url, data=body,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": WEB_BASE,
            "Referer": f"{WEB_BASE}/index",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    parsed = safe_parse(resp)
    # Did we get JSON or the "not logged in" HTML?
    is_html_error = (
        isinstance(parsed, dict)
        and "_raw_text" in parsed
        and "not login" in parsed["_raw_text"].lower()
    )
    if resp.status_code == 200 and not is_html_error:
        name = f"{plant_key}_getMAXHistory_{inverter_sn}_{date_iso}"
        save_fixture(out_dir, name, parsed, resp.status_code, url, body)
    else:
        name = f"{plant_key}_getMAXHistory_{inverter_sn}_{date_iso}_FAILED"
        save_fixture(out_dir, name, parsed, resp.status_code, url, body)
        log.warning("  history request failed: status=%d, html_error=%s",
                      resp.status_code, is_html_error)


def capture_max_total_data(session: requests.Session, out_dir: Path,
                            plant_id: str, plant_key: str) -> None:
    """Plant-level MAX-inverter aggregate data. Verified live."""
    log.info("Capturing getMAXTotalData for plant %s...", plant_key)
    url = f"{WEB_BASE}/panel/max/getMAXTotalData?plantId={plant_id}"
    resp = session.post(
        url, data={},
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": WEB_BASE,
            "Referer": f"{WEB_BASE}/index",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    save_fixture(out_dir, f"{plant_key}_getMAXTotalData", safe_parse(resp),
                  resp.status_code, url)


def capture_max_day_chart(session: requests.Session, out_dir: Path,
                           plant_id: str, plant_key: str,
                           inverter_sn: str, date_iso: str) -> None:
    """Day chart data (probably the same 5-min granularity, chart-friendly format).
    Captured for completeness — may or may not be redundant with getMAXHistory."""
    log.info("Capturing getMAXDayChart for %s on %s...", inverter_sn, date_iso)
    url = f"{WEB_BASE}/panel/max/getMAXDayChart"
    body = {
        "maxSn": inverter_sn,
        "plantId": plant_id,
        "date": date_iso,
    }
    resp = session.post(
        url, data=body,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": WEB_BASE,
            "Referer": f"{WEB_BASE}/index",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    save_fixture(out_dir, f"{plant_key}_getMAXDayChart_{inverter_sn}_{date_iso}",
                  safe_parse(resp), resp.status_code, url, body)


def capture_list_devices(session: requests.Session, out_dir: Path,
                          username: str) -> None:
    log.info("Capturing listDevice for account...")
    url = f"{WEB_BASE}/returnDevice/listDevice?accountName={username}"
    resp = session.get(url, timeout=DEFAULT_TIMEOUT)
    save_fixture(out_dir, "listDevice", safe_parse(resp), resp.status_code, url)


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Capture Growatt web UI fixtures")
    parser.add_argument("--plant-id", default=DEFAULT_PLANT_ID,
                          help=f"Growatt plant_id (default: {DEFAULT_PLANT_ID} = TAIGENE)")
    parser.add_argument("--plant-key", default=DEFAULT_PLANT_KEY,
                          help=f"Plant key for filenames (default: {DEFAULT_PLANT_KEY})")
    parser.add_argument("--date", default=None,
                          help="ISO date for history capture (default: yesterday MX)")
    parser.add_argument("--out-dir", default="tests/fixtures/growatt_web",
                          help="Output directory (default: tests/fixtures/growatt_web)")
    parser.add_argument("--max-inverters", type=int, default=4,
                          help="Limit number of inverters to capture history for (default: 4)")
    parser.add_argument("--inverter-sns", default=None,
                          help="Comma-separated SNs to use instead of auto-discovery "
                               "(e.g. --inverter-sns JFM7DXN00T,JFM7DXN00U)")
    parser.add_argument("--all-alerts", action="store_true", default=True,
                          help="Loop alertPlantEvent across ALL known Growatt plants "
                               "(default: True; use --no-all-alerts to disable)")
    parser.add_argument("--no-all-alerts", dest="all_alerts", action="store_false",
                          help="Skip multi-plant alert loop")
    args = parser.parse_args(argv)

    # Default date = yesterday in MX time (graceful fallback if tzdata missing)
    if args.date is None:
        import datetime as dt
        yesterday = None
        try:
            from zoneinfo import ZoneInfo
            mx = ZoneInfo("America/Mexico_City")
            yesterday = (dt.datetime.now(mx) - dt.timedelta(days=1)).date()
        except (ImportError, Exception) as e:
            # Either zoneinfo missing entirely, or tzdata package not installed
            log.warning("Could not load America/Mexico_City timezone (%s); falling back to UTC", e)
            yesterday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date()
        args.date = yesterday.isoformat()
    log.info("Capture date: %s", args.date)

    # Credentials
    username = os.environ.get("GROWATT_USERNAME", "").strip()
    password = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not (username and password):
        log.error("GROWATT_USERNAME and GROWATT_PASSWORD must be set")
        log.error("Example:")
        log.error("    export GROWATT_USERNAME=your-user")
        log.error("    export GROWATT_PASSWORD=your-pass")
        return 3

    # Output dir
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Output: %s", out_dir.resolve())

    # Session
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Login
    if not login(session, username, password):
        log.error("Could not log in — check credentials")
        return 2

    failures = 0

    # ----- Account-wide endpoints -----
    try:
        capture_list_devices(session, out_dir, username)
    except Exception as e:
        log.exception("listDevice failed: %s", e)
        failures += 1

    # ----- Primary plant endpoints (the target plant from --plant-id) -----
    discovered_sns: List[str] = []
    try:
        result = capture_get_devices_by_plant(session, out_dir, args.plant_id, args.plant_key)
        if result:
            discovered_sns = result
            log.info("  parsed %d inverter SN(s) from response: %s",
                     len(discovered_sns), ", ".join(discovered_sns))
    except Exception as e:
        log.exception("getDevicesByPlant failed: %s", e)
        failures += 1

    try:
        capture_get_plant_data(session, out_dir, args.plant_id, args.plant_key)
    except Exception as e:
        log.exception("getPlantData failed: %s", e)
        failures += 1

    try:
        capture_alert_plant_event(session, out_dir, args.plant_id, args.plant_key)
    except Exception as e:
        log.exception("alertPlantEvent failed: %s", e)
        failures += 1

    try:
        capture_weather(session, out_dir, args.plant_id, args.plant_key)
    except Exception as e:
        log.exception("getWeatherByPlantId failed: %s", e)
        failures += 1

    # ----- MAX-specific endpoints (Growatt's commercial inverter series) -----
    try:
        capture_max_total_data(session, out_dir, args.plant_id, args.plant_key)
    except Exception as e:
        log.exception("getMAXTotalData failed: %s", e)
        failures += 1

    # ----- Determine inverter SNs to fetch history for -----
    # Priority: --inverter-sns flag > hardcoded fallback > auto-discovered
    inverter_sns: List[str] = []
    if args.inverter_sns:
        inverter_sns = [s.strip() for s in args.inverter_sns.split(",") if s.strip()]
        log.info("Using inverter SNs from --inverter-sns flag: %s", inverter_sns)
    elif args.plant_id in HARDCODED_INVERTER_SNS and \
            len(discovered_sns) < len(HARDCODED_INVERTER_SNS[args.plant_id]):
        # Discovery returned fewer than we know exist — use hardcoded
        inverter_sns = HARDCODED_INVERTER_SNS[args.plant_id]
        log.info("Auto-discovery returned %d SN(s), using hardcoded list of %d for plant %s: %s",
                 len(discovered_sns), len(inverter_sns), args.plant_id, inverter_sns)
    else:
        inverter_sns = discovered_sns
        if inverter_sns:
            log.info("Using auto-discovered SNs: %s", inverter_sns)

    # ----- Per-inverter history capture -----
    if not inverter_sns:
        log.warning("No inverter SNs available; skipping history capture")
        log.warning("Add an entry to HARDCODED_INVERTER_SNS or use --inverter-sns flag")
    else:
        capped = inverter_sns[:args.max_inverters]
        if len(capped) < len(inverter_sns):
            log.info("Capping history capture to first %d of %d inverters (use --max-inverters to change)",
                     len(capped), len(inverter_sns))
        for sn in capped:
            try:
                capture_inverter_history(session, out_dir, args.plant_id,
                                          args.plant_key, sn, args.date)
            except Exception as e:
                log.exception("getMAXHistory failed for %s: %s", sn, e)
                failures += 1
            try:
                capture_max_day_chart(session, out_dir, args.plant_id,
                                       args.plant_key, sn, args.date)
            except Exception as e:
                log.exception("getMAXDayChart failed for %s: %s", sn, e)
                failures += 1

    # ----- Multi-plant alert loop (high-value, low-cost) -----
    if args.all_alerts:
        log.info("=" * 60)
        log.info("Multi-plant alert sweep across %d plants...", len(ALL_GROWATT_PLANTS))
        for pid, pkey in ALL_GROWATT_PLANTS:
            if pid == args.plant_id:
                continue  # Already captured above
            try:
                capture_alert_plant_event(session, out_dir, pid, pkey)
            except Exception as e:
                log.exception("alertPlantEvent failed for %s/%s: %s", pkey, pid, e)
                failures += 1

    log.info("=" * 60)
    if failures == 0:
        log.info("DONE: all fixtures captured to %s", out_dir.resolve())
        log.info("Next: review fixtures, then commit to git")
        return 0
    else:
        log.warning("DONE WITH %d FAILURES: review fixtures + logs", failures)
        return 1


if __name__ == "__main__":
    sys.exit(main())
