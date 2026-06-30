"""Build Sheets rows from SMA rich telemetry + weather.

SMA's data richness depends on the plant type:
- ennexOS plants (sandbox + modern installs): ~10-15 fields usable
- Sunny Portal Classic: fewer fields, no per-phase data

The wide plant row populates whatever SMA returned, leaves the rest blank.
The narrow common row uses the same 15-column contract as Growatt, Huawei,
SolarEdge — vendor='SMA', plus status/power/eToday/temperature/fault_code.

Compared to other vendors:
  Growatt   ~150 fields → ~120 cols populated
  Huawei    105 fields  → ~50 cols populated
  SolarEdge ~30 fields  → ~22 cols populated (Stage 5.1)
  SMA       ~10-15 fields → ~8-12 cols populated (TBC after live capture)

What stays blank for SMA (by API design):
  - Per-MPPT and per-string detail (SMA aggregates at the inverter level)
  - Growatt-style fault_code_1/2 (SMA uses operational state strings)
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, List, Optional

from argia.core.time_utils import MX_TZ
from argia.telemetry.growatt_row import (
    EMPTY_WEATHER,
    WeatherSnapshot,
    _none_to_empty,
    _weather_cells,
)
from argia.telemetry.schema import (
    ARGIA_SCHEMA,
    MPPT_EDAY_COUNT,
    MPPT_POWER_COUNT,
    MPPT_VOLTAGE_COUNT,
    PLANT_SCHEMA,
    STRING_CURRENT_HIGH,
    STRING_CURRENT_LOW,
    STRING_VOLTAGE_COUNT,
    TYPED_INVERTER_COLS,
    VENDOR_SMA,
)
from argia.vendors.sma_telemetry import SMATelemetryRow

LOG = logging.getLogger("argia.telemetry.sma_row")


def _timestamps_from_telemetry(tel: SMATelemetryRow) -> tuple:
    ts = tel.timestamp_utc
    if isinstance(ts, dt.datetime) and ts.tzinfo is not None:
        return (
            ts.isoformat(),
            ts.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        )
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return (
        now.isoformat(),
        now.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _power_w_int(power_w: Optional[float]) -> Optional[int]:
    if power_w is None:
        return None
    return int(round(power_w))


def _fault_code_from_telemetry(tel: SMATelemetryRow) -> str:
    """Compact fault summary for the common row.

    SMA reports state as a string. "Ok" / "OK" / online states → "0".
    Anything else gets stamped into the fault_code cell so an analyst can
    see why it's offline without joining tabs.
    """
    if not tel.raw_status:
        return "0"
    s = tel.raw_status.strip().upper()
    if s in ("OK", "ONLINE", "OPERATING", "RUN", "MPPT", "ACTIVE", ""):
        return "0"
    return f"STATE={s}"


# ============================================================
# Mapping SMATelemetryRow → wide plant row columns
# ============================================================

_TYPED_MAPPING = [
    ("status",                 lambda t: t.status),
    ("power_w",                lambda t: _power_w_int(t.power_w)),
    ("etoday_kwh",             lambda t: t.etoday_kwh),
    ("pac_w",                  lambda t: t.power_w),
    ("iac_a",                  lambda t: t.iac_a),
    ("pf",                     lambda t: t.power_factor),
    # SMA doesn't (typically) break per-phase voltages — leave vacr/s/t blank,
    # vac (average phase voltage) goes to vac_rs as a single representative
    # value. We'll re-evaluate after seeing real captures.
    ("vac_rs_v",               lambda t: t.vac_v),
    ("fac_hz",                 lambda t: t.fac_hz),
    ("ppv_w",                  lambda t: _power_w_int(t.dc_power_w)),
    ("epv_total_kwh",          lambda t: t.etotal_kwh),
    ("temperature_c",          lambda t: t.temperature_c),
]


def _typed_inverter_cells(tel: SMATelemetryRow) -> List[Any]:
    cells: List[Any] = [None] * len(TYPED_INVERTER_COLS)
    for col_name, getter in _TYPED_MAPPING:
        try:
            idx = TYPED_INVERTER_COLS.index(col_name)
        except ValueError:
            LOG.warning("sma mapping references unknown column '%s'", col_name)
            continue
        try:
            cells[idx] = getter(tel)
        except Exception as e:  # noqa: BLE001
            LOG.warning("sma mapping for '%s' raised %s", col_name, e)
    return cells


def _per_mppt_string_cells(tel: SMATelemetryRow) -> List[Any]:
    """SMA aggregates DC at the inverter. vpv1 carries the DC bus voltage,
    everything else stays blank."""
    cells: List[Any] = []

    # vpv1 = dc_voltage_v if available, else blank; vpv2..vpv16 blank
    for i in range(MPPT_VOLTAGE_COUNT):
        cells.append(tel.dc_voltage_v if i == 0 else None)

    for _ in range(MPPT_POWER_COUNT):
        cells.append(None)

    for _ in range(STRING_VOLTAGE_COUNT):
        cells.append(None)

    for _ in range(STRING_CURRENT_HIGH - STRING_CURRENT_LOW + 1):
        cells.append(None)

    for _ in range(MPPT_EDAY_COUNT):
        cells.append(None)

    for _ in range(MPPT_EDAY_COUNT):
        cells.append(None)

    return cells


def build_plant_row(
    tel: SMATelemetryRow,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Wide row for ``Telemetry_<KEY>``. 142 cells."""
    ts_utc, ts_mx = _timestamps_from_telemetry(tel)

    cells: List[Any] = [
        ts_utc,
        ts_mx,
        tel.inverter_sn,
        inverter_label,
    ]
    cells.extend(_typed_inverter_cells(tel))
    cells.extend(_per_mppt_string_cells(tel))
    cells.extend(_weather_cells(weather))

    cells = _none_to_empty(cells)

    if len(cells) != PLANT_SCHEMA.column_count:
        raise RuntimeError(
            f"sma plant row length mismatch: built {len(cells)} cells, "
            f"schema expects {PLANT_SCHEMA.column_count}"
        )
    return cells


def build_common_row(
    tel: SMATelemetryRow,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Narrow cross-vendor row for ``Telemetry_Argia``. 15 cells."""
    ts_utc, ts_mx = _timestamps_from_telemetry(tel)

    cells: List[Any] = [
        ts_utc,                                # 0
        ts_mx,                                 # 1
        VENDOR_SMA,                            # 2
        tel.plant_key,                         # 3
        tel.inverter_sn,                       # 4
        inverter_label,                        # 5
        tel.status,                            # 6
        _power_w_int(tel.power_w),             # 7
        tel.etoday_kwh,                        # 8
        tel.temperature_c,                     # 9
        _fault_code_from_telemetry(tel),       # 10
        weather.irradiance_wm2,                # 11
        weather.irradiance_kwh_m2_5m,          # 12
        weather.cloud_cover_pct,               # 13
        weather.ambient_temp_c,                # 14
        weather.module_temp_c,                 # 15
    ]

    cells = _none_to_empty(cells)

    if len(cells) != ARGIA_SCHEMA.column_count:
        raise RuntimeError(
            f"sma common row length mismatch: built {len(cells)} cells, "
            f"schema expects {ARGIA_SCHEMA.column_count}"
        )
    return cells
