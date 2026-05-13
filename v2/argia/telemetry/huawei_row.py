"""Build Sheets rows from Huawei InverterSnapshot + weather.

Huawei's API (``getDevRealKpi``) exposes a narrow snapshot: status, power,
day_cap, and a timestamp. The current parser surfaces these as ``InverterSnapshot``
fields. So the row builders here have much less to work with than Growatt's.

The strategy is exactly what we agreed on (Option A from the design chat):

* **Wide plant row** — fill the ~5 columns Huawei actually exposes; the rest
  of the 142 columns stay blank. Schema is identical to Growatt's so adding
  more vendors stays cheap.
* **Common row** — same narrow 15-col format as Growatt; the ``vendor`` column
  is hard-coded to "HUAWEI"; ``temperature_c`` is blank until we extend the
  Huawei parser to read it out of ``dataItemMap``.

Pure functions — no I/O.
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
    PLANT_SCHEMA,
    TYPED_INVERTER_COLS,
    VENDOR_HUAWEI,
)
from argia.vendors.base import InverterSnapshot

LOG = logging.getLogger("argia.telemetry.huawei_row")


# Column indices in TYPED_INVERTER_COLS that Huawei DOES populate.
# Anything not in this map stays blank.
_TYPED_STATUS_IDX = 0       # status
_TYPED_POWER_W_IDX = 1      # power_w
_TYPED_ETODAY_IDX = 2       # etoday_kwh
_TYPED_PAC_W_IDX = 3        # pac_w (= power_w as float)


def _timestamps_from_snapshot(snap: InverterSnapshot) -> tuple:
    """Pick the snapshot's timestamp, return (utc_iso, mx_str)."""
    ts = snap.timestamp_utc
    if isinstance(ts, dt.datetime) and ts.tzinfo is not None:
        return (
            ts.isoformat(),
            ts.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        )
    # Defensive — parser should always set tz-aware timestamps
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return (
        now.isoformat(),
        now.astimezone(MX_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    )


def _power_w_int(power_w: Optional[float]) -> Optional[int]:
    if power_w is None:
        return None
    return int(round(power_w))


def _typed_inverter_cells_sparse(snap: InverterSnapshot) -> List[Any]:
    """37 typed inverter columns, with Huawei filling only what it exposes.

    The rest are None — _none_to_empty later converts to "".
    """
    cells: List[Any] = [None] * len(TYPED_INVERTER_COLS)
    cells[_TYPED_STATUS_IDX] = snap.status
    cells[_TYPED_POWER_W_IDX] = _power_w_int(snap.power_w)
    cells[_TYPED_ETODAY_IDX] = snap.etoday_kwh
    cells[_TYPED_PAC_W_IDX] = snap.power_w
    return cells


def _per_mppt_string_blank_cells() -> List[Any]:
    """All per-MPPT and per-string columns are blank for Huawei (16+9+32+10+15+15=97)."""
    return [None] * (16 + 9 + 32 + 10 + 15 + 15)


# ============================================================
# Public builders
# ============================================================


def build_plant_row(
    snap: InverterSnapshot,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Wide row for ``Telemetry_<KEY>``. 142 cells, mostly blank for Huawei."""
    ts_utc, ts_mx = _timestamps_from_snapshot(snap)

    cells: List[Any] = [
        ts_utc,
        ts_mx,
        snap.inverter_sn,
        inverter_label,
    ]
    cells.extend(_typed_inverter_cells_sparse(snap))
    cells.extend(_per_mppt_string_blank_cells())
    cells.extend(_weather_cells(weather))

    cells = _none_to_empty(cells)

    if len(cells) != PLANT_SCHEMA.column_count:
        raise RuntimeError(
            f"huawei plant row length mismatch: built {len(cells)} cells, "
            f"schema expects {PLANT_SCHEMA.column_count}"
        )
    return cells


def build_common_row(
    snap: InverterSnapshot,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Narrow cross-vendor row for ``Telemetry_Argia``. 15 cells.

    ``snap.plant_key`` is used for the plant_key column. ``snap.raw_status``
    becomes the fault_code (Huawei's devStatus value, e.g. "1", "513").
    """
    ts_utc, ts_mx = _timestamps_from_snapshot(snap)

    # Huawei doesn't expose temperature in the current InverterSnapshot.
    # Leave it blank for now; Stage 4.x will extend the parser to read it
    # from dataItemMap.
    temperature_c: Optional[float] = None

    # fault_code uses raw_status (the devStatus string from Huawei).
    # "1" = online, "3" / "513" / etc. = offline or fault.
    fault_code = snap.raw_status if snap.raw_status else "0"

    cells: List[Any] = [
        ts_utc,                          # 0 timestamp_utc
        ts_mx,                           # 1 timestamp_mx
        VENDOR_HUAWEI,                   # 2 vendor
        snap.plant_key,                  # 3 plant_key
        snap.inverter_sn,                # 4 inverter_sn
        inverter_label,                  # 5 inverter_label
        snap.status,                     # 6 status
        _power_w_int(snap.power_w),      # 7 power_w
        snap.etoday_kwh,                 # 8 etoday_kwh
        temperature_c,                   # 9 temperature_c (blank)
        fault_code,                      # 10 fault_code
        weather.irradiance_wm2,          # 11
        weather.irradiance_kwh_m2_5m,    # 12
        weather.cloud_cover_pct,         # 13
        weather.ambient_temp_c,          # 14
    ]

    cells = _none_to_empty(cells)

    if len(cells) != ARGIA_SCHEMA.column_count:
        raise RuntimeError(
            f"huawei common row length mismatch: built {len(cells)} cells, "
            f"schema expects {ARGIA_SCHEMA.column_count}"
        )
    return cells
