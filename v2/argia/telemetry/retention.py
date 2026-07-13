"""Telemetry retention planning (v95) — pure, no I/O.

Raw per-inverter 5-minute telemetry is the bulk of the workbook and the
only thing that grows without bound. It is transient: once ``kpi_eod``
has stamped a day into KPI_Daily and the dashboards have consumed it,
nothing downstream re-reads the raw rows. So we keep a rolling window in
the sheet (default 10 days) and archive the rest to Drive.

Two invariants live here:

* **Contiguous prefix.** Rows are appended oldest-first, so the prunable
  rows are always the top block. The plan returns a count; the caller
  deletes rows ``2 .. 1+count`` in one ``deleteDimension``.
* **Stamp interlock.** A day's raw telemetry is never pruned until
  KPI_Daily has a full stamp for that plant+day (``stamped_dates``).
  Walking oldest→newest, we STOP at the first day that is old-enough but
  not yet aggregated — that day and everything after it stays. This is
  what guarantees the financial report for any past month is always
  reproducible: the aggregate exists before the raw is removed.

Pass ``stamped_dates=None`` to skip the interlock (window-only), for tabs
that are not per-plant (e.g. the shared irradiance tab) where the 10-day
window is the safety margin.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class PrunePlan:
    n_prune: int                                  # contiguous top rows to drop
    rows_by_day: Dict[str, List[list]] = field(default_factory=dict)
    stop_reason: str = "exhausted"                # recent | unstamped | exhausted


def keep_from_date(today: dt.date, window_days: int = 10) -> dt.date:
    """Oldest MX date to KEEP. ``window_days`` inclusive of today, so
    window_days=10 on the 12th keeps the 3rd..12th and prunes <= the 2nd."""
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    return today - dt.timedelta(days=window_days - 1)


def plan_prune(dated_rows: List[Tuple[dt.date, list]],
               keep_from: dt.date,
               stamped_dates: Optional[Set[str]]) -> PrunePlan:
    """Decide the contiguous top block to archive+delete.

    ``dated_rows`` — ``(mx_date, row_values)`` in sheet order (oldest
    first). ``stamped_dates`` — iso date strings KPI_Daily has fully
    stamped for this plant, or ``None`` to skip the interlock.
    """
    by_day: Dict[str, List[list]] = {}
    n = 0
    reason = "exhausted"
    for d, row in dated_rows:
        if d >= keep_from:
            reason = "recent"
            break
        if stamped_dates is not None and d.isoformat() not in stamped_dates:
            reason = "unstamped"
            break
        by_day.setdefault(d.isoformat(), []).append(row)
        n += 1
    return PrunePlan(n_prune=n, rows_by_day=by_day, stop_reason=reason)


def rows_to_csv(header: List, rows: List[list]) -> str:
    """Header + rows as CSV text (for one archived day). Every archived
    CSV is self-describing, so a downloaded file needs no context."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([("" if c is None else c) for c in header])
    for r in rows:
        w.writerow([("" if c is None else c) for c in r])
    return buf.getvalue()


def mx_date_of(ts) -> Optional[dt.date]:
    """MX calendar date of a telemetry ``timestamp_utc`` cell, matching how
    KPI_Daily buckets a day. Returns None if unparseable (caller must then
    treat the row as a keep-boundary, never prune what it can't date)."""
    from argia.core.time_utils import MX_TZ, UTC
    if isinstance(ts, dt.datetime):
        d = ts
    else:
        s = str(ts).strip()
        if not s:
            return None
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(MX_TZ).date()


def stamped_dates_from_kpi(kpi_rows: List[list]) -> Dict[str, Set[str]]:
    """{plant_key: {iso dates KPI_Daily has FULLY stamped}} from a raw
    KPI_Daily read (header + rows). Only ``data_class == 'full'`` days
    count — a partial day is not a safe basis to drop raw telemetry."""
    if not kpi_rows:
        return {}
    header = [str(h).strip() for h in kpi_rows[0]]
    idx = {h: i for i, h in enumerate(header) if h}
    di, pi = idx.get("date_iso"), idx.get("plant_key")
    dci = idx.get("data_class")
    out: Dict[str, Set[str]] = {}
    if di is None or pi is None:
        return out
    for row in kpi_rows[1:]:
        if pi >= len(row) or di >= len(row):
            continue
        pk = str(row[pi]).strip().upper()
        d = str(row[di]).strip()[:10]
        if not pk or not d:
            continue
        dc = str(row[dci]).strip().lower() if (
            dci is not None and dci < len(row)) else "full"
        if dc == "full":
            out.setdefault(pk, set()).add(d)
    return out
