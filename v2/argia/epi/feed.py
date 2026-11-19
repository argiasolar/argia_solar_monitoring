"""v382: rows -> the engine's 5-minute structures. One place, used by the month report
(scripts/epi600_month.py, from export files) and the plant page (monitoring_gen, from
PostgreSQL), so both judge the same buckets the same way."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Dict, Iterable, Optional, Sequence, Tuple

from argia.epi import engine as E
from argia.epi import gaps as G

Num = Optional[float]


def bucket(ts_epoch: float) -> int:
    return int(ts_epoch) // 300


def station_from_rows(rows: Iterable[Tuple[float, Num, Num, Num]]) -> Dict[int, E.Station]:
    """(epoch, ghi, panel T, ambient T) of every telemetry row of one plant -> station per bucket
    (the rows of one bucket carry the same station; their mean is taken, and their mean time)."""
    acc = defaultdict(list)
    for ts, g, tp, ta in rows:
        acc[bucket(ts)].append((ts, g, tp, ta))
    out = {}
    for b, rs in acc.items():
        def avg(i):
            v = [x[i] for x in rs if x[i] is not None]
            return sum(v) / len(v) if v else None
        out[b] = E.Station(dt.datetime.fromtimestamp(sum(x[0] for x in rs) / len(rs), dt.timezone.utc), avg(1), avg(2), avg(3))
    return out


def samples_from_rows(rows: Iterable[Tuple[str, float, Sequence[Num], Sequence[Num], Sequence[Num]]]
                      ) -> Dict[Tuple[str, int], E.InvSample]:
    """(serial, epoch, str_a, mppt_a, mppt_v) -> string sample per (serial, bucket); the last one wins."""
    out = {}
    for sn, ts, sa, ma, mv in sorted(rows, key=lambda r: r[1]):
        out[(sn, bucket(ts))] = E.InvSample(list(sa or []), list(ma or []), list(mv or []))
    return out


def ac_and_counters(rows: Iterable[Tuple[str, float, Num, Num]]):
    """(serial, epoch, AC power W, day counter kWh) -> (ac {(sn, b): W}, counter {sn: {b: kWh}},
    stale {sn: number of stale records dropped}). Stale API records are missing data (gaps.stale_buckets)."""
    per = defaultdict(list)
    for sn, ts, p, c in rows:
        per[sn].append((bucket(ts), p, c))
    ac, ctr, stale = {}, defaultdict(dict), {}
    for sn, rs in per.items():
        bad = G.stale_buckets(rs)
        stale[sn] = len(bad)
        for b, p, c in rs:
            if b in bad:
                continue
            if p is not None:
                ac[(sn, b)] = p
            if c is not None:
                ctr[sn][b] = c
    return ac, ctr, stale
