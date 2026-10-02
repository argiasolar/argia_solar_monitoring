"""Vendor history import - a plant's daily energy from the vendor's own charts
(v285, Tomasz 2026-10-01: "do the SMS so it matches the total we have
currently in Growatt").

The monitoring only knows a plant from the day it was onboarded (SMS: 10 Jul
2026), but the vendor keeps the plant's whole life. Growatt's web panel gives
two views of it:

  * the month chart - one value per day (getMAXMonthChart)
  * the year chart  - one value per month (getMAXYearChart)

They do not always agree. When the data logger was offline Growatt spreads
the gap over the missing days as EQUAL values (SMS 7-15 Apr 2025: 77.0 kWh a
day) that its month total does not contain. The month total is what Growatt
reports and what adds up to its plant total, so it is the authority:

  1. each day starts from the month chart;
  2. a month whose days do not add up to the year chart's month value gets
     the difference on its gap-fill days (runs of equal values) when it has
     them, otherwise spread over all its days in proportion;
  3. the last 0.001 kWh of rounding goes to the biggest adjusted day, so
     the month adds up exactly and measured days stay as measured.

Everything here is PURE (no I/O): scripts/vendor_history_import.py fetches,
prints and writes.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

EQUAL_TOL_KWH = 0.15        # consecutive days within 0.15 kWh of each other ...
MIN_FILL_RUN = 3            # ... three or more in a row = a gap fill
SAME_TOL_KWH = 0.05         # a stored day this close to the target is left alone
NOTE_MARK = "energy from vendor daily counter"   # protected by kpi_mirror (VENDOR_NOTE_MARK)


def fill_run_indices(values: Sequence[float], tol: float = EQUAL_TOL_KWH,
                     min_run: int = MIN_FILL_RUN) -> Set[int]:
    """Indices of days in runs of >= ``min_run`` consecutive EQUAL positive
    values (each within ``tol`` of the previous) - the vendor's even spread of
    a data-logger gap. Two real days can come out alike; three in a row
    within 0.15 kWh do not happen in practice (found when the synthetic
    test fleet's smooth days were taken for a gap at 0.25 / 2 days)."""
    out: Set[int] = set()
    run: List[int] = []
    for i, v in enumerate(values):
        if (v is not None and v > 0 and run
                and abs(v - values[run[-1]]) <= tol):
            run.append(i)
            continue
        if len(run) >= min_run:
            out.update(run)
        run = [i] if (v is not None and v > 0) else []
    if len(run) >= min_run:
        out.update(run)
    return out


def allocate_month(days: Sequence[float], month_total: Optional[float]) -> List[float]:
    """The month's days, adjusted so they add up to ``month_total`` exactly
    (to 0.001 kWh). ``month_total`` None -> the days unchanged (rounded).
    Never returns a negative day."""
    vals = [max(0.0, float(v or 0.0)) for v in days]
    if month_total is None:
        return [round(v, 3) for v in vals]
    total = max(0.0, float(month_total))
    resid = total - sum(vals)
    touched = list(range(len(vals)))
    if abs(resid) >= 0.0005:
        fills = sorted(fill_run_indices(vals))
        fill_sum = sum(vals[i] for i in fills)
        if fills and fill_sum + resid >= 0:
            touched = fills
            share = resid / len(fills)
            if all(vals[i] + share >= 0 for i in fills):
                for i in fills:
                    vals[i] += share
            else:                                   # uneven runs: scale them instead
                k = (fill_sum + resid) / fill_sum
                for i in fills:
                    vals[i] *= k
        elif sum(vals) > 0:
            k = total / sum(vals)
            vals = [v * k for v in vals]
        elif vals:
            vals = [total / len(vals)] * len(vals)
    vals = [round(v, 3) for v in vals]
    drift = round(total - sum(vals), 3)
    if vals and drift:
        j = max(touched, key=lambda i: vals[i])     # the days that were adjusted anyway
        vals[j] = round(max(0.0, vals[j] + drift), 3)
    return vals


@dataclass(frozen=True)
class DayChange:
    day: str                    # YYYY-MM-DD
    target_kwh: float
    stored_kwh: Optional[float]  # None = no row today
    action: str                 # NEW | UPDATE | SAME


def targets_for_range(month_days: Dict[str, Sequence[float]],
                      month_totals: Dict[str, float],
                      d0: dt.date, d1: dt.date) -> Dict[str, float]:
    """{day: kWh} for every day d0..d1 that the vendor has a month for.

    A month is reconciled to its month total only when every day of it that
    lies OUTSIDE the range is 0 in the vendor's chart (otherwise the total
    would belong partly to days we are not writing)."""
    out: Dict[str, float] = {}
    for ym in sorted(month_days):
        y, m = int(ym[:4]), int(ym[5:7])
        n = calendar.monthrange(y, m)[1]
        days = list(month_days[ym])[:n]
        days += [0.0] * (n - len(days))
        inside = [d0 <= dt.date(y, m, i + 1) <= d1 for i in range(n)]
        if not any(inside):
            continue
        outside_zero = all((days[i] or 0) == 0 for i in range(n) if not inside[i])
        alloc = allocate_month(days, month_totals.get(ym) if outside_zero else None)
        for i in range(n):
            if inside[i]:
                out[dt.date(y, m, i + 1).isoformat()] = alloc[i]
    return out


def plan_changes(targets: Dict[str, float],
                 stored: Dict[str, Optional[float]]) -> List[DayChange]:
    out: List[DayChange] = []
    for day in sorted(targets):
        t = targets[day]
        s = stored.get(day)
        if day not in stored:
            act = "NEW"
        elif s is not None and abs(float(s) - t) <= SAME_TOL_KWH:
            act = "SAME"
        else:
            act = "UPDATE"
        out.append(DayChange(day, t, None if s is None else float(s), act))
    return out


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# energy-proportional columns: rescaled with the energy on an UPDATE
SCALED = ("pr", "pr_stc", "specific_yield", "capacity_factor", "production_pct", "billable_kwh")


def build_sql(plant_key: str, changes: Sequence[DayChange], source_tag: str) -> str:
    """One transaction: NEW rows inserted (source v2), UPDATE rows get the
    vendor energy with the energy-proportional columns rescaled (NULL when
    the stored energy was 0 or missing). SAME rows are not touched."""
    pk = str(plant_key).strip().upper()
    stmts = ["BEGIN;"]
    for c in changes:
        if c.action == "NEW":
            note = f"{NOTE_MARK} ({source_tag}; row created by the history import)"
            stmts.append(
                "INSERT INTO daily_production (plant_key, prod_date, energy_kwh, source, status_note)"
                f" VALUES ({_txt(pk)}, DATE '{c.day}', {c.target_kwh:.3f}, 'v2', {_txt(note)})"
                " ON CONFLICT (plant_key, prod_date) DO NOTHING;")
        elif c.action == "UPDATE":
            was = "NULL" if c.stored_kwh is None else f"{c.stored_kwh:.1f}"
            note = f"{NOTE_MARK} ({source_tag}; was {was})"
            if c.stored_kwh:
                k = c.target_kwh / c.stored_kwh
                scaled = ", ".join(f"{col} = round(({col} * {k:.9f})::numeric, 4)" for col in SCALED)
            else:
                scaled = ", ".join(f"{col} = NULL" for col in SCALED)
            stmts.append(
                f"UPDATE daily_production SET energy_kwh = {c.target_kwh:.3f}, {scaled},"
                f" status_note = {_txt(note)}"
                f" WHERE plant_key = {_txt(pk)} AND prod_date = DATE '{c.day}';")
    stmts.append("COMMIT;")
    return "\n".join(stmts)


def month_summary(targets: Dict[str, float], stored: Dict[str, Optional[float]],
                  month_totals: Dict[str, float], day_sums: Dict[str, float]) -> List[dict]:
    """Per month: vendor month, vendor day-sum, stored now, after import."""
    rows: Dict[str, dict] = {}
    for day, t in targets.items():
        ym = day[:7]
        r = rows.setdefault(ym, {"month": ym, "vendor_month": month_totals.get(ym),
                                 "vendor_days": day_sums.get(ym), "stored": 0.0,
                                 "after": 0.0, "new": 0, "update": 0})
        s = stored.get(day)
        r["stored"] += float(s or 0.0)
        r["after"] += t
        if day not in stored:
            r["new"] += 1
        elif s is None or abs(float(s) - t) > SAME_TOL_KWH:
            r["update"] += 1
    return [rows[k] for k in sorted(rows)]
