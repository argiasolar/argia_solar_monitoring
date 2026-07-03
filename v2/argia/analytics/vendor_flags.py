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


# ---------- string flags: new-bit change detection ----------
#
# str_break / str_unmatch are BITMASKS and, verified on real data
# (2026-07-03), they are dominated by CHRONIC artifacts: healthy inverters
# carry the same bits every day since records began (GTO1 JFM7DXN00T
# break:15 daily; NL1 JGMAE65009 break:13 daily) while producing within 1%
# of peers. Alerting on "non-zero" would create 8 permanent false alarms.
# What IS signal: a NEW bit appearing vs the inverter's own trailing
# baseline — the currently-faulting GTO1 JFM7DXN013 grew unmatch:10,11 on
# 2026-06-01 and break:4 on 2026-06-02, weeks before its FT=302 fault.

STRING_BASELINE_DAYS = 14
"""Trailing window that defines an inverter's chronic-bit baseline."""

MIN_BIT_SAMPLES = 2
"""A new bit must appear in at least this many daylight samples to fire."""

_STRING_COLS = ("str_break", "str_unmatch", "str_unblance")


def _bits(value) -> frozenset:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return frozenset()
    if v <= 0:
        return frozenset()
    return frozenset(i for i in range(32) if v >> i & 1)


@dataclass(frozen=True)
class StringBitBreach:
    """An inverter reporting string-flag bits it never reported before."""

    plant_key: str
    inverter_sn: str
    new_bits: str          # e.g. "unmatch:10, unmatch:11"
    severity: Severity
    message: str


def evaluate_string_new_bits(
    day_samples: List[Tuple[dt.datetime, str, str, Dict[str, object]]],
    baseline_samples: List[Tuple[dt.datetime, str, str, Dict[str, object]]],
    min_samples: int = MIN_BIT_SAMPLES,
) -> List[StringBitBreach]:
    """Flag string-diagnostic bits present today but absent from the
    inverter's own trailing baseline.

    Samples are ``(timestamp_utc, plant_key, inverter_sn, cols)`` where
    ``cols`` maps str_break/str_unmatch/str_unblance to their raw values.
    Day samples are daylight-filtered; the baseline is NOT (a chronic bit is
    chronic whenever it appears). A new bit needs ``min_samples`` daylight
    occurrences today so a single glitchy read cannot fire. Severity is
    WARNING: it names a lead to inspect; actual production loss is the
    production detectors' job.

    Pure function — no I/O.
    """
    # bit -> count for today (daylight only)
    day_counts: Dict[Tuple[str, str], Counter] = defaultdict(Counter)
    for ts, plant_key, sn, cols in day_samples:
        if ts is None:
            continue
        mx = utc_to_mx(ts)
        if not (CLOUD_DAYLIGHT_START_HOUR <= mx.hour < CLOUD_DAYLIGHT_END_HOUR):
            continue
        key = (str(plant_key).strip(), str(sn).strip())
        for col in _STRING_COLS:
            for b in _bits(cols.get(col)):
                day_counts[key][f"{col[4:]}:{b}"] += 1

    baseline: Dict[Tuple[str, str], set] = defaultdict(set)
    for _ts, plant_key, sn, cols in baseline_samples:
        key = (str(plant_key).strip(), str(sn).strip())
        for col in _STRING_COLS:
            baseline[key] |= {f"{col[4:]}:{b}" for b in _bits(cols.get(col))}

    breaches: List[StringBitBreach] = []
    for key, counts in sorted(day_counts.items()):
        new = sorted(b for b, n in counts.items()
                     if n >= min_samples and b not in baseline[key])
        if not new:
            continue
        plant_key, sn = key
        bits_txt = ", ".join(new)
        breaches.append(StringBitBreach(
            plant_key=plant_key,
            inverter_sn=sn,
            new_bits=bits_txt,
            severity=Severity.WARNING,
            message=(
                f"{plant_key} {sn}: NEW string-diagnostic bit(s) [{bits_txt}] "
                f"not seen in prior {STRING_BASELINE_DAYS} days [WARNING]"
            ),
        ))
    return breaches
