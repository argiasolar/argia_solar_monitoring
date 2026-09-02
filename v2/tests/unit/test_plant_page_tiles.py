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
        assert "r_avloss" in SRC and "est. unavailability loss" in SRC

    def test_at_risk_is_presented_as_upper_bound(self):
        assert "≤ <b id=\"r_avloss_v\"" in SRC

    def test_loss_is_priced_for_ppa_plants(self):
        # round 2: loss must show pesos, not just kWh
        assert "s+=' · ~$'+nf(avloss*TARIFF)+' MXN'" in SRC
        assert "const TARIFF={p[\"tariff\"] if is_ppa else 0}" in SRC


class TestRound2Cosmetics:
    def test_weather_expected_line_is_yellow(self):
        assert ".line.wx{stroke:#eab308" in SRC
        assert 'style="background:#eab308"' in SRC   # legend key matches

    def test_inverter_index_shown_as_percent(self):
        assert "{idx*100:,.1f}%</span>" in SRC


def _exec_seg(start, stop, ns):
    seg = SRC[SRC.index(start):SRC.index(stop)]
    exec(compile(seg, "report_gen_seg", "exec"), ns)
    return ns


class TestInverter30d:
    """The per-inverter rolling-30d aggregation (v169) — executed from
    the report_gen source, since that file only imports on pio06."""

    def _fns(self):
        ns = {"f": lambda v: float(v) if v not in ("", None) else 0.0}
        _exec_seg("def _inverter_30d", "inv30 = _inverter_30d", ns)
        _exec_seg("def _median", "def inverter_card", ns)
        return ns

    def test_energy_sums_daily_counter_maxima(self):
        fns = self._fns()
        rows = [["P", "A", "2026-08-01", "100.5", "10", "10"],
                ["P", "A", "2026-08-02", "99.5", "10", "10"]]
        assert fns["_inverter_30d"](rows)["P"]["A"]["kwh"] == 200.0

    def test_silent_inverter_judged_against_busiest_peer(self):
        fns = self._fns()
        # B reported nothing on day 2: availability must count that
        # day's slots against it (the director's NL1 lesson in reverse)
        rows = [["P", "A", "d1", "10", "10", "10"],
                ["P", "B", "d1", "10", "10", "10"],
                ["P", "A", "d2", "10", "12", "12"]]
        out = fns["_inverter_30d"](rows)
        assert out["P"]["A"] == {"kwh": 20.0, "on": 22, "plant_slots": 22}
        assert out["P"]["B"]["on"] == 10          # 10 of 22 -> ~45%
        assert out["P"]["B"]["plant_slots"] == 22

    def test_median_odd_even_empty(self):
        fns = self._fns()
        assert fns["_median"]([3, 1, 2]) == 2
        assert fns["_median"]([4, 1, 3, 2]) == 2.5
        assert fns["_median"]([]) is None

    def test_card_uses_directors_thresholds_and_median(self):
        assert "idx >= 0.96 else 'warn' if idx >= 0.90" in SRC
        assert "Inverters — last 30 days" in SRC
        assert "specific yield ÷ the plant median" in SRC
        # fixed window disclosure — the date picker does not move it
        assert "the date picker above does not move it" in SRC


class TestColoredTilesSayWhy:
    """Tomasz 2026-09-02: a yellow/red tile must state its reason on
    the tile, not leave management to open a tooltip and guess."""

    def test_production_reason_separates_resource_from_performance(self):
        # the director's SAG/Vitalmex explanation: below contract but
        # matching the weather = resource, not a fault
        assert "resource, not performance" in SRC
        assert "below contract AND weather expectation" in SRC

    def test_availability_reason_names_the_worst_days(self):
        assert "Worst days:" in SRC
        assert "telemetry loss, not proven downtime" in SRC
        assert "check grid/site events" in SRC

    def test_pr_reason_covers_downtime_heat_and_string_causes(self):
        assert "includes low-availability days" in SRC
        assert "no module-temperature data to normalize" in SRC
        assert "soiling or string-level losses" in SRC

    def test_reasons_live_on_the_flip_side(self):
        # Tomasz round 2: the reason sits on the BACK of the tile and
        # the tile flips on hover — only when armed (.haswhy)
        assert "tl.classList.toggle('haswhy',!!txt)" in SRC
        assert ".tile.haswhy:hover .flipin" in SRC
        assert "rotateY(180deg)" in SRC
        assert "backface-visibility:hidden" in SRC
        # green tiles never flip: haswhy comes only with a reason
        assert '" haswhy" if pr_why else ""' in SRC


class TestPerPlantSla:
    def test_page_uses_plant_sla_with_assumed_fallback(self):
        assert "plant_sla = p.get('sla') or SLA_TARGET" in SRC
        assert "const SLA={plant_sla}" in SRC
        assert "coalesce(sla_target,0)" in SRC
        # label flips once a real SLA is configured
        assert '"configured", "configurado"' in SRC.replace("'", '"')

    def test_setup_has_sla_editor(self):
        app = (V2 / "server/bundle/setup_app.py").read_text(encoding="utf-8")
        assert "@app.post('/finance/sla')" in app
        assert "/setup/finance/sla" in app
        import server.bundle.finance_core as fin
        assert fin.sql_set_sla("SLP2", 0.97) == \
            "UPDATE plant SET sla_target = 0.9700 WHERE plant_key = 'SLP2';"
        assert fin.SLA_MIN == 0.8 and fin.SLA_MAX == 1.0
        assert "ADD COLUMN IF NOT EXISTS" in fin.ENSURE_SLA_COL_SQL


class TestCurrentMonthOverlay:
    def test_current_month_flagged_and_expectation_carried(self):
        assert "fl.append(2)" in SRC
        assert "cur_exp = sum(expected_month_kwh(k, m) for k in keys)" in SRC

    def test_monthly_svg_draws_actual_over_expected(self):
        seg = SRC[SRC.index("def monthly_svg"):SRC.index("def columns_svg")
                  if SRC.index("def columns_svg") > SRC.index("def monthly_svg")
                  else len(SRC)]
        assert "flags[i] == 2 and cur_exp > 0" in SRC
        assert "expected (full month)" in SRC
        assert "actual so far" in SRC

    def test_fleet_chart_gets_the_overlay_too(self):
        # round 2: "in the main reports please also do grey on blue"
        assert "cur_expected=fleet_cur_exp" in SRC
        assert "flags[i] == 2 and cur_exp > 0" in SRC       # monthly_svg
        assert "flags and flags[i] == 2 and cur_exp > 0" in SRC  # columns_svg


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
        assert "PR baseline &amp; availability SLA per" in app
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
