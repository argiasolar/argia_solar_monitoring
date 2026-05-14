"""Rich Huawei inverter telemetry.

The existing ``HuaweiClient.fetch_inverter_snapshots`` extracts only 4 fields
(status, power, eToday, timestamp) from ``getDevRealKpi`` responses. But the
endpoint actually returns a rich ``dataItemMap`` with ~25+ fields per inverter:
temperature, per-MPPT voltages and currents, AC three-phase, frequency, power
factor, MPPT-level day energy, fault codes, and more.

This module defines:

* ``HuaweiTelemetryRow`` — dataclass holding the full rich snapshot.
* ``parse_telemetry_item`` — parses one ``getDevRealKpi.data[]`` element into
  a ``HuaweiTelemetryRow``. Field reads are *defensive*: every numeric field
  uses ``safe_float`` against multiple candidate keys, so a missing/renamed
  field becomes None (→ blank cell), not a crash.

The Huawei docs list field names that vary by inverter model (SUN2000-100KTL
vs SUN2000-330KTL, etc.). The parser tries the canonical names plus common
variants. If a field is consistently missing for your hardware, the wide
telemetry row simply leaves that cell blank — the rest still lands.

DEBUG instrumentation: when called with logging at DEBUG level, the parser
prints the raw key list from each ``dataItemMap`` so you can see what's
actually available the first time you run live.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from argia.core.normalize import normalize_sn, pick, safe_float, safe_int
from argia.core.time_utils import now_utc, parse_provider_datetime
from argia.vendors.base import normalize_status

LOG = logging.getLogger("argia.vendors.huawei_telemetry")

# Per-MPPT range Huawei supports. SUN2000 inverters expose up to pv24 in big
# units; smaller models stop at pv4 or pv8. We try pv1..pv16 to cover most
# units; the parser just emits None for missing ones.
MPPT_RANGE = range(1, 17)  # pv1 .. pv16


@dataclass(frozen=True)
class HuaweiTelemetryRow:
    """Rich snapshot of one Huawei inverter at a moment in time.

    All energy values in kWh, all power values in W, voltages in V, currents
    in A, temperatures in °C. Frequency in Hz. Any field may be None if the
    inverter doesn't expose it.

    The ``raw_data_item_map`` is preserved so the telemetry row builder can
    fall back to it for any field the parser missed.
    """

    plant_key: str
    inverter_sn: str
    timestamp_utc: dt.datetime

    # Status / state
    status: int           # 1=online, 3=offline (normalized)
    raw_status: str = ""  # devStatus value as string (e.g. "1", "513")
    inverter_state: Optional[int] = None
    run_state: Optional[int] = None

    # AC output
    power_w: Optional[float] = None             # active_power (kW from API → W)
    reactive_power_var: Optional[float] = None
    power_factor: Optional[float] = None
    efficiency_pct: Optional[float] = None
    elec_freq_hz: Optional[float] = None

    # AC three-phase voltage & current (line-to-line and per-phase)
    ab_u_v: Optional[float] = None
    bc_u_v: Optional[float] = None
    ca_u_v: Optional[float] = None
    a_i_a: Optional[float] = None
    b_i_a: Optional[float] = None
    c_i_a: Optional[float] = None

    # Energy
    etoday_kwh: Optional[float] = None        # day_cap
    etotal_kwh: Optional[float] = None        # total_cap
    mppt_total_kwh: Optional[float] = None    # mppt_total_cap

    # DC side
    temperature_c: Optional[float] = None
    mppt_power_w: Optional[float] = None      # mppt_power (kW → W)

    # Per-MPPT — voltage, current, daily energy (up to 16)
    pv_voltages_v: tuple = ()    # (pv1_u, pv2_u, ...)
    pv_currents_a: tuple = ()    # (pv1_i, pv2_i, ...)
    pv_eday_kwh: tuple = ()      # (mppt_1_cap, mppt_2_cap, ...)

    # Raw field map for diagnostics + fallback
    raw_data_item_map: Dict[str, Any] = field(default_factory=dict)


def _convert_power_to_watts(value: Any) -> Optional[float]:
    """Huawei usually returns kW. Heuristic: <=1000 means kW, >1000 already W."""
    v = safe_float(value)
    if v is None:
        return None
    return v * 1000.0 if abs(v) <= 1000 else v


def _per_mppt(data_map: Dict[str, Any], pattern: List[str]) -> tuple:
    """Read per-MPPT values into a tuple.

    ``pattern`` is a list of candidate key formats with ``{i}`` placeholder.
    For each i in 1..16, try each candidate; first match wins. None for misses.
    """
    out: List[Optional[float]] = []
    for i in MPPT_RANGE:
        value: Optional[float] = None
        for fmt in pattern:
            key = fmt.format(i=i)
            if key in data_map:
                value = safe_float(data_map[key])
                break
        out.append(value)
    return tuple(out)


def parse_telemetry_item(
    item: Dict[str, Any],
    plant_key: str,
) -> Optional[HuaweiTelemetryRow]:
    """Parse one ``getDevRealKpi.data[]`` element into a HuaweiTelemetryRow.

    Returns None if the item lacks an SN. All other fields are best-effort —
    missing fields stay None.
    """
    if not isinstance(item, dict):
        return None

    sn = normalize_sn(
        pick(item, ["sn", "devSn", "deviceSn", "serialNum", "esn"])
    )
    if not sn:
        return None

    data_map = item.get("dataItemMap") or {}
    if not isinstance(data_map, dict):
        data_map = {}

    # DEBUG: log the field keys so we can see what's actually available
    if LOG.isEnabledFor(logging.DEBUG):
        LOG.debug(
            "huawei dataItemMap for %s has %d fields: %s",
            sn, len(data_map), sorted(data_map.keys()),
        )

    status_raw = pick(item, ["devStatus", "status", "workStatus"])
    raw_status_str = str(status_raw) if status_raw is not None else ""

    ts_raw = pick(item, ["collectTime", "updateTime", "time"])
    ts = parse_provider_datetime(ts_raw) or now_utc()

    return HuaweiTelemetryRow(
        plant_key=plant_key,
        inverter_sn=sn,
        timestamp_utc=ts,
        status=normalize_status(status_raw),
        raw_status=raw_status_str,
        inverter_state=safe_int(pick(data_map, ["inverter_state", "inverterState"])),
        run_state=safe_int(pick(data_map, ["run_state", "runState"])),

        # AC
        power_w=_convert_power_to_watts(
            pick(data_map, ["active_power", "activePower", "pac", "power"])
        ),
        reactive_power_var=_convert_power_to_watts(
            pick(data_map, ["reactive_power", "reactivePower", "q"])
        ),
        power_factor=safe_float(
            pick(data_map, ["power_factor", "powerFactor", "pf"])
        ),
        efficiency_pct=safe_float(
            pick(data_map, ["efficiency", "eff"])
        ),
        elec_freq_hz=safe_float(
            pick(data_map, ["elec_freq", "elecFreq", "grid_freq", "fac", "frequency"])
        ),

        # AC three-phase
        ab_u_v=safe_float(pick(data_map, ["ab_u", "abU", "u_ab", "uAB"])),
        bc_u_v=safe_float(pick(data_map, ["bc_u", "bcU", "u_bc", "uBC"])),
        ca_u_v=safe_float(pick(data_map, ["ca_u", "caU", "u_ca", "uCA"])),
        a_i_a=safe_float(pick(data_map, ["a_i", "aI", "i_a", "iA"])),
        b_i_a=safe_float(pick(data_map, ["b_i", "bI", "i_b", "iB"])),
        c_i_a=safe_float(pick(data_map, ["c_i", "cI", "i_c", "iC"])),

        # Energy
        etoday_kwh=safe_float(
            pick(data_map, ["day_cap", "daily_cap", "eToday", "todayEnergy"])
        ),
        etotal_kwh=safe_float(
            pick(data_map, ["total_cap", "totalEnergy", "eTotal"])
        ),
        mppt_total_kwh=safe_float(
            pick(data_map, ["mppt_total_cap", "mpptTotalCap"])
        ),

        # DC + temp
        temperature_c=safe_float(
            pick(data_map, ["temperature", "temperature_c", "temp",
                            "internal_temperature", "inverter_temperature"])
        ),
        mppt_power_w=_convert_power_to_watts(
            pick(data_map, ["mppt_power", "mpptPower"])
        ),

        # Per-MPPT — try multiple naming conventions
        pv_voltages_v=_per_mppt(data_map, ["pv{i}_u", "pv{i}U", "u_pv{i}", "uPV{i}"]),
        pv_currents_a=_per_mppt(data_map, ["pv{i}_i", "pv{i}I", "i_pv{i}", "iPV{i}"]),
        pv_eday_kwh=_per_mppt(data_map, ["mppt_{i}_cap", "mppt{i}Cap"]),

        raw_data_item_map=dict(data_map),
    )


def parse_telemetry_response(
    response: Dict[str, Any],
    plant_key: str,
) -> List[HuaweiTelemetryRow]:
    """Parse the entire ``getDevRealKpi`` response into rich telemetry rows.

    Returns one row per inverter Huawei returned data for. Returns empty list
    on ``success: false`` or empty data.
    """
    if not isinstance(response, dict):
        return []
    if not response.get("success"):
        return []
    rows: List[HuaweiTelemetryRow] = []
    for item in response.get("data") or []:
        row = parse_telemetry_item(item, plant_key)
        if row is not None:
            rows.append(row)
    return rows


# ============================================================
# Fetch helper — uses existing HuaweiClient transport
# ============================================================

# Batch size matches HuaweiClient's; if you change one, change both.
_SN_BATCH_SIZE = 50
_INVERTER_DEV_TYPE_ID = 1


def fetch_inverter_telemetry(
    huawei_client: Any,
    plant: Any,
    inverters: List[Any],
) -> List[HuaweiTelemetryRow]:
    """Call ``getDevRealKpi`` via the existing HuaweiClient and parse rich rows.

    This deliberately reuses ``HuaweiClient._post_json`` and ``_ensure_logged_in``
    instead of duplicating transport / auth logic. The client doesn't need a
    new public method for one extra parse path.

    Returns one ``HuaweiTelemetryRow`` per inverter the API returned data for.
    Inverters not returned by Huawei (offline, unknown SN) are simply omitted —
    same behavior as the existing ``fetch_inverter_snapshots``.

    Raises whatever ``_post_json`` raises (``HuaweiAPIError``,
    ``HuaweiAuthError`` from login).
    """
    if not inverters:
        return []
    huawei_client._ensure_logged_in()

    sns = [inv.inverter_sn for inv in inverters]
    out: List[HuaweiTelemetryRow] = []

    # Local import to avoid a top-of-file circular dependency
    from argia.core.normalize import chunked
    from argia.vendors.huawei import HuaweiAPIError

    for batch in chunked(sns, _SN_BATCH_SIZE):
        result = huawei_client._post_json(
            "/getDevRealKpi",
            {"devTypeId": str(_INVERTER_DEV_TYPE_ID), "sns": ",".join(batch)},
        )
        if not result.get("success"):
            raise HuaweiAPIError(
                f"getDevRealKpi failed: failCode={result.get('failCode')} "
                f"msg={result.get('message')}"
            )
        rows = parse_telemetry_response(result, plant.plant_key)
        out.extend(rows)

    LOG.info(
        "[%s] fetched %d rich telemetry rows from %d inverter(s)",
        plant.plant_key, len(out), len(inverters),
    )
    return out
