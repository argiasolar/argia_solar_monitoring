"""
Alerts state machine — Stage 7.1.

Manages the ``Alerts`` tab as a persistent state store for active alerts.

State machine
=============

A unique alert is identified by (alert_key, severity), where alert_key is
a deterministic string built from the plant/inverter/metric being checked.

States:
    OPEN     — condition currently true; we've notified
    RESOLVED — condition has cleared
    SILENCED — open but suppressed by ops (manual)

Lifecycle:
    new condition → OPEN row added, notification fires once
    still true on next run → OPEN row's last_seen_utc updates, NO notification
    condition clears → row transitions to RESOLVED, resolution notification fires
    same condition trips again later → NEW row added in OPEN state, notification

Important: this module ONLY provides the state store + transition primitives.
It does NOT decide WHEN to open or resolve alerts — that's the alert
engine in Stage 7.4. The separation is deliberate: you can populate
fixtures and unit-test transitions without involving any real telemetry.

Sheet schema (Alerts tab):
    alert_id    -- monotonic ID, e.g. "ALT-20260514-001"
    alert_key   -- deterministic, e.g. "QRO1:inv:7E0571B7-AB:offline"
    plant_key
    inverter_sn -- "" for plant-level alerts
    metric      -- e.g. "inverter_offline"
    severity    -- INFO|WARNING|CRITICAL
    state       -- OPEN|RESOLVED|SILENCED
    opened_utc  -- ISO timestamp when alert first became OPEN
    last_seen_utc -- ISO timestamp of most recent check that confirmed condition
    resolved_utc  -- ISO timestamp when transitioned to RESOLVED ("" if still open)
    value         -- observed metric value at most recent check
    threshold     -- threshold value that was breached
    message       -- human-readable, e.g. "Inverter 7E0571B7-AB dark for 47 min"
    channels_sent -- comma-separated channels we've already notified on
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Dict, List, Optional, Tuple

from argia.core.normalize import normalize_text, safe_float
from argia.core.sheets import SheetsClient
from argia.core.time_utils import UTC

LOG = logging.getLogger("argia.core.alerts_state")


# ---------- enums ----------


class AlertState(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    SILENCED = "SILENCED"


# ---------- header ----------

ALERTS_HEADER = [
    "alert_id", "alert_key", "plant_key", "inverter_sn",
    "metric", "severity", "state",
    "opened_utc", "last_seen_utc", "resolved_utc",
    "value", "threshold", "message", "channels_sent",
    "explanation",
]


# ---------- data structure ----------


@dataclass(frozen=True)
class AlertRecord:
    """One row in the Alerts tab. All timestamps are UTC ISO strings."""

    alert_id: str
    alert_key: str
    plant_key: str
    inverter_sn: str
    metric: str
    severity: str           # kept as str for sheet-roundtrip simplicity
    state: AlertState
    opened_utc: str
    last_seen_utc: str
    resolved_utc: str       # "" while OPEN
    value: Optional[float]
    threshold: Optional[float]
    message: str
    channels_sent: str      # comma-separated, e.g. "sheet,email"
    explanation: str = ""   # plain-language meaning + what to check

    def is_open(self) -> bool:
        return self.state == AlertState.OPEN

    def is_resolved(self) -> bool:
        return self.state == AlertState.RESOLVED

    def is_silenced(self) -> bool:
        return self.state == AlertState.SILENCED


@dataclass(frozen=True)
class AlertsLedger:
    """All alerts loaded from the sheet, indexed for fast lookup.

    Constructed by ``load_alerts_ledger()``.
    """

    records: Tuple[AlertRecord, ...] = ()

    # Index: alert_key → list of records sorted by opened_utc (oldest first).
    # Multiple records can share an alert_key — one current OPEN/SILENCED
    # plus any number of historical RESOLVED ones from past trips.
    _by_key: Dict[str, List[AlertRecord]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        idx: Dict[str, List[AlertRecord]] = {}
        for r in self.records:
            idx.setdefault(r.alert_key, []).append(r)
        for k in idx:
            idx[k].sort(key=lambda r: r.opened_utc)
        object.__setattr__(self, "_by_key", idx)

    def current_open(self, alert_key: str) -> Optional[AlertRecord]:
        """The currently-active (OPEN or SILENCED) record for this alert_key,
        or None if no active alert. There should be at most one — if there
        are multiple OPENs for the same key, that's a bug we log and return
        the most recent.

        Note: SILENCED is treated as "still open from the engine's POV" — the
        engine should not re-fire while silenced, but should still mark the
        condition as ongoing."""
        records = self._by_key.get(alert_key, [])
        actives = [r for r in records if r.state in (AlertState.OPEN, AlertState.SILENCED)]
        if not actives:
            return None
        if len(actives) > 1:
            LOG.warning(
                "Multiple active records for alert_key '%s' — using newest. "
                "This indicates a state-store bug. Stale OPENs should be "
                "manually moved to RESOLVED.",
                alert_key,
            )
        # Most recently opened wins
        return max(actives, key=lambda r: r.opened_utc)

    def history_for(self, alert_key: str) -> List[AlertRecord]:
        """All records for an alert_key (including resolved), oldest first."""
        return list(self._by_key.get(alert_key, []))

    def all_open(self) -> List[AlertRecord]:
        """Every currently-OPEN record across all keys."""
        return [r for r in self.records if r.state == AlertState.OPEN]


# ---------- alert_key helpers ----------


def make_inverter_alert_key(
    plant_key: str, inverter_sn: str, metric: str,
) -> str:
    """Deterministic alert_key for inverter-level alerts. Lowercased so
    sheet-level case mismatches don't cause duplicate alerts."""
    return f"{plant_key}:inv:{inverter_sn}:{metric}".lower()


def make_plant_alert_key(plant_key: str, metric: str) -> str:
    return f"{plant_key}:plant:{metric}".lower()


# ---------- ID generation ----------


def make_alert_id(now_utc: dt.datetime, sequence: int) -> str:
    """``ALT-YYYYMMDD-NNN``. Used when creating new OPEN records.

    The caller is responsible for choosing the sequence number — typically
    by counting existing alerts in the ledger and adding 1, or by reading
    a counter cell. Stage 7.4 will use len(ledger.records)+1, padded."""
    return f"ALT-{now_utc.strftime('%Y%m%d')}-{sequence:03d}"


# ---------- loading ----------


def _parse_state(raw) -> AlertState:
    s = normalize_text(raw).upper()
    try:
        return AlertState(s)
    except ValueError:
        # Default to OPEN if state is missing or garbage — but log loudly.
        # The cron run will then see this as still-active.
        LOG.warning("Alerts row had invalid state '%s'; defaulting to OPEN", raw)
        return AlertState.OPEN


def load_alerts_ledger(sheets: SheetsClient) -> AlertsLedger:
    """Read the Alerts tab. Returns an empty ledger if the tab doesn't
    exist or has no rows — that's the first-run state and not an error."""
    try:
        rows = sheets.read_table("Alerts", "A1:O")
    except Exception as e:
        LOG.warning(
            "Could not read Alerts tab (%s). Returning empty ledger.", e,
        )
        return AlertsLedger(records=())

    records: List[AlertRecord] = []
    for i, row in enumerate(rows, start=2):
        alert_id = normalize_text(row.get("alert_id"))
        alert_key = normalize_text(row.get("alert_key"))
        if not alert_id or not alert_key:
            continue

        records.append(AlertRecord(
            alert_id=alert_id,
            alert_key=alert_key,
            plant_key=normalize_text(row.get("plant_key")),
            inverter_sn=normalize_text(row.get("inverter_sn")),
            metric=normalize_text(row.get("metric")),
            severity=normalize_text(row.get("severity")).upper(),
            state=_parse_state(row.get("state")),
            opened_utc=normalize_text(row.get("opened_utc")),
            last_seen_utc=normalize_text(row.get("last_seen_utc")),
            resolved_utc=normalize_text(row.get("resolved_utc")),
            value=safe_float(row.get("value")),
            threshold=safe_float(row.get("threshold")),
            message=normalize_text(row.get("message")),
            channels_sent=normalize_text(row.get("channels_sent")),
            explanation=normalize_text(row.get("explanation")),
        ))

    LOG.info(
        "Loaded alerts ledger: %d total records, %d currently OPEN",
        len(records),
        sum(1 for r in records if r.state == AlertState.OPEN),
    )
    return AlertsLedger(records=tuple(records))


# ---------- pure state-transition helpers ----------
#
# These return NEW AlertRecord values; they don't touch the sheet. Stage 7.4
# will call these and then batch-write the resulting rows. Keeping them
# pure functions makes them trivial to unit test.


def open_alert(
    alert_id: str,
    alert_key: str,
    plant_key: str,
    inverter_sn: str,
    metric: str,
    severity: str,
    now_utc: dt.datetime,
    value: Optional[float],
    threshold: Optional[float],
    message: str,
    explanation: str = "",
) -> AlertRecord:
    """Build a brand-new OPEN alert record."""
    ts = _iso(now_utc)
    return AlertRecord(
        alert_id=alert_id,
        alert_key=alert_key,
        plant_key=plant_key,
        inverter_sn=inverter_sn,
        metric=metric,
        severity=severity.upper(),
        state=AlertState.OPEN,
        opened_utc=ts,
        last_seen_utc=ts,
        resolved_utc="",
        value=value,
        threshold=threshold,
        message=message,
        channels_sent="",
        explanation=explanation,
    )


def touch_alert(
    record: AlertRecord,
    now_utc: dt.datetime,
    value: Optional[float] = None,
    message: Optional[str] = None,
) -> AlertRecord:
    """Refresh last_seen_utc on a still-active alert. Optionally update
    the latest observed value or message (e.g. duration ticking up).
    No state change."""
    return replace(
        record,
        last_seen_utc=_iso(now_utc),
        value=value if value is not None else record.value,
        message=message if message is not None else record.message,
    )


def resolve_alert(
    record: AlertRecord,
    now_utc: dt.datetime,
    final_value: Optional[float] = None,
    final_message: Optional[str] = None,
) -> AlertRecord:
    """Transition an OPEN alert to RESOLVED.

    If already RESOLVED, returns the record unchanged (idempotent). If
    SILENCED, also transitions to RESOLVED — silencing doesn't survive
    the condition clearing.
    """
    if record.state == AlertState.RESOLVED:
        return record
    return replace(
        record,
        state=AlertState.RESOLVED,
        resolved_utc=_iso(now_utc),
        last_seen_utc=_iso(now_utc),
        value=final_value if final_value is not None else record.value,
        message=final_message if final_message is not None else record.message,
    )


def silence_alert(record: AlertRecord) -> AlertRecord:
    """Move OPEN → SILENCED. RESOLVED records are not silenced — returning
    unchanged would be confusing, so we log+return-unchanged but the caller
    should generally not call this on a resolved record."""
    if record.state == AlertState.RESOLVED:
        LOG.warning(
            "silence_alert called on RESOLVED record %s — returning unchanged",
            record.alert_id,
        )
        return record
    return replace(record, state=AlertState.SILENCED)


def mark_channels_sent(
    record: AlertRecord, channels: List[str],
) -> AlertRecord:
    """Append channels to the channels_sent string (deduped, sorted)."""
    existing = {c.strip() for c in record.channels_sent.split(",") if c.strip()}
    existing.update(c.strip() for c in channels if c.strip())
    return replace(record, channels_sent=",".join(sorted(existing)))


# ---------- serialization ----------


def record_to_row(record: AlertRecord) -> List:
    """Serialize an AlertRecord to a Sheets row (cells in ALERTS_HEADER order)."""
    return [
        record.alert_id,
        record.alert_key,
        record.plant_key,
        record.inverter_sn,
        record.metric,
        record.severity,
        record.state.value,
        record.opened_utc,
        record.last_seen_utc,
        record.resolved_utc,
        "" if record.value is None else record.value,
        "" if record.threshold is None else record.threshold,
        record.message,
        record.channels_sent,
        record.explanation,
    ]


# ---------- helpers ----------


def _iso(d: dt.datetime) -> str:
    """Format datetime as UTC ISO 8601 with seconds precision."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).replace(microsecond=0).isoformat()


def create_alerts_tab_if_missing(sheets: SheetsClient) -> bool:
    """Create the Alerts tab with header only (no default rows).

    Returns True if it created the tab, False if already present."""
    sheets.ensure_tab("Alerts")
    existing = sheets.read_range("Alerts", "A1:O1")
    hdr = [str(c).strip() for c in (existing[0] if existing else [])]
    if hdr and any(hdr):
        if len([h for h in hdr if h]) < len(ALERTS_HEADER):
            # schema grew (e.g. the 'explanation' column) — extend the
            # header in place; existing rows keep working, new writes fill
            # the new column(s).
            sheets.write_header_row("Alerts", ALERTS_HEADER)
            LOG.info("Alerts header extended to %d columns",
                     len(ALERTS_HEADER))
            return False
        LOG.info("Alerts tab already has a header — leaving alone")
        return False
    sheets.ensure_header("Alerts", ALERTS_HEADER)
    LOG.info("Bootstrapped Alerts tab (header only)")
    return True
