"""v385: the plants' grid meters (``meter_sample``) - consumption and solar coverage.

Tomasz 2026-11-20: show each plant's consumption and how much of it the
solar covers. The vendor portals already read the meter at the grid
connection of three PPA plants (SAG and Vitalmex on Huawei, Quimica
Coyoacan on Growatt); nothing new is installed.

One normalized sign for every vendor: ``grid_w`` > 0 = drawn FROM the
grid (import), < 0 = sent TO the grid (export).

* Huawei ``getDevFiveMinutes`` (devTypeId 17), one row per 5 minutes:
  ``active_power`` W, export positive (so grid_w = -active_power);
  ``reverse_active_cap`` = lifetime import kWh, ``active_cap`` = lifetime
  export kWh.
* Growatt web ``/device/getMeterHistory``: ``activePower`` is clamped at
  zero and useless, the signed power is ``activePowerL1+L2+L3`` (import
  positive, already our sign); ``positiveActiveTodayEnergy`` /
  ``reverseActiveTodayEnergy`` = import / export so far today (kWh, the
  meter's own day registers); ``activeEnergy`` = lifetime import (kept,
  not trusted until validated); ``reverseActiveEnergy`` is erratic and
  never read. The row time is a Java calendar in local time whose month
  is 0-based.

Pure parsing and arithmetic; the only I/O is ``store`` (fail-soft, like
``string_sample``).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from argia.core.normalize import safe_float

LOG = logging.getLogger("argia.meter")
UTC = dt.timezone.utc
KEEP_DAYS = 800
MAX_GAP_S = 15 * 60          # a longer silence between two samples is not integrated
FULL_DAY_SAMPLES = 250       # 288 expected; fewer = the day is refetched by the self-heal

ENSURE_SQL = (
    "CREATE TABLE IF NOT EXISTS meter_sample (\n"
    "    ts_utc timestamptz NOT NULL,\n"
    "    plant_key text NOT NULL,\n"
    "    meter_id text NOT NULL,\n"
    "    grid_w real,\n"
    "    imp_total_kwh double precision,\n"
    "    exp_total_kwh double precision,\n"
    "    imp_day_kwh real,\n"
    "    exp_day_kwh real,\n"
    "    PRIMARY KEY (plant_key, meter_id, ts_utc)\n);\n"
    "CREATE INDEX IF NOT EXISTS idx_meter_sample_ts ON meter_sample (ts_utc);\n")
PRUNE_SQL = f"DELETE FROM meter_sample WHERE ts_utc < now() - interval '{KEEP_DAYS} days';"


@dataclass(frozen=True)
class MeterSample:
    ts_utc: dt.datetime          # aware, UTC
    plant_key: str
    meter_id: str
    grid_w: Optional[float]      # + import, - export
    imp_total_kwh: Optional[float] = None
    exp_total_kwh: Optional[float] = None
    imp_day_kwh: Optional[float] = None
    exp_day_kwh: Optional[float] = None


# ----------------------------------------------------------------- parsers

def parse_huawei_five_minutes(plant_key: str, payload: Mapping[str, Any]) -> List[MeterSample]:
    """``getDevFiveMinutes`` answer -> samples (all meters in the answer)."""
    out = []
    for row in (payload or {}).get("data") or []:
        m = row.get("dataItemMap") or {}
        ms = safe_float(row.get("collectTime"))
        if ms is None:
            continue
        p = safe_float(m.get("active_power"))
        out.append(MeterSample(
            ts_utc=dt.datetime.fromtimestamp(ms / 1000.0, UTC), plant_key=plant_key,
            meter_id=str(row.get("devId") or row.get("sn") or ""),
            grid_w=None if p is None else -p,
            imp_total_kwh=safe_float(m.get("reverse_active_cap")),
            exp_total_kwh=safe_float(m.get("active_cap"))))
    return [s for s in out if s.meter_id]


def growatt_time(cal: Mapping[str, Any], tz: dt.tzinfo) -> Optional[dt.datetime]:
    """Java calendar (local time, 0-based month) -> aware UTC datetime."""
    try:
        local = dt.datetime(int(cal["year"]), int(cal["month"]) + 1, int(cal["dayOfMonth"]),
                            int(cal["hourOfDay"]), int(cal["minute"]), int(cal.get("second") or 0), tzinfo=tz)
    except (KeyError, TypeError, ValueError):
        return None
    return local.astimezone(UTC)


def parse_growatt_meter_history(plant_key: str, meter_id: str, rows: Iterable[Mapping[str, Any]],
                                tz: dt.tzinfo) -> List[MeterSample]:
    """``getMeterHistory`` ``obj.datas`` rows (any order) -> samples."""
    out = []
    for x in rows or []:
        ts = growatt_time(x.get("calendar") or {}, tz)
        if ts is None:
            continue
        ph = [safe_float(x.get(k)) for k in ("activePowerL1", "activePowerL2", "activePowerL3")]
        out.append(MeterSample(
            ts_utc=ts, plant_key=plant_key, meter_id=meter_id,
            grid_w=None if all(v is None for v in ph) else sum(v or 0.0 for v in ph),
            imp_total_kwh=safe_float(x.get("activeEnergy")),
            imp_day_kwh=safe_float(x.get("positiveActiveTodayEnergy")),
            exp_day_kwh=safe_float(x.get("reverseActiveTodayEnergy"))))
    return out


# -------------------------------------------------------------- arithmetic

def _local_day(s: MeterSample, tz) -> str:
    return s.ts_utc.astimezone(tz).date().isoformat()


def _dedup(samples: Iterable[MeterSample]) -> List[MeterSample]:
    seen = {}
    for s in samples:
        seen[s.ts_utc] = s
    return [seen[k] for k in sorted(seen)]


def integrate(samples: Sequence[MeterSample], tz, by_hour: bool = False) -> Dict[Any, List[float]]:
    """Energy from the power samples of ONE meter: {local day (or (day, hour)): [import kWh, export kWh]}.
    Each sample's power holds until the next one (gaps over 15 min are not integrated)."""
    s = _dedup(x for x in samples if x.grid_w is not None)
    out: Dict[Any, List[float]] = {}
    for a, b in zip(s, s[1:]):
        secs = (b.ts_utc - a.ts_utc).total_seconds()
        if secs <= 0 or secs > MAX_GAP_S:
            continue
        loc = a.ts_utc.astimezone(tz)
        key = (loc.date().isoformat(), loc.hour) if by_hour else loc.date().isoformat()
        e = a.grid_w * secs / 3600.0 / 1000.0
        acc = out.setdefault(key, [0.0, 0.0])
        if e >= 0:
            acc[0] += e
        else:
            acc[1] -= e
    return out


def day_energy(samples: Sequence[MeterSample], day: str, tz) -> Optional[dict]:
    """Import / export of one local day for ONE meter, from the best register the vendor gives:
    the meter's day registers (Growatt), else the lifetime counters (Huawei: last reading of the
    day minus the last of the day before, else the first of the day), else the integrated power.
    Also returns the integrated power and the sample count so the two can be compared."""
    s = _dedup(samples)
    today = [x for x in s if _local_day(x, tz) == day]
    if not today:
        return None
    integ = integrate(s, tz).get(day)
    res = {"day": day, "n": len(today), "imp_power": integ[0] if integ else None,
           "exp_power": integ[1] if integ else None}
    di = [x.imp_day_kwh for x in today if x.imp_day_kwh is not None]
    de = [x.exp_day_kwh for x in today if x.exp_day_kwh is not None]
    if di and de:
        res.update(imp=max(di), exp=max(de), basis="day-register")
        return res
    ci = [x for x in today if x.imp_total_kwh is not None and x.exp_total_kwh is not None]
    if ci:
        prev = [x for x in s if x.ts_utc < ci[0].ts_utc and x.imp_total_kwh is not None
                and x.exp_total_kwh is not None and _local_day(x, tz) < day]
        start = prev[-1] if prev and (ci[0].ts_utc - prev[-1].ts_utc).total_seconds() <= MAX_GAP_S else ci[0]
        imp, exp = ci[-1].imp_total_kwh - start.imp_total_kwh, ci[-1].exp_total_kwh - start.exp_total_kwh
        if imp >= 0 and exp >= 0:                       # a counter that went back = meter swap / reset
            res.update(imp=imp, exp=exp, basis="lifetime-counter")
            return res
    if integ:
        res.update(imp=integ[0], exp=integ[1], basis="power")
        return res
    return None


def balance(pv_kwh: Optional[float], imp_kwh: Optional[float], exp_kwh: Optional[float]) -> Optional[dict]:
    """Consumption = solar + import - export; solar share = solar used on site / consumption.
    ``ok`` False when the numbers cannot be one site behind one meter (solar used on site below
    zero, i.e. more export than solar, beyond 2 % / 2 kWh)."""
    if pv_kwh is None or imp_kwh is None or exp_kwh is None:
        return None
    used = pv_kwh - exp_kwh
    cons = pv_kwh + imp_kwh - exp_kwh
    ok = used >= -max(2.0, 0.02 * max(pv_kwh, exp_kwh))
    share = (max(used, 0.0) / cons) if cons > 0 and ok else None
    return {"pv": pv_kwh, "imp": imp_kwh, "exp": exp_kwh, "cons": cons, "used": used, "share": share, "ok": ok}


def period_share(rows: Iterable[Optional[dict]]) -> Optional[float]:
    """Solar share of several days = ratio of the sums (never the mean of the days); bad days left out."""
    rows = [r for r in rows if r and r["ok"]]
    cons = sum(r["cons"] for r in rows)
    return sum(max(r["used"], 0.0) for r in rows) / cons if rows and cons > 0 else None


def short_days(counts: Mapping[str, int], days: Sequence[str]) -> List[str]:
    """Days of ``days`` holding fewer than a full day of samples (oldest first) - the self-heal list."""
    return [d for d in days if counts.get(d, 0) < FULL_DAY_SAMPLES]


# -------------------------------------------------------------------- store

def _num(v: Optional[float], nd: int = 3) -> str:
    return "NULL" if v is None else repr(round(float(v), nd))


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def build_upsert_sql(samples: Sequence[MeterSample]) -> Optional[str]:
    rows, seen = [], set()
    for s in samples:
        key = (s.plant_key, s.meter_id, s.ts_utc)
        if key in seen or s.grid_w is None and s.imp_total_kwh is None and s.imp_day_kwh is None:
            continue
        seen.add(key)
        rows.append(f"({_q(s.ts_utc.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'))}, {_q(s.plant_key)}, "
                    f"{_q(s.meter_id)}, {_num(s.grid_w, 1)}, {_num(s.imp_total_kwh)}, {_num(s.exp_total_kwh)}, "
                    f"{_num(s.imp_day_kwh)}, {_num(s.exp_day_kwh)})")
    if not rows:
        return None
    return ("INSERT INTO meter_sample (ts_utc, plant_key, meter_id, grid_w, imp_total_kwh, exp_total_kwh,"
            " imp_day_kwh, exp_day_kwh) VALUES\n" + ",\n".join(rows)
            + "\nON CONFLICT (plant_key, meter_id, ts_utc) DO UPDATE SET grid_w = EXCLUDED.grid_w,"
              " imp_total_kwh = EXCLUDED.imp_total_kwh, exp_total_kwh = EXCLUDED.exp_total_kwh,"
              " imp_day_kwh = EXCLUDED.imp_day_kwh, exp_day_kwh = EXCLUDED.exp_day_kwh;")


def store(samples: Sequence[MeterSample], dry_run: bool = False, log: Optional[logging.Logger] = None) -> int:
    """Upsert; returns rows written. Never raises (monitoring, not billing)."""
    lg = log or LOG
    sql = build_upsert_sql(samples)
    if sql is None:
        return 0
    n = sql.count("\n(")
    if dry_run:
        lg.info("DRY RUN: would upsert %d meter samples", n)
        return n
    try:
        from argia.store import pgq
        pgq.psql_exec(ENSURE_SQL + sql + "\n" + PRUNE_SQL, timeout=60)
    except Exception as e:  # noqa: BLE001
        lg.warning("meter_sample write failed: %s", str(e)[-300:])
        return 0
    return n
