"""Tests: contract design baseline (Design_Monthly -> kpi -> report).

2026-07-08: the weather-adjusted expected went blind on the block day
(183% fiction); contracts are written against the design estimate. The
static Design_Monthly baseline gives every day a 'vs design' figure that
no vendor outage can distort — the Prologis actual/expected/estimated
triple.
"""

from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.kpi.design import design_kwh_for_day, load_design_monthly


def sheets_with(rows):
    m = MagicMock(spec=SheetsClient)
    m.read_range.return_value = rows
    return m


HDR = ["plant_key", "year", "month", "design_kwh"]


class TestLoadDesignMonthly:
    def test_happy_path(self):
        m = sheets_with([HDR,
                         ["NL1", 2026, 7, 96706],
                         ["slp1", "2026", "7", "25011.0"]])
        d = load_design_monthly(m)
        assert d[("NL1", 2026, 7)] == 96706.0
        assert d[("SLP1", 2026, 7)] == 25011.0   # case + string tolerant

    def test_tab_fallback_order(self):
        # v61: Contract_Monthly became the primary design source; the
        # legacy names remain as fallbacks. This test previously pinned
        # ("Design_Monthly", "design_monthly") — consciously rewritten
        # for the new candidate chain.
        m = MagicMock(spec=SheetsClient)
        m.read_range.side_effect = [RuntimeError("no such tab"),
                                    RuntimeError("no such tab"),
                                    [HDR, ["NL1", 2026, 7, 96706]]]
        d = load_design_monthly(m)
        assert d[("NL1", 2026, 7)] == 96706.0
        attempts = [c[0][0] for c in m.read_range.call_args_list]
        assert attempts == ["Contract_Monthly", "Design_Monthly",
                            "design_monthly"]

    def test_missing_tab_degrades_to_empty(self):
        m = MagicMock(spec=SheetsClient)
        m.read_range.side_effect = RuntimeError("no such tab")
        assert load_design_monthly(m) == {}

    def test_malformed_rows_skipped_not_fatal(self):
        m = sheets_with([HDR,
                         ["NL1", 2026, 7, 96706],
                         ["GTO1", 2026, 13, 100],      # month 13
                         ["MEX1", 2026, 7, "n/a"],     # bad number
                         ["", 2026, 7, 100],            # no plant
                         ["MEX2", 2026, 7, 0]])         # zero design
        d = load_design_monthly(m)
        assert list(d) == [("NL1", 2026, 7)]

    def test_wrong_header_refused(self):
        m = sheets_with([["plant", "yr", "mo", "kwh"], ["NL1", 2026, 7, 9]])
        assert load_design_monthly(m) == {}


class TestDailyProration:
    D = {("NL1", 2026, 7): 96706.0,      # July: 31 days
         ("NL1", 2026, 6): 90000.0,      # June: 30 days
         ("NL1", 2028, 2): 29000.0}      # Feb leap: 29 days

    def test_month_lengths(self):
        assert design_kwh_for_day(self.D, "NL1", "2026-07-08") == \
            pytest.approx(96706 / 31, abs=0.1)
        assert design_kwh_for_day(self.D, "NL1", "2026-06-15") == 3000.0
        assert design_kwh_for_day(self.D, "nl1", "2028-02-29") == 1000.0

    def test_missing_year_or_plant_is_none(self):
        assert design_kwh_for_day(self.D, "NL1", "2027-07-08") is None
        assert design_kwh_for_day(self.D, "GTO1", "2026-07-08") is None
        assert design_kwh_for_day(self.D, "NL1", "garbage") is None


class TestWiring:
    def test_kpi_eod_loads_and_stamps(self):
        from pathlib import Path
        v2 = Path(__file__).resolve().parents[2]
        kpi = (v2 / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        assert "load_design_monthly(sheets)" in kpi
        assert 'stamp_column(sheets, "design_kwh", design_stamps' in kpi


class TestReportBuilderFallback:
    def test_builder_uses_tab_when_kpi_cell_empty(self):
        """Evening (live) editions have no KPI row — design comes from
        the Design_Monthly tab directly, so the contract comparison
        exists in BOTH daily editions."""
        from pathlib import Path
        v2 = Path(__file__).resolve().parents[2]
        src = (v2 / "argia" / "report" / "daily.py").read_text(
            encoding="utf-8")
        assert "load_design_monthly(sheets)" in src
        assert 'k.get("design")\n' in src or 'k.get("design")' in src
        assert "design_kwh_for_day(design_map" in src
