"""Tests for argia.core.alerts_state."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.core.alerts_state import (
    ALERTS_HEADER,
    AlertRecord,
    AlertsLedger,
    AlertState,
    load_alerts_ledger,
    make_alert_id,
    make_inverter_alert_key,
    make_plant_alert_key,
    mark_channels_sent,
    open_alert,
    record_to_row,
    resolve_alert,
    silence_alert,
    touch_alert,
)
from argia.core.time_utils import UTC


# ============================================================
# Helpers
# ============================================================


def _dt(year=2026, month=5, day=14, hour=12, minute=0, second=0):
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _open_record(now=None, **overrides):
    """Build a baseline OPEN AlertRecord."""
    now = now or _dt()
    base = open_alert(
        alert_id="ALT-20260514-001",
        alert_key="qro1:inv:sn1:inverter_offline",
        plant_key="QRO1",
        inverter_sn="SN1",
        metric="inverter_offline",
        severity="WARNING",
        now_utc=now,
        value=0.0,
        threshold=15.0,
        message="Inverter SN1 dark for 15 min",
    )
    if overrides:
        from dataclasses import replace
        return replace(base, **overrides)
    return base


# ============================================================
# Constants
# ============================================================


class TestHeader:
    def test_header_has_14_cols(self):
        assert len(ALERTS_HEADER) == 14

    def test_header_columns(self):
        # If you change the order, the migration matters — pin it.
        assert ALERTS_HEADER == [
            "alert_id", "alert_key", "plant_key", "inverter_sn",
            "metric", "severity", "state",
            "opened_utc", "last_seen_utc", "resolved_utc",
            "value", "threshold", "message", "channels_sent",
        ]


# ============================================================
# alert_key helpers
# ============================================================


class TestAlertKey:
    def test_inverter_key_format(self):
        key = make_inverter_alert_key("QRO1", "SN1", "inverter_offline")
        assert key == "qro1:inv:sn1:inverter_offline"

    def test_inverter_key_lowercased(self):
        key = make_inverter_alert_key("Qro1", "Sn1", "Inverter_Offline")
        assert key == "qro1:inv:sn1:inverter_offline"

    def test_plant_key_format(self):
        key = make_plant_alert_key("QRO1", "plant_offline")
        assert key == "qro1:plant:plant_offline"

    def test_inverter_and_plant_keys_differ(self):
        a = make_inverter_alert_key("QRO1", "SN1", "offline")
        b = make_plant_alert_key("QRO1", "offline")
        assert a != b


class TestAlertId:
    def test_format(self):
        assert make_alert_id(_dt(), 1) == "ALT-20260514-001"
        assert make_alert_id(_dt(), 42) == "ALT-20260514-042"

    def test_pads_to_three_digits(self):
        assert make_alert_id(_dt(), 5) == "ALT-20260514-005"

    def test_does_not_pad_beyond(self):
        assert make_alert_id(_dt(), 1000) == "ALT-20260514-1000"


# ============================================================
# open_alert
# ============================================================


class TestOpenAlert:
    def test_basic_fields_set(self):
        rec = open_alert(
            alert_id="ALT-X", alert_key="key1", plant_key="QRO1",
            inverter_sn="SN1", metric="inverter_offline", severity="WARNING",
            now_utc=_dt(), value=0.0, threshold=15.0, message="dark",
        )
        assert rec.state == AlertState.OPEN
        assert rec.alert_id == "ALT-X"
        assert rec.resolved_utc == ""
        assert rec.channels_sent == ""

    def test_severity_uppercased(self):
        rec = open_alert(
            alert_id="X", alert_key="k", plant_key="P", inverter_sn="",
            metric="pr_daily", severity="warning", now_utc=_dt(),
            value=0.6, threshold=0.75, message="msg",
        )
        assert rec.severity == "WARNING"

    def test_opened_equals_last_seen(self):
        rec = _open_record()
        assert rec.opened_utc == rec.last_seen_utc

    def test_iso_format_includes_timezone(self):
        rec = _open_record()
        assert rec.opened_utc.endswith("+00:00")


# ============================================================
# touch_alert
# ============================================================


class TestTouchAlert:
    def test_updates_last_seen(self):
        rec = _open_record(now=_dt(hour=10))
        later = _dt(hour=11)
        touched = touch_alert(rec, later)
        assert touched.last_seen_utc != rec.last_seen_utc
        assert touched.opened_utc == rec.opened_utc  # unchanged
        assert touched.state == AlertState.OPEN

    def test_does_not_change_alert_id(self):
        rec = _open_record()
        touched = touch_alert(rec, _dt(hour=14))
        assert touched.alert_id == rec.alert_id

    def test_optional_value_update(self):
        rec = _open_record()
        touched = touch_alert(rec, _dt(hour=14), value=42.0)
        assert touched.value == 42.0

    def test_value_unchanged_when_not_passed(self):
        rec = _open_record()
        touched = touch_alert(rec, _dt(hour=14))
        assert touched.value == rec.value

    def test_message_update(self):
        rec = _open_record()
        touched = touch_alert(rec, _dt(hour=14), message="now dark 60 min")
        assert touched.message == "now dark 60 min"


# ============================================================
# resolve_alert
# ============================================================


class TestResolveAlert:
    def test_transitions_open_to_resolved(self):
        rec = _open_record()
        resolved = resolve_alert(rec, _dt(hour=14))
        assert resolved.state == AlertState.RESOLVED
        assert resolved.resolved_utc != ""
        assert resolved.last_seen_utc == resolved.resolved_utc

    def test_already_resolved_unchanged(self):
        rec = _open_record()
        once = resolve_alert(rec, _dt(hour=14))
        twice = resolve_alert(once, _dt(hour=15))
        # Same record back; resolved_utc not bumped
        assert once == twice

    def test_silenced_can_be_resolved(self):
        """Silencing doesn't survive condition clearing."""
        rec = _open_record()
        silenced = silence_alert(rec)
        assert silenced.state == AlertState.SILENCED
        resolved = resolve_alert(silenced, _dt(hour=14))
        assert resolved.state == AlertState.RESOLVED

    def test_final_value_recorded(self):
        rec = _open_record()
        resolved = resolve_alert(rec, _dt(hour=14), final_value=100.0)
        assert resolved.value == 100.0

    def test_final_message_recorded(self):
        rec = _open_record()
        resolved = resolve_alert(rec, _dt(hour=14), final_message="recovered")
        assert resolved.message == "recovered"


# ============================================================
# silence_alert
# ============================================================


class TestSilenceAlert:
    def test_open_to_silenced(self):
        rec = _open_record()
        s = silence_alert(rec)
        assert s.state == AlertState.SILENCED

    def test_silenced_to_silenced(self):
        rec = _open_record()
        s = silence_alert(silence_alert(rec))
        assert s.state == AlertState.SILENCED

    def test_resolved_unchanged(self):
        rec = _open_record()
        resolved = resolve_alert(rec, _dt(hour=14))
        s = silence_alert(resolved)
        assert s.state == AlertState.RESOLVED


# ============================================================
# mark_channels_sent
# ============================================================


class TestMarkChannelsSent:
    def test_adds_to_empty(self):
        rec = _open_record()
        marked = mark_channels_sent(rec, ["email"])
        assert marked.channels_sent == "email"

    def test_adds_to_existing(self):
        rec = _open_record()
        once = mark_channels_sent(rec, ["sheet"])
        twice = mark_channels_sent(once, ["email"])
        # Sorted alphabetically
        assert twice.channels_sent == "email,sheet"

    def test_dedupes(self):
        rec = _open_record()
        once = mark_channels_sent(rec, ["email"])
        twice = mark_channels_sent(once, ["email"])
        assert twice.channels_sent == "email"

    def test_multiple_at_once(self):
        rec = _open_record()
        marked = mark_channels_sent(rec, ["email", "sheet", "slack"])
        assert marked.channels_sent == "email,sheet,slack"

    def test_empty_strings_ignored(self):
        rec = _open_record()
        marked = mark_channels_sent(rec, ["", "email", ""])
        assert marked.channels_sent == "email"


# ============================================================
# Ledger lookup
# ============================================================


class TestAlertsLedger:
    def _ledger(self, *records):
        return AlertsLedger(records=tuple(records))

    def test_empty_ledger(self):
        ledger = self._ledger()
        assert ledger.current_open("anything") is None
        assert ledger.history_for("anything") == []
        assert ledger.all_open() == []

    def test_finds_open_record(self):
        rec = _open_record()
        ledger = self._ledger(rec)
        result = ledger.current_open(rec.alert_key)
        assert result is not None
        assert result.alert_id == rec.alert_id

    def test_resolved_record_not_returned_by_current_open(self):
        rec = _open_record()
        resolved = resolve_alert(rec, _dt(hour=14))
        ledger = self._ledger(resolved)
        assert ledger.current_open(resolved.alert_key) is None

    def test_silenced_record_returned_as_active(self):
        """Silenced = still active from engine POV."""
        rec = _open_record()
        s = silence_alert(rec)
        ledger = self._ledger(s)
        assert ledger.current_open(s.alert_key) is not None

    def test_history_includes_all_states(self):
        rec1 = _open_record(now=_dt(hour=10))
        resolved1 = resolve_alert(rec1, _dt(hour=11))
        rec2 = _open_record(now=_dt(hour=12), alert_id="ALT-20260514-002")
        ledger = self._ledger(resolved1, rec2)
        history = ledger.history_for(rec1.alert_key)
        assert len(history) == 2
        # Sorted oldest-first
        assert history[0].opened_utc < history[1].opened_utc

    def test_multiple_opens_for_same_key_returns_newest(self):
        """Should not happen, but if it does, return newest and log."""
        from dataclasses import replace
        rec1 = _open_record(now=_dt(hour=10))
        rec2 = replace(rec1, alert_id="ALT-20260514-002",
                       opened_utc=rec1.opened_utc.replace("12:00", "14:00"))
        ledger = self._ledger(rec1, rec2)
        result = ledger.current_open(rec1.alert_key)
        # Whichever has later opened_utc string sorts later
        assert result.opened_utc >= rec1.opened_utc

    def test_all_open_excludes_resolved(self):
        rec1 = _open_record()
        rec2 = resolve_alert(_open_record(alert_id="X2"), _dt(hour=14))
        ledger = self._ledger(rec1, rec2)
        opens = ledger.all_open()
        assert len(opens) == 1
        assert opens[0].alert_id == rec1.alert_id


# ============================================================
# Serialization
# ============================================================


class TestRecordToRow:
    def test_row_has_14_cols(self):
        rec = _open_record()
        row = record_to_row(rec)
        assert len(row) == 14

    def test_columns_in_header_order(self):
        rec = _open_record(value=0.6, threshold=0.75)
        row = record_to_row(rec)
        assert row[ALERTS_HEADER.index("alert_id")] == "ALT-20260514-001"
        assert row[ALERTS_HEADER.index("plant_key")] == "QRO1"
        assert row[ALERTS_HEADER.index("state")] == "OPEN"
        assert row[ALERTS_HEADER.index("value")] == 0.6
        assert row[ALERTS_HEADER.index("threshold")] == 0.75

    def test_none_value_becomes_empty(self):
        from dataclasses import replace
        rec = replace(_open_record(), value=None, threshold=None)
        row = record_to_row(rec)
        assert row[ALERTS_HEADER.index("value")] == ""
        assert row[ALERTS_HEADER.index("threshold")] == ""

    def test_state_as_string(self):
        rec = _open_record()
        row = record_to_row(rec)
        # Must be the string, not the enum object
        assert row[ALERTS_HEADER.index("state")] == "OPEN"
        assert isinstance(row[ALERTS_HEADER.index("state")], str)


# ============================================================
# load_alerts_ledger
# ============================================================


def _ledger_row(**overrides):
    row = {
        "alert_id": "ALT-20260514-001",
        "alert_key": "qro1:inv:sn1:inverter_offline",
        "plant_key": "QRO1", "inverter_sn": "SN1",
        "metric": "inverter_offline", "severity": "WARNING",
        "state": "OPEN",
        "opened_utc": "2026-05-14T10:00:00+00:00",
        "last_seen_utc": "2026-05-14T10:00:00+00:00",
        "resolved_utc": "", "value": "0", "threshold": "15",
        "message": "dark", "channels_sent": "",
    }
    row.update(overrides)
    return row


class TestLoadLedger:
    def _mock_sheets(self, rows):
        sheets = MagicMock()
        sheets.read_table.return_value = rows
        return sheets

    def test_empty_tab_returns_empty_ledger(self):
        ledger = load_alerts_ledger(self._mock_sheets([]))
        assert len(ledger.records) == 0

    def test_single_open_row(self):
        ledger = load_alerts_ledger(self._mock_sheets([_ledger_row()]))
        assert len(ledger.records) == 1
        assert ledger.records[0].state == AlertState.OPEN

    def test_missing_alert_id_skipped(self):
        ledger = load_alerts_ledger(self._mock_sheets([_ledger_row(alert_id="")]))
        assert len(ledger.records) == 0

    def test_missing_alert_key_skipped(self):
        ledger = load_alerts_ledger(self._mock_sheets([_ledger_row(alert_key="")]))
        assert len(ledger.records) == 0

    def test_invalid_state_defaults_to_open(self):
        """An invalid state value should default to OPEN so the engine
        keeps tracking it (and ops can fix the value)."""
        ledger = load_alerts_ledger(self._mock_sheets([_ledger_row(state="GARBAGE")]))
        assert len(ledger.records) == 1
        assert ledger.records[0].state == AlertState.OPEN

    def test_resolved_state_parsed(self):
        ledger = load_alerts_ledger(self._mock_sheets(
            [_ledger_row(state="RESOLVED",
                         resolved_utc="2026-05-14T11:00:00+00:00")],
        ))
        assert ledger.records[0].state == AlertState.RESOLVED

    def test_silenced_state_parsed(self):
        ledger = load_alerts_ledger(self._mock_sheets(
            [_ledger_row(state="SILENCED")],
        ))
        assert ledger.records[0].state == AlertState.SILENCED

    def test_sheets_error_returns_empty(self):
        sheets = MagicMock()
        sheets.read_table.side_effect = Exception("tab missing")
        ledger = load_alerts_ledger(sheets)
        assert len(ledger.records) == 0


# ============================================================
# End-to-end transition lifecycle
# ============================================================


class TestTransitionLifecycle:
    """Walk a full open→touch→resolve cycle to make sure the pieces compose."""

    def test_full_lifecycle(self):
        # 10:00 — alert opens
        rec1 = open_alert(
            alert_id="ALT-20260514-001",
            alert_key="qro1:inv:sn1:offline",
            plant_key="QRO1", inverter_sn="SN1",
            metric="inverter_offline", severity="WARNING",
            now_utc=_dt(hour=10),
            value=0.0, threshold=15.0, message="dark 15 min",
        )
        # 11:00 — still dark, touch
        rec2 = touch_alert(rec1, _dt(hour=11),
                           message="dark 75 min")
        assert rec2.state == AlertState.OPEN
        assert rec2.opened_utc == rec1.opened_utc
        assert rec2.last_seen_utc != rec1.last_seen_utc
        # 11:00 — email sent
        rec3 = mark_channels_sent(rec2, ["email"])
        assert "email" in rec3.channels_sent
        # 12:00 — back online, resolve
        rec4 = resolve_alert(rec3, _dt(hour=12),
                             final_message="back online")
        assert rec4.state == AlertState.RESOLVED
        assert rec4.resolved_utc != ""
        assert rec4.message == "back online"
        # opened_utc preserved throughout
        assert rec4.opened_utc == rec1.opened_utc
