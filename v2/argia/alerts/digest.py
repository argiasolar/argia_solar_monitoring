"""Daily open-alerts digest.

WHY (design gap, 2026-07-06): every alert e-mails exactly ONCE (the
Alert_Notifications dedupe — the right call against flapping). But that
means ongoing issues go silent: three GTO1 inverters sat in FAULT for
days while the inbox stayed quiet, and a quiet inbox read as "all good".
The digest restores the invariant *silence means all clear*: each
morning, if anything is still OPEN, the daily tier appends ONE fresh
digest alert summarizing it. The notifier mails it like any other new
OPEN row (no Apps Script changes); the next morning's run resolves it
and opens the next one — or stays silent once the list is empty.

Everything here is pure (no I/O) so it is fully unit-testable; the
alerts_daily script owns persistence.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from argia.alerts.engine import make_alert_id
from argia.core.alerts_state import AlertRecord, open_alert, resolve_alert

DIGEST_METRIC = "daily_digest"
DIGEST_KEY = "portfolio:daily_digest"


def reportable_alerts(records: List[AlertRecord]) -> List[AlertRecord]:
    """Active alerts as surfaces (report, dashboards) should see them:
    OPEN/SILENCED, minus digest rows — the digest is a mail vehicle, not
    an issue, and must never inflate critical/warning counts."""
    return [r for r in records
            if (r.is_open() or r.is_silenced()) and r.metric != DIGEST_METRIC]


def _age_days(rec: AlertRecord, now_utc: dt.datetime) -> int:
    try:
        opened = dt.datetime.fromisoformat(rec.opened_utc)
    except (TypeError, ValueError):
        return 0
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=dt.timezone.utc)
    # Calendar days, not floored 24h blocks: a fault open since the 5th
    # is "2d" on the 7th — timedelta.days would understate it as 1.
    return max(0, (now_utc.date() - opened.date()).days)


def summarize_open_alerts(
    records: List[AlertRecord], now_utc: dt.datetime,
) -> Optional[Tuple[str, str, str]]:
    """(severity, message, explanation) for the digest, or None when
    nothing is open — None means the morning stays silent, by design."""
    open_recs = reportable_alerts(records)
    open_recs = [r for r in open_recs if r.is_open()]
    if not open_recs:
        return None
    n_crit = sum(1 for r in open_recs if r.severity == "CRITICAL")
    n_warn = len(open_recs) - n_crit
    severity = "CRITICAL" if n_crit else "WARNING"
    message = (f"Daily digest \u2014 still open: "
               f"{n_crit} critical / {n_warn} warning")

    groups: Dict[Tuple[str, str], List[AlertRecord]] = {}
    for r in open_recs:
        groups.setdefault((r.plant_key, r.metric), []).append(r)
    parts = []
    for (plant, metric), recs in sorted(groups.items()):
        age = max(_age_days(r, now_utc) for r in recs)
        label = metric.replace("_", " ")
        count = f" \u00d7{len(recs)}" if len(recs) > 1 else ""
        parts.append(f"{plant}: {label}{count} ({age}d)")
    explanation = (" \u00b7 ".join(parts)
                   + " \u2014 this reminder repeats each morning until "
                     "the list is clear.")
    return severity, message, explanation


@dataclass
class DigestResult:
    changed: bool = False
    resolved_ids: List[str] = field(default_factory=list)
    opened: Optional[AlertRecord] = None

    def log_lines(self) -> List[str]:
        out = [f"DIGEST resolve {i}" for i in self.resolved_ids]
        if self.opened:
            out.append(f"DIGEST open    {self.opened.alert_id}  "
                       f"{self.opened.message}")
        return out


def apply_daily_digest(records: List[AlertRecord],
                       now_utc: dt.datetime) -> DigestResult:
    """Mutates `records` in place: resolve yesterday's digest row(s)
    (reconcile leaves non-engine metrics strictly alone, so this module
    owns their lifecycle), then append today's digest if anything real
    is still open."""
    res = DigestResult()
    for i, rec in enumerate(records):
        if rec.metric == DIGEST_METRIC and rec.is_open():
            records[i] = resolve_alert(rec, now_utc)
            res.resolved_ids.append(rec.alert_id)
            res.changed = True

    summary = summarize_open_alerts(records, now_utc)
    if summary is None:
        return res
    severity, message, explanation = summary
    rec = open_alert(
        alert_id=make_alert_id(now_utc, len(records) + 1),
        alert_key=DIGEST_KEY,
        plant_key="PORTFOLIO",
        inverter_sn="",
        metric=DIGEST_METRIC,
        severity=severity,
        now_utc=now_utc,
        value=None,
        threshold=None,
        message=message,
        explanation=explanation,
    )
    records.append(rec)
    res.opened = rec
    res.changed = True
    return res
