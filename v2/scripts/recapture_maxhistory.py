#!/usr/bin/env python3
"""
Re-capture the 4 MAXHistory fixtures with the safe_parse bug FIXED.

Background
==========
The original capture script `growatt_capture_fixtures.py` has a bug in
`safe_parse`: it only attempts `resp.json()` when the response's
Content-Type header contains "json". Growatt returns valid JSON with
``Content-Type: text/html`` for many endpoints (their bug, not ours), so
the parse branch is skipped — the response is stored as ``_raw_text``,
and any response over 100KB gets silently truncated mid-stream.

The four ``GTO1_getMAXHistory_*.json`` fixtures captured at commit
``bf988ba`` are ~340KB each (150 rows × 155 fields). They got truncated
at byte 100,000, producing fixtures with malformed JSON in their
``_raw_text`` field. Stage 2 tests blow up trying to decode them with
``JSONDecodeError: Expecting ':' delimiter: line 2 column 1 (char 100001)``.

This script
===========
A standalone one-shot that:

1. Logs in using the same path the main capture script uses.
2. Hits ``/device/getMAXHistory`` for the 4 known TAIGENE SNs.
3. Stores responses using a FIXED ``safe_parse`` that always tries
   ``resp.json()`` first, regardless of Content-Type. No truncation
   needed: the result is a real dict, serialized to disk by ``json.dumps``,
   no string length cap involved.
4. Writes fixtures to ``tests/fixtures/growatt_web/`` with the SAME
   filenames as the existing corrupt ones (using ``--date 2026-05-11``).
   They get overwritten in place.

Usage
=====
    cd v2
    export GROWATT_USERNAME=<your-user>
    export GROWATT_PASSWORD=<your-pass>
    python scripts/recapture_maxhistory.py

After running:
    PYTHONPATH=. pytest tests/unit/test_growatt_web_parser.py -v
    # All 80 tests should pass.

    git add v2/tests/fixtures/growatt_web/
    git commit -m "Re-capture MAXHistory fixtures (fix Stage 0 truncation bug)"
    git push

Then on a calm day, port the safe_parse fix into the main capture script
so future captures don't hit this again. The patch is:
    OLD: if "json" in ct.lower(): try: return resp.json() except: pass
    NEW: try: return resp.json() except ValueError: pass
    Plus bump 100_000 cap to 5_000_000 for the fallback text path.

Exit codes
==========
    0  all 4 fixtures captured
    1  partial — some captures failed (others written to disk OK)
    2  login failed
    3  config error (missing credentials)
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
# Config — matches the main capture script
# -------------------------------------------------------------------------

WEB_BASE = "https://server.growatt.com"
DEFAULT_TIMEOUT = 30

# The 4 TAIGENE SNs that were corrupted at bf988ba.
# Same list as HARDCODED_INVERTER_SNS["9309575"] in the main script.
TAIGENE_PLANT_ID = "9309575"
TAIGENE_PLANT_KEY = "GTO1"
TAIGENE_INVERTER_SNS = ["JFM7DXN00T", "JFM7DXN00U", "JFM5D8900B", "JFMCE9D014"]

# Date in the corrupt fixture filenames. Overwriting them in place means
# the existing test code doesn't need to change.
DEFAULT_RECAPTURE_DATE = "2026-05-11"

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
log = logging.getLogger("recapture")


# -------------------------------------------------------------------------
# Fixed safe_parse — THE FIX
# -------------------------------------------------------------------------

def safe_parse_fixed(resp: requests.Response) -> Any:
    """
    Try JSON first regardless of Content-Type. Growatt returns JSON-shaped
    bodies with ``Content-Type: text/html`` for many endpoints, so we
    cannot trust the header. Only fall back to raw text if JSON parse
    fails outright.

    When the fallback is taken, the text cap is 5 MB (vs 100 KB in the
    buggy original) — enough headroom for any plausible Growatt response.
    """
    # Always try JSON first. resp.json() does its own decoding using
    # resp.encoding; if the body really is JSON it succeeds regardless of
    # what the server's Content-Type header claims.
    try:
        return resp.json()
    except ValueError:
        pass

    # True non-JSON response (e.g. an HTML "not logged in" page). Store as
    # text, with a much bigger cap than the original. The truncation
    # marker is still present so we can detect and refuse to ship a
    # truncated fixture if it ever does hit the cap.
    text = resp.text
    if len(text) > 5_000_000:
        text = text[:5_000_000] + f"\n...[truncated, total {len(text)} chars]"
    return {"_raw_text": text}


# -------------------------------------------------------------------------
# Helpers (copied from the main capture script for self-containment)
# -------------------------------------------------------------------------

def _redact(obj: Any) -> Any:
    """Recursively scrub anything that looks like a credential."""
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
    """Write a fixture file in the same shape as the main capture script."""
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


def validate_no_truncation(fixture_path: Path) -> bool:
    """
    Sanity check after writing: load the fixture and confirm that if it
    has a ``_raw_text`` field, the text does NOT contain the truncation
    marker. Returns True if OK, False if truncated (caller should fail).
    """
    try:
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error("  fixture %s is itself not valid JSON: %s", fixture_path.name, e)
        return False

    response = data.get("response")
    if isinstance(response, dict) and "_raw_text" in response:
        if "[truncated" in response["_raw_text"]:
            log.error(
                "  fixture %s has TRUNCATED _raw_text. "
                "The response was larger than the 5 MB cap (or safe_parse_fixed "
                "didn't take the JSON branch). Aborting.",
                fixture_path.name,
            )
            return False

    return True


# -------------------------------------------------------------------------
# Login (mirrors the main capture script)
# -------------------------------------------------------------------------

def login(session: requests.Session, username: str, password: str) -> bool:
    """POST credentials to /login. Success = ``assToken`` cookie present."""
    log.info("Logging in as %s...", username)

    # Prime session with a GET so initial cookies/CSRF are set
    try:
        session.get(f"{WEB_BASE}/login", timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        log.error("Pre-login GET failed: %s", e)
        return False

    try:
        resp = session.post(
            f"{WEB_BASE}/login",
            data={"account": username, "password": password},
            headers={
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/login",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.error("Login POST failed: %s", e)
        return False

    cookies = session.cookies.get_dict()
    if "assToken" in cookies:
        log.info("Login OK (assToken cookie present)")
        return True

    log.error(
        "Login failed: no assToken cookie. HTTP %d. Body snippet: %s",
        resp.status_code,
        (resp.text or "").strip().replace("\n", " ")[:240],
    )
    return False


# -------------------------------------------------------------------------
# The capture — only getMAXHistory, only the 4 SNs
# -------------------------------------------------------------------------

def capture_max_history(session: requests.Session, out_dir: Path,
                         plant_key: str, inverter_sn: str, date_iso: str) -> bool:
    """Returns True on success, False on failure."""
    log.info("Capturing getMAXHistory for %s on %s...", inverter_sn, date_iso)
    url = f"{WEB_BASE}/device/getMAXHistory"
    body = {
        "maxSn": inverter_sn,
        "startDate": date_iso,
        "endDate": date_iso,
        "start": "0",
    }
    try:
        resp = session.post(
            url, data=body,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": WEB_BASE,
                "Referer": f"{WEB_BASE}/index",
            },
            timeout=DEFAULT_TIMEOUT,
        )
    except requests.RequestException as e:
        log.error("  request failed: %s", e)
        return False

    parsed = safe_parse_fixed(resp)

    # Sanity check: did we get JSON, or an HTML "not logged in" page?
    is_html_error = (
        isinstance(parsed, dict)
        and "_raw_text" in parsed
        and ("not login" in parsed["_raw_text"].lower()
              or "<html" in parsed["_raw_text"].lower())
    )
    if resp.status_code != 200 or is_html_error:
        log.error("  HTTP %d, html_error=%s — not saving",
                   resp.status_code, is_html_error)
        return False

    name = f"{plant_key}_getMAXHistory_{inverter_sn}_{date_iso}"
    path = save_fixture(out_dir, name, parsed, resp.status_code, url, body)

    # The whole point of this script: confirm the new fixture is NOT truncated.
    if not validate_no_truncation(path):
        return False

    # And confirm the result shape looks right (has datas list).
    response = json.loads(path.read_text(encoding="utf-8"))["response"]
    obj = response.get("obj") if isinstance(response, dict) else None
    if isinstance(obj, dict):
        datas = obj.get("datas")
        if isinstance(datas, list):
            log.info("  OK — %d rows in datas[]", len(datas))
        else:
            log.warning("  obj has no 'datas' list — schema may have changed")
    else:
        log.warning("  response.obj is not a dict — Growatt API may have changed")

    return True


# -------------------------------------------------------------------------
# Main
# -------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-capture corrupt MAXHistory fixtures (one-shot fix for the "
                    "safe_parse Content-Type bug)"
    )
    parser.add_argument(
        "--date", default=DEFAULT_RECAPTURE_DATE,
        help=f"ISO date in fixture filenames (default: {DEFAULT_RECAPTURE_DATE}, "
             f"matching the existing corrupt fixtures so they're overwritten in place)",
    )
    parser.add_argument(
        "--out-dir", default="tests/fixtures/growatt_web",
        help="Output directory (default: tests/fixtures/growatt_web)",
    )
    parser.add_argument(
        "--inverter-sns", default=",".join(TAIGENE_INVERTER_SNS),
        help="Comma-separated SNs (default: the 4 TAIGENE inverters)",
    )
    args = parser.parse_args(argv)

    username = os.environ.get("GROWATT_USERNAME", "").strip()
    password = os.environ.get("GROWATT_PASSWORD", "").strip()
    if not (username and password):
        log.error("GROWATT_USERNAME and GROWATT_PASSWORD must be set")
        log.error("    export GROWATT_USERNAME=your-user")
        log.error("    export GROWATT_PASSWORD=your-pass")
        return 3

    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("Output directory: %s", out_dir)
    log.info("Re-capture date:  %s", args.date)

    sns: List[str] = [s.strip() for s in args.inverter_sns.split(",") if s.strip()]
    log.info("Inverter SNs:     %s", ", ".join(sns))

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    if not login(session, username, password):
        return 2

    failures = 0
    for sn in sns:
        if not capture_max_history(session, out_dir, TAIGENE_PLANT_KEY, sn, args.date):
            failures += 1

    log.info("=" * 60)
    if failures == 0:
        log.info("DONE: %d MAXHistory fixtures re-captured cleanly.", len(sns))
        log.info("Now run: PYTHONPATH=. pytest tests/unit/test_growatt_web_parser.py -v")
        return 0
    else:
        log.warning("DONE with %d failures out of %d", failures, len(sns))
        return 1


if __name__ == "__main__":
    sys.exit(main())
