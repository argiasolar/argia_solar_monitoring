"""Maintenance_Events loader + O&M rollup tests.

Fail-closed is the property under test: a draft (no approved_by) is inert
for both deemed energy and cost. Timestamps must survive the two shapes
Sheets hands back (a serial float from UNFORMATTED reads, and an ISO
string), and malformed rows must be dropped, not crash the loader.
"""

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.core.cells import GOOGLE_EPOCH
from argia.core.sheets import SheetsClient
from argia.core.time_utils import MX_TZ
from argia.finance.income import Period
from argia.maintenance.events import (
    MAINTENANCE_EVENTS_HEADER, load_maintenance_events, om_cost_from_events,
)


def _serial(y, m, d, h=0, mi=0):
    """A Google Sheets datetime serial (days since 1899-12-30)."""
    delta = dt.datetime(y, m, d, h, mi) - GOOGLE_EPOCH
    return delta.total_seconds() / 86400.0


def _sheets(rows):
    """Mock whose read_table serves ``rows`` (list of dicts) for the
    Maintenance_Events tab and raises for anything else."""
    def read_table(tab, a1="A1:Z"):
        if tab == "Maintenance_Events":
            return rows
        raise RuntimeError("no such tab: " + tab)
    s = MagicMock(spec=SheetsClient)
    s.read_table.side_effect = read_table
    return s


def _row(plant_key="SLP1", start_ts="2026-07-05 09:00:00", end_ts="",
         category="customer", cost_type="repair", cost_mxn="",
         note="", approved_by="tomasz"):
    return {"plant_key": plant_key, "start_ts": start_ts, "end_ts": end_ts,
            "category": category, "cost_type": cost_type,
            "cost_mxn": cost_mxn, "note": note, "approved_by": approved_by}


class TestLoader:
    def test_missing_tab_returns_empty(self):
        s = MagicMock(spec=SheetsClient)
        s.read_table.side_effect = RuntimeError("no tab")
        assert load_maintenance_events(s) == []

    def test_parses_iso_string_timestamps(self):
        ev = load_maintenance_events(_sheets([_row(
            start_ts="2026-07-05 09:00:00", end_ts="2026-07-05 17:00:00")]))
        assert len(ev) == 1
        e = ev[0]
        assert e.start_ts == dt.datetime(2026, 7, 5, 9, tzinfo=MX_TZ)
        assert e.end_ts == dt.datetime(2026, 7, 5, 17, tzinfo=MX_TZ)
        assert e.plant_key == "SLP1"

    def test_parses_serial_timestamps(self):
        ev = load_maintenance_events(_sheets([_row(
            start_ts=_serial(2026, 7, 5, 9),
            end_ts=_serial(2026, 7, 5, 17))]))
        e = ev[0]
        assert e.start_ts == dt.datetime(2026, 7, 5, 9, tzinfo=MX_TZ)
        assert e.end_ts == dt.datetime(2026, 7, 5, 17, tzinfo=MX_TZ)

    def test_blank_end_is_ongoing(self):
        e = load_maintenance_events(_sheets([_row(end_ts="")]))[0]
        assert e.end_ts is None
        assert e.is_ongoing

    def test_approved_gate(self):
        approved = load_maintenance_events(_sheets([_row(approved_by="t")]))[0]
        draft = load_maintenance_events(_sheets([_row(approved_by="")]))[0]
        assert approved.approved is True
        assert draft.approved is False

    def test_category_validation(self):
        for cat, billable in [("customer", True), ("argia", False),
                              ("force_majeure", False)]:
            e = load_maintenance_events(_sheets([_row(category=cat)]))[0]
            assert e.category == cat
            assert e.is_billable_category is billable

    def test_unknown_category_kept_but_not_billable(self):
        e = load_maintenance_events(
            _sheets([_row(category="grid_operator")]))[0]
        assert e.category == "grid_operator"
        assert e.is_billable_category is False

    def test_unknown_cost_type_becomes_other(self):
        e = load_maintenance_events(
            _sheets([_row(cost_type="landscaping")]))[0]
        assert e.cost_type == "other"

    def test_cost_parsed_comma_tolerant(self):
        e = load_maintenance_events(
            _sheets([_row(cost_mxn="20,000")]))[0]
        assert e.cost_mxn == pytest.approx(20000.0)

    def test_blank_plant_key_skipped(self):
        assert load_maintenance_events(_sheets([_row(plant_key="")])) == []

    def test_unparseable_start_skipped(self):
        assert load_maintenance_events(
            _sheets([_row(start_ts="not a date")])) == []

    def test_end_before_start_skipped(self):
        assert load_maintenance_events(_sheets([_row(
            start_ts="2026-07-05 17:00:00",
            end_ts="2026-07-05 09:00:00")])) == []

    def test_plant_key_uppercased(self):
        e = load_maintenance_events(_sheets([_row(plant_key="slp1")]))[0]
        assert e.plant_key == "SLP1"

    def test_header_matches_code(self):
        # the tab the loader reads and the header the setup script writes
        # must be the same 8 columns
        assert MAINTENANCE_EVENTS_HEADER == [
            "plant_key", "start_ts", "end_ts", "category", "cost_type",
            "cost_mxn", "note", "approved_by"]


class TestOmCostFromEvents:
    JULY = Period.from_iso("2026-07-01", "2026-07-31")

    def test_sums_approved_costs_in_period(self):
        events = load_maintenance_events(_sheets([
            _row(plant_key="GTO1", start_ts="2026-07-03 09:00:00",
                 cost_mxn="20000", approved_by="t"),
            _row(plant_key="GTO1", start_ts="2026-07-20 09:00:00",
                 cost_mxn="5000", approved_by="t"),
        ]))
        assert om_cost_from_events(events, "GTO1", self.JULY) == \
            pytest.approx(25000.0)

    def test_draft_excluded(self):
        events = load_maintenance_events(_sheets([
            _row(plant_key="GTO1", start_ts="2026-07-03 09:00:00",
                 cost_mxn="20000", approved_by=""),   # draft
        ]))
        assert om_cost_from_events(events, "GTO1", self.JULY) == 0.0

    def test_out_of_period_excluded(self):
        events = load_maintenance_events(_sheets([
            _row(plant_key="GTO1", start_ts="2026-08-03 09:00:00",
                 cost_mxn="20000", approved_by="t"),
        ]))
        assert om_cost_from_events(events, "GTO1", self.JULY) == 0.0

    def test_blank_cost_contributes_nothing(self):
        events = load_maintenance_events(_sheets([
            _row(plant_key="GTO1", start_ts="2026-07-03 09:00:00",
                 cost_mxn="", approved_by="t"),
        ]))
        assert om_cost_from_events(events, "GTO1", self.JULY) == 0.0

    def test_every_category_counts_toward_cost(self):
        # argia/force_majeure are not BILLABLE, but their spend is real
        events = load_maintenance_events(_sheets([
            _row(plant_key="GTO1", start_ts="2026-07-03 09:00:00",
                 category="argia", cost_mxn="12000", approved_by="t"),
        ]))
        assert om_cost_from_events(events, "GTO1", self.JULY) == \
            pytest.approx(12000.0)

    def test_scoped_to_plant(self):
        events = load_maintenance_events(_sheets([
            _row(plant_key="GTO1", start_ts="2026-07-03 09:00:00",
                 cost_mxn="20000", approved_by="t"),
        ]))
        assert om_cost_from_events(events, "SLP1", self.JULY) == 0.0
