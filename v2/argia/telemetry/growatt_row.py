"""Build a wide Sheets row from a Growatt MAXHistoryRow + weather snapshot.

Two builders:
* ``build_plant_row`` — for ``Telemetry_<KEY>`` tabs (no plant_key column)
* ``build_argia_row`` — for ``Telemetry_Argia`` (plant_key inserted)

Both reach into ``row.raw`` (the original JSON dict) for everything they need
rather than depending on the typed dataclass attributes. This means a parser
field rename or addition won't break this module — the JSON field names from
Growatt's API are the contract. The parser's column-family accessors are still
used for the wide groups (per-MPPT, per-string).

Why the indirection: the typed dataclass has ~30 fields. The wide row has 140+.
Mapping every column to a dataclass attribute would duplicate the dataclass
shape into a second source of truth. Reading raw keeps the mapping in one
place — the JSON contract.

Pure functions — no I/O. Hand them a parsed row and weather; they hand back a
Python list ready for the Sheets append/upsert.
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
    MPPT_EDAY_COUNT,
    MPPT_POWER_COUNT,
    MPPT_VOLTAGE_COUNT,
    PLANT_SCHEMA,
    STRING_CURRENT_HIGH,
    STRING_CURRENT_LOW,
    STRING_VOLTAGE_COUNT,
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
# Weather snapshot
# ============================================================


@dataclass(frozen=True)
class WeatherSnapshot:
    """Per-plant weather at a moment in time. Any field may be None.

    All four fields show up as their own columns at the end of every row.
    None values become empty cells in Sheets.
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
    """Return the .raw dict from a row, or {} if absent."""
    raw = getattr(row, "raw", None)
    return raw if isinstance(raw, dict) else {}


def _gf(raw: Dict[str, Any], key: str) -> Optional[float]:
    """Get a value from raw as a float (or None)."""
    return safe_float(raw.get(key))


def _gi(raw: Dict[str, Any], key: str) -> Optional[int]:
    """Get a value from raw as an int (or None)."""
    return safe_int(raw.get(key))


def _none_to_empty(cells: List[Any]) -> List[Any]:
    """Replace None with empty string so Sheets renders blank cells."""
    return [c if c is not None else "" for c in cells]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _timestamps(row: Any) -> tuple:
    """Pick the row's timestamp, return (utc_iso, mx_str) for the two columns.

    Priority order:
      1. ``row.timestamp_mx`` (set by parser from the calendar dict — handles
         Growatt's 0-indexed month bug)
      2. Parse ``raw['calendar']`` directly as a fallback
      3. ``row.timestamp_utc``
      4. Now

    Both columns are derived from the same instant, so they always agree on the
    moment they describe.
    """
    raw = _row_raw(row)

    # Priority 1: typed attribute set by parser
    ts_mx = getattr(row, "timestamp_mx", None)
    if isinstance(ts_mx, dt.datetime) and ts_mx.tzinfo is not None:
        ts_utc = ts_mx.astimezone(dt.timezone.utc)
        return (
            ts_utc.isoformat(),
            ts_mx.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # Priority 2: parse calendar dict directly
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

    # Priority 3: timestamp_utc attribute (may exist)
    ts_utc = getattr(row, "timestamp_utc", None)
    if isinstance(ts_utc, dt.datetime) and ts_utc.tzinfo is not None:
        ts_mx_derived = ts_utc.astimezone(MX_TZ)
        return (
            ts_utc.isoformat(),
            ts_mx_derived.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # Priority 4: now
    ts_utc_now = _utc_now()
    return (
        ts_utc_now.isoformat(),
        ts_utc_now.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _derive_status(raw: Dict[str, Any]) -> int:
    """Status code: 1 if no fault, 3 if any fault code is non-zero.

    Mirrors ``build_inverter_snapshot`` in the parser so the snapshot10m tab
    and the new telemetry tab agree on status for the same data.
    """
    fc1 = _gi(raw, "faultCode1") or 0
    fc2 = _gi(raw, "faultCode2") or 0
    fault_type = _gi(raw, "faultType") or 0
    if fc1 != 0 or fc2 != 0 or fault_type != 0:
        return 3
    return 1


def _power_w_int(raw: Dict[str, Any]) -> Optional[int]:
    """Round pac to an integer watt count for the top-level power_w column."""
    pac = _gf(raw, "pac")
    if pac is None:
        return None
    return int(round(pac))


# ============================================================
# Column groups (return lists of cell values, length-checked)
# ============================================================


def _typed_inverter_cells(raw: Dict[str, Any]) -> List[Any]:
    """The 37 'typed' inverter columns in schema order.

    Length MUST equal ``len(TYPED_INVERTER_COLS)`` — the row builder asserts it.
    """
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
    """All per-MPPT and per-string columns in schema order.

    16 + 9 + 32 + 10 + 15 + 15 = 97 cells.
    """
    cells: List[Any] = []
    cells.extend(per_mppt_voltages(row))     # 16 (vpv1..16)
    cells.extend(per_mppt_powers(row))       # 9  (ppv1..9)
    cells.extend(per_string_voltages(row))   # 32 (vString1..32)

    # Per-string currents 20..29 (the rest are always zero on captured fixtures)
    raw = _row_raw(row)
    for i in range(STRING_CURRENT_LOW, STRING_CURRENT_HIGH + 1):
        cells.append(_gf(raw, f"currentString{i}"))

    cells.extend(per_mppt_eday_today_kwh(row))  # 15 (epv1Today..15Today)
    cells.extend(per_mppt_eday_total_kwh(row))  # 15 (epv1Total..15Total)
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
    """Build a row for a per-plant ``Telemetry_<KEY>`` tab.

    The returned list has length ``PLANT_SCHEMA.column_count``. None values are
    converted to empty strings so Sheets renders blank cells.
    """
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
        # Loud failure beats silent column drift
        raise RuntimeError(
            f"plant row length mismatch: built {len(cells)} cells, "
            f"schema expects {PLANT_SCHEMA.column_count}"
        )
    return cells


def build_argia_row(
    row: Any,
    plant_key: str,
    inverter_sn: str,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Build a row for the aggregated ``Telemetry_Argia`` tab.

    Same as ``build_plant_row`` but with ``plant_key`` inserted between the
    timestamp columns and the inverter identity.
    """
    raw = _row_raw(row)
    ts_utc, ts_mx = _timestamps(row)

    cells: List[Any] = [
        ts_utc,
        ts_mx,
        plant_key,
        inverter_sn,
        inverter_label,
    ]
    cells.extend(_typed_inverter_cells(raw))
    cells.extend(_per_mppt_string_cells(row))
    cells.extend(_weather_cells(weather))

    cells = _none_to_empty(cells)

    if len(cells) != ARGIA_SCHEMA.column_count:
        raise RuntimeError(
            f"argia row length mismatch: built {len(cells)} cells, "
            f"schema expects {ARGIA_SCHEMA.column_count}"
        )
    return cells
