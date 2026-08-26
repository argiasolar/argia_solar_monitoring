"""Argia_Mont — nightly satellite irradiance cross-check (Open-Meteo).

Runs on pio06 daily at 06:20 MX (argia-satcheck.timer), right after
argia-kpi has stamped yesterday's irradiance, and BEFORE argia-mailer's
next tick — so a drift verdict lands in the same morning's alert mail.

Per active plant with coordinates: fetch ~40 days of satellite daily GHI,
build the measured/satellite ratio series from daily_production
.irradiance_kwh_m2, run the drift check, upsert one row per (plant, day)
into satellite_check. The alert mailer emails REVIEW verdicts.

USAGE
    PYTHONPATH=. python scripts/satellite_check.py
    PYTHONPATH=. python scripts/satellite_check.py --dry-run

EXIT CODES
    0  every plant produced a verdict (OK / REVIEW / honest NO_DATA)
    1  one or more plants failed to fetch or store
    2  PG mirror disabled or plant config unreadable
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request
from typing import Dict, List, Tuple

from argia.kpi.satellite import (
    build_url, drift_check, parse_daily_ghi, ratio_series,
)
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.satellite_check")

FETCH_TIMEOUT_S = 30
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
        LOG.error("ARGIA_PG_MIRROR not enabled — nothing to do")
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

    failures = 0
    for pk, lat, lon in plants:
        try:
            sat = parse_daily_ghi(fetch_json(build_url(lat, lon)))
        except Exception as e:  # noqa: BLE001 — one plant must not kill the run
            LOG.error("%s: Open-Meteo fetch failed: %s", pk, e)
            failures += 1
            continue
        if not sat:
            LOG.error("%s: Open-Meteo response unusable (unit/shape)", pk)
            failures += 1
            continue
        ratios = ratio_series(measured.get(pk, {}), sat)
        dc = drift_check(ratios)
        LOG.info("%s: %s drift=%s%% recent=%s baseline=%s (%d/%d days)%s",
                 pk, dc.status,
                 dc.drift_pct if dc.drift_pct is not None else "-",
                 dc.recent_median, dc.baseline_median,
                 dc.n_recent, dc.n_baseline,
                 " — " + dc.note if dc.status != "OK" else "")
        if not args.dry_run:
            try:
                psql_exec(upsert_sql(pk, dc))
            except RuntimeError as e:
                LOG.error("%s: store failed: %s", pk, e)
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
