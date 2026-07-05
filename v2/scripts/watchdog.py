"""Watchdog — the dead-man's switch for the data pipeline.

Answers one question every morning: DID THE DATA ARRIVE? Three checks:

  kpi        yesterday (MX) has KPI_Daily rows for at least --min-kpi-plants
             plants  -> catches kpi_eod failure / empty stamping
  telemetry  Telemetry_Argia's newest timestamp_mx is younger than
             --max-age-min -> catches the v2 collector dying (only checked
             inside the 06:00-21:59 MX collection window; silence at night
             is normal)
  pi         v1 sheet InverterUnified10m newest ExtractedAtUTC is younger
             than --max-age-min -> catches the Pi / v1 collector being down
             (READ-ONLY on the sacred ARGIA_Solar sheet; skipped with a
             notice when GOOGLE_SHEET_ID for v1 is not configured)

Failures append rows to the append-only ``Watchdog_Alerts`` tab (same
outbox pattern as Report_Outbox); the Apps Script notifier mails rows with
empty notified_at and stamps them. Success writes NOTHING (no spam).
The job also exits 1 on failure so the Actions run shows red.

HONEST LIMITATION: this runs in GitHub Actions, so "GitHub itself is down"
is not detectable here. The notifier Apps Script (Google infra) carries a
small independent staleness nag for that case.

Usage (from v2/):
  PYTHONPATH=. python scripts/watchdog.py               # dry-run: check, print
  PYTHONPATH=. python scripts/watchdog.py --apply       # write failure rows
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from zoneinfo import ZoneInfo

from argia.core.cells import coerce_date, coerce_ts
from argia.core.sheets import SheetsClient

MX_TZ = ZoneInfo("America/Mexico_City")
UTC = dt.timezone.utc

WATCHDOG_TAB = "Watchdog_Alerts"
WATCHDOG_HEADER = ["detected_utc", "check", "severity", "detail",
                   "notified_at"]

V1_FEED_TAB = "InverterUnified10m"
V1_FEED_TS_COLUMN = "ExtractedAtUTC"

COLLECT_START_H = 6      # MX collection window (matches telemetry crons)
COLLECT_END_H = 22


def newest_ts(rows: list[dict], column: str) -> dt.datetime | None:
    """Newest timestamp in a column, via the SHARED cell coercion — the
    live API returns serial floats, which the watchdog's original private
    parser could not read (false-alarm incident, 2026-07-05)."""
    out = None
    for r in rows:
        t = coerce_ts(r.get(column))
        if t and (out is None or t > out):
            out = t
    return out


# --- pure checks (all take `now` explicitly -> trivially testable) -----------

def check_kpi_yesterday(kpi_rows: list[dict], now_mx: dt.datetime,
                        min_plants: int) -> dict | None:
    """Fail when yesterday has fewer than min_plants KPI rows."""
    yday = now_mx.date() - dt.timedelta(days=1)
    n = 0
    for r in kpi_rows:
        if coerce_date(r.get("date_iso")) == yday and r.get("plant_key"):
            n += 1
    if n >= min_plants:
        return None
    return {"check": "kpi_yesterday", "severity": "CRITICAL",
            "detail": f"KPI_Daily has {n} plant rows for {yday.isoformat()} "
                      f"(expected >= {min_plants}) — kpi_eod failed or "
                      f"had no telemetry to stamp"}


def in_collection_window(now_mx: dt.datetime) -> bool:
    return COLLECT_START_H <= now_mx.hour < COLLECT_END_H


def check_freshness(name: str, newest: dt.datetime | None,
                    now: dt.datetime, max_age_min: int,
                    what: str) -> dict | None:
    """Fail when the newest timestamp is missing or older than max_age_min.

    `newest` and `now` must be in the SAME clock (both MX-naive or both
    UTC-aware) — the callers guarantee that.
    """
    if newest is None:
        return {"check": name, "severity": "CRITICAL",
                "detail": f"{what}: no parseable timestamps at all"}
    age_min = (now - newest).total_seconds() / 60.0
    if age_min <= max_age_min:
        return None
    return {"check": name, "severity": "CRITICAL",
            "detail": f"{what}: newest data is {age_min:.0f} min old "
                      f"(threshold {max_age_min} min; newest "
                      f"{newest:%Y-%m-%d %H:%M})"}


# --- IO -----------------------------------------------------------------------

def write_failures(sheets: SheetsClient, failures: list[dict],
                   now_utc: dt.datetime) -> None:
    sheets.ensure_tab(WATCHDOG_TAB)
    sheets.ensure_header(WATCHDOG_TAB, WATCHDOG_HEADER)
    sheets.append_rows(WATCHDOG_TAB, [[
        now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        f["check"], f["severity"], f["detail"], "",
    ] for f in failures])


def run(v2: SheetsClient, v1: SheetsClient | None, *, apply: bool,
        max_age_min: int, min_kpi_plants: int,
        now_mx: dt.datetime | None = None) -> int:
    now_mx = now_mx or dt.datetime.now(MX_TZ).replace(tzinfo=None)
    now_utc = dt.datetime.now(UTC)
    failures: list[dict] = []

    f = check_kpi_yesterday(v2.read_table("KPI_Daily", "A1:V"),
                            now_mx, min_kpi_plants)
    if f:
        failures.append(f)

    if in_collection_window(now_mx):
        tele = v2.read_table("Telemetry_Argia", "A1:Z")
        f = check_freshness("telemetry_v2",
                            newest_ts(tele, "timestamp_mx"),
                            now_mx, max_age_min,
                            "v2 telemetry (Telemetry_Argia)")
        if f:
            failures.append(f)

        if v1 is not None:
            v1rows = v1.read_table(V1_FEED_TAB, "A1:B")
            f = check_freshness("pi_v1_feed",
                                newest_ts(v1rows, V1_FEED_TS_COLUMN),
                                now_utc.replace(tzinfo=None), max_age_min,
                                "Pi / v1 collector (InverterUnified10m)")
            if f:
                failures.append(f)
        else:
            print("NOTICE: GOOGLE_SHEET_ID (v1) not set — Pi check skipped")
    else:
        print(f"outside collection window ({now_mx:%H:%M} MX) — "
              f"freshness checks skipped, KPI check only")

    if not failures:
        print("watchdog: ALL OK — data arrived")
        return 0

    for f in failures:
        print(f"watchdog FAILURE [{f['check']}] {f['detail']}")
    if apply:
        write_failures(v2, failures, now_utc)
        print(f"[apply] {len(failures)} row(s) appended to {WATCHDOG_TAB} "
              f"— notifier will e-mail within ~5 min")
    else:
        print("[dry-run] failure rows NOT written (pass --apply)")
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pipeline dead-man's switch")
    ap.add_argument("--apply", action="store_true",
                    help="write failure rows (default: dry-run)")
    ap.add_argument("--max-age-min", type=int, default=90,
                    help="freshness threshold in minutes (default 90)")
    ap.add_argument("--min-kpi-plants", type=int, default=4,
                    help="minimum KPI rows expected for yesterday")
    args = ap.parse_args(argv)

    v2_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not v2_id:
        print("ERROR: GOOGLE_SHEET_ID_V2 not set", file=sys.stderr)
        return 2
    v1_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    v2 = SheetsClient(v2_id)
    v1 = SheetsClient(v1_id) if v1_id else None
    return run(v2, v1, apply=args.apply, max_age_min=args.max_age_min,
               min_kpi_plants=args.min_kpi_plants)


if __name__ == "__main__":
    sys.exit(main())
