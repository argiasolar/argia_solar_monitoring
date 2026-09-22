"""v257 - Huawei FusionSolar device alarms (``/thirdData/getAlarmList``).

Until now the Huawei client called only ``login``, ``getStationRealKpi``
and ``getDevRealKpi``: the vendor's *alarm list* was never read, so
FusionSolar could be showing three Major "Device Fault" alarms on SAG's
inverters (2026-09-16 12:46:48) while ARGIA knew only that the power had
gone to zero - the cause, the severity and Huawei's own repair
instructions were all sitting one API call away, unread.

The field list below is NOT guessed. It was captured from the live
account on 2026-09-17 (see ``tests/fixtures/huawei/alarm_list.json``,
which is that payload with the identifiers replaced):

    alarmCause  alarmId  alarmName  alarmType  causeId  devName
    devTypeId   esnCode  lev        raiseTime  repairSuggestion
    stationCode stationName status

``raiseTime`` is epoch milliseconds. ``status`` 1 = active. ``lev`` is
the vendor's severity; the portal renders lev=2 as "Major" (confirmed
against the FusionSolar UI for the SAG alarms), and 1/3/4 follow
Huawei's documented order - critical, minor, warning.

Pure: every function takes plain values and returns plain values, so the
tests run on the captured payload and never on the live API.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

# lev -> (label, our severity). Only lev=2 is confirmed against the
# portal; the rest follow Huawei's documented ordering. An unknown lev is
# never silently downgraded - it is treated as CRITICAL and says so.
LEVELS: Dict[int, tuple] = {
    1: ("Critical", "CRITICAL"),
    2: ("Major", "CRITICAL"),
    3: ("Minor", "WARNING"),
    4: ("Warning", "WARNING"),
}
ACTIVE_STATUS = 1

# devTypeId 63 is the datalogger (captured: "Logger-1024A5402200"). An
# alarm on the logger means the SITE lost its link, not one inverter.
DEV_TYPE_DATALOGGER = 63


@dataclass(frozen=True)
class HuaweiAlarm:
    station_code: str
    station_name: str
    dev_name: str
    esn_code: str
    dev_type_id: Optional[int]
    alarm_id: Optional[int]
    alarm_name: str
    alarm_cause: str
    cause_id: Optional[int]
    alarm_type: Optional[int]
    lev: Optional[int]
    status: Optional[int]
    raise_time_ms: Optional[int]
    repair_suggestion: str

    # ---- derived ----
    @property
    def active(self) -> bool:
        return self.status == ACTIVE_STATUS

    @property
    def level_label(self) -> str:
        return LEVELS.get(self.lev, ("Unknown", "CRITICAL"))[0]

    @property
    def severity(self) -> str:
        return LEVELS.get(self.lev, ("Unknown", "CRITICAL"))[1]

    @property
    def is_datalogger(self) -> bool:
        return self.dev_type_id == DEV_TYPE_DATALOGGER

    @property
    def raised_utc(self) -> Optional[dt.datetime]:
        if not self.raise_time_ms:
            return None
        try:
            return dt.datetime.fromtimestamp(int(self.raise_time_ms) / 1000, dt.timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    def key(self) -> str:
        """Stable identity: the same alarm on the same device must touch
        its existing alert, never open a second one."""
        return f"{self.station_code}:{self.esn_code or self.dev_name}:{self.alarm_id}"


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _i(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_alarms(payload: Any) -> List[HuaweiAlarm]:
    """Parse a getAlarmList response (or its ``data`` list). Unknown
    extra fields are ignored; a malformed record is skipped, never fatal."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    out: List[HuaweiAlarm] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        out.append(HuaweiAlarm(
            station_code=_s(d.get("stationCode")), station_name=_s(d.get("stationName")),
            dev_name=_s(d.get("devName")), esn_code=_s(d.get("esnCode")),
            dev_type_id=_i(d.get("devTypeId")), alarm_id=_i(d.get("alarmId")),
            alarm_name=_s(d.get("alarmName")), alarm_cause=_s(d.get("alarmCause")),
            cause_id=_i(d.get("causeId")), alarm_type=_i(d.get("alarmType")),
            lev=_i(d.get("lev")), status=_i(d.get("status")),
            raise_time_ms=_i(d.get("raiseTime")),
            repair_suggestion=_s(d.get("repairSuggestion")),
        ))
    return out


def active_alarms(alarms: Iterable[HuaweiAlarm]) -> List[HuaweiAlarm]:
    return [a for a in alarms if a.active]


def message(alarm: HuaweiAlarm, plant_key: str) -> str:
    """One line a person can act on: what the vendor called it, how bad
    the vendor thinks it is, which device, and since when."""
    when = alarm.raised_utc
    since = f", since {when:%Y-%m-%d %H:%M} UTC" if when else ""
    what = alarm.alarm_name or "unnamed alarm"
    dev = alarm.dev_name or alarm.esn_code or "device"
    scope = "DATALOGGER" if alarm.is_datalogger else dev
    tail = f" [{alarm.severity}]"
    return (f"{plant_key} {scope}: FusionSolar alarm {alarm.alarm_id} "
            f"- {what} ({alarm.level_label}){since}{tail}")


def explanation(alarm: HuaweiAlarm) -> str:
    """The vendor's own cause and repair text - no ARGIA interpretation.

    This is the point of reading the alarm list at all: Huawei already
    writes a cause and a repair suggestion for every alarm, so we never
    have to invent one."""
    bits = []
    if alarm.alarm_cause:
        bits.append(f"Cause (Huawei): {alarm.alarm_cause}")
    if alarm.repair_suggestion:
        bits.append(f"Huawei's suggestion: {alarm.repair_suggestion}")
    if alarm.is_datalogger:
        bits.append("This alarm is on the datalogger, so the whole site is "
                    "off the air - inverter readings stop even when the "
                    "inverters themselves are fine.")
    return "  ".join(bits)
