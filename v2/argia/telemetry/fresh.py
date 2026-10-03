"""Repeated (frozen) telemetry readings - one rule for every live page (v302).

Found 2026-10-02 (Tomasz, cpa.argia.com.mx said "43 kW now" after sunset):
when a Huawei datalogger stops uploading, FusionSolar keeps answering with
the inverter's LAST values. The collector stores them every 5 minutes as
new rows, so the newest reading looks fresh and the plant looks like it is
producing. SAG (MEX1) on 2 Oct: 12,301 + 15,520 + 15,519 W and the same day
counters from 16:35 to past midnight; in the 30 days before, the same
pattern also appeared in daylight (MEX1 and MEX2), i.e. the portal showed a
logger outage as live power.

A reading is a REPEAT of the previous one of the same inverter (ignoring
rows without power) when

* its power is above zero and identical to the previous power, AND
* its day counter (etoday_kwh) is known and identical to the previous one,
  AND
* that power, held for the time between the two readings, would have
  added at least REPEAT_MIN_KWH to the counter.

A real inverter cannot do that: delivering power moves the counter. At
very low power (below ~0.6 kW over 5 minutes) a counter with 0.1 kWh steps
may legitimately not move, so the rule stays silent there. A plant held at
its export limit has constant power but a moving counter - not a repeat.
Checked against 30 days of production telemetry before release: every
identical-power run with a moving counter: 0.

A repeat is not data: the live pages skip it, so the inverter's newest
REAL reading decides how old the data is (stale / dark) and what power is
shown. The stored rows are never changed.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Sequence, Tuple

REPEAT_MIN_KWH = 0.05

#: SQL condition on a row that carries lag() values p0 (power), e0 (counter), t0 (time)
REPEAT_SQL = ("power_w > 0 AND power_w = p0 AND etoday_kwh IS NOT NULL AND etoday_kwh = e0"
              f" AND power_w / 1000.0 * extract(epoch FROM ts_utc - t0) / 3600.0 >= {REPEAT_MIN_KWH}")


def repeat_cte(since_sql: str) -> str:
    """``rep AS (...)``: (plant_key, inverter_sn, ts_utc) of every repeat
    since ``since_sql`` (an SQL timestamp expression). Start it a little
    earlier than the rows you read, so their predecessor is inside it."""
    return ("rep AS (SELECT plant_key, inverter_sn, ts_utc FROM ("
            " SELECT plant_key, inverter_sn, ts_utc, power_w, etoday_kwh,"
            "  lag(power_w) OVER w AS p0, lag(etoday_kwh) OVER w AS e0, lag(ts_utc) OVER w AS t0"
            f"  FROM telemetry WHERE power_w IS NOT NULL AND ts_utc > {since_sql}"
            "  WINDOW w AS (PARTITION BY plant_key, inverter_sn ORDER BY ts_utc)) x"
            f" WHERE {REPEAT_SQL})")


def not_repeat(alias: str = "telemetry") -> str:
    """SQL predicate for a telemetry row (table alias ``alias``): it is not a repeat."""
    return (f"NOT EXISTS (SELECT 1 FROM rep WHERE rep.plant_key = {alias}.plant_key"
            f" AND rep.inverter_sn = {alias}.inverter_sn AND rep.ts_utc = {alias}.ts_utc)")


Reading = Tuple[dt.datetime, Optional[float], Optional[float]]     # (ts, power_w, etoday_kwh)


def is_repeat(prev: Optional[Reading], cur: Reading) -> bool:
    """The rule above in Python (the SQL twin is tested to agree). Pure."""
    if prev is None:
        return False
    t0, p0, e0 = prev
    t, p, e = cur
    if p is None or p0 is None or e is None or e0 is None or p <= 0:
        return False
    hours = (t - t0).total_seconds() / 3600.0
    return p == p0 and e == e0 and p / 1000.0 * hours >= REPEAT_MIN_KWH


def repeats(readings: Sequence[Reading]) -> list:
    """Flags for one inverter's readings in time order; rows without power
    are skipped (they neither repeat nor break a run). Pure."""
    out, prev = [], None
    for r in readings:
        if r[1] is None:
            out.append(False)
            continue
        out.append(is_repeat(prev, r))
        prev = r
    return out


def drop_repeats(rows: Sequence, inverter, reading) -> list:
    """v303: ``rows`` without the repeats - for the alert jobs, which read
    telemetry rows rather than SQL. ``inverter(row)`` names the unit,
    ``reading(row)`` gives its (ts, power_w, etoday_kwh). Rows keep their
    order; each inverter's rows are judged in time order. Pure."""
    by_inv: dict = {}
    for i, r in enumerate(rows):
        by_inv.setdefault(inverter(r), []).append(i)
    drop = set()
    for idx in by_inv.values():
        idx.sort(key=lambda i: reading(rows[i])[0])
        for i, flag in zip(idx, repeats([reading(rows[i]) for i in idx])):
            if flag:
                drop.add(i)
    return [r for i, r in enumerate(rows) if i not in drop]
