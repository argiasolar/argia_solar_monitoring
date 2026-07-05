"""Tests: HTML dashboard renderer + publish script.

Contract under test:
* the rendered file embeds the EXACT numbers it was given (no re-derivation
  that could drift from the Dashboard tabs / KPI truth)
* JSON embedding is <script>-safe
* the publish script coerces Sheets strings to numbers, respects dry-run,
  uploads with the right headers, and fails loudly on a bad HTTP status
"""

import datetime as dt
import importlib
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.report import dashboard_html as H

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
P = importlib.import_module("dashboard_html_publish")


def _plant_row(**kw):
    d = {"date_mx": "2026-07-02", "hour_label": "12:00", "plant_key": "GTO1",
         "customer": "Taigene", "kwp_dc": 818.33, "total_kwh": 555.6,
         "theoretical_kwh": 560.3, "cloud_cover_pct": 12.0,
         "inverters_total": 6, "inverters_reporting": 6,
         "inverters_faulted": 4}
    d.update(kw)
    return d


def _inv_row(**kw):
    d = {"date_mx": "2026-07-02", "hour_label": "12:00", "plant_key": "GTO1",
         "inverter_sn": "A", "inverter_label": "Inverter 1",
         "energy_kwh": 176.4, "temperature_c": 81.0, "status": "ONLINE",
         "status_reason": ""}
    d.update(kw)
    return d


def _extract_payload(html: str) -> dict:
    m = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                  html, re.S)
    assert m, "embedded data block missing"
    return json.loads(m.group(1))


# --- renderer -----------------------------------------------------------------

class TestRenderer:
    def test_payload_carries_exact_numbers(self):
        html = H.render([_plant_row()], [_inv_row()], generated_at="t")
        data = _extract_payload(html)
        assert data["plant_rows"][0]["total_kwh"] == 555.6
        assert data["plant_rows"][0]["theoretical_kwh"] == 560.3
        assert data["inverter_rows"][0]["energy_kwh"] == 176.4
        assert data["inverter_rows"][0]["status"] == "ONLINE"

    def test_only_contracted_fields_embedded(self):
        html = H.render([_plant_row(secret_col="X")], [_inv_row()],
                        generated_at="t")
        data = _extract_payload(html)
        assert "secret_col" not in data["plant_rows"][0]
        assert set(data["plant_rows"][0]) == set(H.PLANT_FIELDS)
        assert set(data["inverter_rows"][0]) == set(H.INVERTER_FIELDS)

    def test_script_close_tag_cannot_break_embedding(self):
        evil = _inv_row(status_reason="</script><script>alert(1)</script>")
        html = H.render([_plant_row()], [evil], generated_at="t")
        data = _extract_payload(html)   # parse still succeeds
        assert "alert(1)" in data["inverter_rows"][0]["status_reason"]

    def test_inactive_plants_excluded(self):
        rows = [_plant_row(), _plant_row(plant_key="QRO1", total_kwh=0)]
        html = H.render(rows, [], generated_at="t",
                        active_plants=["GTO1"])
        data = _extract_payload(html)
        assert data["plants"] == ["GTO1"]
        assert all(r["plant_key"] == "GTO1" for r in data["plant_rows"])

    def test_default_plants_are_those_with_production(self):
        rows = [_plant_row(), _plant_row(plant_key="QRO1", total_kwh=0)]
        html = H.render(rows, [], generated_at="t")
        assert _extract_payload(html)["plants"] == ["GTO1"]

    def test_every_status_has_a_color(self):
        html = H.render([_plant_row()], [_inv_row()], generated_at="t")
        colors = _extract_payload(html)["status_colors"]
        for st in ("ONLINE", "UNDERPERFORMING", "FAULT", "DERATED",
                   "OFFLINE", "IDLE_NIGHT", "NO_DATA"):
            assert st in colors


# --- publish script helpers -----------------------------------------------------

class TestPublishHelpers:
    def test_coerce_rows_turns_sheet_strings_into_numbers(self):
        rows = [{"total_kwh": "555.6", "theoretical_kwh": "", "plant_key": "GTO1"}]
        out = P.coerce_rows(rows, P.NUMERIC_PLANT)
        assert out[0]["total_kwh"] == 555.6
        assert out[0]["theoretical_kwh"] is None
        assert out[0]["plant_key"] == "GTO1"

    def test_active_plants_filters_config(self):
        cfg = [{"plant_key": "GTO1", "active": "TRUE"},
               {"plant_key": "QRO1", "active": "FALSE"},
               {"plant_key": "SLP1", "active": True}]
        assert P.active_plants(cfg) == ["GTO1", "SLP1"]


# --- publish run (mocked client + session) ---------------------------------------

def _tables():
    return {
        "Plants": [{"plant_key": "GTO1", "active": "TRUE"},
                   {"plant_key": "QRO1", "active": "FALSE"}],
        "Dashboard_Plant": [
            {k: str(v) for k, v in _plant_row().items()}],
        "Dashboard_Inverter": [
            {k: str(v) for k, v in _inv_row().items()}],
    }


def _client():
    c = MagicMock(spec=SheetsClient)
    tables = _tables()
    c.read_table.side_effect = lambda tab, rng="A1:Z": tables[tab]
    return c


class TestPublishRun:
    def test_dry_run_renders_but_never_uploads(self, tmp_path):
        out = tmp_path / "d.html"
        session = MagicMock()
        rc = P.run(_client(), out_path=str(out), apply=False,
                   bucket="argia-dashboard", session=session)
        assert rc == 0
        session.post.assert_not_called()
        data = _extract_payload(out.read_text(encoding="utf-8"))
        assert data["plants"] == ["GTO1"]          # QRO1 config-filtered
        assert data["plant_rows"][0]["total_kwh"] == 555.6  # coerced

    def test_apply_uploads_with_html_and_nocache_headers(self, tmp_path):
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=200)
        rc = P.run(_client(), out_path=str(tmp_path / "d.html"), apply=True,
                   bucket="argia-dashboard", session=session)
        assert rc == 0
        args, kwargs = session.post.call_args
        assert "b/argia-dashboard/o" in args[0]
        assert "name=dashboard.html" in args[0]
        assert kwargs["headers"]["Cache-Control"] == "no-cache"
        assert b"ARGIA SOLAR" in kwargs["data"]

    def test_apply_without_bucket_skips_gracefully(self, tmp_path):
        session = MagicMock()
        rc = P.run(_client(), out_path=str(tmp_path / "d.html"), apply=True,
                   bucket=None, session=session)
        assert rc == 0
        session.post.assert_not_called()

    def test_upload_failure_raises_loudly(self, tmp_path):
        session = MagicMock()
        session.post.return_value = MagicMock(status_code=403, text="denied")
        with pytest.raises(RuntimeError, match="403"):
            P.run(_client(), out_path=str(tmp_path / "d.html"), apply=True,
                  bucket="argia-dashboard", session=session)


class TestVisualRegressions:
    def test_series_palette_is_single_green_ramp(self):
        """User request 2026-07-04: stacked inverters of one plant use green
        shades, not a multicolor categorical palette."""
        import re as _re
        m = _re.search(r"var SERIES = \[([^\]]+)\]", H._TEMPLATE)
        colors = _re.findall(r"#[0-9A-Fa-f]{6}", m.group(1))
        greens = {"#0F6E56", "#5DCAA5", "#3B6D11", "#97C459",
                  "#085041", "#1D9E75", "#639922", "#9FE1CB"}
        assert set(colors) == greens

    def test_gauge_arc_never_uses_large_arc_flag(self):
        """Regression: a half-circle gauge sweeps <= 180 deg, so the SVG
        large-arc flag must be hard 0. frac>0.5 with large-arc 1 rendered
        the arc the long way around (broken blobs, 2026-07-04)."""
        assert "A70 70 0 0 1" in H._TEMPLATE          # fixed form present
        assert "large" not in H._TEMPLATE.split("function arc")[1].split("}")[0]


class TestFeatureRegressions20260704:
    """User-requested features, locked so edits can't drop them silently."""

    def test_portfolio_view_option_present(self):
        assert "__ALL__" in H._TEMPLATE
        assert "drawPortfolio" in H._TEMPLATE

    def test_inverters_naturally_sorted_1_to_n(self):
        assert "invSortKey" in H._TEMPLATE          # numeric-suffix sort

    def test_cloud_cover_line_on_secondary_percent_axis(self):
        assert "Cloud cover %" in H._TEMPLATE
        assert "y1" in H._TEMPLATE and "max: 100" in H._TEMPLATE
        assert "cloud_cover_pct" in H.PLANT_FIELDS  # data actually embedded

    def test_canvas_forced_to_high_dpr_for_sharpness(self):
        assert "devicePixelRatio" in H._TEMPLATE

    def test_portfolio_shows_availability_not_temperature(self):
        """User request: the all-plants view leads with fleet availability
        (reporting/expected inverters, daylight buckets); the per-plant view
        keeps the hottest-inverter gauge."""
        assert "Fleet availability" in H._TEMPLATE
        port = H._TEMPLATE.split("function drawPortfolio")[1].split(
            "function draw()")[0]
        assert "AVAIL_OK" in port            # operational, not comms-based
        assert "Hottest inverter" not in port
        plant = H._TEMPLATE.split("function drawPlant")[1].split(
            "function drawPortfolio")[0]
        assert "Hottest inverter" in plant


class TestFeatureRegressions20260705:
    def test_portfolio_table_has_availability_column_with_kwp_names(self):
        port = H._TEMPLATE.split("function drawPortfolio")[1].split(
            "function draw()")[0]
        assert "Availability" in port
        assert "kWp DC" in port                    # size in plant name, power unit

    def test_live_day_is_prorated_to_current_mx_hour(self):
        """User report 2026-07-05: today's Expected looked full-day-sized
        (likely forecast irradiance rows with future timestamps). The page
        must compare pace-vs-pace on the live day: both production and
        expected truncated to complete hours before the current MX hour.
        Completed days keep the full-day comparison."""
        assert "function cutLive" in H._TEMPLATE
        assert "mxTodayIso" in H._TEMPLATE
        assert "if (day !== mxTodayIso()) return rows;" in H._TEMPLATE
        # both draw paths apply the cut
        assert H._TEMPLATE.count("cutLive(") >= 4

    def test_portfolio_has_fleet_hourly_and_per_plant_charts(self):
        assert "Fleet hourly" in H._TEMPLATE
        assert 'id="chart2"' in H._TEMPLATE
        port = H._TEMPLATE.split("function drawPortfolio")[1]
        assert "newChart2" in port

    def test_expected_card_label_shows_cutoff_on_live_day(self):
        assert "Expected \\u00b7 to " in H._TEMPLATE or "Expected · to " in H._TEMPLATE


class TestIssuesAndAvailability20260705:
    def test_availability_is_operational_not_comms(self):
        """Two real incidents, 2026-07-05:
        (a) a fleet-wide 07:00 telemetry gap made every plant read exactly
            80% -> non-producing buckets are data gaps and stay excluded;
        (b) SAG Inverter 2 reports telemetry while producing 0 kWh and the
            plant read 100% -> availability must mean OPERATING (status
            ONLINE/UNDERPERFORMING/DERATED in producing buckets), so a
            chatty dead inverter counts unavailable."""
        port = H._TEMPLATE.split("function drawPortfolio")[1]
        assert "OPERATIONAL availability" in port
        assert "AVAIL_OK = { ONLINE: 1, UNDERPERFORMING: 1, DERATED: 1 }" in port
        assert "producing[r.hour_label]" in port
        # comms-based counting must be gone
        assert "rep += r.inverters_reporting" not in port

    def test_portfolio_surfaces_issues_not_just_faults(self):
        """MEX1 Inverter 2 OFFLINE and GTO1 Inverter 5 UNDERPERFORMING were
        invisible on the overview because only FAULT was counted."""
        assert "ISSUE_STATUSES" in H._TEMPLATE
        for st in ("FAULT", "OFFLINE", "DERATED", "UNDERPERFORMING"):
            assert st + ": 1" in H._TEMPLATE
        assert "Inverters with issues" in H._TEMPLATE
        assert ">Issues</th>" in H._TEMPLATE

    def test_default_day_is_today_with_stale_fallback(self):
        """User request 2026-07-05: open on TODAY (live ops view; the
        pro-rating banner covers the estimate caveat). A stale copy without
        today falls back to its newest day instead of an empty page."""
        assert "daySel.value = days.indexOf(todayIso) >= 0" in H._TEMPLATE
        assert "todayIso : maxDay" in H._TEMPLATE


class TestLossAndInverterAvailability20260705:
    def test_loss_fields_embedded(self):
        assert "est_loss_kwh" in H.INVERTER_FIELDS
        assert "tariff_mxn_per_kwh" in H.PLANT_FIELDS

    def test_plant_table_has_avail_and_loss_columns(self):
        plant = H._TEMPLATE.split("function drawPlant")[1].split(
            "function drawPortfolio")[0]
        assert '>Avail</th>' in plant and '>Loss</th>' in plant
        assert "AVAIL_OK_SET" in H._TEMPLATE   # same rule as portfolio

    def test_loss_shows_kwh_until_tariff_set(self):
        """tariff_mxn_per_kwh is still EMPTY in Plants — the page must
        degrade to kWh with a hint, never invent pesos."""
        assert "set tariff_mxn_per_kwh for MXN" in H._TEMPLATE
        assert "tariffs incomplete" in H._TEMPLATE


class TestLogoAndAudit20260705:
    def test_logo_replaces_text_header(self):
        html = H.render([_plant_row()], [_inv_row()], generated_at="t")
        assert "ARGIA SOLAR — plant dashboard" not in html
        assert 'alt="ARGIA SOLAR"' in html
        assert "data:image/png;base64," in html
        assert len(H.LOGO_B64) > 10000          # a real image, not a stub

    def test_audit_footer_explains_every_headline_number(self):
        html = H.render([_plant_row()], [_inv_row()], generated_at="t")
        assert "How these numbers are calculated" in html
        for term in ("Production kWh", "Expected kWh",
                     "Availability (operational)", "Status",
                     "Est. loss (unavailability)"):
            assert term in html
        # the honest caveats must be in the audit text
        assert "carryover" in html
        assert "&plusmn;10%" in html
        assert "NOT" in html                     # loss exclusion stated
