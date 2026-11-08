"""v366 - the vendor's own alarms as ARGIA alerts (Huawei FusionSolar first).

Vitalmex, 7 Nov 2026 07:42: FusionSolar raised "Device Fault" (Major, alarm
2064, "a major fault has occurred on the internal circuit of the device") on
two of the three inverters; the alarm list reader existed since v257 but no
job called it, so ARGIA showed the plant green for a day.

How it runs:
* the telemetry job (already signed in to FusionSolar every 5 minutes) asks
  for the alarm list every ALARM_EVERY_MIN minutes and writes the ACTIVE
  alarms to a small state file (``write_state``) - one session, no second
  login competing with the telemetry;
* the acute alert job (every 30 min) reads the file (``load_state``), opens
  or touches one ``vendor_alarm`` alert per device (``breaches``) and
  resolves the ones FusionSolar no longer lists (``cleared_keys``) - only
  when the file is fresh and the fetch succeeded, so a failed fetch never
  closes an alarm.

``vendor_alarm`` is not an engine metric: the daily tier never resolves it;
this module does, from the vendor's own list. Pure apart from the two file
functions.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from typing import Dict, Iterable, List, Optional, Tuple

from argia.analytics.acute import AcuteBreach
from argia.analytics.inverter_health import Severity
from argia.core.time_utils import utc_to_mx
from argia.vendors.huawei_alarms import HuaweiAlarm, active_alarms, parse_alarms

METRIC = "vendor_alarm"
ALARM_EVERY_MIN = 15
STATE_MAX_AGE_MIN = 40
LOOKBACK_DAYS = 30
STATE_FILE = os.environ.get("ARGIA_HUAWEI_ALARMS_FILE",
                            os.path.join(os.environ.get("ARGIA_LOG_DIR", os.path.join(os.path.expanduser("~"), "argia_logs")),
                                         "huawei_alarms.json"))
_SEV = {"CRITICAL": Severity.CRITICAL, "WARNING": Severity.WARNING}


def due(now_mx: dt.datetime, last_fetch_utc: Optional[dt.datetime], now_utc: dt.datetime) -> bool:
    """Ask FusionSolar now? On the quarter hours (the 5-minute tick that
    starts one), or when the last successful fetch is older than
    ALARM_EVERY_MIN + 5 minutes (a missed tick catches up). Pure."""
    if last_fetch_utc is None:
        return True
    if (now_utc - last_fetch_utc).total_seconds() >= (ALARM_EVERY_MIN + 5) * 60:
        return True
    return now_mx.minute % ALARM_EVERY_MIN < 5


def to_state(raw_records: Iterable[dict], plant_by_station: Dict[str, str], now_utc: dt.datetime,
             ok: bool = True, error: str = "") -> dict:
    """The file's content: the ACTIVE alarms of the stations we monitor. Pure."""
    alarms = []
    for a in active_alarms(parse_alarms(list(raw_records or []))):
        pk = plant_by_station.get(a.station_code)
        if not pk:
            continue
        alarms.append({"plant_key": pk, "station_code": a.station_code, "dev_name": a.dev_name,
                       "esn": a.esn_code, "dev_type_id": a.dev_type_id, "alarm_id": a.alarm_id,
                       "name": a.alarm_name, "cause": a.alarm_cause, "repair": a.repair_suggestion,
                       "lev": a.lev, "raise_ms": a.raise_time_ms})
    return {"fetched_utc": now_utc.isoformat(timespec="seconds"), "ok": bool(ok), "error": error[:300],
            "alarms": alarms}


def write_state(state: dict, path: str = STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_state(path: str = STATE_FILE) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def last_fetch(state: Optional[dict]) -> Optional[dt.datetime]:
    try:
        return dt.datetime.fromisoformat(str((state or {})["fetched_utc"]))
    except (KeyError, ValueError, TypeError):
        return None


def usable(state: Optional[dict], now_utc: dt.datetime, max_age_min: int = STATE_MAX_AGE_MIN) -> bool:
    """Fresh and from a successful fetch - the only state that may open OR
    close alerts. Pure."""
    t = last_fetch(state)
    return bool(state and state.get("ok") and t is not None
                and (now_utc - t).total_seconds() <= max_age_min * 60)


def _alarm(d: dict) -> HuaweiAlarm:
    return HuaweiAlarm(station_code=str(d.get("station_code") or ""), station_name="",
                       dev_name=str(d.get("dev_name") or ""), esn_code=str(d.get("esn") or ""),
                       dev_type_id=d.get("dev_type_id"), alarm_id=d.get("alarm_id"),
                       alarm_name=str(d.get("name") or ""), alarm_cause=str(d.get("cause") or ""),
                       cause_id=None, alarm_type=None, repair_suggestion=str(d.get("repair") or ""),
                       lev=d.get("lev"), status=1, raise_time_ms=d.get("raise_ms"))


def breaches(state: Optional[dict], configured: Dict[str, List[str]], now_utc: dt.datetime) -> List[AcuteBreach]:
    """One breach per (plant, device): the worst of its active alarms, the
    vendor's words in the message. The device is matched to our inverter by
    serial (FusionSolar's esnCode); an alarm on the datalogger or an
    unmatched device is plant-level. A datalogger alarm means the site is
    off the air - blind, not a measured loss - so it is at most a WARNING.
    Nothing when the state is not usable. Pure."""
    if not usable(state, now_utc):
        return []
    groups: Dict[Tuple[str, str], List[HuaweiAlarm]] = {}
    for d in state.get("alarms") or []:
        a = _alarm(d)
        pk = str(d.get("plant_key") or "")
        if pk not in configured:
            continue
        sns = {str(x).strip().upper(): str(x).strip() for x in configured[pk]}
        sn = "" if a.is_datalogger else sns.get(a.esn_code.strip().upper(), "")
        groups.setdefault((pk, sn), []).append(a)
    out = []
    for (pk, sn), alarms in sorted(groups.items()):
        rank = {"CRITICAL": 2, "WARNING": 1}
        alarms.sort(key=lambda a: (-rank.get(a.severity, 2), a.raise_time_ms or 0))
        worst = alarms[0]
        sev = worst.severity
        if worst.is_datalogger and sev == "CRITICAL":
            sev = "WARNING"
        when = worst.raised_utc
        since = f", since {utc_to_mx(when):%Y-%m-%d %H:%M} MX" if when else ""
        dev = "datalogger" if worst.is_datalogger else (worst.dev_name or worst.esn_code or "device")
        more = f" (+{len(alarms) - 1} more)" if len(alarms) > 1 else ""
        cause = f" - {worst.alarm_cause}" if worst.alarm_cause else ""
        out.append(AcuteBreach(
            metric=METRIC, plant_key=pk, inverter_sn=sn, severity=_SEV.get(sev, Severity.CRITICAL),
            value=float(worst.alarm_id) if worst.alarm_id is not None else None,
            message=(f"{pk} {sn or dev}: FusionSolar alarm {worst.alarm_id} {worst.alarm_name} "
                     f"({worst.level_label}) on {dev}{since}{more}{cause} [{sev}]")))
    return out


def cleared_keys(open_records: Iterable, active_keys: Iterable[str], state: Optional[dict],
                 now_utc: dt.datetime) -> List[str]:
    """Alert keys of OPEN/SILENCED vendor_alarm records the vendor no longer
    lists - empty unless the state is usable (a failed fetch closes
    nothing). Pure."""
    if not usable(state, now_utc):
        return []
    active = set(active_keys)
    return sorted({r.alert_key for r in open_records
                   if r.metric == METRIC and (r.is_open() or r.is_silenced()) and r.alert_key not in active})
