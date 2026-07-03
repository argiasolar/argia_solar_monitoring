"""Vendor-flag detectors — inverter self-diagnosed problems.

Unlike the production-based detectors (inverter_relative, energy_daily_pct,
plant_twin_yield), these relay what the INVERTER ITSELF reports is wrong.
The device already made the judgement; we surface it with the code attached,
so the alert names the failure instead of inferring it from lost energy.

Data source: the normalized ``Telemetry_Argia.fault_code`` column, already
loaded by the alerts script's day bundle — no extra reads. Its format (see
``growatt_row._format_fault_code``): ``"0"`` when healthy, else a compact
summary like ``"FT=302"`` or ``"FC1=1,FT=203"``.

Real seed case (verified 2026-07-03): GTO1 units JFM5D8900B and JFM7DXN013
carry ``FT=302`` in 115 telemetry rows while our production detectors could
only say "underperforming".
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.analytics.inverter_health import Severity
from argia.archive.kpi_daily import (
    CLOUD_DAYLIGHT_END_HOUR,
    CLOUD_DAYLIGHT_START_HOUR,
)
from argia.core.time_utils import utc_to_mx

LOG = logging.getLogger("argia.analytics.vendor_flags")

MIN_FAULT_SAMPLES = 2
"""A fault must appear in at least this many daylight samples to fire.
One glitchy row (transient comms hiccup, mid-reboot read) is not a fault;
two or more across the day is the device consistently reporting a problem."""


# Token prefixes that mean the device reports an actual FAULT:
#   FT=/FC1=/FC2=  Growatt fault type / fault codes
#   DS=            Huawei devStatus abnormal (device not in normal state)
# Deliberately NOT fault tokens: Huawei IS= (inverter_state) and RS=
# (run_state) are STATE, not faults — IS=512,RS=1 is the normal on-grid
# running state and appears in every healthy sample (verified 2026-07-03:
# treating them as faults flagged all six healthy MEX inverters at 9/9
# samples). Decoding non-standard IS values (e.g. IS=768 seen on a weak
# unit) is a follow-up, not a guess to alert on.
FAULT_TOKEN_PREFIXES = ("FT=", "FC1=", "FC2=", "DS=")


def fault_tokens(code: Optional[str]) -> List[str]:
    """Extract the genuinely fault-indicating tokens from a compact summary."""
    if code is None:
        return []
    text = str(code).strip()
    if text in ("", "0", "0.0"):
        return []
    return [t.strip() for t in text.split(",")
            if t.strip().startswith(FAULT_TOKEN_PREFIXES)]


@dataclass(frozen=True)
class FaultBreach:
    """An inverter that reported vendor fault codes during daylight."""

    plant_key: str
    inverter_sn: str
    codes: str            # e.g. "FT=302 (x115)" — worst/most common first
    samples_faulted: int
    samples_total: int
    severity: Severity
    message: str


def evaluate_inverter_faults(
    samples: List[Tuple[dt.datetime, str, str, Optional[str]]],
    min_samples: int = MIN_FAULT_SAMPLES,
) -> List[FaultBreach]:
    """Flag inverters whose vendor fault summary is non-zero during daylight.

    ``samples`` is [(timestamp_utc, plant_key, inverter_sn, fault_code), ...]
    straight from the day bundle. Night rows are ignored (some devices report
    standby codes after sunset). An inverter fires when it has at least
    ``min_samples`` faulted daylight rows; severity is CRITICAL — the device
    itself says it has a fault, there is no "warning" interpretation.

    Pure function — no I/O.
    """
    total: Dict[Tuple[str, str], int] = defaultdict(int)
    faulted: Dict[Tuple[str, str], int] = defaultdict(int)
    codes: Dict[Tuple[str, str], Counter] = defaultdict(Counter)

    for ts, plant_key, sn, code in samples:
        if ts is None:
            continue
        mx = utc_to_mx(ts)
        if not (CLOUD_DAYLIGHT_START_HOUR <= mx.hour < CLOUD_DAYLIGHT_END_HOUR):
            continue
        key = (str(plant_key).strip(), str(sn).strip())
        total[key] += 1
        tokens = fault_tokens(code)
        if tokens:
            faulted[key] += 1
            codes[key][",".join(tokens)] += 1

    breaches: List[FaultBreach] = []
    for key, n_fault in sorted(faulted.items()):
        if n_fault < min_samples:
            continue
        plant_key, sn = key
        code_summary = ", ".join(
            f"{c} (x{n})" for c, n in codes[key].most_common(3)
        )
        breaches.append(FaultBreach(
            plant_key=plant_key,
            inverter_sn=sn,
            codes=code_summary,
            samples_faulted=n_fault,
            samples_total=total[key],
            severity=Severity.CRITICAL,
            message=(
                f"{plant_key} {sn}: vendor fault {code_summary} in "
                f"{n_fault}/{total[key]} daylight samples [CRITICAL]"
            ),
        ))
    return breaches
