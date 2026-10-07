"""Argia_Mont - nightly satellite irradiance cross-check (Open-Meteo).

Runs on pio06 daily at 06:20 MX (argia-satcheck.timer), right after
argia-kpi has stamped yesterday's irradiance, and BEFORE argia-mailer's
next tick - so a drift verdict lands in the same morning's alert mail.

Per active plant with coordinates: fetch ~40 days of satellite daily GHI,
build the measured/satellite ratio series from daily_production
.irradiance_kwh_m2, run the drift check, upsert one row per (plant, day)
into satellite_check. The alert mailer emails REVIEW verdicts.

USAGE
    PYTHONPATH=. python scripts/satellite_check.py
    PYTHONPATH=. python scripts/satellite_check.py --dry-run

EXIT CODES
    0  every plant produced a verdict (OK / REVIEW / honest NO_DATA), or
       a few could not reach Open-Meteo after FETCH_TRIES attempts (v316:
       stored as NO_DATA with the reason, logged as WARNING)
    1  a store failed, or NO plant could reach Open-Meteo (the service or
       our network is down - that is worth a mail)
    2  PG mirror disabled or plant config unreadable

v316 (7 Oct 2026, "still failing after 6 h: job failed: argia-satcheck"):
one TLS handshake timeout to Open-Meteo for SLP2 at 06:20 MX failed the
whole daily unit, which then stayed "failed" until the next morning, so the
mailer kept reporting it. A fetch is now tried 3 times (waits 10 s, 30 s);
a plant that still cannot be reached gets an honest NO_DATA row saying so.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.request
from typing import Dict, List, Tuple

from argia.kpi.satellite import (
    build_url, drift_check, parse_daily_ghi, ratio_series,
)
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.satellite_check")

FETCH_TIMEOUT_S = 30
FETCH_TRIES = 3
FETCH_WAITS_S = (10, 30)          # between tries
SLEEP = time.sleep                # tests replace it
MEASURED_LOOKBACK_DAYS = 45

DDL = """
CREATE TABLE IF NOT EXISTS satellite_check (
    plant_key       text NOT NULL,
    check_date      date NOT NULL,
    status          text NOT NULL,
    drift_pct       numeric,
    recent_median   numeric,
    baseline_median numeric,
    n_recent        int  NOT NULL,
    n_baseline      int  NOT NULL,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plant_key, check_date)
);"""


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _num(v) -> str:
    return "NULL" if v is None else str(v)


def plant_coords() -> List[Tuple[str, float, float]]:
    out: List[Tuple[str, float, float]] = []
    for r in psql_rows("SELECT plant_key, lat, lon FROM plant"
                       " WHERE active AND lat IS NOT NULL"
                       " AND lon IS NOT NULL ORDER BY 1;"):
        if len(r) >= 3:
            try:
                out.append((r[0], float(r[1]), float(r[2])))
            except ValueError:
                LOG.warning("bad coords for %s: %s", r[0], r[1:3])
    return out


def measured_series() -> Dict[str, Dict[str, float]]:
    """{plant: {date_iso: measured kWh/m2}} from daily_production."""
    out: Dict[str, Dict[str, float]] = {}
    for r in psql_rows(
            "SELECT plant_key, prod_date::text, irradiance_kwh_m2"
            " FROM daily_production"
            f" WHERE prod_date >= current_date - {MEASURED_LOOKBACK_DAYS}"
            " AND irradiance_kwh_m2 IS NOT NULL;"):
        if len(r) >= 3:
            try:
                out.setdefault(r[0], {})[r[1]] = float(r[2])
            except ValueError:
                continue
    return out


def fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
        return json.load(resp)


def fetch_with_retry(url: str, pk: str = "") -> dict:
    """fetch_json, FETCH_TRIES times; the last error is raised."""
    for i in range(FETCH_TRIES):
        try:
            return fetch_json(url)
        except Exception as e:  # noqa: BLE001 - network, TLS, HTTP 5xx, bad JSON
            if i == FETCH_TRIES - 1:
                raise
            wait = FETCH_WAITS_S[min(i, len(FETCH_WAITS_S) - 1)]
            LOG.warning("%s: Open-Meteo try %d/%d failed (%s) - retrying in %d s", pk, i + 1, FETCH_TRIES, e, wait)
            SLEEP(wait)
    raise RuntimeError("unreachable")


def unreachable_verdict(err: Exception):
    """The honest row for a plant Open-Meteo could not serve today."""
    from argia.kpi.satellite import DriftCheck
    return DriftCheck(status="NO_DATA", drift_pct=None, recent_median=None, baseline_median=None,
                      n_recent=0, n_baseline=0,
                      note=f"satellite data unreachable after {FETCH_TRIES} tries: {str(err)[:120]}")


def upsert_sql(pk: str, dc) -> str:
    return (
        "INSERT INTO satellite_check (plant_key, check_date, status,"
        " drift_pct, recent_median, baseline_median, n_recent,"
        " n_baseline, note) VALUES ("
        f"{_txt(pk)},"
        " (now() AT TIME ZONE 'America/Mexico_City')::date,"
        f" {_txt(dc.status)}, {_num(dc.drift_pct)},"
        f" {_num(dc.recent_median)}, {_num(dc.baseline_median)},"
        f" {int(dc.n_recent)}, {int(dc.n_baseline)}, {_txt(dc.note)})"
        " ON CONFLICT (plant_key, check_date) DO UPDATE SET"
        " status = EXCLUDED.status, drift_pct = EXCLUDED.drift_pct,"
        " recent_median = EXCLUDED.recent_median,"
        " baseline_median = EXCLUDED.baseline_median,"
        " n_recent = EXCLUDED.n_recent,"
        " n_baseline = EXCLUDED.n_baseline, note = EXCLUDED.note,"
        " created_at = now();")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="satellite irradiance cross-check")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.error("ARGIA_PG_MIRROR not enabled - nothing to do")
        return 2
    try:
        plants = plant_coords()
        measured = measured_series()
    except RuntimeError as e:
        LOG.error("PG unreadable: %s", e)
        return 2
    if not plants:
        LOG.error("no active plants with coordinates")
        return 2

    if not args.dry_run:
        psql_exec(DDL)

    failures, unreachable = 0, 0
    for pk, lat, lon in plants:
        try:
            sat = parse_daily_ghi(fetch_with_retry(build_url(lat, lon), pk))
        except Exception as e:  # noqa: BLE001 - one plant must not kill the run
            LOG.warning("%s: Open-Meteo unreachable after %d tries: %s - NO_DATA today", pk, FETCH_TRIES, e)
            unreachable += 1
            dc = unreachable_verdict(e)
            sat = None
        else:
            if not sat:
                LOG.error("%s: Open-Meteo response unusable (unit/shape)", pk)
                failures += 1
                continue
            dc = drift_check(ratio_series(measured.get(pk, {}), sat))
        LOG.info("%s: %s drift=%s%% recent=%s baseline=%s (%d/%d days)%s",
                 pk, dc.status,
                 dc.drift_pct if dc.drift_pct is not None else "-",
                 dc.recent_median, dc.baseline_median,
                 dc.n_recent, dc.n_baseline,
                 " - " + dc.note if dc.status != "OK" else "")
        if not args.dry_run:
            try:
                psql_exec(upsert_sql(pk, dc))
            except RuntimeError as e:
                LOG.error("%s: store failed: %s", pk, e)
                failures += 1
    if unreachable == len(plants):
        LOG.error("Open-Meteo unreachable for every plant (%d) - service or network down", unreachable)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
