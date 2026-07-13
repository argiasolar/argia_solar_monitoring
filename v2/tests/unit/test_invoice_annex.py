"""Invoice annex (v93) tests.

The load-bearing property: energía compensada is NEVER recomputed here —
it is ``billable_kwh - energy_kwh`` straight from KPI_Daily (the v91
deemed engine), so the customer annex and the finance report cannot
disagree. The pure ``rollup_month`` is the reference the embedded JS
mirrors; the truth table pins measured/deemed/billing/performance and
the no-data-day handling.
"""

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
        "co2_factor": 0.444,
    }


class TestRollupMonth:
    def test_measured_deemed_billable_and_amount(self):
        r = rollup_month(_payload(), "2026-07")
        assert r["measured_kwh"] == 900.0
        assert r["deemed_kwh"] == 200.0
        assert r["billable_kwh"] == 1100.0
        assert r["amount_mxn"] == pytest.approx(1100.0 * 2.367, abs=0.01)
        assert r["co2_kg"] == pytest.approx(1100.0 * 0.444, abs=0.1)

    def test_no_events_means_zero_compensada(self):
        assert rollup_month(_payload(), "2026-06")["deemed_kwh"] == 0.0

    def test_performance_means_ignore_no_data_days(self):
        r = rollup_month(_payload(), "2026-07")
        # only Jul-1 has PR/avail; Jul-2 is a no-data day
        assert r["pr"] == pytest.approx(0.84)
        assert r["availability"] == pytest.approx(1.0)
        # production_pct = measured / design over data days = 900/1050
        assert r["production_pct"] == pytest.approx(900.0 / 1050.0)

    def test_no_tariff_leaves_amount_none(self):
        p = _payload(tariffs={"2026-06": 2.367, "2026-07": None})
        assert rollup_month(p, "2026-07")["amount_mxn"] is None

    def test_has_data_flag(self):
        p = _payload()
        assert rollup_month(p, "2026-07")["has_data"] is True
        # a month entirely out of range
        assert rollup_month(p, "2026-01")["has_data"] is False


class TestAnnualRollup:
    def test_months_in_order(self):
        rows = annual_rollup(_payload())
        assert [r["ym"] for r in rows] == ["2026-06", "2026-07"]

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
        assert "Anexo de facturaci" in h            # header
        assert "Rendimiento del sistema" in h       # performance section
        assert "Generaci" in h and "n anual" in h   # annual table
        assert "window.print()" in h                # Descargar → print/PDF

    def test_embeds_atoms_and_default_month(self):
        h = self._html()
        assert '"MEX2"' in h and '"tariff_by_month"' in h
        # default selected month is the latest with data
        assert 'sel.value="2026-07"' in h

    def test_footer_states_compensada_provenance(self):
        h = self._html()
        assert "billable_kwh" in h and "energy_kwh" in h
        assert "v91" in h
