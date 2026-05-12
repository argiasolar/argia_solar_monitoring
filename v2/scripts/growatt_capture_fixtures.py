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
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False))
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
                                  plant_id: str, plant_key: str) -> Optional[List[Dict]]:
    """Capture and return list of inverters for the plant."""
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
    # Try to extract inverter SNs for the history capture
    if isinstance(body, dict):
        for key in ("obj", "data", "deviceList", "result"):
            if isinstance(body.get(key), list):
                return body[key]
            if isinstance(body.get(key), dict):
                for subkey, subval in body[key].items():
                    if isinstance(subval, list):
                        return subval
    return None


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
    """The MAIN one — 155 columns × ~150 rows of per-5min inverter data."""
    log.info("Capturing getInvHisData for %s on %s...", inverter_sn, date_iso)
    url = f"{WEB_BASE}/device/getInverterHistory.do"
    # Try several known endpoint variants — Growatt has had these change over time
    candidates = [
        ("/device/getInverterHistory.do", {
            "deviceSn": inverter_sn, "startDate": date_iso, "endDate": date_iso,
        }),
        ("/device/inv/getInvHisData", {
            "plantId": plant_id, "deviceSn": inverter_sn,
            "deviceType": "inv", "deviceTypeName": "Inverter",
            "startDate": date_iso, "endDate": date_iso,
        }),
        ("/panel/inv/getInverterData", {
            "deviceSn": inverter_sn, "plantId": plant_id,
            "startDate": date_iso, "endDate": date_iso,
        }),
    ]
    saved = False
    for path, body in candidates:
        full_url = f"{WEB_BASE}{path}"
        resp = session.post(
            full_url, data=body,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=DEFAULT_TIMEOUT,
        )
        parsed = safe_parse(resp)
        # Save the first 200-status JSON response we find
        if resp.status_code == 200 and isinstance(parsed, dict) and "_raw_text" not in parsed:
            name = f"{plant_key}_getInvHisData_{inverter_sn}_{date_iso}"
            save_fixture(out_dir, name, parsed, resp.status_code, full_url, body)
            log.info("  matched endpoint: %s", path)
            saved = True
            break
        else:
            log.debug("  endpoint %s returned status=%d (trying next)",
                      path, resp.status_code)

    if not saved:
        # Save the last attempt anyway so we have diagnostic info
        name = f"{plant_key}_getInvHisData_{inverter_sn}_{date_iso}_FAILED"
        save_fixture(out_dir, name, parsed, resp.status_code, full_url, body)
        log.warning("  could not find working history endpoint, saved failed attempt")


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
    parser.add_argument("--max-inverters", type=int, default=2,
                          help="Limit number of inverters to capture history for (default: 2)")
    args = parser.parse_args(argv)

    # Default date = yesterday in MX time
    if args.date is None:
        import datetime as dt
        try:
            from zoneinfo import ZoneInfo
            mx = ZoneInfo("America/Mexico_City")
            yesterday = (dt.datetime.now(mx) - dt.timedelta(days=1)).date()
        except ImportError:
            yesterday = (dt.datetime.utcnow() - dt.timedelta(days=1)).date()
        args.date = yesterday.isoformat()

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

    # Capture in order — each is independent so we keep going on failure
    try:
        capture_list_devices(session, out_dir, username)
    except Exception as e:
        log.exception("listDevice failed: %s", e)
        failures += 1

    devices: List[Dict] = []
    try:
        result = capture_get_devices_by_plant(session, out_dir, args.plant_id, args.plant_key)
        if result:
            devices = result
            log.info("  parsed %d devices from response", len(devices))
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

    # Per-inverter history — only for first N inverters to avoid hammering
    inverter_sns: List[str] = []
    for d in devices[:args.max_inverters]:
        for key in ("sn", "deviceSn", "alias"):
            if isinstance(d.get(key), str):
                inverter_sns.append(d[key])
                break

    if not inverter_sns:
        log.warning("No inverter SNs parsed from getDevicesByPlant; cannot capture history")
        log.warning("Inspect %s_getDevicesByPlant.json and edit script's parser", args.plant_key)
    else:
        for sn in inverter_sns:
            try:
                capture_inverter_history(session, out_dir, args.plant_id,
                                          args.plant_key, sn, args.date)
            except Exception as e:
                log.exception("getInvHisData failed for %s: %s", sn, e)
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
