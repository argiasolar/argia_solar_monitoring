"""Shared per-inverter status classification.

ONE state machine, consumed by every surface that labels an inverter
(dashboard builder today; report/acute tier as they migrate). Consolidation
after the 2026-07-03 carryover incident showed three consumers computing
three different answers from the same telemetry.

It deliberately owns NO detection logic of its own — it delegates to the
canonical detectors:

  * vendor faults  -> ``argia.analytics.vendor_flags.fault_tokens``
    (string fault summaries like "FT=302"; Huawei IS=/RS= STATE tokens are
    already excluded there — the 2026-07-03 MEX false-positive lesson)
  * peer comparison -> ``argia.analytics.inverter_health
    .evaluate_inverter_relative`` (leave-one-out, per-kW normalized so a
    smaller inverter is not falsely flagged — real case: GTO1 MWKNE9500D is
    60 kW among 124 kW peers; raw comparison reads a healthy unit at ~40%)

Status vocabulary (stable strings, Looker-friendly), first match wins:

  FAULT            vendor says so (status flag 3, or fault tokens present)
  OFFLINE          expected to produce (sun up) but silent or at zero
  DERATED          derating flag active while producing
  UNDERPERFORMING  producing, but below peer thresholds (per-kW)
  IDLE_NIGHT       dark because the sun is down / plant-wide calm
  NO_DATA          no telemetry and no basis to judge
  ONLINE           none of the above
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.analytics.inverter_health import (
    DEFAULT_CRIT_BELOW,
    DEFAULT_WARN_BELOW,
    InverterReading,
    evaluate_inverter_relative,
)
from argia.analytics.vendor_flags import fault_tokens

# Status vocabulary — stable strings; Looker filters/colors key on these.
ONLINE = "ONLINE"
UNDERPERFORMING = "UNDERPERFORMING"
FAULT = "FAULT"
DERATED = "DERATED"
OFFLINE = "OFFLINE"
IDLE_NIGHT = "IDLE_NIGHT"
NO_DATA = "NO_DATA"
RECOVERED = "RECOVERED"   # v96: was OFFLINE/FAULT earlier today, producing now

#: Hard-down states an inverter can "recover" from within a day.
_RECOVERABLE_FROM = frozenset({OFFLINE, FAULT})
#: States that mean the inverter is currently producing.
_PRODUCING_STATES = frozenset({ONLINE, UNDERPERFORMING, DERATED})


def display_status(worst_status: str, latest_status: str) -> str:
    """Consolidated day status for one inverter.

    The per-bucket ``worst_status`` alone mislabels an inverter that was
    OFFLINE/FAULT this morning but is producing now as still OFFLINE all
    day (the 2026-07-13 SAG confusion). When the worst bucket was a
    hard-down state but the LATEST bucket is producing, report RECOVERED
    instead — it stays an issue (its availability loss is real and kept),
    but it reads as "came back", not "currently down".
    """
    if worst_status in _RECOVERABLE_FROM and latest_status in _PRODUCING_STATES:
        return RECOVERED
    return worst_status

# Below this an inverter counts as "producing nothing" (kWh in the window
# under judgement — a bucket or a day; same constant the dashboard used).
ZERO_KWH = 0.05

# Growatt status flag meaning fault/standby.
VENDOR_STATUS_FAULT = 3


def is_vendor_fault(status_flag: Optional[float],
                    fault_code: Optional[str]) -> bool:
    """True when the DEVICE ITSELF reports a fault.

    ``fault_code`` is the normalized Telemetry_Argia string summary ("0",
    "FT=302", "IS=512,RS=1", ...). Token semantics live in
    ``vendor_flags.fault_tokens`` — the single place that knows which
    prefixes are faults and which are benign state.
    """
    if status_flag is not None:
        try:
            if int(status_flag) == VENDOR_STATUS_FAULT:
                return True
        except (TypeError, ValueError):
            pass
    return bool(fault_tokens(fault_code))


@dataclass(frozen=True)
class InverterBucket:
    """One inverter's aggregate over the window being classified."""

    inverter_sn: str
    energy_kwh: float
    reported: bool
    status_flag: Optional[float] = None      # vendor status (1 ok, 3 fault)
    fault_code: Optional[str] = None         # normalized string summary
    derating_mode: Optional[float] = None
    rated_kw: Optional[float] = None         # nameplate, for per-kW fairness


def classify_plant_bucket(
    inverters: List[InverterBucket],
    *,
    plant_key: str,
    sun_up: bool,
    warn_below: float = DEFAULT_WARN_BELOW,
    crit_below: float = DEFAULT_CRIT_BELOW,
) -> Dict[str, Tuple[str, str]]:
    """Classify every inverter of ONE plant for one window.

    Whole-plant call on purpose: UNDERPERFORMING is relative to peers, so
    it can only be judged with all siblings present. Returns
    ``{inverter_sn: (status, short_reason)}``.
    """
    # Peer judgement via the canonical detector, per-kW normalized when
    # nameplates are known. Only producing units enter (a dead unit is
    # OFFLINE/FAULT, not "0% of peers"); floor keeps dawn/dusk quiet.
    breaches = {}
    if sun_up:
        readings = [
            InverterReading(plant_key=plant_key, inverter_sn=b.inverter_sn,
                            value=b.energy_kwh, rated_kw=b.rated_kw)
            for b in inverters if b.reported and b.energy_kwh > ZERO_KWH
        ]
        for br in evaluate_inverter_relative(
                readings, warn_below=warn_below, crit_below=crit_below,
                min_peer_floor=ZERO_KWH):
            breaches[br.inverter_sn] = br

    any_producing = any(
        b.reported and b.energy_kwh > ZERO_KWH for b in inverters)
    any_reported = any(b.reported for b in inverters)

    out: Dict[str, Tuple[str, str]] = {}
    for b in inverters:
        out[b.inverter_sn] = _classify_one(
            b, sun_up=sun_up, any_producing=any_producing,
            any_reported=any_reported, breach=breaches.get(b.inverter_sn))
    return out


def _classify_one(b: InverterBucket, *, sun_up: bool, any_producing: bool,
                  any_reported: bool, breach) -> Tuple[str, str]:
    """Priority state machine for one inverter. First match wins."""
    if not b.reported:
        if sun_up:
            return OFFLINE, "no telemetry during daylight"
        if any_reported:
            return IDLE_NIGHT, "no telemetry (sun down)"
        return NO_DATA, "no telemetry"

    if is_vendor_fault(b.status_flag, b.fault_code):
        toks = ",".join(fault_tokens(b.fault_code)) or "status=3"
        return FAULT, f"vendor fault ({toks})"

    if not sun_up:
        return IDLE_NIGHT, "sun down"

    if b.energy_kwh <= ZERO_KWH:
        if any_producing:
            return OFFLINE, "0 kWh while peers producing"
        return IDLE_NIGHT, "0 kWh (no plant production this window)"

    if b.derating_mode not in (None, 0, 0.0):
        return DERATED, "derating active"

    if breach is not None:
        pct = round(breach.ratio * 100)
        return UNDERPERFORMING, f"{pct}% of peers [{breach.severity.value}]"

    return ONLINE, ""


def _had_context(b: InverterBucket) -> bool:
    """Whether we have enough plant context to call silence 'idle' rather
    than 'no data'. Kept as a seam; today silence at night is idle."""
    return True
