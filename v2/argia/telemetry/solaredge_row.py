"""Build Sheets rows from SolarEdge rich telemetry + weather.

Stage 5.1: now exploits the per-phase L1Data/L2Data/L3Data + line-to-line
voltages + acFrequency that the SolarEdge API actually exposes. The Stage 5
wide row populated ~7 cells; Stage 5.1 populates ~22.

Mapping from SolarEdgeTelemetryRow to the wide schema:
  status, power_w, etoday_kwh, pac_w  → from telemetry directly
  epv_total_kwh                       → from etotal_kwh
  temperature_c                       → from telemetry directly
  vpv1_v                              → dc_voltage_v (single DC bus)
  vacr_v / vacs_v / vact_v            → L1.acVoltage / L2.acVoltage / L3.acVoltage
  vac_rs_v / vac_st_v / vac_tr_v      → v_l1_to_l2 / v_l2_to_l3 / v_l3_to_l1
  pacr_w / pacs_w / pact_w            → L1.activePower / L2 / L3
  fac_hz                              → mean of L1/L2/L3.acFrequency (they agree)
  pf                                  → mean of L1/L2/L3.cosPhi
  iac_a                               → mean of L1/L2/L3.acCurrent

Columns that STILL stay blank for SolarEdge:
  - ppv1..ppv9, vstring*, istring*    → no per-MPPT / per-string data
  - Growatt-style fault_code_1/2      → different fault model (use raw_mode instead)
  - epv1..15_today/total              → no per-MPPT energy
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
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
    VENDOR_SOLAREDGE,
)
from argia.vendors.solaredge_telemetry import SolarEdgeTelemetryRow

LOG = logging.getLogger("argia.telemetry.solaredge_row")


def _timestamps_from_telemetry(tel: SolarEdgeTelemetryRow) -> tuple:
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


def _fault_code_from_telemetry(tel: SolarEdgeTelemetryRow) -> str:
    """Compact fault summary for the common row.

    "0" when mode is healthy. Else, mode name (e.g. "FAULT", "SLEEPING").
    """
    if tel.raw_mode and tel.raw_mode.upper() not in ("MPPT", "THROTTLED", "IDLE", ""):
        return f"MODE={tel.raw_mode.upper()}"
    return "0"


def _phase_mean(values: List[Optional[float]]) -> Optional[float]:
    """Mean of non-None values, or None if all are None."""
    non_none = [v for v in values if v is not None]
    if not non_none:
        return None
    return round(statistics.mean(non_none), 6)


# ============================================================
# Mapping SolarEdgeTelemetryRow → wide plant row columns
# ============================================================
#
# Phase-mean derivations (fac_hz, pf, iac_a):
#   SolarEdge reports per-phase. The wide schema has one column for each,
#   so we take the mean. The three phases nearly always agree to within
#   <0.1% so this is honest. If they ever diverge significantly the
#   per-phase reactive_power_var or activePower deltas will reveal it.
_TYPED_MAPPING = [
    ("status",                 lambda t: t.status),
    ("power_w",                lambda t: _power_w_int(t.power_w)),
    ("etoday_kwh",             lambda t: t.etoday_kwh),
    ("pac_w",                  lambda t: t.power_w),
    # Line-to-neutral (per-phase) voltages from L1/L2/L3.acVoltage
    ("vacr_v",                 lambda t: t.l1.ac_voltage_v),
    ("vacs_v",                 lambda t: t.l2.ac_voltage_v),
    ("vact_v",                 lambda t: t.l3.ac_voltage_v),
    # Line-to-line voltages
    ("vac_rs_v",               lambda t: t.v_l1_to_l2_v),
    ("vac_st_v",               lambda t: t.v_l2_to_l3_v),
    ("vac_tr_v",               lambda t: t.v_l3_to_l1_v),
    # Per-phase active power
    ("pacr_w",                 lambda t: t.l1.active_power_w),
    ("pacs_w",                 lambda t: t.l2.active_power_w),
    ("pact_w",                 lambda t: t.l3.active_power_w),
    # Phase-mean derived
    ("iac_a",                  lambda t: _phase_mean(
        [t.l1.ac_current_a, t.l2.ac_current_a, t.l3.ac_current_a])),
    ("pf",                     lambda t: _phase_mean(
        [t.l1.cos_phi, t.l2.cos_phi, t.l3.cos_phi])),
    ("fac_hz",                 lambda t: _phase_mean(
        [t.l1.ac_frequency_hz, t.l2.ac_frequency_hz, t.l3.ac_frequency_hz])),
    ("epv_total_kwh",          lambda t: t.etotal_kwh),
    ("temperature_c",          lambda t: t.temperature_c),
]


def _typed_inverter_cells(tel: SolarEdgeTelemetryRow) -> List[Any]:
    cells: List[Any] = [None] * len(TYPED_INVERTER_COLS)
    for col_name, getter in _TYPED_MAPPING:
        try:
            idx = TYPED_INVERTER_COLS.index(col_name)
        except ValueError:
            LOG.warning("solaredge mapping references unknown column '%s'", col_name)
            continue
        try:
            cells[idx] = getter(tel)
        except Exception as e:  # noqa: BLE001
            LOG.warning("solaredge mapping for '%s' raised %s", col_name, e)
    return cells


def _per_mppt_string_cells(tel: SolarEdgeTelemetryRow) -> List[Any]:
    """Populate vpv1 with dc_voltage_v (SolarEdge has a single DC bus).
    All other per-MPPT and per-string columns stay blank."""
    cells: List[Any] = []

    # vpv1 = dc_voltage_v; vpv2..vpv16 = blank
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
    tel: SolarEdgeTelemetryRow,
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
            f"solaredge plant row length mismatch: built {len(cells)} cells, "
            f"schema expects {PLANT_SCHEMA.column_count}"
        )
    return cells


def build_common_row(
    tel: SolarEdgeTelemetryRow,
    inverter_label: str,
    weather: WeatherSnapshot = EMPTY_WEATHER,
) -> List[Any]:
    """Narrow cross-vendor row for ``Telemetry_Argia``. 15 cells."""
    ts_utc, ts_mx = _timestamps_from_telemetry(tel)

    cells: List[Any] = [
        ts_utc,                                # 0
        ts_mx,                                 # 1
        VENDOR_SOLAREDGE,                      # 2
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
    ]

    cells = _none_to_empty(cells)

    if len(cells) != ARGIA_SCHEMA.column_count:
        raise RuntimeError(
            f"solaredge common row length mismatch: built {len(cells)} cells, "
            f"schema expects {ARGIA_SCHEMA.column_count}"
        )
    return cells
