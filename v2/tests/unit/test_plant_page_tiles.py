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

    def test_pr_tooltip_names_formula_and_config_authority(self):
        assert "metered energy ÷ (kWp DC × plane-of-array irradiance)" in SRC
        # Tomasz 2026-09-02: never name the internal sheet in the UI
        assert "ARGIA_MONT_V2" not in SRC
        assert "Plant configuration" in SRC
        assert "last 30 days" in SRC          # answers the 82.5-vs-83.3 question

    def test_pr_color_uses_temperature_normalized_value(self):
        # solar director: NL1's July->August PR drop was cell temperature;
        # a hot month must not paint a healthy plant red
        assert "pr_for_color = prstc if prstc else pr" in SRC
        assert "temp-normalized" in SRC and "pr_stc" in SRC

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
        assert "irradiance_kwh_m2" in SRC
        assert ";const X=" in SRC
        assert "line wx" in SRC and "stroke-dasharray" in SRC
        assert "expected from weather" in SRC

    def test_expectation_is_self_calibrated_with_config_floor(self):
        # calibrated to the plant's demonstrated PR, but max() with the
        # config factor so a sick plant (GTO2 ~46% PR) is never graded
        # against its own illness
        assert "percentile_cont(0.5)" in SRC
        assert "data_class = 'full'" in SRC
        assert "max(_cfgf.get(k) or 0.0" in SRC

    def test_sla_verdict_reviews_instead_of_breach_when_energy_ok(self):
        # director's NL1 case: 93.6% availability, 102% of contract —
        # produced-through-the-gap must read REVIEW, not BREACH
        assert "ranFine=xsum>0&&exsum>=0.97*xsum" in SRC
        assert "'REVIEW'" in SRC
        assert "telemetry, not downtime" in SRC

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


class TestPrBaselineEditor:
    def test_sql_builder_and_bounds(self):
        import server.bundle.finance_core as fin
        assert fin.sql_set_pr_baseline("GTO1", 0.88) == \
            "UPDATE plant SET pr_baseline = 0.8800 WHERE plant_key = 'GTO1';"
        assert fin.PRB_MIN == 0.5 and fin.PRB_MAX == 1.0
        # the shared numeric parser enforces the bounds
        assert fin.parse_num("0.88", fin.PRB_MIN, fin.PRB_MAX) == 0.88
        assert fin.parse_num("88", fin.PRB_MIN, fin.PRB_MAX) is None
        assert fin.parse_num("0.3", fin.PRB_MIN, fin.PRB_MAX) is None

    def test_setup_page_has_editor_and_route(self):
        app = (V2 / "server/bundle/setup_app.py").read_text(encoding="utf-8")
        assert "/setup/finance/prbaseline" in app       # form action
        assert "@app.post('/finance/prbaseline')" in app
        assert "PR baseline per plant" in app
        # guarded and audited like every finance edit
        seg = app.split("def finance_prbaseline()", 1)[1][:800]
        assert "_fin_guard()" in seg and "_fin_write(" in seg

    def test_sync_script_demoted_to_import_tool(self):
        s = (V2 / "scripts/sync_pr_baseline.py").read_text(encoding="utf-8")
        assert "OVERWRITE audited admin edits" in s
        assert "the DATABASE became the authority" in s


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
