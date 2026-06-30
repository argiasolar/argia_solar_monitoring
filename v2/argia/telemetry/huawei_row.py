"""Build Sheets rows from Huawei rich telemetry + weather.

Stage 4.1 introduced rich extraction from ``getDevRealKpi``'s ``dataItemMap``
(temperature, AC three-phase, per-MPPT voltages, frequency, power factor).

Stage 4.2 fixes two issues that surfaced in live daylight data:

1. **Per-MPPT energy semantics.** Live data showed ``mppt_X_cap`` values were
   identical across overnight runs — they don't reset at midnight. So
   ``mppt_X_cap`` is **lifetime cumulative energy in Wh per MPPT**, not daily
   energy. The parser now converts Wh → kWh and routes the values to the
   ``epv{i}_total_kwh`` columns. The ``epv{i}_today_kwh`` columns stay blank
   for Huawei (the API doesn't expose per-MPPT daily energy).

2. **Phase voltages.** Stage 4.1 left ``vacr_v``, ``vacs_v``, ``vact_v`` blank
   because we assumed Huawei only exposed line-to-line (ab/bc/ca). The DEBUG
   log showed it ALSO exposes line-to-neutral (a_u, b_u, c_u). Now populated.

Unknown / missing fields still stay blank — every safe_float in the parser
handles None gracefully. So an inverter model that doesn't expose
``mppt_5_cap`` just leaves that cell blank instead of crashing.

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
    MPPT_EDAY_COUNT,
    MPPT_POWER_COUNT,
    MPPT_VOLTAGE_COUNT,
    PLANT_SCHEMA,
    STRING_CURRENT_HIGH,
    STRING_CURRENT_LOW,
    STRING_VOLTAGE_COUNT,
    TYPED_INVERTER_COLS,
    VENDOR_HUAWEI,
)
from argia.vendors.huawei_telemetry import HuaweiTelemetryRow

LOG = logging.getLogger("argia.telemetry.huawei_row")


# ============================================================
# Helpers
# ============================================================


def _timestamps_from_telemetry(tel: HuaweiTelemetryRow) -> tuple:
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


def _fault_code_from_telemetry(tel: HuaweiTelemetryRow) -> str:
    """Compact fault summary for the common row.

    Huawei doesn't have Growatt-style numeric fault codes. We derive a useful
    string from what's there:
    - If raw_status is set and not "1" (online), use DS=<value>
    - If inverter_state is non-zero, include IS=<value>
    - If run_state is non-zero, include RS=<value>
    - Otherwise "0"
    """
    parts: List[str] = []
    if tel.raw_status and tel.raw_status not in ("1", ""):
        parts.append(f"DS={tel.raw_status}")
    if tel.inverter_state is not None and tel.inverter_state != 0:
        parts.append(f"IS={tel.inverter_state}")
    if tel.run_state is not None and tel.run_state != 0:
        parts.append(f"RS={tel.run_state}")
    return ",".join(parts) if parts else "0"


# ============================================================
# Mapping HuaweiTelemetryRow → wide plant row columns
# ============================================================
#
# Each entry maps a wide-schema column name to a getter on HuaweiTelemetryRow.
# Columns not in this list stay blank (None → "").
#
# Notes on the mapping (Stage 4.2):
#   - Stage 4.2 ADDS line-to-neutral phase voltages to vacr_v / vacs_v /
#     vact_v from a_u / b_u / c_u. Line-to-line still maps to vac_rs/st/tr.
#     The wide schema accommodates both Y (phase-to-N) and Δ (line-to-line).
#   - Huawei doesn't split active power by phase (no pacr/s/t equivalent).
#   - The wide schema's iac_a is a single AC current; Huawei reports per-phase
#     (a_i, b_i, c_i) — too lossy to pick one, so left blank.
_TYPED_MAPPING = [
    ("status",                 lambda t: t.status),
    ("power_w",                lambda t: _power_w_int(t.power_w)),
    ("etoday_kwh",             lambda t: t.etoday_kwh),
    ("pac_w",                  lambda t: t.power_w),
    # Phase-to-neutral (NEW in Stage 4.2)
    ("vacr_v",                 lambda t: t.a_u_v),
    ("vacs_v",                 lambda t: t.b_u_v),
    ("vact_v",                 lambda t: t.c_u_v),
    # Line-to-line
    ("vac_rs_v",               lambda t: t.ab_u_v),
    ("vac_st_v",               lambda t: t.bc_u_v),
    ("vac_tr_v",               lambda t: t.ca_u_v),
    ("pf",                     lambda t: t.power_factor),
    ("fac_hz",                 lambda t: t.elec_freq_hz),
    ("ppv_w",                  lambda t: t.mppt_power_w),
    ("epv_total_kwh",          lambda t: t.etotal_kwh),
    ("temperature_c",          lambda t: t.temperature_c),
]


def _typed_inverter_cells_rich(tel: HuaweiTelemetryRow) -> List[Any]:
    """37 typed inverter columns from rich telemetry. Defaults to None for
    unmapped columns (which become "" via _none_to_empty).
    """
    cells: List[Any] = [None] * len(TYPED_INVERTER_COLS)

    for col_name, getter in _TYPED_MAPPING:
        try:
            idx = TYPED_INVERTER_COLS.index(col_name)
        except ValueError:
            LOG.warning("huawei mapping references unknown column '%s'", col_name)
            continue
        try:
            cells[idx] = getter(tel)
        except Exception as e:  # noqa: BLE001
            LOG.warning("huawei mapping for '%s' raised %s", col_name, e)

    return cells


def _per_mppt_string_rich_cells(tel: HuaweiTelemetryRow) -> List[Any]:
    """All per-MPPT and per-string columns (16+9+32+10+15+15 = 97).

    Stage 4.2 routing changes:
    - epv{i}_today_kwh columns: now BLANK for Huawei (API doesn't expose
      per-MPPT daily energy; the values we used to put here were lifetime Wh).
    - epv{i}_total_kwh columns: now POPULATED from tel.pv_etotal_kwh
      (mppt_X_cap converted Wh → kWh by the parser).
    """
    cells: List[Any] = []

    # vpv1..vpv16 — Huawei's pv1_u, pv2_u, ...
    for i in range(MPPT_VOLTAGE_COUNT):
        cells.append(
            tel.pv_voltages_v[i] if i < len(tel.pv_voltages_v) else None
        )

    # ppv1..ppv9 — Huawei doesn't expose per-MPPT power directly. Stays blank.
    for _ in range(MPPT_POWER_COUNT):
        cells.append(None)

    # vstring1..vstring32 — Huawei doesn't report per-string voltage. Blank.
    for _ in range(STRING_VOLTAGE_COUNT):
        cells.append(None)

    # istring20..istring29 — Huawei reports per-MPPT current as pv_i, not per
    # string. The wide schema's istring slot is for Growatt's per-string
    # measurement. Blank.
    for _ in range(STRING_CURRENT_HIGH - STRING_CURRENT_LOW + 1):
        cells.append(None)

    # epv1_today..epv15_today — Huawei doesn't expose per-MPPT daily energy.
    # BLANK in Stage 4.2 (was incorrectly filled with lifetime Wh in Stage 4.1).
    for _ in range(MPPT_EDAY_COUNT):
        cells.append(None)

    # epv1_total..epv15_total — Huawei's mppt_X_cap (lifetime Wh → kWh by parser).
    # NEWLY POPULATED in Stage 4.2.
    for i in range(MPPT_EDAY_COUNT):
        cells.append(
            tel.pv_etotal_kwh[i] if i < len(tel.pv_etotal_kwh) else None
        )

    return cells


# ============================================================
# Public builders
# ============================================================


def build_plant_row(
    tel: HuaweiTelemetryRow,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Wide row for ``Telemetry_<KEY>``. 142 cells. Populates as many columns
    as Huawei's ``getDevRealKpi`` response allows."""
    ts_utc, ts_mx = _timestamps_from_telemetry(tel)

    cells: List[Any] = [
        ts_utc,
        ts_mx,
        tel.inverter_sn,
        inverter_label,
    ]
    cells.extend(_typed_inverter_cells_rich(tel))
    cells.extend(_per_mppt_string_rich_cells(tel))
    cells.extend(_weather_cells(weather))

    cells = _none_to_empty(cells)

    if len(cells) != PLANT_SCHEMA.column_count:
        raise RuntimeError(
            f"huawei plant row length mismatch: built {len(cells)} cells, "
            f"schema expects {PLANT_SCHEMA.column_count}"
        )
    return cells


def build_common_row(
    tel: HuaweiTelemetryRow,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Narrow cross-vendor row for ``Telemetry_Argia``. 15 cells."""
    ts_utc, ts_mx = _timestamps_from_telemetry(tel)

    cells: List[Any] = [
        ts_utc,                                # 0 timestamp_utc
        ts_mx,                                 # 1 timestamp_mx
        VENDOR_HUAWEI,                         # 2 vendor
        tel.plant_key,                         # 3 plant_key
        tel.inverter_sn,                       # 4 inverter_sn
        inverter_label,                        # 5 inverter_label
        tel.status,                            # 6 status
        _power_w_int(tel.power_w),             # 7 power_w
        tel.etoday_kwh,                        # 8 etoday_kwh
        tel.temperature_c,                     # 9 temperature_c
        _fault_code_from_telemetry(tel),       # 10 fault_code
        weather.irradiance_wm2,                # 11
        weather.irradiance_kwh_m2_5m,          # 12
        weather.cloud_cover_pct,               # 13
        weather.ambient_temp_c,                # 14
        weather.module_temp_c,                 # 15
    ]

    cells = _none_to_empty(cells)

    if len(cells) != ARGIA_SCHEMA.column_count:
        raise RuntimeError(
            f"huawei common row length mismatch: built {len(cells)} cells, "
            f"schema expects {ARGIA_SCHEMA.column_count}"
        )
    return cells
