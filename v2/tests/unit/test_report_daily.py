"""Tests for the daily report (report family, part 1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.core.alerts_state import AlertRecord, AlertState
from argia.core.drive import DriveClient
from argia.report.daily import (
    AMBER,
    GRAY,
    GREEN,
    RED,
    InverterDay,
    PlantDay,
    ReportData,
    allocate_theoretical,
    inverter_dot,
    plant_semaphore,
    render_html,
    svg_inverter_bars,
    short_name,
    svg_fleet_bars,
    portfolio_semaphore,
    fleet_stats,
    summary_sentence,
)


def _plant(pk="SLP1", pp=1.05, av=1.0, dc="full", **kw):
    return PlantDay(plant_key=pk, name=kw.get("name", pk),
                    energy_kwh=kw.get("e", 1000.0),
                    expected_kwh=kw.get("x", 950.0), production_pct=pp,
                    pr=0.8, availability=av, soiling=kw.get("soil"),
                    cloud_pct=40.0, data_class=dc,
                    status_note=kw.get("note", "On plan."),
                    inverters=kw.get("inv", []),
                    kwp_dc=kw.get("kwp_dc"),
                    tariff_mxn_per_kwh=kw.get("tariff_mxn_per_kwh"))


def _inv(sn="A", kwh=500.0, rated=124.0, t=50.0, faults=(), rel=None):
    return InverterDay(sn=sn, label=sn, kwh=kwh, rated_kw=rated,
                       tmax_c=t, faults=list(faults), rel=rel)


class TestPlantSemaphore:
    def test_green_amber_red_bands(self):
        assert plant_semaphore(_plant(pp=1.00), False, False) == GREEN
        assert plant_semaphore(_plant(pp=0.90), False, False) == AMBER
        assert plant_semaphore(_plant(pp=0.75), False, False) == RED

    def test_alert_presence_colors(self):
        assert plant_semaphore(_plant(pp=1.05), False, True) == AMBER
        assert plant_semaphore(_plant(pp=1.05), True, True) == RED

    def test_low_availability_is_red(self):
        assert plant_semaphore(_plant(pp=1.0, av=0.78), False, False) == RED

    def test_untrustworthy_day_is_gray(self):
        assert plant_semaphore(_plant(pp=None), False, False) == GRAY
        assert plant_semaphore(_plant(dc="partial"), False, False) == GRAY


class TestInverterDot:
    def test_bands(self):
        assert inverter_dot(_inv()) == GREEN
        assert inverter_dot(_inv(t=66.0)) == AMBER
        assert inverter_dot(_inv(t=75.4)) == RED           # real NL1 case
        assert inverter_dot(_inv(faults=["FT=302"])) == AMBER
        assert inverter_dot(_inv(rel=("WARNING", 0.75))) == AMBER
        assert inverter_dot(_inv(rel=("CRITICAL", 0.58))) == RED


class TestAllocateTheoretical:
    def test_nameplate_shares_sum_exactly(self):
        invs = [_inv("A", rated=124.0), _inv("B", rated=124.0),
                _inv("C", rated=60.0)]
        alloc = allocate_theoretical(4978.0, invs)
        assert round(sum(alloc.values()), 1) == 4978.0
        assert alloc["A"] == alloc["B"]                    # equal nameplates
        assert alloc["C"] < alloc["A"]                     # smaller unit

    def test_missing_inputs_empty(self):
        assert allocate_theoretical(None, [_inv()]) == {}
        assert allocate_theoretical(1000.0, []) == {}
        assert allocate_theoretical(
            1000.0, [_inv(rated=None), _inv(rated=0)]) == {}


class TestRenderSmoke:
    def _data(self):
        gto = _plant("GTO1", pp=0.7497, av=0.7833, e=3731.9, x=4977.98,
                     note="Below plan (75%) — inverter availability 78% "
                          "— see Alerts",
                     inv=[_inv("JFM7DXN00T", 899.5),
                          _inv("JFM7DXN013", 432.8, t=49.9,
                               faults=["FT=302"], rel=("CRITICAL", 0.584))])
        alert = AlertRecord(
            alert_id="ALT-20260703-005",
            alert_key="gto1:inv:jfm7dxn013:inverter_fault",
            plant_key="GTO1", inverter_sn="JFM7DXN013",
            metric="inverter_fault", severity="CRITICAL",
            state=AlertState.OPEN, opened_utc="2026-07-03T20:58:45+00:00",
            last_seen_utc="2026-07-03T21:06:50+00:00", resolved_utc="",
            value=4.0, threshold=None,
            message="GTO1 JFM7DXN013: vendor fault FT=302 (x4)",
            channels_sent="",
            explanation="Needs attention now. The inverter itself reported "
                        "a fault code.")
        return ReportData(date_iso="2026-07-02", plants=[gto],
                          alerts=[alert])

    def test_html_contains_every_layer(self):
        html = render_html(self._data())
        # header + rail
        assert "2026-07-02" in html and 'class="rail"' in html
        # status note verbatim (the words column, rendered)
        assert "inverter availability 78%" in html
        # alert with plain-language explanation
        assert "vendor fault FT=302 (x4)" in html
        assert "The inverter itself reported" in html
        # inverter table with flags and allocation
        assert "JFM7DXN013" in html and "FT=302" in html
        assert "peer median" in html                       # chart annotation
        # honesty footer — updated with the dense-irradiance rollout
        assert "nameplate share" in html
        assert "minute-scale history" in html
        assert "validated to &lt;1%" in html

    def test_no_alerts_renders_placeholder(self):
        d = self._data()
        d.alerts.clear()
        assert "No open alerts." in render_html(d)

    def test_inverter_chart_skips_unrated(self):
        p = _plant(inv=[_inv(rated=None)])
        assert svg_inverter_bars(p) == ""


class TestDriveUpload:
    def _svc(self):
        return MagicMock()

    def test_upload_new_file_request_shape(self, tmp_path):
        f = tmp_path / "r.pdf"
        f.write_bytes(b"%PDF-1.4 test")
        svc = self._svc()
        svc.files().list().execute.return_value = {"files": []}
        svc.files().create().execute.return_value = {"id": "NEW"}
        d = DriveClient(service=svc)
        assert d.upload_file("FOLDER", "r.pdf", str(f),
                             "application/pdf") == "NEW"
        body = svc.files().create.call_args.kwargs["body"]
        assert body == {"name": "r.pdf", "parents": ["FOLDER"]}
        assert svc.files().create.call_args.kwargs["supportsAllDrives"]

    def test_upload_existing_updates_in_place(self, tmp_path):
        f = tmp_path / "r.pdf"
        f.write_bytes(b"%PDF-1.4 test")
        svc = self._svc()
        svc.files().list().execute.return_value = {
            "files": [{"id": "OLD"}]}
        d = DriveClient(service=svc)
        assert d.upload_file("FOLDER", "r.pdf", str(f),
                             "application/pdf") == "OLD"
        assert svc.files().update.call_args.kwargs["fileId"] == "OLD"
        svc.files().create.assert_not_called()

    def test_ensure_folder_reuses_existing(self):
        svc = self._svc()
        svc.files().list().execute.return_value = {
            "files": [{"id": "FID"}]}
        d = DriveClient(service=svc)
        assert d.ensure_folder("PARENT", "Reports") == "FID"
        svc.files().create.assert_not_called()


class TestDashboardFamilyStyle20260707:
    """The PDF is the customer-facing sibling of the dashboard — one
    visual language. Also: no external font fetch inside the PDF-printing
    Chromium, so the PDF renders identically offline."""

    def test_lockup_and_shared_logo(self):
        html = render_html(TestRenderSmoke()._data())
        assert "PERFORMANCE&nbsp;REPORT" in html
        assert "data:image/png;base64," in html
        from argia.report.dashboard_html import LOGO_B64
        assert LOGO_B64[:40] in html            # the SAME logo asset

    def test_dashboard_palette_and_no_webfonts(self):
        html = render_html(TestRenderSmoke()._data())
        assert "#0E8A6D" in html and "#f4f3ef" in html
        assert "fonts.googleapis.com" not in html
        assert "IBM Plex" not in html


class TestMedianLabelPlacement20260707:
    """User-reported: 'peer median' overlapped the last bar's value text.
    The label now hangs BELOW the chart and flips sides near the right
    edge — collision-impossible by construction."""

    def test_label_below_bars_and_height_extended(self):
        p = _plant(inv=[_inv(sn="A", kwh=700, rated=100),
                        _inv(sn="B", kwh=690, rated=100)])
        svg = svg_inverter_bars(p)
        h = 2 * 34
        assert f'y="{h + 13}"' in svg            # below the last bar row
        assert f'viewBox="0 0 660 {h + 18}"' in svg

    def test_label_flips_left_when_median_near_right_edge(self):
        # both inverters at the same yield -> median line at the bar tip,
        # far right: the exact collision case from the screenshot
        p = _plant(inv=[_inv(sn="A", kwh=700, rated=100),
                        _inv(sn="B", kwh=700, rated=100)])
        assert 'text-anchor="end">peer median' in svg_inverter_bars(p)

    def test_label_stays_right_of_line_when_median_left(self):
        p = _plant(inv=[_inv(sn="A", kwh=700, rated=100),
                        _inv(sn="B", kwh=100, rated=100)])
        assert 'text-anchor="start">peer median' in svg_inverter_bars(p)


class TestPlantNamesAndCaptionClipping20260707:
    def test_short_name_trim_rules_match_dashboard(self):
        assert short_name(_plant(name="HOLIDAY INN EXPRESS, Turistica "
                                 "Arizona PPA roof (SLP, SLP)")) == \
            "HOLIDAY INN EXPRESS"
        assert short_name(_plant(name="TAIGENE PPA roof (Leon, GTO)")) == \
            "TAIGENE"
        assert short_name(_plant(pk="GTO1", name="")) == "GTO1"  # key fallback

    def test_rail_and_bars_show_names_not_keys(self):
        d = TestRenderSmoke()._data()
        d.plants[0].name = "TAIGENE PPA roof (Leon, GTO)"
        html = render_html(d)
        rail = html.split('class="rail"')[1].split("</div></div>")[0]
        assert "TAIGENE" in rail
        assert ">GTO1<" not in html.split("aria-label")[1].split("</svg>")[0]

    def test_caption_cannot_clip(self):
        """User screenshot 2026-07-05: GTO1's '... kWh · 87%' was cut at
        the viewBox edge. Worst case = full-width outline + longest
        caption must fit inside the viewBox."""
        p = _plant(name="X", e=88888.0, x=99999.0, pp=0.87)
        svg = svg_fleet_bars([p], {p.plant_key: GREEN})
        import re
        view_w = int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
        text_x = max(float(m) for m in
                     re.findall(r'<text x="(\d+)" y="19" class="axv"', svg))
        caption = "88,888 / 99,999 kWh · 87%"
        assert 200 + text_x + len(caption) * 6.6 <= view_w


def test_fleet_summary_is_prominent():
    """User request 2026-07-07: the fleet line under the tiles was 13px
    muted — now a 16px card strip with bold key numbers."""
    html = render_html(TestRenderSmoke()._data())
    assert "font-size:16px" in html.split(".fleetline{")[1].split("}")[0]
    assert "<b>" in html.split('class="portnums"')[1].split("</div>")[0]


class TestPortfolioSemaphore20260707:
    def _plants(self):
        return [_plant(pk="GTO1", name="TAIGENE PPA"),
                _plant(pk="SLP1", name="QUIMICA PPA")]

    def test_worst_plant_dominates_and_is_named(self):
        c, title, why = portfolio_semaphore(
            self._plants(), {"GTO1": RED, "SLP1": GREEN}, 0, 0, 1.0)
        assert (c, title) == (RED, "ATTENTION")
        assert "TAIGENE" in why and "QUIMICA" not in why

    def test_critical_alert_forces_red_even_if_plants_green(self):
        c, title, _ = portfolio_semaphore(
            self._plants(), {"GTO1": GREEN, "SLP1": GREEN}, 1, 0, 1.02)
        assert (c, title) == (RED, "ATTENTION")

    def test_fleet_below_85_forces_red(self):
        c, *_ = portfolio_semaphore(
            self._plants(), {"GTO1": GREEN, "SLP1": GREEN}, 0, 0, 0.80)
        assert c == RED

    def test_amber_band(self):
        c, title, why = portfolio_semaphore(
            self._plants(), {"GTO1": AMBER, "SLP1": GREEN}, 0, 0, 0.97)
        assert (c, title) == (AMBER, "WATCH")
        assert "watch: TAIGENE" in why

    def test_all_green(self):
        c, title, why = portfolio_semaphore(
            self._plants(), {"GTO1": GREEN, "SLP1": GREEN}, 0, 0, 1.0)
        assert (c, title) == (GREEN, "ON PLAN")
        assert "all 2 plants on plan" in why

    def test_all_gray_is_incomplete(self):
        c, title, _ = portfolio_semaphore(
            self._plants(), {"GTO1": GRAY, "SLP1": GRAY}, 0, 0, None)
        assert (c, title) == (GRAY, "INCOMPLETE DAY")

    def test_rendered_block_present(self):
        html = render_html(TestRenderSmoke()._data())
        assert "PORTFOLIO:" in html and 'class="portlamp' in html


class TestPortfolioSummary20260707:
    """User review: the report led with issues; the business overview
    (production, availability, size, income, CO2) hid in a text strip.
    Now a Portfolio summary section renders FIRST."""

    def _plants(self):
        return [_plant(pk="GTO1", name="TAIGENE PPA", e=2559.0, x=2955.0,
                       pp=0.87, av=0.65, kwp_dc=818.0,
                       tariff_mxn_per_kwh=1.975),
                _plant(pk="SLP1", name="QUIMICA PPA", e=1006.0, x=1068.0,
                       pp=0.94, av=1.0, kwp_dc=189.0,
                       tariff_mxn_per_kwh=2.596)]

    def test_fleet_stats_math(self):
        st = fleet_stats(self._plants())
        assert st["production_kwh"] == 3565.0
        assert st["kwp"] == 1007.0
        # kWp-weighted availability: (0.65*818 + 1.0*189) / 1007
        assert st["availability"] == pytest.approx(0.7157, abs=1e-3)
        assert st["income_mxn"] == pytest.approx(
            2559 * 1.975 + 1006 * 2.596, rel=1e-6)
        assert st["co2_kg"] == pytest.approx(3565 * 0.435, rel=1e-6)

    def test_income_skips_missing_tariff_not_energy(self):
        plants = self._plants()
        plants[1].tariff_mxn_per_kwh = None
        st = fleet_stats(plants)
        assert st["income_mxn"] == pytest.approx(2559 * 1.975, rel=1e-6)
        assert st["production_kwh"] == 3565.0   # energy still counts

    def test_sentence_carries_verdict_numbers_and_offenders(self):
        st = fleet_stats(self._plants())
        s = summary_sentence(st, "ATTENTION", "below plan: TAIGENE")
        assert s.startswith("ATTENTION: the portfolio produced 3,565 kWh")
        assert "72% availability" in s
        assert "t CO\u2082 avoided" in s
        assert "(below plan: TAIGENE.)" in s
        clean = summary_sentence(st, "ON PLAN", "all on plan")
        assert "(" not in clean          # clean day: no offender clause

    def test_summary_renders_before_the_tiles(self):
        html = render_html(TestRenderSmoke()._data())
        assert 'class="portsummary"' in html
        assert html.index('class="portsummary"') < html.index('class="rail"')
        for label in ("Portfolio size", "Income (est.)", "CO&#8322; avoided"):
            assert label in html


def test_rail_is_single_row_grid():
    """User review on A4: long customer names made the flex rail wrap to
    two rows in the PDF while the browser fit one. The rail is now a
    grid with one equal column per plant — always one row, names wrap
    INSIDE their tile, robust to fleet growth."""
    html = render_html(TestRenderSmoke()._data())
    assert "grid" in html.split(".rail{")[1].split("}")[0]
    # WeasyPrint does NOT support auto-fit (measured: 6 tiles collapsed
    # to 6 rows) — the column count is injected per render instead
    assert "auto-fit" not in html
    n = len(TestRenderSmoke()._data().plants)
    assert f'style="grid-template-columns:repeat({n},1fr)"' in html
    assert "min-width:110px" not in html


class TestOutboxChannel20260707:
    """Four recipient lists (om/reporting/shareholders/invoicing) are
    routed by the notifier via a channel column on Report_Outbox rows.
    Daily reports default to 'reporting'; future monthly/invoicing jobs
    just pass their channel — the notifier needs no further changes."""

    def _sheets(self):
        from argia.core.sheets import SheetsClient
        return MagicMock(spec=SheetsClient)

    def test_header_and_default_channel(self):
        from scripts.report_daily import OUTBOX_HEADER, append_outbox
        assert OUTBOX_HEADER[-1] == "channel"
        sheets = self._sheets()
        append_outbox(sheets, date_iso="2026-07-07", kind="morning_yesterday",
                      pdf_file_id="p1", html_file_id="h1",
                      now_utc_iso="t")
        row = sheets.append_rows.call_args[0][1][0]
        assert row[-1] == "reporting" and len(row) == len(OUTBOX_HEADER)

    def test_future_jobs_pass_their_channel(self):
        from scripts.report_daily import append_outbox
        sheets = self._sheets()
        append_outbox(sheets, date_iso="2026-07-31", kind="monthly",
                      pdf_file_id="p", html_file_id=None,
                      now_utc_iso="t", channel="shareholders")
        assert sheets.append_rows.call_args[0][1][0][-1] == "shareholders"
