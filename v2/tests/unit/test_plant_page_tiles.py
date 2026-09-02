"""Tests: the plant-page dashboard redesign (management feedback v167).

report_gen.py runs only on pio06 (it needs psql at import), so these
are source-level invariants: every promise made to management — a
tooltip on each KPI tile, the weather-expected chart line, the
energy-at-risk estimate, the neutral coverage tile — is asserted
against the generator source, and the pure diff logic of the
pr_baseline sync gets real unit tests.
"""

import pathlib

from scripts.sync_pr_baseline import diff_rows

V2 = pathlib.Path(__file__).resolve().parents[2]
# encoding pinned: Windows read_text() defaults to cp1250 and the
# ≥/≤/÷ assertions fail (caught live on the laptop suite, v167)
SRC = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")


class TestTileTooltips:
    def test_every_kpi_tile_has_an_info_tooltip(self):
        # one ti(...) per explained tile: production, money, availability,
        # coverage, PR  (lifetime/capacity are self-explanatory)
        assert SRC.count("+ ti(") + SRC.count("ti(\"") >= 5
        assert "class=\"ti\"" in SRC and "tipbox" in SRC

    def test_tooltips_explain_the_color_logic(self):
        for frag in ("Green ≥ 95% of contract",
                     "Green ≥ 98%, amber ≥ 95%",
                     "Green ≥ baseline, amber within 5 pts"):
            assert frag in SRC, frag

    def test_pr_tooltip_names_formula_and_sheet_authority(self):
        assert "metered energy ÷ (kWp DC × plane-of-array irradiance)" in SRC
        assert "ARGIA_MONT_V2 Plants sheet" in SRC
        assert "last 30 days" in SRC          # answers the 82.5-vs-83.3 question

    def test_availability_tooltip_admits_comms_conflation(self):
        assert "conservative floor" in SRC
        assert "assumed target" in SRC        # SLA 98% honesty survives

    def test_coverage_tile_replaced_data_quality_and_is_neutral(self):
        assert "Telemetry coverage, selected range" in SRC
        assert "Data quality, selected range" not in SRC
        assert "NOT lost revenue" in SRC
        # JS: coverage tile never gets a good/warn/bad class any more
        assert "semTile('t_dq','')" in SRC
        assert "semTile('t_dq',q2" not in SRC

    def test_tooltips_are_bilingual_and_hidden_in_print(self):
        assert "Cobertura de telemetría" in SRC
        assert "línea base limpia" in SRC
        assert "@media print{.ti,.tipbox{display:none!important;}}" in SRC


class TestWeatherExpectedLine:
    def test_daily_chart_gets_weather_series_and_legend(self):
        assert "expected_kwh FROM daily_production" in SRC
        assert ";const X=" in SRC
        assert "line wx" in SRC and "stroke-dasharray" in SRC
        assert "expected from weather" in SRC

    def test_weather_line_skips_missing_days_instead_of_zeroing(self):
        # null values break the path (pen up), never plot as 0
        assert "if(v==null){pen=false;continue;}" in SRC

    def test_monthly_aggregation_carries_weather_series(self):
        assert "gx[m]=(gx[m]||0)+X[i]" in SRC


class TestEnergyAtRisk:
    def test_at_risk_uses_expected_times_unavailability(self):
        assert "avloss+=X[i]*(1-a)" in SRC
        assert "r_avloss" in SRC and "energy at risk" in SRC

    def test_at_risk_is_presented_as_upper_bound(self):
        assert "≤ <b id=\"r_avloss_v\"" in SRC


class TestPrBaselineSyncDiff:
    def test_reports_only_real_differences(self):
        d = diff_rows({"A": 0.85, "B": 0.90}, {"A": 0.85, "B": 0.99})
        assert d == [("B", 0.90, 0.99)]

    def test_missing_pg_value_counts_as_difference(self):
        assert diff_rows({"A": 0.85}, {}) == [("A", 0.85, None)]
        assert diff_rows({"A": 0.85}, {"A": None}) == [("A", 0.85, None)]

    def test_sheet_silence_never_touches_pg(self):
        # a plant absent from the sheet keeps its PG value
        assert diff_rows({}, {"A": 0.90}) == []

    def test_float_noise_is_not_a_difference(self):
        assert diff_rows({"A": 0.8500004}, {"A": 0.85}) == []
