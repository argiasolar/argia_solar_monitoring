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


def _unwrap(js: Any) -> Any:
    """Accept both raw Growatt JSON and v2's fixture-shaped envelope
    ({_meta, response}), including Growatt's habit of serving JSON with a
    text/html content-type (envelope carries it as _raw_text). The
    2026-07-06 all-zeros bug: parsing the ENVELOPE for `obj` silently
    yields nothing."""
    if isinstance(js, dict) and "response" in js and "_meta" in js:
        js = js["response"]
    if isinstance(js, dict) and "_raw_text" in js:
        try:
            import json
            js = json.loads(js["_raw_text"])
        except (ValueError, TypeError):
            return None
    return js


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
    js = _unwrap(js)
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


def parse_env_list(js: Any) -> List[Tuple[str, int]]:
    """getEnvList -> [(datalogSn, addr)]. datas sits at the TOP level of
    the response (unlike history, which nests under obj)."""
    js = _unwrap(js)
    if not isinstance(js, dict):
        return []
    datas = js.get("datas") or (js.get("obj") or {}).get("datas") or []
    out: List[Tuple[str, int]] = []
    for d in datas:
        if not isinstance(d, dict):
            continue
        sn = d.get("datalogSn") or d.get("dataLogSn") or d.get("sn")
        addr = d.get("addr")
        if not sn or addr is None:
            continue
        try:
            out.append((str(sn), int(addr)))
        except (TypeError, ValueError):
            continue
    return out


def pick_env_device(devices: List[Tuple[str, int]],
                    prefer_sn: Optional[str],
                    prefer_addr: Optional[int]) -> Optional[Tuple[str, int]]:
    """v1 semantics: exact (sn, addr) match, then sn-only, then first."""
    if not devices:
        return None
    if prefer_sn:
        for sn, a in devices:
            if sn == prefer_sn and (prefer_addr is None or a == prefer_addr):
                return sn, a
        for sn, a in devices:
            if sn == prefer_sn:
                return sn, a
    return devices[0]


def fetch_env_day(
    web,
    plant_id: str,
    datalog_sn: str,
    addr: int,
    day_iso: str,
    *,
    max_pages: int = MAX_PAGES,
    sleep_s: float = SLEEP_BETWEEN_PAGES_S,
) -> List[Tuple[dt.datetime, float]]:
    """All (timestamp, W/m²) points for one day, deduped and sorted.

    `web` must provide get_env_history(plant_id, datalog_sn, addr, day_iso,
    start) with plant-context seeding. Network errors raise — the caller
    decides whether dense irradiance is best-effort.
    """
    points: List[Tuple[dt.datetime, float]] = []
    start = 0
    for page in range(max_pages):
        js = web.get_env_history(plant_id, datalog_sn, addr, day_iso, start)
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
    if not seen:
        # self-explaining diagnostics (v1-style): name what came back
        raw = _unwrap(web.get_env_history(plant_id, datalog_sn, addr,
                                          day_iso, 0))
        keys = list(raw.keys())[:8] if isinstance(raw, dict) else type(raw)
        obj = raw.get("obj") if isinstance(raw, dict) else None
        n = len((obj or {}).get("datas") or []) if isinstance(obj, dict) else 0
        row0 = ((obj or {}).get("datas") or [None])[0] if n else None
        LOG.warning("env history EMPTY for plant=%s sn=%s addr=%s day=%s: "
                    "top_keys=%s obj_rows=%d first_row_keys=%s",
                    plant_id, datalog_sn, addr, day_iso, keys, n,
                    list(row0.keys())[:10] if isinstance(row0, dict) else None)
    return sorted(seen.items())


def fetch_env_day_auto(
    web,
    plant_id: str,
    prefer_sn: Optional[str],
    prefer_addr: Optional[int],
    day_iso: str,
) -> Tuple[List[Tuple[dt.datetime, float]], Optional[str], Optional[int]]:
    """Configured device first; if it yields nothing, ask getEnvList which
    env device actually exists (v1's warning: the configured datalogger SN
    is not guaranteed to be the env device) and retry once.

    Returns (points, used_sn, used_addr)."""
    if hasattr(web, "seed_env_page") and plant_id:
        web.seed_env_page(plant_id)
    sn = prefer_sn
    addr = int(prefer_addr or DEFAULT_ENV_ADDR)
    points: List[Tuple[dt.datetime, float]] = []
    if sn:
        points = fetch_env_day(web, plant_id, sn, addr, day_iso)
        if points:
            return points, sn, addr
    if plant_id:
        picked = pick_env_device(parse_env_list(web.get_env_list(plant_id)),
                                 prefer_sn, prefer_addr)
        if picked and picked != (sn, addr):
            LOG.info("env device fallback for plant=%s: configured (%s,%s) "
                     "-> envList picked %s", plant_id, sn, addr, picked)
            sn, addr = picked
            points = fetch_env_day(web, plant_id, sn, addr, day_iso)
    return points, sn, addr
