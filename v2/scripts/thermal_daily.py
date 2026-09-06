"""Nightly inverter thermal health (v216): evaluates every plant-day's
5-minute telemetry with argia.analytics.thermal and stores thermal_daily
(per inverter: peak, hours hot, events, ΔT vs peers / ambient, suspected
derating minutes, lost kWh, cooling health) and thermal_bins (the
temperature-binned actual/expected ratios behind the derating curve).

    thermal_daily.py                      # yesterday MX
    thermal_daily.py --date 2026-09-05
    thermal_daily.py --days-back 90       # backfill (history is the evidence)
    thermal_daily.py --report GTO1        # print the 30-day curve + knee, no writes

The DC-size baseline of each inverter (its usual ratio to cooler peers
on cool intervals) comes from the last 30 stored days, so the first
backfill pass should run oldest-first (it does) and the numbers settle
after a few days. Requires ARGIA_PG_MIRROR=1; quiet elsewhere.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from typing import Dict, List, Optional

from argia.analytics import thermal as TH
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror
from argia.store.pgq import psql_exec, psql_rows

LOG = logging.getLogger("argia.thermal_daily")
BASELINE_DAYS = 30


def rated_by_plant() -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    for r in psql_rows("SELECT plant_key, inverter_sn, rated_kw FROM inverter WHERE active AND rated_kw > 0;"):
        if len(r) >= 3:
            try:
                out.setdefault(r[0], {})[r[1].strip()] = float(r[2])
            except ValueError:
                continue
    return out


def baseline(plant_key: str, date_iso: str) -> Dict[str, float]:
    rows = psql_rows("SELECT inverter_sn, cool_ratio FROM thermal_daily"
                     f" WHERE plant_key = '{plant_key}' AND prod_date < DATE '{date_iso}'"
                     f" AND prod_date >= DATE '{date_iso}' - {BASELINE_DAYS} AND cool_ratio IS NOT NULL;")
    return TH.baseline_from_history((r[0], float(r[1])) for r in rows if len(r) >= 2 and r[1])


def samples(plant_key: str, date_iso: str) -> List[TH.Sample]:
    out: List[TH.Sample] = []
    for r in psql_rows(TH.telemetry_sql(plant_key, date_iso)):
        if len(r) < 5:
            continue
        ts = TH.parse_ts(r[0])
        if ts is None:
            continue
        out.append((ts, r[1], float(r[2]) if r[2] else None, float(r[3]) if r[3] else None,
                    float(r[4]) if r[4] else None))
    return out


def run_day(date_iso: str, rated: Dict[str, Dict[str, float]], dry_run: bool) -> int:
    n = 0
    for pk, kw in sorted(rated.items()):
        smp = samples(pk, date_iso)
        if not smp:
            continue
        days = TH.evaluate_day(smp, kw, baseline(pk, date_iso))
        hot = [d for d in days.values() if d.samples and d.peak_c is not None and d.peak_c >= TH.T_HOT]
        for d in sorted(days.values(), key=lambda x: x.sn):
            if d.samples:
                LOG.info("thermal %s %s %s: peak %.1f C (%s) hot %d min, dT peer %s, derating %d min, lost %.1f kWh, cooling %s",
                         date_iso, pk, d.sn, d.peak_c, d.band, d.minutes_over_65,
                         d.dt_peer_peak_c, d.derating_minutes, d.lost_kwh, d.cooling_health)
        if not dry_run:
            for sql in TH.build_upsert_sql(pk, date_iso, days):
                psql_exec(sql)
        n += sum(1 for d in days.values() if d.samples)
        if hot:
            LOG.info("thermal %s %s: %d inverter(s) >= %.0f C, lost %.1f kWh",
                     date_iso, pk, len(hot), TH.T_HOT, sum(d.lost_kwh for d in hot))
    return n


def report(plant_key: str, days_back: int = 30) -> None:
    today = dt.datetime.now(MX_TZ).date()
    d0 = (today - dt.timedelta(days=days_back)).isoformat()
    rows = psql_rows("SELECT inverter_sn, bin_c, n, ratio_sum FROM thermal_bins"
                     f" WHERE plant_key = '{plant_key}' AND prod_date >= DATE '{d0}';")
    by_sn: Dict[str, List] = {}
    for r in rows:
        if len(r) >= 4:
            by_sn.setdefault(r[0], []).append((float(r[1]), int(r[2]), float(r[3])))
    for sn, lst in sorted(by_sn.items()):
        c = TH.derating_curve(TH.merge_bins(lst))
        print(f"{plant_key} {sn}: knee {c['knee_c']} C, ratio above 65 C {c['ratio_above_65']} "
              f"(loss {c['loss_above_65_pct']} %)")
        for p in c["points"]:
            print(f"   {p['bin_c']:5.1f} C  n={p['n']:5d}  ratio={p['ratio']:.3f}")
    tot = psql_rows("SELECT inverter_sn, sum(minutes_over_65), sum(events), sum(derating_minutes), sum(lost_kwh),"
                    " max(peak_c), max(dt_peer_peak_c) FROM thermal_daily"
                    f" WHERE plant_key = '{plant_key}' AND prod_date >= DATE '{d0}' GROUP BY 1 ORDER BY 1;")
    for r in tot:
        print(f"{plant_key} {r[0]}: {int(float(r[1] or 0))//60} h >= 65 C, {r[2]} events, "
              f"{int(float(r[3] or 0))//60} h suspected derating, lost {float(r[4] or 0):.1f} kWh, "
              f"peak {r[5]} C, dT peer {r[6]} C")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="nightly inverter thermal health")
    ap.add_argument("--date", default=None, help="MX date (default: yesterday)")
    ap.add_argument("--days-back", type=int, default=1, help="evaluate this many days ending at --date, oldest first")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default=None, help="print the 30-day curve for a plant, no writes")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    if a.report:
        report(a.report.upper())
        return 0
    psql_exec(TH.ENSURE_SQL)
    end = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(MX_TZ).date() - dt.timedelta(days=1)
    rated = rated_by_plant()
    total = 0
    for back in range(a.days_back - 1, -1, -1):
        d = (end - dt.timedelta(days=back)).isoformat()
        total += run_day(d, rated, a.dry_run)
    LOG.info("DONE: %d inverter-days evaluated dry_run=%s", total, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
