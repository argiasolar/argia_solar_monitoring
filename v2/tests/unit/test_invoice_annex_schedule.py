"""Monthly-invoice scheduling helpers (v94).

The 1st-of-month cron uses ``last_complete_month`` to pick the period —
the month that just closed. The edge cases that matter: the January
roll-back to the prior December, and month windows landing on the right
last day (28/29/30/31).
"""

import datetime as dt

from argia.core.time_utils import MX_TZ
from scripts.report_invoice_annex import (
    last_complete_month, month_window, previous_month,
)


class TestPreviousMonth:
    def test_mid_year(self):
        assert previous_month(2026, 7) == (2026, 6)

    def test_january_rolls_to_prior_december(self):
        assert previous_month(2026, 1) == (2025, 12)


class TestLastCompleteMonth:
    def test_first_of_month_is_prior_month(self):
        # cron fires 2026-07-01 07:30 → invoice June
        now = dt.datetime(2026, 7, 1, 7, 30, tzinfo=MX_TZ)
        assert last_complete_month(now) == "2026-06"

    def test_first_of_january_is_prior_december(self):
        now = dt.datetime(2026, 1, 1, 7, 30, tzinfo=MX_TZ)
        assert last_complete_month(now) == "2025-12"

    def test_mid_month_still_prior_month(self):
        # (only ever run on the 1st, but the rule holds any day)
        now = dt.datetime(2026, 7, 15, tzinfo=MX_TZ)
        assert last_complete_month(now) == "2026-06"


class TestMonthWindow:
    def test_30_day_month(self):
        w = month_window("2026-06")
        assert w.start.isoformat() == "2026-06-01"
        assert w.end.isoformat() == "2026-06-30"

    def test_31_day_month(self):
        w = month_window("2026-07")
        assert w.end.isoformat() == "2026-07-31"

    def test_february_leap_year(self):
        assert month_window("2028-02").end.isoformat() == "2028-02-29"

    def test_february_non_leap(self):
        assert month_window("2026-02").end.isoformat() == "2026-02-28"
