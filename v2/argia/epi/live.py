"""v382: the plant page's troubleshooting card from PostgreSQL.

The generator runs every 5 minutes and also redraws the pages of past days, so:
* today is computed on every run (one plant-day, two read-only queries);
* a closed day is computed once and kept as JSON in CACHE_DIR/<plant_key>/<date>.json,
  with its own tallies, so the month-to-date (MTD) is a sum of cached tallies;
* a cached day is used only when it was computed with the same configuration
  (fingerprint of the EPI files + RULES_VERSION) and after the day had settled
  (SETTLE_H hours after its end: late API backfills land within that time);
* at most FILL_PER_RUN missing days are computed per plant and run, so a cold
  cache never stalls the generator; the card says how many days its MTD holds.
Every failure is the caller's to swallow: a card must never cost the page.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from argia.epi import engine as E
from argia.epi import feed as F
from argia.epi import views as V
from argia.epi.config import FILES, PlantConfig

CACHE_DIR = os.environ.get('ARGIA_EPI_CACHE', '/var/lib/argia/epi')
RULES_VERSION = 'v382'
SETTLE_H = 6
FILL_PER_RUN = 3
Query = Callable[[str], List[List[str]]]
_KEY = re.compile(r'^[A-Z0-9]{2,12}$')


def fingerprint(epi_dir: str, layout_dir: str, key: str) -> str:
    h = hashlib.sha1(RULES_VERSION.encode())
    for p in [os.path.join(epi_dir, f) for f in FILES] + [os.path.join(layout_dir, f'{key}.json')]:
        with open(p, 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()[:16]


def _num(s: str) -> Optional[float]:
    try:
        return float(s) if s not in ('', None) else None
    except ValueError:
        return None


def _arr(s: str) -> List[Optional[float]]:
    return [_num(x) for x in s.split(',')] if s else []


def day_bounds(day: dt.date, tz: dt.tzinfo) -> Tuple[str, str]:
    t0 = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
    t1 = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz)
    return (t0.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S+00'),
            t1.astimezone(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S+00'))


def load_day(q: Query, key: str, day: dt.date, tz: dt.tzinfo):
    """(station, samples, ac) of one plant, one local day: read-only SELECTs."""
    if not _KEY.match(key):
        raise ValueError(f'bad plant key {key!r}')
    t0, t1 = day_bounds(day, tz)
    w = f"plant_key = '{key}' AND ts_utc >= '{t0}' AND ts_utc < '{t1}'"
    tel = q("SET statement_timeout='30s'; SELECT inverter_sn, extract(epoch FROM ts_utc), power_w, etoday_kwh,"
            f" irradiance_wm2, module_temp_c, ambient_temp_c FROM telemetry WHERE {w};")
    st = q("SET statement_timeout='30s'; SELECT inverter_sn, extract(epoch FROM ts_utc),"
           " array_to_string(str_a, ',', ''), array_to_string(mppt_a, ',', ''), array_to_string(mppt_v, ',', '')"
           f" FROM string_sample WHERE {w};")
    tel = [r + [''] * (7 - len(r)) for r in tel if len(r) >= 2]
    station = F.station_from_rows((float(r[1]), _num(r[4]), _num(r[5]), _num(r[6])) for r in tel)
    ac, _ctr, _stale = F.ac_and_counters((r[0], float(r[1]), _num(r[2]), _num(r[3])) for r in tel)
    st = [r + [''] * (5 - len(r)) for r in st if len(r) >= 2]
    samples = F.samples_from_rows((r[0], float(r[1]), _arr(r[2]), _arr(r[3]), _arr(r[4])) for r in st)
    return station, samples, ac


def build_day(q: Query, cfg: PlantConfig, day: dt.date, tz: dt.tzinfo, inv_labels=None) -> dict:
    station, samples, ac = load_day(q, cfg.key, day, tz)
    labels = {s.uid: s.uid for s in cfg.strings}
    return V.build(cfg.pm, cfg.strings, cfg.channel_of, cfg.sn_of, station, samples, ac, day, tz,
                   labels=labels, inv_labels=inv_labels, workorder=cfg.workorder)


def _path(cache_dir: str, key: str, day: dt.date) -> str:
    return os.path.join(cache_dir, key, f'{day.isoformat()}.json')


def read_cached(cache_dir: str, key: str, day: dt.date, fp: str) -> Optional[dict]:
    try:
        with open(_path(cache_dir, key, day), encoding='utf-8') as fh:
            c = json.load(fh)
    except (OSError, ValueError):
        return None
    return c['payload'] if c.get('fp') == fp and c.get('settled') else None


def write_cached(cache_dir: str, key: str, day: dt.date, fp: str, payload: dict, settled: bool) -> None:
    p = _path(cache_dir, key, day)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump({'fp': fp, 'settled': settled, 'computed_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'payload': payload}, fh, separators=(',', ':'))
    os.replace(tmp, p)


def is_settled(day: dt.date, tz: dt.tzinfo, now: dt.datetime) -> bool:
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=tz)
    return now >= end + dt.timedelta(hours=SETTLE_H)


class Builder:
    """One per generator run: remembers the fill budget per plant and the payloads it made."""

    def __init__(self, q: Query, configs: Dict[str, PlantConfig], tz: dt.tzinfo, now: dt.datetime,
                 fps: Dict[str, str], cache_dir: str = CACHE_DIR, fill_per_run: int = FILL_PER_RUN):
        self.q, self.configs, self.tz, self.now, self.fps = q, configs, tz, now, fps
        self.cache_dir, self.fill_per_run = cache_dir, fill_per_run
        self.budget: Dict[str, int] = {}
        self.sums: Dict[Tuple[str, dt.date], dict] = {}     # only the tallies stay in memory

    def day_payload(self, key: str, day: dt.date, inv_labels=None, use_budget: bool = True) -> Optional[dict]:
        """The day-only payload (no MTD), from memory, the cache or a fresh build; None = not computed now."""
        cfg, fp = self.configs[key], self.fps[key]
        settled = is_settled(day, self.tz, self.now)
        p = read_cached(self.cache_dir, key, day, fp) if settled else None
        if p is None:
            if use_budget:
                left = self.budget.setdefault(key, self.fill_per_run)
                if left <= 0:
                    return None
                self.budget[key] = left - 1
            p = build_day(self.q, cfg, day, self.tz, inv_labels)
            if settled:
                write_cached(self.cache_dir, key, day, fp, p, True)
        self.sums[(key, day)] = p['tallies']
        return p

    def day_tallies(self, key: str, day: dt.date, inv_labels=None) -> Optional[dict]:
        if (key, day) not in self.sums:
            self.day_payload(key, day, inv_labels)
        return self.sums.get((key, day))

    def card_payload(self, key: str, day: dt.date, inv_labels=None) -> Optional[dict]:
        """The day's payload with the month-to-date of the days before it (as far as computed).
        Today is always computed; a past day counts against the fill budget (None = not yet)."""
        p = self.day_payload(key, day, inv_labels, use_budget=day < self.now.astimezone(self.tz).date())
        if p is None:
            return None
        mtd, n = {}, 1
        for k in range(1, day.day):
            dd = day.replace(day=k)
            if any(s.cod <= dd for s in self.configs[key].strings):
                e = self.day_tallies(key, dd, inv_labels)
                if e is None:
                    continue
                mtd, n = V.merge_tallies(mtd, e), n + 1
        days = sum(1 for k in range(1, day.day + 1)
                   if any(s.cod <= day.replace(day=k) for s in self.configs[key].strings))
        V.apply_mtd(p, mtd, n)                   # p is this call's own copy (built or read from the cache)
        p['plant']['mtd_days_expected'] = days
        return p
