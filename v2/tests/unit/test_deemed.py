"""Deemed-energy engine tests.

The truth table is the spec: full outage on a full-day window must equal
``contract_daily``; a plant that limped is compensated only for the
shortfall; night hours deem nothing; and only APPROVED customer events
ever produce a number. Window arithmetic is exercised across midnight
and month boundaries because that is where off-by-a-day bugs live.
"""

import datetime as dt

import pytest

from argia.core.time_utils import MX_TZ
from argia.maintenance.deemed import (
    daylight_fraction, deemed_by_plant_day, deemed_for_date,
    deemed_kwh_for_day, event_day_spans, measured_in_window_from_buckets,
)
from argia.maintenance.events import MaintenanceEvent


def _mx(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, 0, tzinfo=MX_TZ)


def _event(pk="SLP1", start=None, end=None, category="customer",
           approved="tomasz", cost=None, cost_type="repair"):
    return MaintenanceEvent(
        plant_key=pk,
        start_ts=start or _mx(2026, 7, 5, 6, 0),
        end_ts=end,
        category=category, cost_type=cost_type, cost_mxn=cost,
        note="", approved_by=approved)


class TestDaylightFraction:
    DAY = dt.date(2026, 7, 5)

    def test_full_daylight_window_is_one(self):
        assert daylight_fraction(
            self.DAY, _mx(2026, 7, 5, 6), _mx(2026, 7, 5, 20)) == 1.0

    def test_half_window(self):
        # 06:00-13:00 = 7h of the 14h daylight window
        assert daylight_fraction(
            self.DAY, _mx(2026, 7, 5, 6), _mx(2026, 7, 5, 13)
        ) == pytest.approx(0.5)

    def test_night_window_is_zero(self):
        assert daylight_fraction(
            self.DAY, _mx(2026, 7, 5, 0), _mx(2026, 7, 5, 6)) == 0.0
        assert daylight_fraction(
            self.DAY, _mx(2026, 7, 5, 20), _mx(2026, 7, 5, 23, 59)) == 0.0

    def test_window_wider_than_daylight_clamps_to_one(self):
        assert daylight_fraction(
            self.DAY, _mx(2026, 7, 5, 4), _mx(2026, 7, 5, 23)) == 1.0

    def test_clipped_to_the_named_day_only(self):
        # a window running past midnight only counts THIS day's daylight
        assert daylight_fraction(
            self.DAY, _mx(2026, 7, 5, 13), _mx(2026, 7, 6, 10)
        ) == pytest.approx(0.5)  # 13:00-20:00 = 7h


class TestDeemedKwhForDay:
    DAY = dt.date(2026, 7, 5)
    S = _mx(2026, 7, 5, 6)
    E = _mx(2026, 7, 5, 20)      # full daylight window
    CD = 1400.0                  # contract_daily

    def test_full_outage_equals_contract_daily(self):
        assert deemed_kwh_for_day(
            self.DAY, self.S, self.E, self.CD, 0.0) == pytest.approx(1400.0)

    def test_plant_produced_full_no_deemed(self):
        assert deemed_kwh_for_day(
            self.DAY, self.S, self.E, self.CD, 1400.0) == 0.0

    def test_plant_limped_deems_shortfall(self):
        assert deemed_kwh_for_day(
            self.DAY, self.S, self.E, self.CD, 900.0
        ) == pytest.approx(500.0)

    def test_overproduction_never_negative(self):
        assert deemed_kwh_for_day(
            self.DAY, self.S, self.E, self.CD, 1500.0) == 0.0

    def test_partial_window_prorates_expected(self):
        # 06:00-13:00 = frac 0.5 → expected 700; measured 200 → deemed 500
        assert deemed_kwh_for_day(
            self.DAY, self.S, _mx(2026, 7, 5, 13), self.CD, 200.0
        ) == pytest.approx(500.0)

    def test_no_contract_basis_is_zero(self):
        assert deemed_kwh_for_day(
            self.DAY, self.S, self.E, None, 0.0) == 0.0
        assert deemed_kwh_for_day(
            self.DAY, self.S, self.E, 0.0, 0.0) == 0.0

    def test_night_window_is_zero(self):
        assert deemed_kwh_for_day(
            self.DAY, _mx(2026, 7, 5, 0), _mx(2026, 7, 5, 5),
            self.CD, 0.0) == 0.0


class TestEventDaySpans:
    def test_single_day(self):
        e = _event(start=_mx(2026, 7, 5, 9), end=_mx(2026, 7, 5, 17))
        spans = event_day_spans(e)
        assert len(spans) == 1
        day, s, en = spans[0]
        assert day == dt.date(2026, 7, 5)
        assert s == _mx(2026, 7, 5, 9)
        assert en == _mx(2026, 7, 5, 17)

    def test_spans_midnight_into_two_days(self):
        e = _event(start=_mx(2026, 7, 5, 22), end=_mx(2026, 7, 6, 3))
        spans = event_day_spans(e)
        assert [d for d, _, _ in spans] == [dt.date(2026, 7, 5),
                                            dt.date(2026, 7, 6)]
        # day1 ends at midnight, day2 starts at midnight
        assert spans[0][2] == _mx(2026, 7, 6, 0)
        assert spans[1][1] == _mx(2026, 7, 6, 0)

    def test_spans_month_boundary(self):
        e = _event(start=_mx(2026, 7, 31, 10), end=_mx(2026, 8, 1, 10))
        days = [d for d, _, _ in event_day_spans(e)]
        assert days == [dt.date(2026, 7, 31), dt.date(2026, 8, 1)]

    def test_ongoing_event_bounded_at_now(self):
        e = _event(start=_mx(2026, 7, 5, 8), end=None)   # ongoing
        now = _mx(2026, 7, 7, 12)
        days = [d for d, _, _ in event_day_spans(e, now=now)]
        assert days == [dt.date(2026, 7, 5), dt.date(2026, 7, 6),
                        dt.date(2026, 7, 7)]
        # last span clipped at now
        assert event_day_spans(e, now=now)[-1][2] == now


class TestDeemedByPlantDay:
    def _contract_daily(self, cd_map):
        def fn(pk, y, m):
            return cd_map.get(pk.upper())
        return fn

    def _measured_zero(self, *_a, **_k):
        return 0.0

    def test_approved_customer_event_deems(self):
        e = _event(pk="SLP1", start=_mx(2026, 7, 5, 6),
                   end=_mx(2026, 7, 5, 20))
        out = deemed_by_plant_day(
            [e], self._contract_daily({"SLP1": 1400.0}),
            self._measured_zero)
        assert out == {("SLP1", "2026-07-05"): pytest.approx(1400.0)}

    def test_draft_event_never_deems(self):
        e = _event(approved="")   # draft
        out = deemed_by_plant_day(
            [e], self._contract_daily({"SLP1": 1400.0}),
            self._measured_zero)
        assert out == {}

    def test_argia_and_force_majeure_do_not_deem(self):
        for cat in ("argia", "force_majeure"):
            e = _event(category=cat, start=_mx(2026, 7, 5, 6),
                       end=_mx(2026, 7, 5, 20))
            out = deemed_by_plant_day(
                [e], self._contract_daily({"SLP1": 1400.0}),
                self._measured_zero)
            assert out == {}, cat

    def test_no_contract_key_is_isolated(self):
        # a lighting project / CAPEX key with no contract_kwh → 0 deemed
        e = _event(pk="LGTO1", start=_mx(2026, 7, 5, 6),
                   end=_mx(2026, 7, 5, 20))
        out = deemed_by_plant_day(
            [e], self._contract_daily({"SLP1": 1400.0}),  # LGTO1 absent
            self._measured_zero)
        assert out == {}

    def test_overlapping_events_sum_on_same_day(self):
        e1 = _event(pk="SLP1", start=_mx(2026, 7, 5, 6),
                    end=_mx(2026, 7, 5, 13))   # frac .5 → 700
        e2 = _event(pk="SLP1", start=_mx(2026, 7, 5, 13),
                    end=_mx(2026, 7, 5, 20))   # frac .5 → 700
        out = deemed_by_plant_day(
            [e1, e2], self._contract_daily({"SLP1": 1400.0}),
            self._measured_zero)
        assert out[("SLP1", "2026-07-05")] == pytest.approx(1400.0)

    def test_measured_subtracted_per_window(self):
        e = _event(pk="SLP1", start=_mx(2026, 7, 5, 6),
                   end=_mx(2026, 7, 5, 20))

        def measured(pk, d, s, en):
            return 400.0
        out = deemed_by_plant_day(
            [e], self._contract_daily({"SLP1": 1400.0}), measured)
        assert out[("SLP1", "2026-07-05")] == pytest.approx(1000.0)

    def test_deemed_for_date_filters_one_day(self):
        e = _event(pk="SLP1", start=_mx(2026, 7, 5, 22),
                   end=_mx(2026, 7, 6, 20))
        cd = self._contract_daily({"SLP1": 1400.0})
        # 2026-07-05 22:00-24:00 is night → deemed 0 → filtered out entirely
        only5 = deemed_for_date([e], "2026-07-05", cd, self._measured_zero)
        assert only5 == {}
        # 2026-07-06 00:00-20:00 covers the full daylight window → 1400
        only6 = deemed_for_date([e], "2026-07-06", cd, self._measured_zero)
        assert only6 == {"SLP1": pytest.approx(1400.0)}


class TestMeasuredInWindowFromBuckets:
    def test_full_day_uses_energy_no_buckets(self):
        got = measured_in_window_from_buckets(
            None, "SLP1", "2026-07-05",
            _mx(2026, 7, 5, 6), _mx(2026, 7, 5, 20),
            energy_kwh_full_day=1234.0, daylight_frac=1.0)
        assert got == pytest.approx(1234.0)

    def test_partial_sums_in_window_buckets(self):
        rows = [
            {"plant_key": "SLP1", "date_mx": "2026-07-05",
             "hour_label": "07:00", "total_kwh": 50},
            {"plant_key": "SLP1", "date_mx": "2026-07-05",
             "hour_label": "08:00", "total_kwh": 60},
            {"plant_key": "SLP1", "date_mx": "2026-07-05",
             "hour_label": "14:00", "total_kwh": 999},   # outside window
            {"plant_key": "SLP2", "date_mx": "2026-07-05",
             "hour_label": "08:00", "total_kwh": 777},    # other plant
        ]
        got = measured_in_window_from_buckets(
            rows, "SLP1", "2026-07-05",
            _mx(2026, 7, 5, 7), _mx(2026, 7, 5, 9),
            energy_kwh_full_day=500.0, daylight_frac=0.14)
        assert got == pytest.approx(110.0)   # 50 + 60, not 999 or 777

    def test_partial_no_buckets_falls_back_to_proration(self):
        got = measured_in_window_from_buckets(
            [], "SLP1", "2026-07-05",
            _mx(2026, 7, 5, 7), _mx(2026, 7, 5, 9),
            energy_kwh_full_day=1400.0, daylight_frac=0.5)
        assert got == pytest.approx(700.0)   # 1400 * 0.5
