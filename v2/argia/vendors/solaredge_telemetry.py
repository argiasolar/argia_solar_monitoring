"""Rich SolarEdge inverter telemetry.

Stage 5.1: extracts the per-phase (L1Data/L2Data/L3Data), line-to-line
voltages (vL1To2, vL2To3, vL3To1), and grid frequency that the SolarEdge
API ACTUALLY returns. Stage 5 was leaving these blank because we assumed
SolarEdge had a thin response — live capture proved otherwise.

The ``/equipment/{siteId}/{sn}/data`` endpoint returns ~30 meaningful values
per inverter (not the 9 documented in older API references):

  Top-level (Stage 5 already extracted):
    totalActivePower, totalEnergy, temperature, dcVoltage,
    inverterMode, operationMode, powerLimit, groundFaultResistance, date

  Line-to-line voltages (NEW in 5.1):
    vL1To2, vL2To3, vL3To1

  Per-phase nested dicts (NEW in 5.1):
    L1Data, L2Data, L3Data — each containing:
      acCurrent, acVoltage, acFrequency,
      activePower, apparentPower, reactivePower, cosPhi

Rate limit: SolarEdge enforces 300 requests/day per site/api_key combo.
The pipeline catches HTTP 429 and skips remaining SE plants for the run.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from argia.core.normalize import normalize_sn, pick, safe_float
from argia.core.time_utils import MX_TZ, UTC
from argia.vendors.solaredge import (
    OFFLINE_INVERTER_MODES,
    SolarEdgeAPIError,
    SolarEdgeAuthError,
)

LOG = logging.getLogger("argia.vendors.solaredge_telemetry")


@dataclass(frozen=True)
class PhaseData:
    """One phase's electrical data, parsed from L1Data / L2Data / L3Data.

    Any field may be None if the phase block is missing (e.g. single-phase
    inverter, or some hardware revisions don't report all fields).
    """

    ac_voltage_v: Optional[float] = None
    ac_current_a: Optional[float] = None
    ac_frequency_hz: Optional[float] = None
    active_power_w: Optional[float] = None
    apparent_power_va: Optional[float] = None
    reactive_power_var: Optional[float] = None
    cos_phi: Optional[float] = None


EMPTY_PHASE = PhaseData()


@dataclass(frozen=True)
class SolarEdgeTelemetryRow:
    """Rich snapshot of one SolarEdge inverter at a moment in time.

    All energy values in kWh, all power values in W. Any field may be None
    if the API didn't return it. ``raw_telemetry`` preserves the latest
    response entry for diagnostics.
    """

    plant_key: str
    inverter_sn: str
    timestamp_utc: dt.datetime

    # Status / mode
    status: int                       # 1=online, 3=offline (normalized)
    raw_mode: str = ""                # e.g. "MPPT", "SLEEPING", "FAULT"
    operation_mode: Optional[int] = None

    # AC output (top-level)
    power_w: Optional[float] = None   # totalActivePower (already in W)

    # AC line-to-line voltages (NEW in Stage 5.1)
    v_l1_to_l2_v: Optional[float] = None
    v_l2_to_l3_v: Optional[float] = None
    v_l3_to_l1_v: Optional[float] = None

    # Per-phase electrical data (NEW in Stage 5.1)
    # L1, L2, L3 each get their own PhaseData
    l1: PhaseData = EMPTY_PHASE
    l2: PhaseData = EMPTY_PHASE
    l3: PhaseData = EMPTY_PHASE

    # Energy
    etoday_kwh: Optional[float] = None       # derived from totalEnergy diff
    etotal_kwh: Optional[float] = None       # totalEnergy / 1000

    # DC side
    temperature_c: Optional[float] = None
    dc_voltage_v: Optional[float] = None
    power_limit_pct: Optional[float] = None
    ground_fault_resistance: Optional[float] = None

    raw_telemetry: Dict[str, Any] = field(default_factory=dict)


def _parse_site_local_to_utc(value: Any, site_tz=MX_TZ) -> Optional[dt.datetime]:
    """Parse SolarEdge timestamp 'YYYY-MM-DD HH:MM:SS' (site-local) → UTC."""
    if not value:
        return None
    s = str(value).strip()
    try:
        naive = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=site_tz).astimezone(UTC)


def _inverter_mode_to_status(mode_raw: Any) -> int:
    """SolarEdge ``inverterMode`` string → 1 (online) or 3 (offline)."""
    if mode_raw is None:
        return 1
    mode = str(mode_raw).strip().upper()
    if mode in OFFLINE_INVERTER_MODES:
        return 3
    return 1


def _parse_phase(phase_dict: Any) -> PhaseData:
    """Parse one of L1Data / L2Data / L3Data into a PhaseData.

    Defensive: returns EMPTY_PHASE if the input isn't a dict.
    """
    if not isinstance(phase_dict, dict):
        return EMPTY_PHASE
    return PhaseData(
        ac_voltage_v=safe_float(phase_dict.get("acVoltage")),
        ac_current_a=safe_float(phase_dict.get("acCurrent")),
        ac_frequency_hz=safe_float(phase_dict.get("acFrequency")),
        active_power_w=safe_float(phase_dict.get("activePower")),
        apparent_power_va=safe_float(phase_dict.get("apparentPower")),
        reactive_power_var=safe_float(phase_dict.get("reactivePower")),
        cos_phi=safe_float(phase_dict.get("cosPhi")),
    )


def parse_telemetry_response(
    response: Dict[str, Any],
    plant_key: str,
    inverter_sn: str,
    site_tz=MX_TZ,
) -> Optional[SolarEdgeTelemetryRow]:
    """Parse a ``/equipment/{siteId}/{sn}/data`` response into a rich row.

    The response has multiple telemetry entries (one per 5/15 min through
    today). We pick the LATEST entry for current-state fields and use the
    diff between first and last for ``etoday_kwh``.

    Returns None if there are no telemetry entries.
    """
    if not isinstance(response, dict):
        return None

    data = response.get("data") or {}
    telemetries = data.get("telemetries") or []
    if not isinstance(telemetries, list) or not telemetries:
        return None

    # Sort defensively (API usually chronological but verify)
    def _entry_ts(entry: Dict[str, Any]) -> str:
        return str(entry.get("date", "")) if isinstance(entry, dict) else ""

    sorted_entries = sorted(telemetries, key=_entry_ts)
    latest = sorted_entries[-1]
    first = sorted_entries[0]
    if not isinstance(latest, dict):
        return None

    if LOG.isEnabledFor(logging.DEBUG):
        LOG.debug(
            "solaredge telemetry latest entry keys for %s: %s",
            inverter_sn, sorted(latest.keys()),
        )

    power_w = safe_float(pick(latest, ["totalActivePower", "power"]))
    mode_raw = pick(latest, ["inverterMode", "mode"])
    raw_mode_str = str(mode_raw) if mode_raw is not None else ""
    status = _inverter_mode_to_status(mode_raw)

    latest_total = safe_float(latest.get("totalEnergy"))
    etotal_kwh = (
        round(latest_total / 1000.0, 3) if latest_total is not None else None
    )

    # eToday from totalEnergy diff (latest minus first entry of the day)
    first_total = safe_float(first.get("totalEnergy")) if isinstance(first, dict) else None
    if latest_total is not None and first_total is not None:
        etoday_wh = max(0.0, latest_total - first_total)
        etoday_kwh: Optional[float] = round(etoday_wh / 1000.0, 3)
    else:
        etoday_kwh = None

    ts_utc = _parse_site_local_to_utc(latest.get("date"), site_tz)
    if ts_utc is None:
        ts_utc = dt.datetime.now(UTC).replace(microsecond=0)

    # NEW in Stage 5.1: line-to-line voltages
    v_l1_l2 = safe_float(latest.get("vL1To2"))
    v_l2_l3 = safe_float(latest.get("vL2To3"))
    v_l3_l1 = safe_float(latest.get("vL3To1"))

    # NEW in Stage 5.1: per-phase nested data
    l1 = _parse_phase(latest.get("L1Data"))
    l2 = _parse_phase(latest.get("L2Data"))
    l3 = _parse_phase(latest.get("L3Data"))

    return SolarEdgeTelemetryRow(
        plant_key=plant_key,
        inverter_sn=normalize_sn(inverter_sn),
        timestamp_utc=ts_utc,
        status=status,
        raw_mode=raw_mode_str,
        operation_mode=None if latest.get("operationMode") is None
                       else int(safe_float(latest.get("operationMode")) or 0),
        power_w=power_w,
        v_l1_to_l2_v=v_l1_l2,
        v_l2_to_l3_v=v_l2_l3,
        v_l3_to_l1_v=v_l3_l1,
        l1=l1, l2=l2, l3=l3,
        etoday_kwh=etoday_kwh,
        etotal_kwh=etotal_kwh,
        temperature_c=safe_float(latest.get("temperature")),
        dc_voltage_v=safe_float(latest.get("dcVoltage")),
        power_limit_pct=safe_float(latest.get("powerLimit")),
        ground_fault_resistance=safe_float(latest.get("groundFaultResistance")),
        raw_telemetry=dict(latest),
    )


# ============================================================
# Fetch helper — uses existing SolarEdgeClient transport
# ============================================================


def fetch_inverter_telemetry(
    se_client: Any,
    plant: Any,
    inverters: List[Any],
    site_tz=MX_TZ,
) -> List[SolarEdgeTelemetryRow]:
    """Call ``/equipment/{siteId}/{sn}/data`` for each inverter and parse rich rows.

    Uses ``SolarEdgeClient._get_json``. Failures per inverter are logged and
    skipped — other inverters still process. The whole batch returns whatever
    rows succeeded.

    **Rate-limit handling**: HTTP 429 surfaces as ``SolarEdgeAPIError`` with
    "rate-limited" in the message. Re-raised so the orchestrator can skip
    remaining SE plants for this run.
    """
    if not inverters:
        return []

    now_local = dt.datetime.now(site_tz)
    start_of_day = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    params_template = {
        "startTime": start_of_day.strftime("%Y-%m-%d %H:%M:%S"),
        "endTime": now_local.strftime("%Y-%m-%d %H:%M:%S"),
    }

    out: List[SolarEdgeTelemetryRow] = []
    for inv in inverters:
        try:
            response = se_client._get_json(
                f"/equipment/{plant.site_id}/{inv.inverter_sn}/data",
                dict(params_template),
            )
        except SolarEdgeAuthError as e:
            LOG.error("[%s/%s] auth error: %s",
                      plant.plant_key, inv.inverter_sn, e)
            raise
        except SolarEdgeAPIError as e:
            msg = str(e)
            if "rate-limited" in msg.lower() or "429" in msg:
                LOG.warning(
                    "[%s/%s] rate-limited — skipping remaining inverters",
                    plant.plant_key, inv.inverter_sn,
                )
                raise
            LOG.warning(
                "[%s/%s] API error: %s",
                plant.plant_key, inv.inverter_sn, e,
            )
            continue

        row = parse_telemetry_response(
            response, plant.plant_key, inv.inverter_sn, site_tz,
        )
        if row is None:
            LOG.warning(
                "[%s/%s] no telemetry entries — likely offline or no data today",
                plant.plant_key, inv.inverter_sn,
            )
            continue
        out.append(row)

    LOG.info(
        "[%s] fetched %d telemetry rows from %d inverter(s)",
        plant.plant_key, len(out), len(inverters),
    )
    return out
