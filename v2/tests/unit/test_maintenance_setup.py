"""Maintenance_Events setup-script tests.

Dry-run must not touch the sheet; apply must create the tab, write the
header, and attach the provenance notes; and the setup must refuse to
run if any column lacks a provenance note (the same drift guard the
finance completeness test enforces, checked here at write time).
"""

from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from scripts.maintenance_events_setup import run_setup


def _sheets():
    s = MagicMock(spec=SheetsClient)
    s.set_header_notes.return_value = 8
    return s


def test_dry_run_writes_nothing():
    s = _sheets()
    summary = run_setup(s, apply=False)
    assert summary["applied"] == 0
    s.ensure_tab.assert_not_called()
    s.ensure_header.assert_not_called()
    s.set_header_notes.assert_not_called()
    s.freeze_and_bold_header.assert_not_called()


def test_apply_creates_tab_header_and_notes():
    s = _sheets()
    summary = run_setup(s, apply=True)
    assert summary["applied"] == 1
    s.ensure_tab.assert_called_once()
    s.ensure_header.assert_called_once()
    s.set_header_notes.assert_called_once()
    s.freeze_and_bold_header.assert_called_once()
    # header passed to ensure_header is the code's 8-column header
    _tab, header = s.ensure_header.call_args[0]
    assert header[0] == "plant_key"
    assert "approved_by" in header


def test_refuses_when_a_column_is_undocumented(monkeypatch):
    # simulate a column added to the header but not to provenance
    import scripts.maintenance_events_setup as mod
    monkeypatch.setattr(
        mod, "MAINTENANCE_EVENTS_HEADER",
        list(mod.MAINTENANCE_EVENTS_HEADER) + ["undocumented_col"])
    with pytest.raises(ValueError):
        run_setup(_sheets(), apply=True)
