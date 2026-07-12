"""Maintenance-window helpers (v92) — the operational read of events.

These drive alert suppression and the daily-report badge, so the tests
pin: date coverage across a multi-day and ongoing window, that the read
is approval-INDEPENDENT (a draft still means "operator knows"), and the
badge wording per category.
"""

import datetime as dt

from argia.core.time_utils import MX_TZ
from argia.maintenance.events import (
    MaintenanceEvent, maintenance_badge_text, plant_maintenance_on_date,
)


def _mx(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, 0, tzinfo=MX_TZ)


def _event(pk="GTO1", start=None, end=None, category="argia",
           approved="tomasz", note=""):
    return MaintenanceEvent(
        plant_key=pk, start_ts=start or _mx(2026, 7, 14, 6),
        end_ts=end, category=category, cost_type="repair",
        cost_mxn=None, note=note, approved_by=approved)


class TestCoversDate:
    def test_covers_each_day_in_window(self):
        e = _event(start=_mx(2026, 7, 14, 6), end=_mx(2026, 7, 15, 20))
        assert e.covers_date("2026-07-14")
        assert e.covers_date("2026-07-15")

    def test_excludes_days_outside_window(self):
        e = _event(start=_mx(2026, 7, 14, 6), end=_mx(2026, 7, 15, 20))
        assert not e.covers_date("2026-07-13")
        assert not e.covers_date("2026-07-16")

    def test_ongoing_covers_through_now(self):
        e = _event(start=_mx(2026, 7, 10, 8), end=None)   # ongoing
        now = _mx(2026, 7, 12, 9)
        assert e.covers_date("2026-07-10", now=now)
        assert e.covers_date("2026-07-11", now=now)
        assert e.covers_date("2026-07-12", now=now)
        assert not e.covers_date("2026-07-13", now=now)   # future of now


class TestPlantMaintenanceOnDate:
    def test_maps_plant_to_event(self):
        events = [_event(pk="GTO1", start=_mx(2026, 7, 14, 6),
                         end=_mx(2026, 7, 15, 20))]
        m = plant_maintenance_on_date(events, "2026-07-14")
        assert set(m) == {"GTO1"}
        assert m["GTO1"].plant_key == "GTO1"

    def test_draft_events_still_count(self):
        # approval-independent: a draft window still means the plant is down
        events = [_event(pk="GTO1", approved="",
                         start=_mx(2026, 7, 14, 6), end=_mx(2026, 7, 15, 20))]
        m = plant_maintenance_on_date(events, "2026-07-14")
        assert "GTO1" in m

    def test_no_events_on_date_is_empty(self):
        events = [_event(pk="GTO1", start=_mx(2026, 7, 14, 6),
                         end=_mx(2026, 7, 15, 20))]
        assert plant_maintenance_on_date(events, "2026-07-20") == {}

    def test_multiple_plants(self):
        events = [
            _event(pk="GTO1", start=_mx(2026, 7, 14, 6),
                   end=_mx(2026, 7, 14, 20)),
            _event(pk="MEX2", start=_mx(2026, 7, 14, 8),
                   end=_mx(2026, 7, 14, 12)),
        ]
        assert set(plant_maintenance_on_date(events, "2026-07-14")) == \
            {"GTO1", "MEX2"}


class TestBadgeText:
    def test_argia_is_known_maintenance(self):
        e = _event(category="argia", note="awaiting protection parts",
                   end=None)
        txt = maintenance_badge_text(e)
        assert txt == "known maintenance \u2014 awaiting protection parts (ongoing)"

    def test_customer_label(self):
        e = _event(category="customer", note="roof works",
                   end=_mx(2026, 7, 15, 20))
        assert maintenance_badge_text(e).startswith("customer maintenance")
        assert "(ongoing)" not in maintenance_badge_text(e)

    def test_force_majeure_label(self):
        e = _event(category="force_majeure", note="", end=_mx(2026, 7, 15))
        assert maintenance_badge_text(e) == "force majeure"

    def test_long_note_truncated(self):
        e = _event(note="x" * 200, end=_mx(2026, 7, 15))
        txt = maintenance_badge_text(e, max_note=20)
        assert "\u2026" in txt
        assert len(txt) < 60
