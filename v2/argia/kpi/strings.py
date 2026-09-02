"""Per-MPPT / per-string daily statistics from Growatt MAX history rows.

The solar director's monthly closes analyse plant health string by
string. Every number he uses is already inside the getMAXHistory rows
the pipeline downloads daily — per-string currents (currentString1..32),
per-MPPT voltages (vpv1..16) and daily energies (epv1Today..16Today,
NOTE: epv16Today exists in real fixtures even though an older parser
comment said otherwise), and the StrUnmatch / StrUnblance flags. This
module reduces one day of samples for one inverter to one compact
record per channel. Pure functions — no I/O, testable from fixtures.

Method, chosen for honesty:
  * MPPT energy = max(epvXToday) — the inverter's own counter, exact.
  * String energy = MPPT energy split by each string's share of the
    pair's integrated current (amp-hours). Current share is what the
    director's shared-MPPT analysis measures; multiplying by the
    counter avoids integrating voltage noise into an invented total.
  * dt between samples is clamped to 10 minutes so a telemetry gap
    does not overweight the sample after it.
  * MAX-series mapping: strings 2i-1 and 2i share MPPT i.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Mapping, Optional

from argia.core.normalize import safe_float, safe_int

MAX_MPPT = 16
MAX_STRINGS = 32
DT_CLAMP_H = 10.0 / 60.0          # a gap never counts for more than 10 min
V_ACTIVE_MIN = 50.0               # below this the MPPT is asleep, not data


def _row(source: Any) -> Mapping[str, Any]:
    raw = getattr(source, "raw", None)
    if isinstance(raw, Mapping):
        return raw
    if isinstance(source, Mapping):
        return source
    raise TypeError(f"expected Mapping or MAXHistoryRow, got {type(source).__name__}")


def _ts(row: Mapping[str, Any]) -> Optional[dt.datetime]:
    s = str(row.get("time", "")).strip()
    try:
        return dt.datetime.fromisoformat(s)
    except ValueError:
        return None


def mppt_of_string(n: int) -> int:
    """MAX wiring convention: strings (2i-1, 2i) share MPPT i."""
    return (n + 1) // 2


def channel_day_stats(rows: List[Any]) -> Dict[str, Any]:
    """Reduce one inverter-day of history rows to per-channel records.

    Returns {'mppt': {i: {...}}, 'string': {n: {...}}, 'flags': {...},
    'samples': int}. Channels that never woke up (no voltage above
    V_ACTIVE_MIN and no current and no energy) are omitted entirely —
    an unpopulated input is not a zero-performing string.
    """
    parsed = []
    for r in rows:
        raw = _row(r)
        ts = _ts(raw)
        if ts is not None:
            parsed.append((ts, raw))
    parsed.sort(key=lambda p: p[0])

    mppt: Dict[int, Dict[str, float]] = {}
    stringq: Dict[int, float] = {}
    v_sum: Dict[int, float] = {}
    v_n: Dict[int, int] = {}
    unmatch = 0
    unblance = 0

    prev_ts: Optional[dt.datetime] = None
    for ts, raw in parsed:
        dt_h = DT_CLAMP_H
        if prev_ts is not None:
            dt_h = min(max((ts - prev_ts).total_seconds() / 3600.0, 0.0),
                       DT_CLAMP_H)
        prev_ts = ts

        unmatch = max(unmatch, safe_int(raw.get("StrUnmatch")) or 0)
        unblance = max(unblance, safe_int(raw.get("StrUnblance")) or 0)

        for i in range(1, MAX_MPPT + 1):
            e = safe_float(raw.get(f"epv{i}Today"))
            v = safe_float(raw.get(f"vpv{i}"))
            if e is not None:
                m = mppt.setdefault(i, {"energy_kwh": 0.0})
                m["energy_kwh"] = max(m["energy_kwh"], e)
            if v is not None and v >= V_ACTIVE_MIN:
                v_sum[i] = v_sum.get(i, 0.0) + v
                v_n[i] = v_n.get(i, 0) + 1

        for n in range(1, MAX_STRINGS + 1):
            c = safe_float(raw.get(f"currentString{n}"))
            if c is not None and c > 0:
                stringq[n] = stringq.get(n, 0.0) + c * dt_h

    out_mppt: Dict[int, Dict[str, Any]] = {}
    for i, m in mppt.items():
        active = m["energy_kwh"] > 0 or v_n.get(i, 0) > 0
        if not active:
            continue
        out_mppt[i] = {
            "energy_kwh": round(m["energy_kwh"], 3),
            "v_avg": round(v_sum[i] / v_n[i], 1) if v_n.get(i) else None,
        }

    out_str: Dict[int, Dict[str, Any]] = {}
    for n, q in stringq.items():
        i = mppt_of_string(n)
        pair_q = sum(stringq.get(p, 0.0)
                     for p in (2 * i - 1, 2 * i))
        share = q / pair_q if pair_q > 0 else None
        m_energy = (out_mppt.get(i) or {}).get("energy_kwh")
        out_str[n] = {
            "mppt": i,
            "q_ah": round(q, 3),
            "share": round(share, 4) if share is not None else None,
            "energy_kwh": (round(m_energy * share, 3)
                           if (m_energy is not None and share is not None)
                           else None),
        }

    return {"mppt": out_mppt, "string": out_str,
            "flags": {"str_unmatch": unmatch, "str_unblance": unblance},
            "samples": len(parsed)}


ENSURE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS string_daily (
    plant_key    text NOT NULL,
    inverter_sn  text NOT NULL,
    prod_date    date NOT NULL,
    channel      text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('mppt','string')),
    energy_kwh   numeric(10,3),
    q_ah         numeric(12,3),
    v_avg        numeric(8,2),
    share        numeric(6,4),
    samples      int NOT NULL,
    str_unmatch  int,
    str_unblance int,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plant_key, inverter_sn, prod_date, channel));"""


def upsert_sqls(plant_key: str, sn: str, date_iso: str,
                stats: Dict[str, Any]) -> List[str]:
    """INSERT ... ON CONFLICT DO UPDATE statements for one inverter-day.
    Pure — the caller executes them."""
    def _n(v):
        return "NULL" if v is None else f"{v}"

    fl = stats.get("flags", {})
    samples = stats.get("samples", 0)
    out = []
    for i, m in sorted(stats.get("mppt", {}).items()):
        out.append(
            "INSERT INTO string_daily (plant_key, inverter_sn, prod_date,"
            " channel, kind, energy_kwh, q_ah, v_avg, share, samples,"
            " str_unmatch, str_unblance) VALUES"
            f" ('{plant_key}','{sn}',DATE '{date_iso}','pv{i}','mppt',"
            f"{_n(m.get('energy_kwh'))},NULL,{_n(m.get('v_avg'))},NULL,"
            f"{samples},{fl.get('str_unmatch', 0)},{fl.get('str_unblance', 0)})"
            " ON CONFLICT (plant_key, inverter_sn, prod_date, channel)"
            " DO UPDATE SET energy_kwh = EXCLUDED.energy_kwh,"
            " v_avg = EXCLUDED.v_avg, samples = EXCLUDED.samples,"
            " str_unmatch = EXCLUDED.str_unmatch,"
            " str_unblance = EXCLUDED.str_unblance, loaded_at = now();")
    for n, s in sorted(stats.get("string", {}).items()):
        out.append(
            "INSERT INTO string_daily (plant_key, inverter_sn, prod_date,"
            " channel, kind, energy_kwh, q_ah, v_avg, share, samples,"
            " str_unmatch, str_unblance) VALUES"
            f" ('{plant_key}','{sn}',DATE '{date_iso}','s{n}','string',"
            f"{_n(s.get('energy_kwh'))},{_n(s.get('q_ah'))},NULL,"
            f"{_n(s.get('share'))},{samples},{fl.get('str_unmatch', 0)},"
            f"{fl.get('str_unblance', 0)})"
            " ON CONFLICT (plant_key, inverter_sn, prod_date, channel)"
            " DO UPDATE SET energy_kwh = EXCLUDED.energy_kwh,"
            " q_ah = EXCLUDED.q_ah, share = EXCLUDED.share,"
            " samples = EXCLUDED.samples,"
            " str_unmatch = EXCLUDED.str_unmatch,"
            " str_unblance = EXCLUDED.str_unblance, loaded_at = now();")
    return out
