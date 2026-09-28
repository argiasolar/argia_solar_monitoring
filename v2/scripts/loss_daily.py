"""v264 - nightly: expected vs actual per plant-day, the loss in MXN, by cause.

Writes ``loss_daily`` (created here if missing). The method and every
threshold live in argia/analytics/losses.py; this script only gathers the
inputs from PostgreSQL and upserts the result.

Runs at the end of kpi_eod (06:00 MX, after the day's energy and
expected_kwh are stamped) for the last 3 days, so a late vendor counter or
a thermal re-run is picked up. By hand:

    python scripts/loss_daily.py --dry-run                   # print, write nothing
    python scripts/loss_daily.py --from 2026-09-01           # backfill (telemetry is kept ~30 days;
                                                             # older days get no unavailability split)
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections import defaultdict
from statistics import median
from typing import Dict, List, Optional, Tuple

from argia.analytics import losses as L
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.loss_daily")
CALIB_WINDOW_DAYS = 90       # long enough that a few bad weeks cannot redefine "normal"

ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS loss_daily (
    plant_key text NOT NULL,
    prod_date date NOT NULL,
    kwp_dc numeric(10,3),
    expected_weather_kwh numeric(12,1),
    expected_peers_kwh numeric(12,1),
    expected_kwh numeric(12,1),
    expected_basis text DEFAULT ''::text NOT NULL,
    peers text DEFAULT ''::text NOT NULL,
    actual_kwh numeric(12,1),
    lost_kwh numeric(12,1),
    unavailability_kwh numeric(12,1),
    overheating_kwh numeric(12,1),
    underperformance_kwh numeric(12,1),
    excused_kwh numeric(12,1),
    tariff_mxn numeric(8,4),
    lost_mxn numeric(12,2),
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT loss_daily_pkey PRIMARY KEY (plant_key, prod_date)
);
"""


def _f(v) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def plants() -> Dict[str, dict]:
    out = {}
    for r in psql_rows("SELECT plant_key, kwp_dc, lat, lon, pr_baseline, tariff_mxn_per_kwh, portfolio,"
                       " (SELECT count(*) FROM inverter i WHERE i.plant_key = p.plant_key AND i.active)"
                       " FROM plant p WHERE active ORDER BY 1;"):
        out[r[0]] = {"kwp": _f(r[1]) or 0.0, "lat": _f(r[2]), "lon": _f(r[3]), "pr": _f(r[4]),
                     "tariff": _f(r[5]), "portfolio": r[6], "n_inv": int(r[7] or 0)}
    return out


def production(d0: dt.date, d1: dt.date) -> Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]]:
    """(plant, day) -> (energy_kwh, weather expected_kwh)."""
    return {(r[0], r[1]): (_f(r[2]), _f(r[3])) for r in psql_rows(
        "SELECT plant_key, prod_date::text, energy_kwh, expected_kwh FROM daily_production"
        f" WHERE prod_date BETWEEN DATE '{d0}' AND DATE '{d1}';")}


def tariffs(d0: dt.date, d1: dt.date) -> Dict[Tuple[str, str], float]:
    """(plant, 'YYYY-MM') -> PPA tariff of the month."""
    return {(r[0], f"{int(r[1]):04d}-{int(r[2]):02d}"): float(r[3]) for r in psql_rows(
        "SELECT plant_key, year, month, tariff_mxn FROM contract_monthly WHERE tariff_mxn > 0"
        f" AND make_date(year, month, 1) BETWEEN date_trunc('month', DATE '{d0}') AND DATE '{d1}';")}


def thermal(d0: dt.date, d1: dt.date) -> Dict[Tuple[str, str], float]:
    return {(r[0], r[1]): _f(r[2]) or 0.0 for r in psql_rows(
        "SELECT plant_key, prod_date::text, sum(coalesce(lost_kwh, 0)) FROM thermal_daily"
        f" WHERE prod_date BETWEEN DATE '{d0}' AND DATE '{d1}' GROUP BY 1, 2;")}


def slots(d0: dt.date, d1: dt.date) -> Dict[Tuple[str, str], Dict[str, L.Slot]]:
    """(plant, MX day) -> {slot: Slot} for every 5-minute slot with telemetry.
    Excused = inside an approved customer maintenance window."""
    rows = psql_rows(f"""
WITH s AS (
  SELECT plant_key, to_timestamp(floor(extract(epoch FROM ts_utc) / 300) * 300) AS slot,
         inverter_sn, max(power_w) AS p, max(irradiance_wm2) AS irr
  FROM telemetry
  WHERE ts_utc >= (DATE '{d0}' AT TIME ZONE 'America/Mexico_City')
    AND ts_utc <  ((DATE '{d1}' + 1) AT TIME ZONE 'America/Mexico_City')
  GROUP BY 1, 2, 3)
SELECT s.plant_key, (s.slot AT TIME ZONE 'America/Mexico_City')::date::text, to_char(s.slot, 'HH24:MI'),
       max(s.irr), count(*) FILTER (WHERE s.p = 0), count(s.p), coalesce(sum(s.p), 0) / 1000.0,
       EXISTS (SELECT 1 FROM maintenance_event m
               WHERE m.plant_key = s.plant_key AND m.category = 'customer'
                 AND m.approved_by IS NOT NULL AND s.slot >= m.start_ts
                 AND s.slot < coalesce(m.end_ts, now()))
FROM s GROUP BY 1, 2, 3, s.slot;""")
    out: Dict[Tuple[str, str], Dict[str, L.Slot]] = defaultdict(dict)
    for r in rows:
        out[(r[0], r[1])][r[2]] = L.Slot(_f(r[3]), int(r[4] or 0), int(r[5] or 0), _f(r[6]) or 0.0, r[7] == "t")
    return out


def compute(d0: dt.date, d1: dt.date) -> List[L.LossDay]:
    P = plants()
    groups = L.peer_groups({k: (v["lat"], v["lon"]) for k, v in P.items()})
    c0 = d0 - dt.timedelta(days=CALIB_WINDOW_DAYS)
    prod = production(c0, d1)
    tar, th, sl = tariffs(d0, d1), thermal(d0, d1), slots(d0, d1)

    def sy(k, day):
        e, _ = prod.get((k, day), (None, None))
        return (e / P[k]["kwp"]) if (e is not None and P[k]["kwp"]) else None

    def is_healthy(k, day):
        e, x = prod.get((k, day), (None, None))
        return L.healthy(e, x)

    def healthy_peer_yields(k, day):
        return {p: sy(p, day) for p in groups.get(k, []) if p in P and is_healthy(p, day) and sy(p, day)}

    def peer_irradiance(k, day):
        per_slot: Dict[str, list] = defaultdict(list)
        for p in groups.get(k, []):
            for ts, s in sl.get((p, day), {}).items():
                if s.irradiance is not None:
                    per_slot[ts].append(s.irradiance)
        return {ts: median(v) for ts, v in per_slot.items()}

    out = []
    for k, meta in P.items():
        # this plant's normal, from the CALIB_WINDOW_DAYS before each day (rolling:
        # a long backfill must not judge September by what was known in June)
        hist = []                                     # (day, own actual/model, own sy / peer median sy)
        day = c0
        while day < d1:
            ds = day.isoformat()
            if is_healthy(k, ds):
                e, x = prod[(k, ds)]
                py = healthy_peer_yields(k, ds)
                hist.append((ds, (e, x), (sy(k, ds), median(py.values())) if py else None))
            day += dt.timedelta(days=1)
        day = d0
        while day <= d1:
            ds = day.isoformat()
            actual, weather = prod.get((k, ds), (None, None))
            if actual is not None or weather is not None:
                lo = (day - dt.timedelta(days=CALIB_WINDOW_DAYS)).isoformat()
                window = [h for h in hist if lo <= h[0] < ds]
                peer_ratio = L.calibration([h[2] for h in window if h[2]])
                weather_ratio = L.calibration([h[1] for h in window])
                tariff = (tar.get((k, ds[:7])) or meta["tariff"]) if meta["portfolio"] == "PPA" else None
                out.append(L.compute_day(
                    k, ds, meta["kwp"], actual, weather, healthy_peer_yields(k, ds), peer_ratio, weather_ratio,
                    sl.get((k, ds), {}), peer_irradiance(k, ds), meta["n_inv"], th.get((k, ds), 0.0), tariff))
            day += dt.timedelta(days=1)
    return out


def _sql(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return repr(float(v))


def upsert_sql(days: List[L.LossDay]) -> str:
    cols = ("plant_key", "prod_date", "kwp_dc", "expected_weather_kwh", "expected_peers_kwh", "expected_kwh",
            "expected_basis", "peers", "actual_kwh", "lost_kwh", "unavailability_kwh", "overheating_kwh",
            "underperformance_kwh", "excused_kwh", "tariff_mxn", "lost_mxn")
    vals = ",\n".join("(" + ", ".join(_sql(getattr(d, c)) for c in cols) + ")" for d in days)
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[2:])
    return (f"INSERT INTO loss_daily ({', '.join(cols)}) VALUES\n{vals}\n"
            f"ON CONFLICT (plant_key, prod_date) DO UPDATE SET {upd}, computed_at = now();")


def run(d0: dt.date, d1: dt.date, dry_run: bool = False) -> List[L.LossDay]:
    psql_exec(ENSURE_SQL)
    days = compute(d0, d1)
    for d in days:
        if d.lost_kwh:
            LOG.info("%s %s: expected %s kWh (%s), actual %s, lost %s kWh = unavailability %s + overheating %s"
                     " + underperformance %s%s", d.prod_date, d.plant_key, d.expected_kwh, d.expected_basis,
                     d.actual_kwh, d.lost_kwh, d.unavailability_kwh, d.overheating_kwh, d.underperformance_kwh,
                     f" -> {d.lost_mxn:,.0f} MXN" if d.lost_mxn is not None else " (CAPEX, no tariff)")
    if days and not dry_run:
        psql_exec(upsert_sql(days))
    LOG.info("DONE: %d plant-day(s) %s..%s, lost %.0f kWh = %.0f MXN (PPA) dry_run=%s", len(days), d0, d1,
             sum(d.lost_kwh or 0 for d in days), sum(d.lost_mxn or 0 for d in days), dry_run)
    return days


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="daily production loss in MXN, by cause")
    ap.add_argument("--from", dest="d_from", default=None, help="first MX date (default: 3 days before --to)")
    ap.add_argument("--to", dest="d_to", default=None, help="last MX date (default: yesterday)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled - nothing to do here")
        return 0
    d1 = dt.date.fromisoformat(a.d_to) if a.d_to else dt.datetime.now(MX_TZ).date() - dt.timedelta(days=1)
    d0 = dt.date.fromisoformat(a.d_from) if a.d_from else d1 - dt.timedelta(days=2)
    run(d0, d1, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
