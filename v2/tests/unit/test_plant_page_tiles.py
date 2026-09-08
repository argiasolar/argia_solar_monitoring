"""Tests: the plant-page dashboard redesign (management feedback v167).

report_gen.py runs only on pio06 (it needs psql at import), so these
are source-level invariants: every promise made to management — a
tooltip on each KPI tile, the weather-expected chart line, the
energy-at-risk estimate, the neutral coverage tile — is asserted
against the generator source, and the pure diff logic of the
pr_baseline sync gets real unit tests.
"""

import pathlib


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
        # JS: coverage tile never gets a good/warn/bad class from its
        # value (v241: red only when the whole range is empty)
        assert "semTile('t_dq',nodata?'bad':'')" in SRC
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
        assert "per_irr = per_irr_factor(_cfgf.get(k), mp, nfull, kwp)" in SRC
        assert "return max(cfg, med_pr * (kwp or 0.0))" in SRC

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
    def test_loss_is_its_own_tile_now(self):
        # round 3: "maybe it should be a new tile like it used to be"
        assert "avloss+=X[i]*(1-a)" in SRC
        assert 'id="t_loss"' in SRC
        assert "Est. loss — unavailability" in SRC

    def test_loss_is_priced_and_labeled_upper_bound(self):
        assert "'≤ ~$'+nf(avloss*TARIFF)+' MXN'" in SRC
        assert "const TARIFF={p[\"tariff\"] if is_ppa else 0}" in SRC
        assert "upper bound — comms gaps count as loss" in SRC
        assert "the ceiling, not the bill" in SRC      # tooltip honesty

    def test_zero_exposure_reads_as_zero_not_dash(self):
        assert "'≈ 0'" in SRC
        assert "no measurable exposure in range" in SRC


class TestRound2Cosmetics:
    def test_weather_expected_line_is_yellow(self):
        assert ".line.wx{stroke:#eab308" in SRC
        assert 'style="background:#eab308"' in SRC   # legend key matches

    def test_inverter_index_shown_as_percent(self):
        assert "{idx*100:,.1f}%</span>" in SRC


class TestRound3Layout:
    def test_hovered_tile_rises_above_its_neighbours(self):
        # the production tooltip was buried under the PR tile
        assert ".tile:hover{z-index:70;}" in SRC

    def test_info_icon_pinned_level_with_title(self):
        assert ".tlabel .ti{margin-left:auto;align-self:flex-start" in SRC

    def test_card_tooltips_anchor_to_the_card_not_the_page(self):
        # the inverter-card tooltip rendered as a bar at the page top
        assert ".card{position:relative;}" in SRC


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
        _exec_seg("def _median", "INV_COLORS = ", ns)
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
        assert out["P"]["A"] == {"kwh": 20.0, "on": 22, "plant_slots": 22, "daily": {"d1": 10.0, "d2": 10.0}}
        assert out["P"]["B"]["on"] == 10          # 10 of 22 -> ~45%
        assert out["P"]["B"]["plant_slots"] == 22

    def test_median_odd_even_empty(self):
        fns = self._fns()
        assert fns["_median"]([3, 1, 2]) == 2
        assert fns["_median"]([4, 1, 3, 2]) == 2.5
        assert fns["_median"]([]) is None

    def test_card_uses_directors_thresholds_and_median(self):
        assert "idx >= 0.96 else 'warn' if idx >= 0.90" in SRC


class TestInverterChart:
    """v231: the per-inverter daily production chart in the 30-day card
    (Tomasz: 'a graph like the Growatt server shows')."""

    def _fns(self):
        import html, math
        ns = {"f": lambda v: float(v) if v not in ("", None) else 0.0, "html": html, "math": math, "json": __import__("json"),
              "q": lambda sql: [], "t": lambda en, es: en}
        seg = SRC[SRC.index("def yticks"):]
        exec(compile(seg[:seg.index("\n\n\n")], "report_gen_seg", "exec"), ns)      # yticks alone
        _exec_seg("def _inverter_30d", "inv30 = _inverter_30d", ns)
        _exec_seg("INV_COLORS = ", "def inverter_card", ns)
        return ns

    def test_one_line_per_inverter_with_serial_and_kwh_per_kw(self):
        fns = self._fns()
        rows = [["P", "A", "2026-09-01", "600", "10", "10"], ["P", "A", "2026-09-02", "650", "10", "10"],
                ["P", "A", "2026-09-03", "640", "10", "10"],
                ["P", "B", "2026-09-01", "500", "10", "10"], ["P", "B", "2026-09-03", "520", "10", "10"]]   # B missed a day
        stats = fns["_inverter_30d"](rows)["P"]
        svg = fns["inverter_chart_svg"](stats, {"A": ("Inverter 1", 124.0), "B": ("Inverter 2", 124.0)})
        assert svg.count('<path class="line"') == 2
        assert '<title>2026-09-02 · Inverter 1 (A): 650 kWh · 5.24 kWh/kW</title>' in svg
        assert 'Inverter 2 <span class="sn">B</span>' in svg and 'Inverter 1 <span class="sn">A</span>' in svg
        # B's gap breaks its line instead of drawing through the missing day
        b_path = [seg for seg in svg.split('<path class="line"') if "<title>Inverter 2</title>" in seg][0].split('d="')[1].split('"')[0]
        assert b_path.count("M") == 2 and "L" not in b_path
        assert svg.count('<circle class="pt"') == 5          # v233: visible dots on every point
        # v232: each series is a toggleable group, the legend has a checkbox per unit, the last label has room
        assert svg.count('<g class="ser" data-sn=') == 2 and svg.count('<input type="checkbox" checked data-sn=') == 2
        assert 'onchange="argiaInvToggle(this)"' in svg          # the toggle lives in the chrome since v234
        assert 'x2="860"' in svg          # plot ends 40 px before the edge (was 12)
        assert "tkpill" not in svg        # tickets live in the table, never in the legend (v233)
        # v233: the day box — the series ride along as JSON, the hover JS and the guide line are there
        import json as _json, html as _html
        data = _json.loads(_html.unescape(svg.split('data-chart="', 1)[1].split('"', 1)[0]))
        assert data["labels"] == ["2026-09-01", "2026-09-02", "2026-09-03"] and data["xs"] == [54.0, 457.0, 860.0] and data["W"] == 900
        assert [s_["label"] for s_ in data["series"]] == ["Inverter 1", "Inverter 2"]
        assert data["series"][1]["vals"] == [500.0, None, 520.0] and data["series"][0]["extra"][1] == "5.24 kWh/kW"
        assert data["series"][0]["sn"] == "A" and data["series"][0]["unit"] == "kWh"
        # v234: the hover is the chrome's generic one (argiaChartHover), shared with the daily and monthly charts
        assert 'onmousemove="argiaChartHover(event,this)"' in svg and 'class="chguide"' in svg
        assert "function argiaInvHover" not in svg and "<script>" not in svg
        # nothing to draw: one day only, or all zero
        assert fns["inverter_chart_svg"]({"A": {"daily": {"2026-09-01": 5.0}}}, {}) == ""
        assert fns["inverter_chart_svg"]({"A": {"daily": {"d1": 0.0, "d2": 0.0}}}, {}) == ""

    def test_open_ticket_shows_in_legend_and_table_row(self):
        fns = self._fns()
        rows = [["P", "A", "2026-09-01", "600", "10", "10"], ["P", "A", "2026-09-02", "650", "10", "10"]]
        stats = fns["_inverter_30d"](rows)["P"]
        svg = fns["inverter_chart_svg"](stats, {"A": ("Inverter 1", 124.0)}, tickets={"A": ("TK-NL1-0002", "IN_PROGRESS", "P2")})
        assert "tkpill" not in svg                                    # v233: not in the legend any more
        assert fns["ticket_pill"](("TK-NL1-0002", "IN_PROGRESS", "P2")) == \
            '<a href="/maintenance/t/TK-NL1-0002/" class="tkpill" title="P2 · In progress">TK-NL1-0002 · In progress</a>'
        assert fns["ticket_pill"](None) == ""
        assert "tkp = ticket_pill(tickets.get(sn))" in SRC and "(f'<br>{tkp}' if tkp else '')" in SRC
        assert "TICKET_BY_INVERTER.get((k, sn))" in SRC

    def test_inverter_card_renders_end_to_end(self):
        """Executes inverter_card itself (the v232 deploy failed on an
        UnboundLocalError a source-text test could not see)."""
        import html, math
        ns = {"f": lambda v: float(v) if v not in ("", None) else 0.0, "html": html, "math": math, "json": __import__("json"),
              "q": lambda sql: [], "t": lambda en, es: en, "ti": lambda en, es: "<i/>"}
        seg = SRC[SRC.index("def yticks"):]
        exec(compile(seg[:seg.index("\n\n\n")], "report_gen_seg", "exec"), ns)
        _exec_seg("def _inverter_30d", "inv30 = _inverter_30d", ns)
        _exec_seg("def _median", "# ================= page: plant performance", ns)
        rows = [["NL1", "A", "2026-09-01", "600", "10", "10"], ["NL1", "A", "2026-09-02", "650", "10", "10"],
                ["NL1", "B", "2026-09-01", "500", "10", "10"], ["NL1", "B", "2026-09-02", "520", "10", "10"]]
        ns["inv30"] = ns["_inverter_30d"](rows)
        ns["inv_meta"] = {("NL1", "A"): ("Inverter 1", 124.0), ("NL1", "B"): ("Inverter 2", 124.0)}
        ns["TICKET_BY_INVERTER"] = {("NL1", "B"): ("TK-NL1-0002", "NEW", "P2")}
        card = ns["inverter_card"]("NL1")
        assert card.count('class="tkpill"') == 1            # the table row of Inverter 2 only (v233: legend stays clean)
        assert card.index("<svg") < card.index("<table")
        assert "Inverter 1<br>" in card and 'TK-NL1-0002 · New</a></td>' in card
        assert ns["inverter_card"]("ZZZ").count("No inverter telemetry") == 1

    def test_card_embeds_the_chart_above_the_table(self):
        assert "chart = inverter_chart_svg(stats, {sn: inv_meta.get((k, sn)) or (sn, 0) for sn in stats})" in SRC
        # the chart's CSS lives in the portal chrome — report_gen's own stylesheet never reaches the portal pages
        chrome = (V2 / "server" / "bundle" / "portal_chrome.py").read_text(encoding="utf-8")
        for rule in (".invchart,.hchart{position:relative", ".invlegs{", ".invleg{", ".chtip{", ".chguide{", ".tkpill{", ".sw{",
                     ".card>.invchart,.card>.hchart,.card>p.note{margin-left:20px;margin-right:20px}"):
            assert rule in chrome, rule
        for fn in ("function argiaChartHover", "function argiaChartLeave", "function argiaInvToggle"):
            assert fn in chrome, fn
        assert ".card{background:#fff;border:1px solid var(--line);border-radius:12px;position:relative}" in chrome   # h2 tooltips anchor

    def test_tile_values_fit_one_line(self):
        """v233/v234 (Tomasz: the big bold numbers wrap inside the tiles) — the Map tile look:
        clamp(15px,1.4vw,20px), weight 700, one line."""
        chrome = (V2 / "server" / "bundle" / "portal_chrome.py").read_text(encoding="utf-8")
        assert ".tval{font-size:clamp(15px,1.4vw,20px);line-height:1.15;font-weight:700;color:var(--ink);white-space:nowrap}" in chrome
        assert ".tile .thero{font-size:clamp(15px,1.4vw,20px);font-weight:700}" in chrome
        assert ".kpi .v{font-size:24px" in (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")

    def test_daily_and_monthly_charts_carry_the_hover_box(self):
        """v234: hover a day / a month -> actual, contract or expected, expected from weather, money."""
        assert "s+='<line class=\"chguide\" x1=\"'+pl+'\"" in SRC and 'onmousemove="argiaChartHover(event,this)"' in SRC
        assert "return '<div class=\"hchart\" data-chart=\"'+data+'\">'+s+'</div>';}" in SRC
        assert "const ser=[{label:CH_L.actual,color:'#05b1a9',vals:vals,unit:unit" in SRC
        assert "+ 'const CH_L=' + json.dumps({'actual': 'actual', 'contract': 'contract' if is_ppa else 'expected'," in SRC
        import html, json, math
        ns = {"f": lambda v: float(v) if v not in ("", None) else 0.0, "html": html, "math": math, "json": json,
              "MONTH_EN": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
              "MONTH_ES": ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]}
        seg = SRC[SRC.index("def yticks"):]
        exec(compile(seg[:seg.index("\n\n\n")], "report_gen_seg", "exec"), ns)
        seg = SRC[SRC.index("def monthly_svg"):]
        exec(compile(seg[:seg.index("\n\n\n")], "report_gen_seg", "exec"), ns)
        pairs = [("2026-07", "111000"), ("2026-08", "133000"), ("2026-09", "26000"), ("2026-10", "84000")]
        out = ns["monthly_svg"](pairs, [0, 0, 2, 1], contract_mwh=[95, 98, 84, 84], revenue_kmxn=[220, 260, 50, 165],
                                cur_expected_kwh=83000)
        assert out.startswith('<div class="hchart" data-chart="') and out.endswith("</svg></div>")
        d = json.loads(html.unescape(out.split('data-chart="', 1)[1].split('"', 1)[0]))
        assert d["labels"] == ["Jul 2026", "Aug 2026", "Sep 2026", "Oct 2026"] and len(d["xs"]) == 4
        by = {s_["label"]: s_ for s_ in d["series"]}
        assert by["actual"]["vals"] == [111.0, 133.0, 26.0, None]          # a forecast month has no actual
        assert by["expected"]["vals"] == [None, None, 83.0, 84.0]          # in-progress month: the full-month expectation
        assert by["contract baseline"]["vals"] == [95, 98, 84, 84] and by["revenue"]["unit"] == "k MXN"
        assert "money_label='revenue' if is_ppa else 'savings'" in SRC
        assert '<p class="note" style="margin:0 20px 4px">' in SRC        # the chart note keeps the card\'s 20 px margin
        assert 'class="chguide"' in out and 'onmousemove="argiaChartHover(event,this)"' in out
        assert "+ chart +" in SRC and "Daily kWh per inverter, each from its own counter" in SRC
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
        assert "tl.classList.toggle('haswhy',!!html)" in SRC
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


class TestRound4:
    def test_tooltips_are_dark_on_bright(self):
        # Tomasz: "never dark background with white letters"
        assert "background:#fffdf4;color:#243041" in SRC
        assert "background:#20293a" not in SRC

    def test_report_and_financial_tiles_fit_one_line(self):
        assert ".tiles.oneline{grid-template-columns:repeat(5,minmax(0,1fr));}" in SRC
        assert SRC.count('class="tiles oneline"') == 2     # home + financial

    def test_financial_assets_link_to_their_plant_page(self):
        assert "`<a href=\"../${{k.toLowerCase()}}/\"" in SRC
        assert "m.type==='LaaS'?`<b title=" in SRC         # LaaS: no page, no link (v204: short name, full in title)

    def test_monitoring_mtd_pairs_production_with_expected_days(self):
        mon = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        # the Sep-2026 lesson: never divide full-month production by a
        # partial month of expectation, and None-expected must not
        # collapse to 0.0
        assert "f(r[3]) if r[3] not in ('', None) else None" in mon
        assert "'pm': pm" in mon
        assert "100*m.get('pm', 0)/m_exp" in mon
        assert mon.count("100.0 * mtd_pm / mtd_exp") == 2  # PPA + CAPEX


class TestRound5:
    def test_duplicate_actual_vs_contract_chart_removed(self):
        # duplicated the monthly production card right above it
        assert "Actual vs. contracted energy" not in SRC
        assert "Actual vs. expected energy" not in SRC
        # the 6-month table fed by the same data stays
        assert "Last 6 months" in SRC


