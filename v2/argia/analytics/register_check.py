"""String register against the field (v394, Arturo's methodology R3 and R4).

R3 - "Registers follow the field, and the system checks that they do." Every night, per
inverter of a plant with a string register (the EPI config: Annex A + layout + channel
overrides), the noon current of every input is compared with the register:

* **unregistered** - an input that carries current (median >= LIVE_A at noon) on at least
  2 of the last 3 producing days but is in no register line: the register misses a string
  (SAG before its overrides: inverter 2 used PV16, the register said PV4). INFO - it costs
  nothing by itself; the portal shows it, no mail.
* **dead** - a registered input at < DEAD_A on each of the last DAYS producing days while
  the inverter produced: a string lost. WARNING (energy is being lost).

A day counts for an inverter only when it produced: at least half of its registered inputs
>= LIVE_A at noon (a stopped inverter or an overcast noon is not evidence about a string).

R4 - "Show the index since the last known change." ``last_change`` finds, from the same
noon data, the first day of the inverter's current set of live inputs when that set differs
from the one before (SAG inverter 1: inputs 10, 11, 12 and 15 at 0 A until 31 Oct, live
since 1 Nov). Only sunny noons count (strongest input >= SUNNY_A) and a set must hold for
2 such days, so a cloud or one odd day is not a change.

Pure; the data comes from string_noon_daily (refreshed by kpi_eod, ``refresh_sql``).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

DEAD_A = 0.4
LIVE_A = 1.0
SUNNY_A = 4.0
DAYS = 3
LOOKBACK_DAYS = 7
STABLE_DAYS = 2

Key = Tuple[str, str]                  # (plant_key, inverter_sn)
Input = Tuple[str, int]                # ("str" | "mppt", 1-based index)

TABLE_SQL = ("CREATE TABLE IF NOT EXISTS string_noon_daily (plant_key text NOT NULL, inverter_sn text NOT NULL,"
             " prod_date date NOT NULL, arr text NOT NULL, idx integer NOT NULL, median_a real, n integer,"
             " PRIMARY KEY (plant_key, inverter_sn, prod_date, arr, idx));")


def refresh_sql(first: dt.date, last: dt.date) -> str:
    """Recompute string_noon_daily for MX days first..last from string_sample: the median of
    every input's current between 11:00 and 13:59 MX (both arrays). One transaction."""
    window = (f" WHERE ts_utc >= (DATE '{first}')::timestamp AT TIME ZONE 'America/Mexico_City'"
              f" AND ts_utc < (DATE '{last}' + 1)::timestamp AT TIME ZONE 'America/Mexico_City'"
              " AND extract(hour FROM ts_utc AT TIME ZONE 'America/Mexico_City') BETWEEN 11 AND 13")
    sel = ("SELECT plant_key, inverter_sn, (ts_utc AT TIME ZONE 'America/Mexico_City')::date, '{arr}', i::int,"
           " percentile_cont(0.5) WITHIN GROUP (ORDER BY v), count(*)"
           " FROM string_sample, unnest({col}) WITH ORDINALITY u(v, i)" + window + " AND v IS NOT NULL"
           " GROUP BY 1, 2, 3, 5")
    return (TABLE_SQL + "\nBEGIN;\n"
            f"DELETE FROM string_noon_daily WHERE prod_date BETWEEN DATE '{first}' AND DATE '{last}';\n"
            "INSERT INTO string_noon_daily (plant_key, inverter_sn, prod_date, arr, idx, median_a, n) "
            + sel.format(arr="str", col="str_a") + ";\n"
            "INSERT INTO string_noon_daily (plant_key, inverter_sn, prod_date, arr, idx, median_a, n) "
            + sel.format(arr="mppt", col="mppt_a") + ";\nCOMMIT;\n")


def registered_inputs(cfgs: Mapping[str, object]) -> Dict[Key, Dict[Input, str]]:
    """{(plant, serial): {(arr, idx): register uid}} from argia.epi.config.load(). Huawei "pv"
    and Growatt "str" channels read the string array, "mppt" channels the MPPT array."""
    out: Dict[Key, Dict[Input, str]] = {}
    for key, c in cfgs.items():
        sn_of = getattr(c, "sn_of", {}) or {}
        for s in getattr(c, "strings", ()) or ():
            sn = sn_of.get(s.inverter)
            ch = c.channel_of.get(s.uid)
            if not sn or ch is None:
                continue
            arr = "mppt" if ch.kind == "mppt" else "str"
            out.setdefault((key, sn), {})[(arr, int(ch.index))] = s.uid
    return out


@dataclass(frozen=True)
class Flag:
    plant_key: str
    inverter_sn: str
    kind: str                  # "unregistered" | "dead"
    arr: str
    idx: int
    uid: str                   # register line ("" for unregistered)
    median_a: float            # latest producing day
    days: Tuple[str, ...]      # the producing days judged


def _by_day(rows: Iterable[Sequence]) -> Dict[Key, Dict[str, Dict[Input, float]]]:
    """rows (plant, serial, date, arr, idx, median_a) -> {(plant, sn): {date: {(arr, idx): a}}}"""
    out: Dict[Key, Dict[str, Dict[Input, float]]] = {}
    for pk, sn, d, arr, idx, a in rows:
        if a is None:
            continue
        out.setdefault((pk, sn), {}).setdefault(str(d), {})[(arr, int(idx))] = float(a)
    return out


def producing(day: Mapping[Input, float], reg: Iterable[Input]) -> bool:
    reg = list(reg)
    live = sum(1 for i in reg if day.get(i, 0.0) >= LIVE_A)
    return bool(reg) and live * 2 >= len(reg)


def evaluate(registered: Mapping[Key, Mapping[Input, str]], rows: Iterable[Sequence], asof: dt.date
             ) -> List[Flag]:
    """The register flags at ``asof`` from the last LOOKBACK_DAYS of noon medians."""
    first = (asof - dt.timedelta(days=LOOKBACK_DAYS - 1)).isoformat()
    data = _by_day(r for r in rows if first <= str(r[2]) <= asof.isoformat())
    out: List[Flag] = []
    for key, reg in sorted(registered.items()):
        days = data.get(key) or {}
        good = sorted(d for d, v in days.items() if producing(v, reg))[-DAYS:]
        if not good:
            continue
        latest = days[good[-1]]
        arrs = {a for a, _i in reg}
        if len(good) == DAYS:
            for inp, uid in sorted(reg.items()):
                vals = [days[d].get(inp) for d in good]
                if all(v is not None and v < DEAD_A for v in vals):
                    out.append(Flag(key[0], key[1], "dead", inp[0], inp[1], uid, round(vals[-1], 2), tuple(good)))
        seen: Set[Input] = {i for d in good for i in days[d] if i[0] in arrs}
        for inp in sorted(seen - set(reg)):
            vals = [days[d].get(inp) for d in good]
            live = sum(1 for v in vals if v is not None and v >= LIVE_A)
            if live >= min(2, len(good)) and (latest.get(inp) or 0.0) >= LIVE_A:
                out.append(Flag(key[0], key[1], "unregistered", inp[0], inp[1], "", round(latest[inp], 2), tuple(good)))
    return out


def live_sets(days: Mapping[str, Mapping[Input, float]], arr: str = "str") -> List[Tuple[str, FrozenSet[int]]]:
    """[(date, inputs at >= LIVE_A)] on sunny noons (strongest input >= SUNNY_A), date order."""
    out = []
    for d in sorted(days):
        v = {i: a for (k, i), a in days[d].items() if k == arr}
        if v and max(v.values()) >= SUNNY_A:
            out.append((d, frozenset(i for i, a in v.items() if a >= LIVE_A)))
    return out


def last_change(sets: Sequence[Tuple[str, FrozenSet[int]]]) -> Optional[str]:
    """First day of the current set of live inputs when it differs from the set before it.
    Runs shorter than STABLE_DAYS are noise (dropped); None = no change in the data."""
    runs: List[List] = []
    for d, s in sets:
        if runs and runs[-1][1] == s:
            runs[-1][2] += 1
        else:
            runs.append([d, s, 1])
    runs = [r for r in runs if r[2] >= STABLE_DAYS]
    merged: List[List] = []
    for r in runs:
        if merged and merged[-1][1] == r[1]:
            merged[-1][2] += r[2]
        else:
            merged.append(list(r))
    return merged[-1][0] if len(merged) >= 2 else None


def changes_by_inverter(rows: Iterable[Sequence]) -> Dict[Key, str]:
    """{(plant, serial): first day of the current live-input set} for inverters whose set changed."""
    out = {}
    for key, days in _by_day(rows).items():
        d = last_change(live_sets(days))
        if d:
            out[key] = d
    return out


def message(flags: Sequence[Flag], label: str) -> Tuple[str, str]:
    """(severity, text) of one inverter's flags - dead strings make it a WARNING."""
    dead = [f for f in flags if f.kind == "dead"]
    unreg = [f for f in flags if f.kind == "unregistered"]
    parts = []
    if dead:
        parts.append("registered string(s) at 0 A for %d producing days: %s" % (
            DAYS, ", ".join(f"{f.uid} (input {f.idx}, {f.median_a:.1f} A)" for f in dead)))
    if unreg:
        parts.append("input(s) with current but in no register line: %s" % ", ".join(
            f"{f.idx} ({f.median_a:.1f} A)" for f in unreg))
    sev = "WARNING" if dead else "INFO"
    return sev, f"{label}: " + "; ".join(parts) + " - register vs field, confirm on site [" + sev + "]"
