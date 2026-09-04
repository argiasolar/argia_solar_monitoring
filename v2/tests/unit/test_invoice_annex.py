"""Invoice annex (v93) tests.

The load-bearing property: energía compensada is NEVER recomputed here —
it is ``billable_kwh - energy_kwh`` straight from KPI_Daily (the v91
deemed engine), so the customer annex and the finance report cannot
disagree. The pure ``rollup_month`` is the reference the embedded JS
mirrors; the truth table pins measured/deemed/billing/performance and
the no-data-day handling.
"""

import pathlib
from unittest.mock import MagicMock

import pytest

from argia.core.config import PlantConfig, Portfolio
from argia.finance.annex import (
    ATOM_WIDTH, annual_rollup, build_annex_data, render_annex_html,
    rollup_month,
)
from argia.finance.income import Period


def _payload(atoms=None, days=None, tariffs=None):
    return {
        "plant_key": "MEX2", "client": "Faurecia", "kwp": 610,
        "days": days or ["2026-06-30", "2026-07-01", "2026-07-02"],
        "atoms": atoms or [
            [1000.0, 1100.0, 1050.0, 0.0, 0.3, 0.86, 1.0, 0.02],
            [900.0, 1100.0, 1050.0, 200.0, 0.4, 0.84, 1.0, 0.03],
            [None] * ATOM_WIDTH,
        ],
        "tariff_by_month": tariffs or {"2026-06": 2.367, "2026-07": 2.367},
        "co2_factor": 0.438,
    }


class TestRollupMonth:
    def test_measured_deemed_billable_and_amount(self):
        r = rollup_month(_payload(), "2026-07")
        assert r["measured_kwh"] == 900.0
        assert r["deemed_kwh"] == 200.0
        assert r["billable_kwh"] == 1100.0
        assert r["amount_mxn"] == pytest.approx(1100.0 * 2.367, abs=0.01)
        assert r["co2_kg"] == pytest.approx(1100.0 * 0.438, abs=0.1)

    def test_no_events_means_zero_compensada(self):
        assert rollup_month(_payload(), "2026-06")["deemed_kwh"] == 0.0

    def test_expected_comes_from_design_atoms_without_history(self):
        r = rollup_month(_payload(), "2026-07")
        assert r["expected_kwh"] == pytest.approx(1050.0)

    def test_history_wins_over_atoms_for_an_invoiced_month(self):
        """v158: an invoiced month reads the ARGIA Solar workbook —
        the factura must equal what was billed, not a recomputation."""
        p = _payload()
        p["history"] = {"2026-07": {"kwh": 950.0, "penalty": 25.0,
                                    "income": 2308.5, "expected": 1100.0}}
        r = rollup_month(p, "2026-07")
        assert r["measured_kwh"] == 950.0
        assert r["deemed_kwh"] == 25.0
        assert r["billable_kwh"] == 975.0
        assert r["amount_mxn"] == 2308.5        # NOT billable*tariff
        assert r["expected_kwh"] == 1100.0

    def test_a_future_history_month_keeps_expected_only(self):
        """Sep..Dec rows carry expected but no kwh — the annual chart
        shows the grey expectativa bar, the table shows zeros."""
        p = _payload()
        p["history"] = {"2026-07": {"kwh": None, "penalty": 0.0,
                                    "income": 0.0, "expected": 1234.0}}
        r = rollup_month(p, "2026-07")
        assert r["expected_kwh"] == 1234.0
        assert r["measured_kwh"] == 900.0       # atoms still count

    def test_no_tariff_leaves_amount_none(self):
        p = _payload(tariffs={"2026-06": 2.367, "2026-07": None})
        assert rollup_month(p, "2026-07")["amount_mxn"] is None

    def test_has_data_flag(self):
        p = _payload()
        assert rollup_month(p, "2026-07")["has_data"] is True
        # a month entirely out of range
        assert rollup_month(p, "2026-01")["has_data"] is False


class TestCloudFraction:
    """Sheet stores percent; the chart speaks fractions. Unscaled, the
    daily chart painted every month as 100% cloud."""

    def test_percent_values_become_fractions(self):
        from argia.finance.annex import _cloud_fraction
        assert _cloud_fraction(51.8) == pytest.approx(0.518)
        assert _cloud_fraction(99.13) == pytest.approx(0.9913)

    def test_fractions_pass_through(self):
        from argia.finance.annex import _cloud_fraction
        assert _cloud_fraction(0.45) == 0.45
        assert _cloud_fraction(1.0) == 1.0

    def test_none_stays_none(self):
        from argia.finance.annex import _cloud_fraction
        assert _cloud_fraction(None) is None


class TestAnnualRollup:
    def test_all_twelve_months_like_the_old_factura(self):
        """January through December, zeros included — the customer
        table always shows the whole year."""
        rows = annual_rollup(_payload())
        assert [r["ym"] for r in rows] == [
            "2026-%02d" % m for m in range(1, 13)]
        assert not rows[0]["has_data"]           # January: no data
        assert rows[5]["has_data"]               # June: data

    def test_totals_add_up(self):
        rows = annual_rollup(_payload())
        assert sum(r["deemed_kwh"] for r in rows) == 200.0
        assert sum(r["measured_kwh"] for r in rows) == 1900.0


# ---- build_annex_data (reads KPI_Daily + Contract_Monthly) ----

def _plant():
    return PlantConfig(
        plant_key="MEX2", customer="Faurecia", brand="growatt",
        site_id="1", kwp_dc=610.0, kwp_ac=450.0, lat=None, lon=None,
        expected_factor=0.8, pr_target=0.85, installation_date="",
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True, tariff_mxn_per_kwh=2.367)


def _sheets():
    kpi = [
        ["date_iso", "plant_key", "energy_kwh", "billable_kwh",
         "expected_kwh", "design_kwh", "pr", "availability",
         "soiling_loss_pct", "cloud_coverage_pct"],
        # normal day: billable == energy → deemed 0
        ["2026-07-01", "MEX2", "1000", "1000", "1100", "1050",
         "0.86", "1.0", "0.02", "0.3"],
        # compensada day: billable 1400 > energy 200 → deemed 1200
        ["2026-07-02", "MEX2", "200", "1400", "1100", "1050",
         "0.10", "0.0", "0.02", "0.9"],
        # other plant — must be ignored
        ["2026-07-01", "SLP1", "500", "500", "600", "580",
         "0.83", "1.0", "0.01", "0.2"],
    ]
    contract = [
        ["plant_key", "year", "month", "design_kwh", "contract_kwh",
         "tariff_mxn", "fixed_income_ccy", "ccy"],
        ["MEX2", "2026", "7", "1050", "89879", "2.367", "", ""],
    ]

    def read_range(tab, a1="A1:Z"):
        if tab == "KPI_Daily":
            return kpi
        if tab == "Contract_Monthly":
            return contract
        return []

    sc = MagicMock()
    sc.read_range.side_effect = read_range
    return sc


def _portfolio():
    p = MagicMock(spec=Portfolio)
    p.plants = {"MEX2": _plant()}
    return p


class TestBuildAnnexData:
    WIN = Period.from_iso("2026-07-01", "2026-07-03")

    def test_deemed_is_billable_minus_energy(self):
        data = build_annex_data(_sheets(), _portfolio(), "MEX2", self.WIN)
        # day index 0 = 2026-07-01 (deemed 0), 1 = 2026-07-02 (deemed 1200)
        assert data["days"][0] == "2026-07-01"
        assert data["atoms"][0][3] == 0.0          # A_DEEMED
        assert data["atoms"][1][3] == 1200.0       # 1400 - 200

    def test_other_plant_excluded(self):
        data = build_annex_data(_sheets(), _portfolio(), "MEX2", self.WIN)
        # SLP1 row must not leak — MEX2 day-1 measured is 1000, not 500
        assert data["atoms"][0][0] == 1000.0

    def test_dense_day_axis_with_gaps(self):
        data = build_annex_data(_sheets(), _portfolio(), "MEX2", self.WIN)
        assert data["days"] == ["2026-07-01", "2026-07-02", "2026-07-03"]
        # 2026-07-03 had no KPI row → all-None atom
        assert data["atoms"][2] == [None] * ATOM_WIDTH

    def test_tariff_from_contract(self):
        data = build_annex_data(_sheets(), _portfolio(), "MEX2", self.WIN)
        assert data["tariff_by_month"]["2026-07"] == 2.367

    def test_unknown_plant_raises(self):
        with pytest.raises(ValueError):
            build_annex_data(_sheets(), _portfolio(), "NOPE", self.WIN)

    def test_end_to_end_rollup_matches(self):
        data = build_annex_data(_sheets(), _portfolio(), "MEX2", self.WIN)
        r = rollup_month(data, "2026-07")
        # measured 1000+200=1200, deemed 0+1200=1200, billable 2400
        assert r["measured_kwh"] == 1200.0
        assert r["deemed_kwh"] == 1200.0
        assert r["amount_mxn"] == pytest.approx(2400.0 * 2.367, abs=0.01)


class TestRenderAnnexHtml:
    def _html(self):
        data = build_annex_data(
            _sheets(), _portfolio(), "MEX2",
            Period.from_iso("2026-07-01", "2026-07-03"))
        return render_annex_html(data, "2026-07-12 10:00 MX")

    def test_contains_client_and_sections(self):
        h = self._html()
        assert "Faurecia" in h
        assert "anexo de la factura" in h           # Looker header text
        assert "Generaci\u00f3n Fotovoltaica (kWh)" in h
        assert "Generaci\u00f3n Fotovoltaica Anual" in h
        assert "Generaci\u00f3n Anual" in h
        assert "window.print()" in h                # Descargar → print/PDF

    def test_the_pr_diario_chart_is_gone(self):
        """Tomasz 2026-09-01: "do not show Performance ratio diario,
        it is a noise"."""
        h = self._html()
        assert "Performance ratio" not in h
        assert "chart_pr" not in h

    def test_landscape_print(self):
        assert "size:A4 landscape" in self._html()

    def test_the_annual_chart_exists(self):
        h = self._html()
        assert "chart_annual" in h and "annualChart" in h

    def test_cloud_cover_rides_the_daily_chart(self):
        h = self._html()
        assert "Cobertura de nubes" in h

    def test_embeds_atoms_and_default_month(self):
        h = self._html()
        assert '"MEX2"' in h and '"tariff_by_month"' in h
        # default selected month is the latest with data
        assert 'sel.value="2026-07"' in h

    def test_no_footer_text_single_page(self):
        """Tomasz 2026-09-01: "fit everything into single page (remove
        the bottom explanation text)" — the methodology paragraph is
        gone entirely."""
        h = self._html()
        assert "registro de facturaci" not in h
        assert "IVA se aplica" not in h
        assert 'class="foot"' not in h

    def test_no_plant_key_on_the_customer_document(self):
        """GTO1/MEX2/... are internal codes; the visible document must
        not carry them (the embedded data payload may)."""
        h = self._html()
        assert '<div class="sub">energ' in h
        import re
        # the subtitle line must not lead with the plant key
        assert not re.search(r'class="sub">\s*MEX2', h)

    def test_header_text_is_readable(self):
        h = self._html()
        assert "font-size:19px" in h        # the anexo title


class TestCo2FactorOnTheAnnex:
    """v186 — the annex carries a factor register, not one scalar, so a
    2023 month is billed at 2023's factor and SAG gets the 0.202 they
    contracted for their whole history."""

    def _pl(self, pk):
        from argia.core import co2
        p = _payload()
        p["plant_key"] = pk
        p["co2_factor"] = co2.factor(None, pk)
        p["co2_factor_by_year"] = co2.factors_by_year(pk)
        p["co2_factor_contracted"] = pk in co2.PLANT_OVERRIDE
        return p

    def test_ordinary_plant_uses_the_year_factor(self):
        r = rollup_month(self._pl("MEX2"), "2026-07")
        assert r["co2_kg"] == pytest.approx(1100.0 * 0.444, abs=0.1)

    def test_sag_uses_its_contracted_factor(self):
        r = rollup_month(self._pl("MEX1"), "2026-07")
        assert r["co2_kg"] == pytest.approx(1100.0 * 0.202, abs=0.1)

    def test_an_older_month_uses_that_years_factor(self):
        p = self._pl("MEX2")
        p["days"] = ["2023-07-01", "2023-07-02", "2023-07-03"]
        p["tariff_by_month"] = {"2023-07": 2.0}
        r = rollup_month(p, "2023-07")
        assert r["co2_kg"] == pytest.approx(
            r["billable_kwh"] * 0.438, abs=0.1)

    def test_a_payload_without_the_year_map_still_works(self):
        # older cached payloads carry only the scalar
        p = _payload()
        p["co2_factor"] = 0.444
        assert rollup_month(p, "2026-07")["co2_kg"] == pytest.approx(
            1100.0 * 0.444, abs=0.1)

    def test_the_page_discloses_which_factor_it_applied(self):
        from argia.finance import annex
        src = pathlib.Path(annex.__file__).read_text(encoding="utf-8")
        assert 'id="c_co2f"' in src
        assert "SEMARNAT/CRE" in src and "contratado" in src

    def test_money_is_untouched_by_the_factor_change(self):
        """The CO2 line must never move a billing number."""
        a = rollup_month(self._pl("MEX1"), "2026-07")
        b = rollup_month(self._pl("MEX2"), "2026-07")
        assert a["amount_mxn"] == b["amount_mxn"]
        assert a["billable_kwh"] == b["billable_kwh"]
        assert a["co2_kg"] != b["co2_kg"]
