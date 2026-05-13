"""Build Sheets rows from Growatt MAXHistoryRow + weather.

Two builders:

* ``build_plant_row`` — wide row for ``Telemetry_<KEY>`` (142 cols)
* ``build_common_row`` — narrow cross-vendor row for ``Telemetry_Argia`` (15 cols)

Both reach into ``row.raw`` for everything they need so the parser's typed
dataclass shape is not a constraint on this module — the JSON field names
from Growatt's API are the contract. The parser's column-family accessors
are still used for the wide groups (per-MPPT, per-string).

Pure functions — no I/O.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from argia.core.normalize import safe_float, safe_int
from argia.core.time_utils import MX_TZ, parse_growatt_calendar
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    PLANT_SCHEMA,
    STRING_CURRENT_HIGH,
    STRING_CURRENT_LOW,
    VENDOR_GROWATT,
)
from argia.vendors.growatt_web_parser import (
    per_mppt_eday_today_kwh,
    per_mppt_eday_total_kwh,
    per_mppt_powers,
    per_mppt_voltages,
    per_string_voltages,
)

LOG = logging.getLogger("argia.telemetry.growatt_row")


# ============================================================
# Weather snapshot (shared across vendors)
# ============================================================


@dataclass(frozen=True)
class WeatherSnapshot:
    """Per-plant weather at a moment in time. Any field may be None.

    All four fields show up as columns at the end of every row. None values
    become empty cells in Sheets.
    """

    irradiance_wm2: Optional[float] = None
    irradiance_kwh_m2_5m: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    ambient_temp_c: Optional[float] = None


EMPTY_WEATHER = WeatherSnapshot()


# ============================================================
# Helpers
# ============================================================


def _row_raw(row: Any) -> Dict[str, Any]:
    raw = getattr(row, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _gf(raw: Dict[str, Any], key: str) -> Optional[float]:
    return safe_float(raw.get(key))


def _gi(raw: Dict[str, Any], key: str) -> Optional[int]:
    return safe_int(raw.get(key))


def _none_to_empty(cells: List[Any]) -> List[Any]:
    return [c if c is not None else "" for c in cells]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _timestamps(row: Any) -> tuple:
    """Pick the row's timestamp, return (utc_iso, mx_str)."""
    raw = _row_raw(row)

    ts_mx = getattr(row, "timestamp_mx", None)
    if isinstance(ts_mx, dt.datetime) and ts_mx.tzinfo is not None:
        ts_utc = ts_mx.astimezone(dt.timezone.utc)
        return (
            ts_utc.isoformat(),
            ts_mx.strftime("%Y-%m-%d %H:%M:%S"),
        )

    cal = raw.get("calendar")
    if isinstance(cal, dict):
        try:
            ts_mx_parsed = parse_growatt_calendar(cal)
        except Exception:  # noqa: BLE001
            ts_mx_parsed = None
        if isinstance(ts_mx_parsed, dt.datetime) and ts_mx_parsed.tzinfo is not None:
            ts_utc = ts_mx_parsed.astimezone(dt.timezone.utc)
            return (
                ts_utc.isoformat(),
                ts_mx_parsed.strftime("%Y-%m-%d %H:%M:%S"),
            )

    ts_utc_attr = getattr(row, "timestamp_utc", None)
    if isinstance(ts_utc_attr, dt.datetime) and ts_utc_attr.tzinfo is not None:
        ts_mx_derived = ts_utc_attr.astimezone(MX_TZ)
        return (
            ts_utc_attr.isoformat(),
            ts_mx_derived.strftime("%Y-%m-%d %H:%M:%S"),
        )

    ts_utc_now = _utc_now()
    return (
        ts_utc_now.isoformat(),
        ts_utc_now.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _derive_status(raw: Dict[str, Any]) -> int:
    """1 if no fault, 3 if any fault code is non-zero."""
    fc1 = _gi(raw, "faultCode1") or 0
    fc2 = _gi(raw, "faultCode2") or 0
    fault_type = _gi(raw, "faultType") or 0
    if fc1 != 0 or fc2 != 0 or fault_type != 0:
        return 3
    return 1


def _format_fault_code(raw: Dict[str, Any]) -> str:
    """Compact human-readable fault summary for the common row.

    "0" when healthy. "FC1=X,FC2=Y,FT=Z" when anything non-zero (omitting
    fields that ARE zero, so a single FC1 fault shows as "FC1=1").
    """
    fc1 = _gi(raw, "faultCode1") or 0
    fc2 = _gi(raw, "faultCode2") or 0
    ft = _gi(raw, "faultType") or 0
    parts: List[str] = []
    if fc1:
        parts.append(f"FC1={fc1}")
    if fc2:
        parts.append(f"FC2={fc2}")
    if ft:
        parts.append(f"FT={ft}")
    return ",".join(parts) if parts else "0"


def _power_w_int(raw: Dict[str, Any]) -> Optional[int]:
    pac = _gf(raw, "pac")
    if pac is None:
        return None
    return int(round(pac))


# ============================================================
# Column groups for the WIDE plant row
# ============================================================


def _typed_inverter_cells(raw: Dict[str, Any]) -> List[Any]:
    """37 typed inverter columns in schema order."""
    return [
        _derive_status(raw),
        _power_w_int(raw),
        _gf(raw, "eacToday"),
        _gf(raw, "pac"),
        _gf(raw, "iac"),
        _gf(raw, "pf"),
        _gf(raw, "pacr"), _gf(raw, "pacs"), _gf(raw, "pact"),
        _gf(raw, "vacr"), _gf(raw, "vacs"), _gf(raw, "vact"),
        _gf(raw, "vacRs"), _gf(raw, "vacSt"), _gf(raw, "vacTr"),
        _gf(raw, "fac"),
        _gf(raw, "ppv"),
        _gf(raw, "epvTotal"),
        _gf(raw, "temperature"),
        _gi(raw, "warnCode"),
        _gi(raw, "warnCode1"),
        _gi(raw, "faultCode1"),
        _gi(raw, "faultCode2"),
        _gi(raw, "faultType"),
        _gi(raw, "pidStatus"),
        _gi(raw, "pidFaultCode"),
        _gi(raw, "apfStatus"),
        _gi(raw, "afciStatus"),
        _gi(raw, "deratingMode"),
        _gi(raw, "realOPPercent"),
        _gf(raw, "pvIso"),
        _gf(raw, "pBusVoltage"),
        _gf(raw, "nBusVoltage"),
        _gi(raw, "StrUnmatch"),
        _gi(raw, "StrUnblance"),
        _gi(raw, "StrBreak"),
        _gf(raw, "gfci"),
    ]


def _per_mppt_string_cells(row: Any) -> List[Any]:
    cells: List[Any] = []
    cells.extend(per_mppt_voltages(row))
    cells.extend(per_mppt_powers(row))
    cells.extend(per_string_voltages(row))
    raw = _row_raw(row)
    for i in range(STRING_CURRENT_LOW, STRING_CURRENT_HIGH + 1):
        cells.append(_gf(raw, f"currentString{i}"))
    cells.extend(per_mppt_eday_today_kwh(row))
    cells.extend(per_mppt_eday_total_kwh(row))
    return cells


def _weather_cells(weather: WeatherSnapshot) -> List[Any]:
    return [
        weather.irradiance_wm2,
        weather.irradiance_kwh_m2_5m,
        weather.cloud_cover_pct,
        weather.ambient_temp_c,
    ]


# ============================================================
# Public builders
# ============================================================


def build_plant_row(
    row: Any,
    inverter_sn: str,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Wide row for ``Telemetry_<KEY>``. 142 cells."""
    raw = _row_raw(row)
    ts_utc, ts_mx = _timestamps(row)

    cells: List[Any] = [
        ts_utc,
        ts_mx,
        inverter_sn,
        inverter_label,
    ]
    cells.extend(_typed_inverter_cells(raw))
    cells.extend(_per_mppt_string_cells(row))
    cells.extend(_weather_cells(weather))

    cells = _none_to_empty(cells)

    if len(cells) != PLANT_SCHEMA.column_count:
        raise RuntimeError(
            f"plant row length mismatch: built {len(cells)} cells, "
            f"schema expects {PLANT_SCHEMA.column_count}"
        )
    return cells


def build_common_row(
    row: Any,
    plant_key: str,
    inverter_sn: str,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Narrow cross-vendor row for ``Telemetry_Argia``. 15 cells.

    Vendor is hard-coded to "GROWATT" since this is the Growatt-specific
    common-row builder.
    """
    raw = _row_raw(row)
    ts_utc, ts_mx = _timestamps(row)

    cells: List[Any] = [
        ts_utc,                          # 0 timestamp_utc
        ts_mx,                           # 1 timestamp_mx
        VENDOR_GROWATT,                  # 2 vendor
        plant_key,                       # 3 plant_key
        inverter_sn,                     # 4 inverter_sn
        inverter_label,                  # 5 inverter_label
        _derive_status(raw),             # 6 status
        _power_w_int(raw),               # 7 power_w
        _gf(raw, "eacToday"),            # 8 etoday_kwh
        _gf(raw, "temperature"),         # 9 temperature_c
        _format_fault_code(raw),         # 10 fault_code
        weather.irradiance_wm2,          # 11
        weather.irradiance_kwh_m2_5m,    # 12
        weather.cloud_cover_pct,         # 13
        weather.ambient_temp_c,          # 14
    ]

    cells = _none_to_empty(cells)

    if len(cells) != ARGIA_SCHEMA.column_count:
        raise RuntimeError(
            f"common row length mismatch: built {len(cells)} cells, "
            f"schema expects {ARGIA_SCHEMA.column_count}"
        )
    return cells
