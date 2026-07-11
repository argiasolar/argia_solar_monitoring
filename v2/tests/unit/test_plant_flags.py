"""v74 plant visibility flags.

The two safety rules this feature lives or dies by, both pinned here:

  1. Report flags NEVER touch data capture — a plant hidden from every
     report still collects telemetry, stamps KPIs and raises alerts
     (`active` is the only machine-axis flag).
  2. Unknown values never silently hide anything: an unrecognized
     portfolio label is kept with a warning; an unrecognized show_*
     value resolves to VISIBLE with a warning.
"""

from argia.core.config import (
    KNOWN_PORTFOLIOS, PlantConfig, Portfolio, _flag_default_true,
    _portfolio_label,
)
from argia.report.dashboard import parse_plants


def _plant(pk="GTO1", **kw):
    base = dict(
        plant_key=pk, customer="c", brand="growatt", site_id="1",
        kwp_dc=100.0, kwp_ac=90.0, lat=None, lon=None,
        expected_factor=0.8, pr_target=0.8, installation_date="",
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True)
    base.update(kw)
    return PlantConfig(**base)


def _portfolio(*plants):
    p = Portfolio()
    for pl in plants:
        p.plants[pl.plant_key] = pl
    return p


class TestFlagParsing:
    def test_blank_means_visible_everywhere(self):
        p = _plant()
        assert p.portfolio == "PPA"
        assert p.show_dashboard and p.show_daily_report \
            and p.show_financial

    def test_flag_default_true_truth_table(self):
        assert _flag_default_true("", "c", "P") is True
        assert _flag_default_true(None, "c", "P") is True
        assert _flag_default_true("FALSE", "c", "P") is False
        assert _flag_default_true("no", "c", "P") is False
        assert _flag_default_true("0", "c", "P") is False
        assert _flag_default_true("TRUE", "c", "P") is True

    def test_unrecognized_flag_value_warns_and_shows(self, caplog):
        with caplog.at_level("WARNING"):
            assert _flag_default_true("maybe", "show_financial",
                                      "GTO1") is True
        assert "unrecognized" in caplog.text

    def test_portfolio_labels(self, caplog):
        assert _portfolio_label("", "P") == "PPA"
        assert _portfolio_label("capex", "P") == "CAPEX"
        assert _portfolio_label("PROLOGIS", "P") == "PROLOGIS"
        with caplog.at_level("WARNING"):
            assert _portfolio_label("WINDFARM", "P") == "WINDFARM"
        assert "unknown label" in caplog.text
        assert set(KNOWN_PORTFOLIOS) == {"PPA", "CAPEX", "PROLOGIS"}


class TestSafetyRuleOne:
    """Hiding from every report must not stop data capture."""

    def test_hidden_plant_stays_on_machine_axis(self):
        hidden = _plant("GTO2", show_dashboard=False,
                        show_daily_report=False, show_financial=False)
        pf = _portfolio(hidden)
        assert [p.plant_key for p in pf.active_plants()] == ["GTO2"]
        assert pf.dashboard_plants() == []
        assert pf.daily_report_plants() == []
        assert pf.financial_plants() == []

    def test_inactive_trumps_all_flags(self):
        dead = _plant("QRO1", active=False)   # flags default TRUE
        pf = _portfolio(dead)
        assert pf.active_plants() == []
        assert pf.dashboard_plants() == []
        assert pf.daily_report_plants() == []
        assert pf.financial_plants() == []

    def test_capex_label_alone_changes_nothing(self):
        capex = _plant("GTO2", portfolio="CAPEX")
        pf = _portfolio(capex)
        # label is pure: still on every surface until a show_* says no
        assert pf.financial_plants()[0].plant_key == "GTO2"
        assert pf.daily_report_plants()[0].plant_key == "GTO2"


class TestPerSurfaceFilters:
    def test_each_flag_filters_only_its_surface(self):
        p = _plant("MEX3", show_financial=False)
        pf = _portfolio(p)
        assert pf.dashboard_plants()[0].plant_key == "MEX3"
        assert pf.daily_report_plants()[0].plant_key == "MEX3"
        assert pf.financial_plants() == []

    def test_dashboard_parse_builds_all_active_plants(self):
        """Rewritten for v84 (was: parse skips show_dashboard=FALSE).
        The Dashboard tabs are now the STORE of live-computed metrics
        for ALL active plants — per-client pages consume CAPEX rows —
        and show_dashboard filters at render time in the publisher
        (rows and selector), keeping the internal page pure-PPA."""
        rows = [
            {"plant_key": "A", "active": "TRUE", "kwp_dc": "100",
             "expected_factor": "0.8", "show_dashboard": "FALSE"},
            {"plant_key": "B", "active": "TRUE", "kwp_dc": "100",
             "expected_factor": "0.8", "show_dashboard": ""},
            {"plant_key": "C", "active": "FALSE", "kwp_dc": "100",
             "expected_factor": "0.8"},   # machine axis still gates
        ]
        out = parse_plants(rows)
        assert "A" in out and "B" in out
        assert "C" not in out

    def test_publisher_filters_rows_to_visible_set(self):
        """v84 companion: the internal page must not EMBED hidden
        plants' rows — filtering the selector alone would still ship
        CAPEX data in the payload."""
        import inspect
        import scripts.dashboard_html_publish as P
        src = inspect.getsource(P.run)
        assert 'r.get("plant_key", "")) in visible' in src
