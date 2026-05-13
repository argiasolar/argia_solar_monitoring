"""Column schema for telemetry tabs.

Two distinct schemas:

* **``PLANT_SCHEMA``** — wide (142 cols), vendor-shaped. One row per inverter
  per 5-min sample. Used for ``Telemetry_<KEY>`` per-plant tabs. Customers
  eventually get scoped access here via Sheets sharing. Each vendor fills in
  what it has and leaves the rest blank.

* **``ARGIA_SCHEMA``** — narrow (15 cols), cross-vendor common. Used for the
  single aggregated ``Telemetry_Argia`` tab. This is what Argia ops looks at:
  power, status, eToday, weather, by vendor/plant/inverter. No wasted columns.

CHANGING THE SCHEMA: every column rename or insertion is a breaking change.
Bump ``COLUMN_VERSION`` and document a migration in the runbook before
touching any column order.

If you change ``ARGIA_SCHEMA``, the existing tab MUST be manually deleted so
the new header gets written. The sheets writer refuses to write into a tab
whose header doesn't match the schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


COLUMN_VERSION = 2  # bumped from 1 when ARGIA_SCHEMA changed to narrow common


# ============================================================
# PLANT_SCHEMA column-family sizing constants (unchanged from Stage 3)
# ============================================================

MPPT_VOLTAGE_COUNT = 16
MPPT_POWER_COUNT = 9
STRING_VOLTAGE_COUNT = 32
STRING_CURRENT_LOW = 20
STRING_CURRENT_HIGH = 29
MPPT_EDAY_COUNT = 15


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
        return list(self.columns)


# ============================================================
# PLANT_SCHEMA — wide, vendor-shaped (UNCHANGED from Stage 3)
# ============================================================


def _identity_cols_plant() -> List[str]:
    return ["timestamp_utc", "timestamp_mx", "inverter_sn", "inverter_label"]


# Top-level inverter measurements that appear in every row, in column order.
# Field names match the Sheets header EXACTLY.
TYPED_INVERTER_COLS: Tuple[str, ...] = (
    "status",
    "power_w",
    "etoday_kwh",
    "pac_w",
    "iac_a",
    "pf",
    "pacr_w", "pacs_w", "pact_w",
    "vacr_v", "vacs_v", "vact_v",
    "vac_rs_v", "vac_st_v", "vac_tr_v",
    "fac_hz",
    "ppv_w",
    "epv_total_kwh",
    "temperature_c",
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
    "gfci_ma",
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
    "irradiance_wm2",
    "irradiance_kwh_m2_5m",
    "cloud_cover_pct",
    "ambient_temp_c",
)


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


PLANT_SCHEMA = TelemetrySchema(
    name="plant",
    columns=_build_plant_columns(),
    natural_key_columns=(0, 2),  # timestamp_utc, inverter_sn
)


# ============================================================
# ARGIA_SCHEMA — narrow, cross-vendor common (NEW in Stage 4)
# ============================================================


# Columns chosen for: cross-vendor consistency + operational signal at a
# glance. Anything vendor-specific stays in the per-plant tabs.
ARGIA_COMMON_COLS: Tuple[str, ...] = (
    "timestamp_utc",       # 0  — UTC ISO string
    "timestamp_mx",        # 1  — MX local "YYYY-MM-DD HH:MM:SS"
    "vendor",              # 2  — GROWATT | HUAWEI | SOLAREDGE | SMA
    "plant_key",           # 3
    "inverter_sn",         # 4
    "inverter_label",      # 5
    "status",              # 6  — 1=online, 3=offline (normalized across vendors)
    "power_w",             # 7
    "etoday_kwh",          # 8
    "temperature_c",       # 9  — blank for vendors that don't expose it yet
    "fault_code",          # 10 — vendor-specific format, stored as string
    "irradiance_wm2",      # 11
    "irradiance_kwh_m2_5m",  # 12
    "cloud_cover_pct",     # 13
    "ambient_temp_c",      # 14 — blank until env-station temp wired in
)


ARGIA_SCHEMA = TelemetrySchema(
    name="argia",
    columns=ARGIA_COMMON_COLS,
    # Natural key: (timestamp_utc, plant_key, inverter_sn).
    # Vendor is NOT in the key — within a single moment, one inverter belongs
    # to one vendor; including vendor in the key would be redundant.
    natural_key_columns=(0, 3, 4),
)


# ============================================================
# Tab naming
# ============================================================


def plant_tab_name(plant_key: str) -> str:
    """Sheets tab name for a plant's telemetry, e.g. ``Telemetry_GTO1``."""
    if not plant_key:
        raise ValueError("plant_key cannot be empty")
    return f"Telemetry_{plant_key}"


ARGIA_TAB_NAME = "Telemetry_Argia"


# ============================================================
# Vendor labels (used by the row builders for the vendor column)
# ============================================================


VENDOR_GROWATT = "GROWATT"
VENDOR_HUAWEI = "HUAWEI"
VENDOR_SOLAREDGE = "SOLAREDGE"
VENDOR_SMA = "SMA"
