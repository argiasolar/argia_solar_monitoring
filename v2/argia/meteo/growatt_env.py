"""Dense ShineMaster irradiance history from the Growatt web API.

WHY: v2's KPI irradiance integrates the W/m² snapshots captured at telemetry
poll times. On ShineMaster plants the logger reports in bursts (~1-2 h), so
the trapezoid over poll snapshots carries ±20-30% sampling error (measured
against v1's model feed, 2026-07-06 analysis). The logger itself STORES a
dense minute-level history; this module fetches it, exactly the way the
proven v1 scripts did:

    POST /device/getEnvHistory  {datalogSn, addr, startDate, endDate, start}
    -> obj.datas: rows with `calendar` (0-BASED month! confirmed in v1 raw
       JSON) and `radiant` (W/m²); obj.haveNext/obj.start paginate.

Everything network-y is injected (the web client), so parsing and pagination
are fully unit-tested; the live behaviour is verified with
scripts/irr_compare.py before the KPI pipeline is switched over.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# v1's observed default device address when the config column is blank.
DEFAULT_ENV_ADDR = 32

# Pagination safety (mirrors v1's guard).
MAX_PAGES = 50
SLEEP_BETWEEN_PAGES_S = 0.15


def calendar_to_dt(cal: Any) -> Optional[dt.datetime]:
    """Growatt `calendar` dict -> naive local datetime. Month is 0-based."""
    if not isinstance(cal, dict):
        return None
    try:
        return dt.datetime(
            int(cal["year"]), int(cal["month"]) + 1,
            int(cal.get("dayOfMonth") or cal.get("day")),
            int(cal.get("hourOfDay", 0)), int(cal.get("minute", 0)),
            int(cal.get("second", 0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse_env_history_page(
    js: Any,
) -> Tuple[List[Tuple[dt.datetime, float]], bool, Optional[int]]:
    """One getEnvHistory page -> (points, have_next, next_start)."""
    obj = (js or {}).get("obj") if isinstance(js, dict) else None
    if not isinstance(obj, dict):
        return [], False, None
    points: List[Tuple[dt.datetime, float]] = []
    for row in obj.get("datas") or []:
        if not isinstance(row, dict) or "radiant" not in row:
            continue
        ts = calendar_to_dt(row.get("calendar"))
        if ts is None:
            continue
        try:
            wm2 = max(float(row["radiant"]), 0.0)
        except (TypeError, ValueError):
            continue
        points.append((ts, wm2))
    have_next = bool(obj.get("haveNext"))
    next_start: Optional[int] = None
    if have_next:
        try:
            next_start = int(obj.get("start"))
        except (TypeError, ValueError):
            next_start = None
    return points, have_next, next_start


def fetch_env_day(
    web,
    datalog_sn: str,
    addr: int,
    day_iso: str,
    *,
    max_pages: int = MAX_PAGES,
    sleep_s: float = SLEEP_BETWEEN_PAGES_S,
) -> List[Tuple[dt.datetime, float]]:
    """All (timestamp, W/m²) points for one day, deduped and sorted.

    `web` must provide get_env_history(datalog_sn, addr, day_iso, start).
    Network errors raise — the caller decides whether dense irradiance is
    best-effort (kpi_eod treats it as best-effort and falls back).
    """
    points: List[Tuple[dt.datetime, float]] = []
    start = 0
    for page in range(max_pages):
        js = web.get_env_history(datalog_sn, addr, day_iso, start)
        got, have_next, next_start = parse_env_history_page(js)
        points.extend(got)
        if not have_next:
            break
        if next_start is None or next_start == start:
            next_start = start + max(len(got), 1)   # v1's stall guard
        start = next_start
        if sleep_s:
            time.sleep(sleep_s)
    else:
        LOG.warning("env history pagination hit max_pages=%d for %s %s",
                    max_pages, datalog_sn, day_iso)
    seen: Dict[dt.datetime, float] = {}
    for ts, w in points:
        seen[ts] = w
    return sorted(seen.items())
