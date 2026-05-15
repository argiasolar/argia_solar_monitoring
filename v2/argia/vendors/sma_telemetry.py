"""Rich SMA inverter telemetry — parser + fetch helper.

SMA's pvGeneration measurement set field names vary between:
- ennexOS plants (current, sandbox primarily simulates these)
- Sunny Portal Classic (older Webconnect/Home-Manager systems)

Until we capture real fixtures from your sandbox we don't know exactly what
field names appear. This module is built defensively:
  - Multiple key variants tried for each field via ``pick()``
  - DEBUG logs print raw keys so the first live run reveals the truth
  - Missing fields stay None, never crash

Architecture mirrors v2/argia/vendors/huawei_telemetry.py and
solaredge_telemetry.py: a dataclass + a parser + a fetch helper that reuses
the existing ``SMAClient._get_json`` transport.

Stage 6.1 hotfix risk: once we have real sandbox capture, this is the file
most likely to need patching to add the right field names. The DEBUG logs
will guide that patch quickly.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from argia.core.normalize import normalize_sn, pick, safe_float
from argia.core.time_utils import MX_TZ, UTC, now_utc, parse_provider_datetime
from argia.vendors.sma import (
    OFFLINE_DEVICE_STATES,
    SMAAPIError,
    SMAAuthError,
    SMAConsentError,
)

LOG = logging.getLogger("argia.vendors.sma_telemetry")


@dataclass(frozen=True)
class SMATelemetryRow:
    """Rich snapshot of one SMA inverter at a moment in time.

    All energy values in kWh, all power values in W. Any field may be None.
    ``raw_set`` preserves the response's ``set`` block for diagnostics.
    """

    plant_key: str
    inverter_sn: str
    timestamp_utc: dt.datetime

    # Status / mode
    status: int                       # 1=online, 3=offline (normalized)
    raw_status: str = ""              # original string from API

    # AC output
    power_w: Optional[float] = None       # active power, W
    reactive_power_var: Optional[float] = None
    apparent_power_va: Optional[float] = None
    power_factor: Optional[float] = None

    # AC voltages / current / freq (often unavailable in sandbox)
    vac_v: Optional[float] = None         # phase voltage average if reported
    iac_a: Optional[float] = None
    fac_hz: Optional[float] = None

    # Energy
    etoday_kwh: Optional[float] = None
    etotal_kwh: Optional[float] = None

    # DC side
    dc_voltage_v: Optional[float] = None
    dc_current_a: Optional[float] = None
    dc_power_w: Optional[float] = None

    # Environmental
    temperature_c: Optional[float] = None

    raw_set: Dict[str, Any] = field(default_factory=dict)


def _parse_sma_timestamp(value: Any, site_tz=MX_TZ) -> Optional[dt.datetime]:
    """Parse SMA timestamp (ISO 8601, possibly with 'Z' or offset) → UTC."""
    if not value:
        return None
    s = str(value).strip()
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=site_tz)
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        pass
    return parse_provider_datetime(s)


def _status_from_state(raw: Any) -> int:
    """SMA device state → 1 (online) or 3 (offline)."""
    if raw is None:
        return 1
    state = str(raw).strip().upper()
    if state in OFFLINE_DEVICE_STATES:
        return 3
    return 1


def _maybe_kw_to_w(value: Optional[float]) -> Optional[float]:
    """Heuristic kW→W conversion for SMA values.

    SMA sandbox sometimes returns power in kW with small magnitudes (e.g.
    25.4 = 25.4 kW for a small residential inverter), sometimes in W.
    Convention: if abs value <= 1000, it's kW; otherwise it's W.

    This is the SAME heuristic Huawei and Growatt clients use. Documented
    upper bound: a 1 MW inverter would read as 1000 kW which we'd
    incorrectly treat as 1000 W = 1 kW. In practice SMA inverters in the
    portfolio we monitor are < 200 kW so this is safe.
    """
    if value is None:
        return None
    return value * 1000.0 if abs(value) <= 1000 else value


def parse_telemetry_response(
    response: Dict[str, Any],
    plant_key: str,
    inverter_sn: str,
    site_tz=MX_TZ,
) -> Optional[SMATelemetryRow]:
    """Parse a /devices/{id}/measurements/sets/pvGeneration response.

    Expected shape (per evcc example & SMA FAQ):
        {
          "device": { "deviceId": "...", "name": "...", "timezone": "..." },
          "setType": "pvGeneration",
          "set": { ...fields... },
          "status": "Ok"
        }

    Returns None if ``set`` is missing or non-dict.
    """
    if not isinstance(response, dict):
        return None

    s = response.get("set")
    if not isinstance(s, dict):
        return None

    device = response.get("device") if isinstance(response.get("device"), dict) else {}

    if LOG.isEnabledFor(logging.DEBUG):
        LOG.debug(
            "sma pvGeneration 'set' keys for %s: %s",
            inverter_sn, sorted(s.keys()),
        )
        if device:
            LOG.debug(
                "sma pvGeneration 'device' keys for %s: %s",
                inverter_sn, sorted(device.keys()),
            )

    # ----- AC active power -----
    power_w = _maybe_kw_to_w(safe_float(pick(s, [
        "power", "pac", "activePower", "totalActivePower",
        "pvPower", "powerW",
    ])))

    # ----- Reactive / apparent / pf -----
    reactive = safe_float(pick(s, ["reactivePower", "qac", "reactive"]))
    apparent = safe_float(pick(s, ["apparentPower", "sac"]))
    pf = safe_float(pick(s, ["powerFactor", "cosPhi", "pf"]))

    # ----- AC voltages / freq -----
    vac = safe_float(pick(s, ["acVoltage", "vac", "voltageAc", "voltage"]))
    iac = safe_float(pick(s, ["acCurrent", "iac", "currentAc"]))
    fac = safe_float(pick(s, ["acFrequency", "fac", "frequency", "gridFrequency"]))

    # ----- Energy -----
    etoday_raw = safe_float(pick(s, [
        "energyDay", "yieldDay", "totalEnergyDay", "eToday", "energyToday",
    ]))
    etoday_kwh: Optional[float] = None
    if etoday_raw is not None:
        # If suspiciously large (>1e6), likely in Wh
        if etoday_raw > 1_000_000:
            etoday_kwh = round(etoday_raw / 1000.0, 3)
        else:
            etoday_kwh = round(etoday_raw, 3)

    etotal_raw = safe_float(pick(s, [
        "energyTotal", "yieldTotal", "totalEnergy", "eTotal", "lifetimeEnergy",
    ]))
    etotal_kwh: Optional[float] = None
    if etotal_raw is not None:
        if etotal_raw > 1_000_000:
            etotal_kwh = round(etotal_raw / 1000.0, 3)
        else:
            etotal_kwh = round(etotal_raw, 3)

    # ----- DC side -----
    dc_voltage = safe_float(pick(s, ["dcVoltage", "vdc", "voltageDc"]))
    dc_current = safe_float(pick(s, ["dcCurrent", "idc", "currentDc"]))
    dc_power = _maybe_kw_to_w(safe_float(pick(s, ["dcPower", "pdc", "powerDc"])))

    # ----- Environmental -----
    temperature = safe_float(pick(s, [
        "temperature", "inverterTemperature", "internalTemperature", "tempIn",
    ]))

    # ----- Status -----
    status_raw = pick(s, ["status", "deviceStatus", "operationalState"])
    if status_raw is None and device:
        status_raw = pick(device, ["status", "operationalState"])
    status = _status_from_state(status_raw)
    raw_status_str = str(status_raw) if status_raw is not None else ""

    # ----- Timestamp -----
    ts_raw = pick(s, ["time", "timestamp", "date"])
    ts_utc = _parse_sma_timestamp(ts_raw, site_tz)
    if ts_utc is None:
        ts_utc = now_utc()

    return SMATelemetryRow(
        plant_key=plant_key,
        inverter_sn=normalize_sn(inverter_sn),
        timestamp_utc=ts_utc,
        status=status,
        raw_status=raw_status_str,
        power_w=power_w,
        reactive_power_var=reactive,
        apparent_power_va=apparent,
        power_factor=pf,
        vac_v=vac,
        iac_a=iac,
        fac_hz=fac,
        etoday_kwh=etoday_kwh,
        etotal_kwh=etotal_kwh,
        dc_voltage_v=dc_voltage,
        dc_current_a=dc_current,
        dc_power_w=dc_power,
        temperature_c=temperature,
        raw_set=dict(s),
    )


def fetch_inverter_telemetry(
    sma_client: Any,
    plant: Any,
    inverters: List[Any],
    site_tz=MX_TZ,
) -> List[SMATelemetryRow]:
    """Call /devices/{id}/measurements/sets/pvGeneration for each inverter.

    Uses ``SMAClient._get_json``. Per-inverter failures (404, rate-limit,
    auth) are logged and skipped. Auth errors propagate (every device will
    fail with the same auth issue, no point continuing).
    """
    if not inverters:
        return []

    out: List[SMATelemetryRow] = []
    for inv in inverters:
        try:
            response = sma_client._get_json(
                f"/devices/{inv.inverter_sn}/measurements/sets/pvGeneration",
                {"Period": "Recent"},
            )
        except SMAAuthError as e:
            LOG.error("[%s/%s] auth error: %s",
                      plant.plant_key, inv.inverter_sn, e)
            raise
        except SMAConsentError as e:
            LOG.error("[%s/%s] consent error: %s",
                      plant.plant_key, inv.inverter_sn, e)
            raise
        except SMAAPIError as e:
            msg = str(e).lower()
            if "rate-limited" in msg or "429" in msg:
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
                "[%s/%s] empty pvGeneration response — likely no data in sandbox",
                plant.plant_key, inv.inverter_sn,
            )
            continue
        out.append(row)

    LOG.info(
        "[%s] fetched %d telemetry rows from %d inverter(s)",
        plant.plant_key, len(out), len(inverters),
    )
    return out
