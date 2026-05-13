"""Column schema for Growatt 5-min telemetry tabs.

There are two tab shapes:

* **Per-plant tab** (``Telemetry_<KEY>``): one row per inverter per 5-min sample.
  Natural key = (timestamp_utc, inverter_sn). Customers will eventually get
  scoped access to their plant's tab via Sheets sharing.

* **Argia aggregated tab** (``Telemetry_Argia``): same columns plus ``plant_key``
  inserted right after the timestamps. Natural key = (timestamp_utc, plant_key,
  inverter_sn). Used by Argia operations + the future alerting layer.

The column list is exhaustive — ~135 columns including per-MPPT voltages,
per-string voltages, fault codes, and weather. Users will never read these
raw; they'll read dashboards built on top. The wide rows are for the backend
and alert rules.

CHANGING THE SCHEMA: every column rename or insertion is a breaking change
to existing tabs. Bump COLUMN_VERSION and bake a migration plan into Stage 3.x
docs before touching any column order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


COLUMN_VERSION = 1


# ============================================================
# Column-family sizing constants
# ============================================================

# Per-MPPT voltage columns (vpv1..vpv16) — Growatt MAX inverters report up to 16
MPPT_VOLTAGE_COUNT = 16

# Per-MPPT power columns (ppv1..ppv9) — only 9 distinct power channels even
# though there are 16 voltage channels; the rest aggregate into earlier ones.
MPPT_POWER_COUNT = 9

# Per-string voltage columns (vString1..vString32)
STRING_VOLTAGE_COUNT = 32

# Per-string current columns. Growatt populates currentString20..currentString29
# in practice; 1..19 and 30..32 are always zero on the captured fixtures. We
# include the 20..29 range so the column is there if/when it appears for other
# inverter types.
STRING_CURRENT_LOW = 20
STRING_CURRENT_HIGH = 29

# Per-MPPT daily/total energy (epv1Today..epv15Today, epv1Total..epv15Total)
MPPT_EDAY_COUNT = 15


# ============================================================
# Column groups (so the row builder and tests share one definition)
# ============================================================


def _identity_cols_plant() -> List[str]:
    """Identity columns for per-plant tab."""
    return ["timestamp_utc", "timestamp_mx", "inverter_sn", "inverter_label"]


def _identity_cols_argia() -> List[str]:
    """Identity columns for aggregated Argia tab.

    plant_key is inserted between the timestamps and the inverter identity so
    that filtering or sorting by plant is straightforward.
    """
    return [
        "timestamp_utc",
        "timestamp_mx",
        "plant_key",
        "inverter_sn",
        "inverter_label",
    ]


# Top-level inverter measurements that appear in every row, in column order.
# Field names match the Sheets header EXACTLY. The row builder reads the
# corresponding values from the parsed row (or its raw dict).
TYPED_INVERTER_COLS: Tuple[str, ...] = (
    "status",               # 1=online, 3=offline (derived from fault codes)
    "power_w",              # int watts, derived from pac
    "etoday_kwh",           # eacToday
    "pac_w",                # pac as float
    "iac_a",                # iac
    "pf",                   # power factor
    "pacr_w", "pacs_w", "pact_w",      # per-phase power
    "vacr_v", "vacs_v", "vact_v",      # per-phase voltage (line-to-neutral)
    "vac_rs_v", "vac_st_v", "vac_tr_v",  # line-to-line voltages
    "fac_hz",               # AC frequency
    "ppv_w",                # total DC power
    "epv_total_kwh",        # epvTotal (lifetime DC energy)
    "temperature_c",        # inverter primary temperature
    "warn_code",
    "warn_code_1",
    "fault_code_1",
    "fault_code_2",
    "fault_type",
    "pid_status",
    "pid_fault_code",
    "apf_status",
    "afci_status",
    "derating_mode",
    "real_op_percent",
    "pv_iso",
    "p_bus_voltage_v",
    "n_bus_voltage_v",
    "str_unmatch",
    "str_unblance",
    "str_break",
    "gfci_ma",              # ground-fault current sensor reading
)


def _per_mppt_voltage_cols() -> List[str]:
    return [f"vpv{i}_v" for i in range(1, MPPT_VOLTAGE_COUNT + 1)]


def _per_mppt_power_cols() -> List[str]:
    return [f"ppv{i}_w" for i in range(1, MPPT_POWER_COUNT + 1)]


def _per_string_voltage_cols() -> List[str]:
    return [f"vstring{i}_v" for i in range(1, STRING_VOLTAGE_COUNT + 1)]


def _per_string_current_cols() -> List[str]:
    return [
        f"istring{i}_a"
        for i in range(STRING_CURRENT_LOW, STRING_CURRENT_HIGH + 1)
    ]


def _per_mppt_eday_today_cols() -> List[str]:
    return [f"epv{i}_today_kwh" for i in range(1, MPPT_EDAY_COUNT + 1)]


def _per_mppt_eday_total_cols() -> List[str]:
    return [f"epv{i}_total_kwh" for i in range(1, MPPT_EDAY_COUNT + 1)]


WEATHER_COLS: Tuple[str, ...] = (
    "irradiance_wm2",         # latest instantaneous W/m² at this plant's env station
    "irradiance_kwh_m2_5m",   # 5-min interval kWh/m² (W/m² * 5/60000)
    "cloud_cover_pct",        # Open-Meteo current/hourly cloud cover
    "ambient_temp_c",         # env station envTemp (when available)
)


# ============================================================
# Schema dataclass
# ============================================================


@dataclass(frozen=True)
class TelemetrySchema:
    """A telemetry tab's column structure."""

    name: str
    columns: Tuple[str, ...]
    natural_key_columns: Tuple[int, ...]

    @property
    def column_count(self) -> int:
        return len(self.columns)

    @property
    def header(self) -> List[str]:
        """The list to pass to ``SheetsClient.ensure_header``."""
        return list(self.columns)


def _build_plant_columns() -> Tuple[str, ...]:
    cols: List[str] = []
    cols.extend(_identity_cols_plant())
    cols.extend(TYPED_INVERTER_COLS)
    cols.extend(_per_mppt_voltage_cols())
    cols.extend(_per_mppt_power_cols())
    cols.extend(_per_string_voltage_cols())
    cols.extend(_per_string_current_cols())
    cols.extend(_per_mppt_eday_today_cols())
    cols.extend(_per_mppt_eday_total_cols())
    cols.extend(WEATHER_COLS)
    return tuple(cols)


def _build_argia_columns() -> Tuple[str, ...]:
    cols: List[str] = []
    cols.extend(_identity_cols_argia())
    cols.extend(TYPED_INVERTER_COLS)
    cols.extend(_per_mppt_voltage_cols())
    cols.extend(_per_mppt_power_cols())
    cols.extend(_per_string_voltage_cols())
    cols.extend(_per_string_current_cols())
    cols.extend(_per_mppt_eday_today_cols())
    cols.extend(_per_mppt_eday_total_cols())
    cols.extend(WEATHER_COLS)
    return tuple(cols)


PLANT_SCHEMA = TelemetrySchema(
    name="plant",
    columns=_build_plant_columns(),
    natural_key_columns=(0, 2),  # timestamp_utc, inverter_sn
)


ARGIA_SCHEMA = TelemetrySchema(
    name="argia",
    columns=_build_argia_columns(),
    natural_key_columns=(0, 2, 3),  # timestamp_utc, plant_key, inverter_sn
)


# ============================================================
# Tab naming
# ============================================================


def plant_tab_name(plant_key: str) -> str:
    """Sheets tab name for a plant's telemetry, e.g. 'Telemetry_GTO1'."""
    if not plant_key:
        raise ValueError("plant_key cannot be empty")
    return f"Telemetry_{plant_key}"


ARGIA_TAB_NAME = "Telemetry_Argia"
