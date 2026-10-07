"""v317: the 5-minute per-string currents, kept (``string_sample``).

Tomasz 2026-10-07: string-level monitoring on the PPA plant pages, drawn
over each plant's layout drawing. Every number it needs is already in
the answers the 5-minute collector downloads - nothing new is asked of
any vendor:

* Growatt MAX history row: ``currentString1..32`` (A, one per string
  input) and ``ipv1..16`` / ``vpv1..16`` (MPPT current and voltage; the
  MAC at Taigene reports only those).
* Huawei ``getDevRealKpi``: ``pv1_i..pv24_i`` / ``pv1_u..`` (one PV
  input per string on the SUN2000 MG0 plants).

``telemetry_detail`` (v203) keeps the wide sheet row, whose string
family only ever had currents 20-29 and no Huawei inputs at all; this
table keeps the whole family as arrays, one row per inverter sample
(index n-1 = input n). Night samples (no current on any input) are not
stored. Same fail-soft rule as the detail mirror: a failed write is a
warning, never a collection error.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from argia.core.normalize import safe_float

LOG = logging.getLogger("argia.string_sample")

MAX_STRINGS = 32
MAX_MPPT = 16
HUAWEI_INPUTS = 24
KEEP_DAYS = 400

ENSURE_SQL = (
    "CREATE TABLE IF NOT EXISTS string_sample (\n"
    "    ts_utc timestamptz NOT NULL,\n"
    "    plant_key text NOT NULL,\n"
    "    inverter_sn text NOT NULL,\n"
    "    str_a real[],\n"
    "    mppt_a real[],\n"
    "    mppt_v real[],\n"
    "    PRIMARY KEY (plant_key, inverter_sn, ts_utc)\n);\n"
    "CREATE INDEX IF NOT EXISTS idx_string_sample_ts ON string_sample (ts_utc);\n")


@dataclass(frozen=True)
class Sample:
    ts_utc: str
    plant_key: str
    inverter_sn: str
    str_a: tuple
    mppt_a: tuple
    mppt_v: tuple

    def has_current(self) -> bool:
        return any((v or 0) > 0 for v in self.str_a + self.mppt_a)


def _raw(row: Any) -> Mapping[str, Any]:
    raw = getattr(row, "raw", None)
    if isinstance(raw, Mapping):
        return raw
    return row if isinstance(row, Mapping) else {}


def _family(raw: Mapping[str, Any], fmts: Sequence[str], n: int) -> tuple:
    out = []
    for i in range(1, n + 1):
        v = None
        for f in fmts:
            if f.format(i=i) in raw:
                v = safe_float(raw.get(f.format(i=i)))
                break
        out.append(v)
    while out and out[-1] is None:
        out.pop()
    return tuple(out)


def from_growatt(plant_key: str, inverter_sn: str, row: Any, ts_utc: str) -> Optional[Sample]:
    raw = _raw(row)
    s = Sample(ts_utc, plant_key, inverter_sn,
               _family(raw, ["currentString{i}"], MAX_STRINGS),
               _family(raw, ["ipv{i}"], MAX_MPPT),
               _family(raw, ["vpv{i}"], MAX_MPPT))
    return s if (s.str_a or s.mppt_a) else None


def from_huawei(plant_key: str, tel: Any, ts_utc: str) -> Optional[Sample]:
    """One PV input per string on the MG0 inverters: the input currents
    are the string currents; Huawei gives no separate MPPT current."""
    raw = getattr(tel, "raw_data_item_map", None) or {}
    s = Sample(ts_utc, plant_key, str(getattr(tel, "inverter_sn", "") or ""),
               _family(raw, ["pv{i}_i", "pv{i}I"], HUAWEI_INPUTS), (),
               _family(raw, ["pv{i}_u", "pv{i}U"], HUAWEI_INPUTS))
    return s if (s.inverter_sn and s.str_a) else None


def _arr(vals: Sequence[Optional[float]]) -> str:
    if not vals:
        return "NULL"
    return "ARRAY[" + ",".join("NULL" if v is None else repr(round(float(v), 3)) for v in vals) + "]::real[]"


def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def build_upsert_sql(samples: Sequence[Sample]) -> Optional[str]:
    """Night samples dropped; a repeated sample (the vendor answered with
    the same last row) is kept once. Pure."""
    rows, seen = [], set()
    for s in samples:
        key = (s.plant_key, s.inverter_sn, s.ts_utc)
        if not s.has_current() or not s.ts_utc or key in seen:
            continue
        seen.add(key)
        rows.append(f"({_q(s.ts_utc)}, {_q(s.plant_key)}, {_q(s.inverter_sn)}, "
                    f"{_arr(s.str_a)}, {_arr(s.mppt_a)}, {_arr(s.mppt_v)})")
    if not rows:
        return None
    return ("INSERT INTO string_sample (ts_utc, plant_key, inverter_sn, str_a, mppt_a, mppt_v) VALUES\n"
            + ",\n".join(rows)
            + "\nON CONFLICT (plant_key, inverter_sn, ts_utc) DO UPDATE SET str_a = EXCLUDED.str_a,"
              " mppt_a = EXCLUDED.mppt_a, mppt_v = EXCLUDED.mppt_v;")


PRUNE_SQL = f"DELETE FROM string_sample WHERE ts_utc < now() - interval '{KEEP_DAYS} days';"


def store(samples: Sequence[Sample], dry_run: bool = False, log: Optional[logging.Logger] = None) -> int:
    """Upsert; returns rows attempted. Never raises (analytics, not billing)."""
    lg = log or LOG
    from argia.store import pg_mirror
    if not pg_mirror.enabled():
        return 0
    sql = build_upsert_sql([s for s in samples if s is not None])
    if sql is None:
        return 0
    n = sql.count("\n(")
    if dry_run:
        lg.info("[PG] DRY RUN: would upsert %d string samples", n)
        return n
    try:
        from argia.store import pgq
        pgq.psql_exec(ENSURE_SQL + sql + "\n" + PRUNE_SQL, timeout=60)
    except Exception as e:  # noqa: BLE001
        lg.warning("[PG] string_sample write failed (telemetry unaffected): %s", str(e)[-300:])
        return 0
    lg.info("[PG] wrote %d string samples", n)
    return n
