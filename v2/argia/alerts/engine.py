"""Alert engine — plan #5 (the missing middle of Stage 7.4).

Turns detector breaches into Alerts-tab rows via the existing state store.

Division of labour (deliberate, matches the scaffolding's design notes):
- detectors (analytics/*)      decide WHAT is breaching right now
- alerts_state (core)          knows HOW an alert record transitions
- THIS module                  decides WHEN: diffs today's breaches against
                               the ledger and applies open/touch/resolve
- scripts/alerts_daily.py      loads real data, runs it, persists rows

Debounce comes from the daily cadence itself: every input here is a
FULL-DAY aggregate (daily energy, daily specific yield, daily expected),
so the transient single-sample zeros that plagued snapshot power cannot
fire anything. A breach means the condition held at day granularity.

Data-quality gate: plant-level candidates are only built from days whose
``data_class`` is "full" — an undercounted partial day (June-30 case:
whole fleet at -35%) must not fire energy alerts. Inverter-relative is
exempt: peers share the same partial window, so the comparison stays fair.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from argia.core.alerts_state import (
    AlertRecord,
    AlertsLedger,
    make_alert_id,
    make_inverter_alert_key,
    make_plant_alert_key,
    open_alert,
    resolve_alert,
    touch_alert,
)

LOG = logging.getLogger("argia.alerts.engine")

# Metrics this engine owns. Only alerts with these metrics are auto-resolved
# when absent from today's candidates — anything else in the ledger (manual
# rows, future engines) is left strictly alone.
ENGINE_METRICS = frozenset({
    "inverter_relative",
    "energy_daily_pct",
    "plant_twin_yield",
})


@dataclass(frozen=True)
class Candidate:
    """One condition a detector says is TRUE today (engine-internal)."""

    alert_key: str
    plant_key: str
    inverter_sn: str     # "" for plant-level
    metric: str
    severity: str        # WARNING | CRITICAL
    value: Optional[float]
    threshold: Optional[float]
    message: str


def candidate_from_relative_breach(b) -> Candidate:
    """Map an inverter_health.RelativeBreach to a Candidate."""
    return Candidate(
        alert_key=make_inverter_alert_key(b.plant_key, b.inverter_sn,
                                          "inverter_relative"),
        plant_key=b.plant_key,
        inverter_sn=b.inverter_sn,
        metric="inverter_relative",
        severity=b.severity.value,
        value=round(b.ratio, 3),
        threshold=b.threshold,
        message=b.message,
    )


def candidate_from_twin_breach(b) -> Candidate:
    """Map a perf_indicators.TwinBreach to a Candidate."""
    return Candidate(
        alert_key=make_plant_alert_key(b.plant_key, "plant_twin_yield"),
        plant_key=b.plant_key,
        inverter_sn="",
        metric="plant_twin_yield",
        severity=b.severity.value,
        value=b.ratio,
        threshold=b.threshold,
        message=b.message,
    )


def candidate_from_expected_breach(b) -> Candidate:
    """Map a perf_indicators.ExpectedBreach to a Candidate."""
    return Candidate(
        alert_key=make_plant_alert_key(b.plant_key, "energy_daily_pct"),
        plant_key=b.plant_key,
        inverter_sn="",
        metric="energy_daily_pct",
        severity=b.severity.value,
        value=b.ratio,
        threshold=b.threshold,
        message=b.message,
    )


@dataclass(frozen=True)
class ReconcileResult:
    """What one engine run decided."""

    records: List[AlertRecord]   # full ledger, post-transition, sheet order
    opened: List[AlertRecord]
    touched: List[AlertRecord]
    resolved: List[AlertRecord]

    def summary(self) -> str:
        return (f"opened={len(self.opened)} touched={len(self.touched)} "
                f"resolved={len(self.resolved)} total_rows={len(self.records)}")


def reconcile_alerts(
    ledger: AlertsLedger,
    candidates: List[Candidate],
    now_utc: dt.datetime,
) -> ReconcileResult:
    """Diff today's candidates against the ledger; apply transitions.

    Rules (pure, no I/O):
    - candidate with no OPEN/SILENCED record for its alert_key -> OPEN new row
    - candidate whose alert_key already has an OPEN/SILENCED record -> touch it
      (last_seen/value/message refresh; severity updated if escalated)
    - OPEN/SILENCED record for an ENGINE metric whose key is NOT in today's
      candidates -> RESOLVE it
    - records with other metrics, or already RESOLVED -> untouched
    - duplicate candidates for one key: keep the worst (CRITICAL > WARNING)

    Ledger order is preserved; new rows append at the end — the sheet is an
    append-plus-in-place-update history, rows never move or vanish.
    """
    # Worst-severity dedupe of candidates by key.
    by_key: Dict[str, Candidate] = {}
    for c in candidates:
        prev = by_key.get(c.alert_key)
        if prev is None or (c.severity == "CRITICAL" and prev.severity != "CRITICAL"):
            by_key[c.alert_key] = c

    records = list(ledger.records)
    opened: List[AlertRecord] = []
    touched: List[AlertRecord] = []
    resolved: List[AlertRecord] = []
    seen_keys: set = set()

    for i, rec in enumerate(records):
        if not (rec.is_open() or rec.is_silenced()):
            continue
        cand = by_key.get(rec.alert_key)
        if cand is not None:
            seen_keys.add(rec.alert_key)
            new = touch_alert(rec, now_utc, value=cand.value,
                              message=cand.message)
            if cand.severity != new.severity:
                # escalation/de-escalation: reflect current severity in place
                from dataclasses import replace as _replace
                new = _replace(new, severity=cand.severity,
                               threshold=cand.threshold)
            records[i] = new
            touched.append(new)
        elif rec.metric in ENGINE_METRICS:
            new = resolve_alert(rec, now_utc)
            records[i] = new
            resolved.append(new)
        # else: not ours — leave strictly alone

    # Brand-new conditions.
    seq = len(records) + 1
    for key, cand in by_key.items():
        if key in seen_keys:
            continue
        rec = open_alert(
            alert_id=make_alert_id(now_utc, seq),
            alert_key=cand.alert_key,
            plant_key=cand.plant_key,
            inverter_sn=cand.inverter_sn,
            metric=cand.metric,
            severity=cand.severity,
            now_utc=now_utc,
            value=cand.value,
            threshold=cand.threshold,
            message=cand.message,
        )
        seq += 1
        records.append(rec)
        opened.append(rec)

    return ReconcileResult(records=records, opened=opened,
                           touched=touched, resolved=resolved)
