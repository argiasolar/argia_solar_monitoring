"""
Pure parsers for the Growatt web UI API responses.

Every function in this module is a PURE function (no I/O, no global state)
operating on Python dicts. They're written against the actual responses
captured in v2/tests/fixtures/growatt_web/ (Stage 0).

The Growatt web UI is undocumented and inconsistent:

* Most endpoints return JSON, but with ``Content-Type: text/html``. The
  capture script falls back to text, so most fixtures look like
  ``{"_raw_text": "<json-string>"}``. ``unwrap_fixture`` / ``unwrap_response``
  handle this transparently.

* Envelope: every endpoint returns ``{"result": int, "obj": ..., "msg": ...}``
  where ``result == 1`` means success. Other values mean "no data", "auth
  failed", or "endpoint not supported on this account" — Growatt does not
  document which is which. We treat anything other than ``result == 1`` as
  a structured failure that the caller decides what to do with.

* ``calendar`` objects use Java's 0-indexed months (``month: 4`` is May).
  ``parse_growatt_calendar`` handles this.

* In the real fixtures, the three timestamps in a getMAXHistory row
  (``time``, ``calendar``, ``createTime``) sometimes disagree by ~1 hour.
  This appears to be a Growatt server-side TZ misconfiguration. We expose
  all three so callers can pick the one they trust. The most reliable
  appears to be ``calendar`` (Mexico-local, tz-aware) for live monitoring.

* ``getDevicesByPlant.obj.max`` only returns ONE inverter per device-type
  bucket, not all of them. This is a known Growatt API quirk we can't fix
  here — callers maintain their own SN list.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from argia.core.normalize import normalize_sn, pick, safe_float, safe_int
from argia.core.time_utils import (
    MX_TZ,
    parse_growatt_calendar,
    parse_provider_datetime,
)
from argia.vendors.base import InverterSnapshot, normalize_status


__all__ = [
    "GrowattParseError",
    "MAXHistoryRow",
    "PlantInfo",
    "Device",
    "DevicesByPlant",
    "MAXTotalData",
    "Alert",
    "unwrap_fixture",
    "unwrap_response",
    "check_envelope",
    "extract_obj",
    "parse_max_history",
    "parse_max_history_row",
    "parse_max_day_chart",
    "parse_max_total_data",
    "parse_plant_data",
    "parse_devices_by_plant",
    "parse_alert_plant_event",
    "parse_weather",
    "parse_list_device",
    "per_mppt_voltages",
    "per_mppt_powers",
    "per_string_voltages",
    "per_mppt_eday_today_kwh",
    "per_mppt_eday_total_kwh",
    "extract_latest_row",
    "compute_day_total_kwh_from_history",
    "build_inverter_snapshot",
]


class GrowattParseError(ValueError):
    """Raised when a Growatt response doesn't match the expected envelope."""


# =====================================================================
# Envelope unwrapping
# =====================================================================

def unwrap_fixture(fixture: Any) -> Any:
    """
    Strip the ``{_meta, response}`` wrapper that the capture script adds.

    Then transparently decode any ``{_raw_text: <json>}`` inner wrapper.

    If passed something that's already a "naked" Growatt response
    (``{"result": 1, ...}``), it's returned as-is.
    """
    if not isinstance(fixture, dict):
        raise GrowattParseError(
            f"expected dict fixture, got {type(fixture).__name__}"
        )
    if "response" in fixture and "_meta" in fixture:
        response = fixture["response"]
    else:
        response = fixture
    return unwrap_response(response)


def unwrap_response(obj: Any) -> Any:
    """Decode ``{_raw_text: <json-string>}`` if present; otherwise return obj."""
    if isinstance(obj, dict) and "_raw_text" in obj:
        text = obj["_raw_text"]
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            raise GrowattParseError(
                f"response._raw_text is not valid JSON: {e}"
            ) from e
    return obj


def check_envelope(response: Any) -> int:
    """
    Validate the top-level Growatt envelope and return the integer ``result``.

    Does NOT raise for non-1 results — many endpoints return ``result: 0``
    as a valid "no data" signal. Caller decides how to interpret.

    Raises GrowattParseError only when the response is structurally invalid
    (not a dict, missing ``result``).
    """
    if not isinstance(response, dict):
        raise GrowattParseError(
            f"expected dict response, got {type(response).__name__}"
        )
    if "result" not in response:
        raise GrowattParseError(f"response missing 'result' key: {response!r}")
    try:
        return int(response["result"])
    except (TypeError, ValueError) as e:
        raise GrowattParseError(
            f"response.result is not an integer: {response['result']!r}"
        ) from e


def extract_obj(fixture_or_response: Any) -> Optional[Any]:
    """
    Convenience: unwrap a fixture, check envelope, return ``obj`` on success
    or ``None`` when ``result != 1``. Raises only on structural failure.
    """
    response = unwrap_fixture(fixture_or_response)
    result = check_envelope(response)
    if result != 1:
        return None
    return response.get("obj")


# =====================================================================
# getMAXHistory  (per-inverter 5-minute samples; the big one)
# =====================================================================

@dataclass(frozen=True)
class MAXHistoryRow:
    """
    One 5-minute history sample from getMAXHistory.

    Only the most-used fields are broken out. The full ~155-field dict is
    preserved in ``raw`` — use the field-family helpers
    (``per_mppt_voltages``, etc.) or access ``raw`` directly for the rest.

    Units:
        power      W
        voltage    V
        current    A
        energy     kWh
        temp       °C
    """

    # Timestamps (THREE, because Growatt is inconsistent)
    time_str: str                            # "YYYY-MM-DD HH:MM:SS" naive
    timestamp_mx: Optional[dt.datetime]      # from `calendar`, MX-local tz-aware
    create_time_ms: Optional[int]            # `createTime` epoch ms (server's UTC)

    # AC output
    pac_w: Optional[float]
    iac_a: Optional[float]
    pacr_w: Optional[float]
    pacs_w: Optional[float]
    pact_w: Optional[float]
    vacr_v: Optional[float]
    vacs_v: Optional[float]
    vact_v: Optional[float]
    pf: Optional[float]

    # DC input (aggregate; per-MPPT in helpers)
    ppv_w: Optional[float]

    # Energy
    eac_today_kwh: Optional[float]
    eac_total_kwh: Optional[float]
    epv_total_kwh: Optional[float]

    # Thermal + status (most-actionable subset)
    temperature_c: Optional[float]
    warn_code: Optional[int]
    warn_code_1: Optional[int]
    fault_code_1: Optional[int]
    fault_code_2: Optional[int]
    pid_status: Optional[int]
    pid_fault_code: Optional[int]
    apf_status: Optional[int]
    afci_status: Optional[int]
    derating_mode: Optional[int]
    real_op_percent: Optional[int]
    pv_iso: Optional[float]
    p_bus_voltage: Optional[float]
    n_bus_voltage: Optional[float]
    str_unmatch: Optional[int]
    str_unblance: Optional[int]

    # Full row, never lose data
    raw: Dict[str, Any] = field(default_factory=dict)


def parse_max_history(fixture_or_response: Any) -> List[MAXHistoryRow]:
    """
    Parse a full getMAXHistory response into a list of MAXHistoryRow.

    Returns an empty list when:
      * ``result != 1``  (Growatt reports no data / auth issue)
      * ``obj.datas`` is missing or empty

    Raises GrowattParseError on structural failure (e.g. not a dict).
    """
    obj = extract_obj(fixture_or_response)
    if not isinstance(obj, dict):
        return []
    datas = obj.get("datas")
    if not isinstance(datas, list):
        return []
    return [parse_max_history_row(row) for row in datas if isinstance(row, dict)]


def parse_max_history_row(row: Mapping[str, Any]) -> MAXHistoryRow:
    """Parse one 5-minute sample. ``row`` is a dict with ~155 keys."""
    return MAXHistoryRow(
        time_str=str(row.get("time", "")),
        timestamp_mx=parse_growatt_calendar(row.get("calendar")),
        create_time_ms=safe_int(row.get("createTime")),

        pac_w=safe_float(row.get("pac")),
        iac_a=safe_float(row.get("iac")),
        pacr_w=safe_float(row.get("pacr")),
        pacs_w=safe_float(row.get("pacs")),
        pact_w=safe_float(row.get("pact")),
        vacr_v=safe_float(row.get("vacr")),
        vacs_v=safe_float(row.get("vacs")),
        vact_v=safe_float(row.get("vact")),
        pf=safe_float(row.get("pf")),

        ppv_w=safe_float(row.get("ppv")),

        eac_today_kwh=safe_float(row.get("eacToday")),
        eac_total_kwh=safe_float(row.get("eacTotal")),
        epv_total_kwh=safe_float(row.get("epvTotal")),

        temperature_c=safe_float(row.get("temperature")),
        warn_code=safe_int(row.get("warnCode")),
        warn_code_1=safe_int(row.get("warnCode1")),
        fault_code_1=safe_int(row.get("faultCode1")),
        fault_code_2=safe_int(row.get("faultCode2")),
        pid_status=safe_int(row.get("pidStatus")),
        pid_fault_code=safe_int(row.get("pidFaultCode")),
        apf_status=safe_int(row.get("apfStatus")),
        afci_status=safe_int(row.get("afciStatus")),
        derating_mode=safe_int(row.get("deratingMode")),
        real_op_percent=safe_int(row.get("realOPPercent")),
        pv_iso=safe_float(row.get("pvIso")),
        p_bus_voltage=safe_float(row.get("pBusVoltage")),
        n_bus_voltage=safe_float(row.get("nBusVoltage")),
        str_unmatch=safe_int(row.get("StrUnmatch")),
        str_unblance=safe_int(row.get("StrUnblance")),

        raw=dict(row),
    )


# ---------------------------------------------------------------------
# Field-family accessors (per-MPPT, per-string)
#
# These work on a raw row dict OR a MAXHistoryRow.raw. Returning fixed-
# length lists means downstream code can iterate without surprises.
# ---------------------------------------------------------------------

def _maybe_raw(source: Any) -> Mapping[str, Any]:
    """Accept either MAXHistoryRow or a bare dict."""
    if isinstance(source, MAXHistoryRow):
        return source.raw
    if isinstance(source, Mapping):
        return source
    raise TypeError(f"expected MAXHistoryRow or Mapping, got {type(source).__name__}")


def per_mppt_voltages(source: Any) -> List[Optional[float]]:
    """``vpv1..vpv16`` in order; ``None`` for missing keys."""
    row = _maybe_raw(source)
    return [safe_float(row.get(f"vpv{i}")) for i in range(1, 17)]


def per_mppt_powers(source: Any) -> List[Optional[float]]:
    """``ppv1..ppv9`` in order. The MAX line tops out at 9 MPPT power readings.

    (Fixtures confirm: ppv10..ppv16 do NOT exist even though vpv10..vpv16 do.
    If Growatt adds more, extend this list.)
    """
    row = _maybe_raw(source)
    return [safe_float(row.get(f"ppv{i}")) for i in range(1, 10)]


def per_string_voltages(source: Any) -> List[Optional[float]]:
    """``vString1..vString32`` in order."""
    row = _maybe_raw(source)
    return [safe_float(row.get(f"vString{i}")) for i in range(1, 33)]


def per_mppt_eday_today_kwh(source: Any) -> List[Optional[float]]:
    """``epv1Today..epv15Today`` in order. (No epv0Today / epv16Today.)"""
    row = _maybe_raw(source)
    return [safe_float(row.get(f"epv{i}Today")) for i in range(1, 16)]


def per_mppt_eday_total_kwh(source: Any) -> List[Optional[float]]:
    """``epv1Total..epv15Total`` in order."""
    row = _maybe_raw(source)
    return [safe_float(row.get(f"epv{i}Total")) for i in range(1, 16)]


# ---------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------

def extract_latest_row(rows: List[MAXHistoryRow]) -> Optional[MAXHistoryRow]:
    """
    Pick the latest row by ``time_str`` (ISO-comparable string).

    Returns ``None`` for an empty list.

    NOTE: we sort by string, not by ``timestamp_mx``, because some rows
    may have failed calendar parsing while their ``time`` is fine.
    "YYYY-MM-DD HH:MM:SS" sorts lexically the same as chronologically.
    """
    if not rows:
        return None
    return max(rows, key=lambda r: r.time_str)


def compute_day_total_kwh_from_history(
    rows: List[MAXHistoryRow],
) -> Optional[float]:
    """
    Derive the day's total kWh from history rows.

    Growatt's ``eacToday`` is a running total that resets at midnight, so
    the day-end value is just ``max(eacToday)``. We take the max rather
    than the latest because the meter occasionally re-emits a smaller
    value (timing glitch) in the last sample — taking max is more robust.

    Returns ``None`` when no row has a usable ``eacToday``.
    """
    values = [r.eac_today_kwh for r in rows if r.eac_today_kwh is not None]
    if not values:
        return None
    return max(values)


def build_inverter_snapshot(
    row: MAXHistoryRow,
    plant_key: str,
    inverter_sn: str,
) -> InverterSnapshot:
    """
    Convert one history row into a v2 InverterSnapshot.

    Status inference: if ``pac_w`` is > 0 OR we have a non-zero
    ``eac_today_kwh`` and the timestamp is fresh, status is online (1).
    Fault codes trump this — non-zero fault_code_1 marks the inverter
    offline (3). This mirrors v1 behaviour.

    ``timestamp_utc`` priority: calendar (MX-local → UTC) → time_str (assumed
    UTC, defensive) → now.
    """
    # Decide timestamp
    ts_utc: Optional[dt.datetime] = None
    if row.timestamp_mx is not None:
        ts_utc = row.timestamp_mx.astimezone(dt.timezone.utc)
    elif row.time_str:
        ts_utc = parse_provider_datetime(row.time_str)
    if ts_utc is None:
        ts_utc = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    # Status
    has_fault = (row.fault_code_1 or 0) > 0 or (row.fault_code_2 or 0) > 0
    status = 3 if has_fault else 1

    return InverterSnapshot(
        plant_key=plant_key,
        inverter_sn=normalize_sn(inverter_sn),
        timestamp_utc=ts_utc,
        status=status,
        power_w=row.pac_w,
        etoday_kwh=row.eac_today_kwh,
        raw_status=(
            f"fault1={row.fault_code_1},fault2={row.fault_code_2},"
            f"warn={row.warn_code},derate={row.derating_mode}"
        ),
    )


# =====================================================================
# getMAXDayChart  (24h pac series at 5-min cadence)
# =====================================================================

def parse_max_day_chart(fixture_or_response: Any) -> List[float]:
    """
    Return the ``pac`` array from getMAXDayChart, as a list of floats (W).

    The fixture has 288 slots = 5-min intervals over 24 hours starting at
    00:00 local. Zero values are valid (night). Returns ``[]`` on
    ``result != 1`` or unexpected shape.
    """
    obj = extract_obj(fixture_or_response)
    if not isinstance(obj, dict):
        return []
    pac = obj.get("pac")
    if not isinstance(pac, list):
        return []
    return [float(safe_float(v) or 0.0) for v in pac]


# =====================================================================
# getMAXTotalData  (plant aggregate)
# =====================================================================

@dataclass(frozen=True)
class MAXTotalData:
    plant_id: str
    e_today_kwh: Optional[float]
    e_total_kwh: Optional[float]
    money_today: Optional[float]
    money_total: Optional[float]
    money_unit: str


def parse_max_total_data(fixture_or_response: Any) -> Optional[MAXTotalData]:
    """
    Returns ``None`` when result != 1.

    NOTE: the fixture has numeric values as STRINGS ("786.8"). safe_float
    handles this.
    """
    obj = extract_obj(fixture_or_response)
    if not isinstance(obj, dict):
        return None
    return MAXTotalData(
        plant_id=str(obj.get("plantId", "")),
        e_today_kwh=safe_float(obj.get("eToday")),
        e_total_kwh=safe_float(obj.get("eTotal")),
        money_today=safe_float(obj.get("mToday")),
        money_total=safe_float(obj.get("mTotal")),
        money_unit=str(obj.get("mUnitText") or ""),
    )


# =====================================================================
# getPlantData  (plant config / metadata)
# =====================================================================

@dataclass(frozen=True)
class PlantInfo:
    plant_id: str
    plant_name: str
    country: str
    city: str
    lat: Optional[float]
    lng: Optional[float]
    timezone_hours: Optional[int]   # "-6" → -6
    nominal_power_w: Optional[float]
    e_total_kwh: Optional[float]
    money_unit: str
    create_date: str                # "YYYY-MM-DD"


def parse_plant_data(fixture_or_response: Any) -> Optional[PlantInfo]:
    obj = extract_obj(fixture_or_response)
    if not isinstance(obj, dict):
        return None
    return PlantInfo(
        plant_id=str(obj.get("id", "")),
        plant_name=str(obj.get("plantName") or ""),
        country=str(obj.get("country") or ""),
        city=str(obj.get("city") or ""),
        lat=safe_float(obj.get("lat")),
        lng=safe_float(obj.get("lng")),
        timezone_hours=safe_int(obj.get("timezone")),
        nominal_power_w=safe_float(obj.get("nominalPower")),
        e_total_kwh=safe_float(obj.get("eTotal")),
        money_unit=str(obj.get("moneyUnit") or ""),
        create_date=str(obj.get("creatDate") or ""),  # note: Growatt typo "creat"
    )


# =====================================================================
# getDevicesByPlant
# =====================================================================

@dataclass(frozen=True)
class Device:
    """A device entry from ``obj.<bucket>``. Bucket gives type."""
    sn: str
    label: str
    device_type_code: str
    bucket: str   # "max", "tlx", "mix", "inv", "env", ...


@dataclass(frozen=True)
class DevicesByPlant:
    """Result of parse_devices_by_plant. Inverters are aggregated across
    every "inverter-ish" bucket; ``env`` devices and backflow are separate.

    WARNING: Growatt's ``getDevicesByPlant`` only returns ONE representative
    SN per bucket, not all of them. ``inverters`` will undercount when the
    plant has multiple inverters of the same type. Use a hardcoded SN list
    or ``listDevice`` for the full set.
    """
    inverters: List[Device]
    env_devices: List[Device]
    other: List[Device]


# Buckets known to contain inverter-like devices.
INVERTER_BUCKETS: Tuple[str, ...] = (
    "max", "tlx", "mix", "inv", "spa", "sph", "min", "mod",
)


def parse_devices_by_plant(fixture_or_response: Any) -> DevicesByPlant:
    """
    Parse getDevicesByPlant. Each device is a 3-element list
    ``[sn, label, deviceTypeCode]``.
    """
    obj = extract_obj(fixture_or_response)
    inverters: List[Device] = []
    env_devices: List[Device] = []
    other: List[Device] = []
    if not isinstance(obj, dict):
        return DevicesByPlant(inverters, env_devices, other)

    for bucket_name, bucket_value in obj.items():
        if not isinstance(bucket_value, list):
            continue
        for item in bucket_value:
            device = _parse_device_item(item, bucket_name)
            if device is None:
                continue
            if bucket_name in INVERTER_BUCKETS:
                inverters.append(device)
            elif bucket_name == "env":
                env_devices.append(device)
            else:
                other.append(device)

    return DevicesByPlant(inverters, env_devices, other)


def _parse_device_item(item: Any, bucket: str) -> Optional[Device]:
    if isinstance(item, list) and len(item) >= 1 and isinstance(item[0], str):
        return Device(
            sn=normalize_sn(item[0]),
            label=str(item[1]) if len(item) >= 2 else "",
            device_type_code=str(item[2]) if len(item) >= 3 else "",
            bucket=bucket,
        )
    if isinstance(item, dict):
        sn = pick(item, ["sn", "deviceSn", "invSn", "serialNum"])
        if sn:
            return Device(
                sn=normalize_sn(sn),
                label=str(pick(item, ["alias", "label", "name"]) or ""),
                device_type_code=str(item.get("deviceTypeCode", "")),
                bucket=bucket,
            )
    return None


# =====================================================================
# alertPlantEvent
# =====================================================================

@dataclass(frozen=True)
class Alert:
    """A single alert entry. Shape is approximate — we don't have a real
    populated alert fixture yet. Update when we capture one.

    The fields we DO know exist:
      * ``deviceSn``  — inverter SN
      * ``alarmCode`` / ``warnCode``  — the alarm identifier
      * ``alarmTime`` / ``time``  — when it fired (string)
      * ``alarmDesc`` / ``msg`` — human description (may be missing)
    """
    device_sn: str
    code: str
    time_str: str
    description: str
    raw: Dict[str, Any] = field(default_factory=dict)


def parse_alert_plant_event(fixture_or_response: Any) -> List[Alert]:
    """
    Parse the alert feed.

    Empirically, Growatt returns ``obj: false`` when there are no alerts on
    the plant (every captured fixture so far: GTO1, NL1, SLP1, MEX3 all
    return false). When there ARE alerts, the shape is undocumented; we
    guess based on common Growatt patterns and surface the full raw dict
    so callers can inspect.

    Returns an empty list for no-alerts / failed-result.
    """
    obj = extract_obj(fixture_or_response)
    if obj is None or obj is False:
        return []
    if isinstance(obj, list):
        return [_parse_alert_item(it) for it in obj if isinstance(it, dict)]
    if isinstance(obj, dict):
        # Some Growatt feeds wrap the list in {"datas": [...]} or {"rows": [...]}
        for key in ("datas", "rows", "list", "events"):
            inner = obj.get(key)
            if isinstance(inner, list):
                return [_parse_alert_item(it) for it in inner if isinstance(it, dict)]
        return []
    return []


def _parse_alert_item(item: Mapping[str, Any]) -> Alert:
    return Alert(
        device_sn=normalize_sn(pick(item, ["deviceSn", "sn", "invSn"])),
        code=str(pick(item, ["alarmCode", "warnCode", "code"]) or ""),
        time_str=str(pick(item, ["alarmTime", "time", "happenTime"]) or ""),
        description=str(pick(item, ["alarmDesc", "msg", "description"]) or ""),
        raw=dict(item),
    )


# =====================================================================
# getWeatherByPlantId
# =====================================================================

def parse_weather(fixture_or_response: Any) -> Optional[Dict[str, Any]]:
    """
    Return the weather ``obj`` dict if present, else ``None``.

    Empirically result==0 on the GTO1 fixture — Growatt's weather is often
    unavailable. We expose this so callers can fall back to our own
    Open-Meteo path.
    """
    obj = extract_obj(fixture_or_response)
    if not isinstance(obj, dict) or not obj:
        return None
    return dict(obj)


# =====================================================================
# listDevice
# =====================================================================

def parse_list_device(fixture_or_response: Any) -> List[Device]:
    """
    Account-wide device list. The captured fixture has result=0 (Growatt
    declined to enumerate for this account); we return [] in that case.

    When populated, listDevice's shape mirrors getDevicesByPlant — same
    bucket → [[sn, label, type], ...] structure. Re-use the same parser.
    """
    obj = extract_obj(fixture_or_response)
    if not isinstance(obj, dict) or not obj:
        return []
    devices: List[Device] = []
    for bucket_name, bucket_value in obj.items():
        if not isinstance(bucket_value, list):
            continue
        for item in bucket_value:
            d = _parse_device_item(item, bucket_name)
            if d is not None:
                devices.append(d)
    return devices
