"""Contract_Monthly tests.

Reconciliation targets are v1 figures verified against the 2026-07
ARGIA_Solar export: July expected incomes per plant, the GTO1
design/contract divergence (phase-2 expansion built but not yet
contracted), the SLP1/SLP2 COD-shifted design values, and the
USD-indexed LaaS fees.
"""

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.finance.contract import (
    CONTRACT_HEADER, load_contract_monthly, tariff_for_month,
)
from argia.kpi.design import DESIGN_TAB_CANDIDATES, load_design_monthly

SEED = (Path(__file__).resolve().parents[2] / "data" / "finance"
        / "contract_monthly_seed.csv")

# v1 ContractData × tariff, July 2026 (verified by hand)
JULY_EXPECTED_INCOME = {
    "SLP1": 24874 * 2.596,      # 64,572.90
    "SLP2": 48931 * 2.462,      # 120,468.12
    "GTO1": 94698 * 1.975,      # 187,028.55
    "MEX1": 87401 * 2.508,      # 219,201.71
    "NL1": 96706 * 2.022,       # 195,539.53
    "MEX2": 89879 * 2.367,      # 212,743.59
}
LAAS_FEES_USD = {"LOAX1": 26750.00, "LGTO1": 15233.00}


def _seed_rows():
    with open(SEED, newline="") as fh:
        return list(csv.DictReader(fh))


def _loaded():
    sheets = MagicMock(spec=SheetsClient)
    rows = _seed_rows()
    sheets.read_range.return_value = (
        [CONTRACT_HEADER] + [[r[c] for c in CONTRACT_HEADER] for r in rows])
    return load_contract_monthly(sheets)


class TestSeedIntegrity:
    def test_row_count_and_plants(self):
        rows = _seed_rows()
        assert len(rows) == 1235
        assert {r["plant_key"] for r in rows} == {
            "SLP1", "SLP2", "GTO1", "MEX1", "NL1", "MEX2",
            "LOAX1", "LGTO1"}

    def test_full_contract_horizon(self):
        years = sorted({int(r["year"]) for r in _seed_rows()})
        assert years[0] == 2024 and years[-1] == 2043

    def test_laas_rows_are_usd_fee_only(self):
        for r in _seed_rows():
            if r["plant_key"] in LAAS_FEES_USD:
                assert r["ccy"] == "USD"
                assert r["contract_kwh"] == "" and r["tariff_mxn"] == ""
            else:
                assert r["fixed_income_ccy"] == ""


class TestJulyReconciliation:
    def test_ppa_expected_income_matches_v1(self):
        cm = _loaded()
        for plant, income in JULY_EXPECTED_INCOME.items():
            row = cm[(plant, 2026, 7)]
            assert row.expected_income_mxn() == pytest.approx(income,
                                                              abs=0.01), plant

    def test_laas_fee_and_fx_conversion(self):
        cm = _loaded()
        for plant, fee in LAAS_FEES_USD.items():
            row = cm[(plant, 2026, 7)]
            assert row.is_laas
            assert row.fixed_income_ccy == pytest.approx(fee)
            assert row.expected_income_mxn(xr=17.98) == pytest.approx(
                fee * 17.98, abs=0.01)
            assert row.expected_income_mxn() is None  # no rate, no number

    def test_gto1_design_diverges_from_contract_in_july(self):
        # Phase-2 expansion: built (design 126,721) but not yet
        # contracted (94,698). June they still agree.
        cm = _loaded()
        jul = cm[("GTO1", 2026, 7)]
        jun = cm[("GTO1", 2026, 6)]
        assert jul.design_kwh == pytest.approx(126721)
        assert jul.contract_kwh == pytest.approx(94698)
        assert jun.design_kwh == pytest.approx(jun.contract_kwh)

    def test_slp_cod_shift_preserved(self):
        # SLP1/SLP2 design is deliberately COD-shifted (installed late);
        # design != contract by ~0.55% for 2026.
        cm = _loaded()
        for plant in ("SLP1", "SLP2"):
            row = cm[(plant, 2026, 7)]
            ratio = row.design_kwh / row.contract_kwh
            assert 1.004 < ratio < 1.007, plant


class TestPenaltyBasis:
    def test_contract_kwh_daily_is_month_over_days(self):
        cm = _loaded()
        mex2 = cm[("MEX2", 2026, 6)]
        # the settled compensada basis: 89,173 / 30 = 2,972.43
        assert mex2.contract_kwh == pytest.approx(89173)
        assert mex2.contract_kwh_daily == pytest.approx(2972.4333, abs=0.01)

    def test_daily_none_for_laas(self):
        cm = _loaded()
        assert cm[("LOAX1", 2026, 7)].contract_kwh_daily is None


class TestTariffs:
    def test_july_tariffs_match_current_scalars(self):
        cm = _loaded()
        for plant, t in {"SLP1": 2.596, "SLP2": 2.462, "GTO1": 1.975,
                         "MEX1": 2.508, "NL1": 2.022,
                         "MEX2": 2.367}.items():
            assert tariff_for_month(cm, plant, 2026, 7) == pytest.approx(t)

    def test_fallback_during_transition(self):
        assert tariff_for_month({}, "SLP1", 2026, 7,
                                fallback=2.596) == 2.596

    def test_tariff_escalations_exist_in_horizon(self):
        # v1 priced escalations into ContractData; the tariff must not
        # be one flat number across the horizon for every plant.
        cm = _loaded()
        for plant in JULY_EXPECTED_INCOME:
            tariffs = {row.tariff_mxn for key, row in cm.items()
                       if key[0] == plant and row.tariff_mxn is not None}
            assert len(tariffs) > 1, plant


class TestDesignCompat:
    def test_contract_monthly_is_primary_design_source(self):
        assert DESIGN_TAB_CANDIDATES[0] == "Contract_Monthly"

    def test_design_loader_reads_contract_monthly_shape(self):
        # kpi/design reads A1:D by header name — Contract_Monthly's
        # first four columns are exactly that shape.
        rows = _seed_rows()
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = (
            [CONTRACT_HEADER[:4]]
            + [[r["plant_key"], r["year"], r["month"], r["design_kwh"]]
               for r in rows])
        dm = load_design_monthly(sheets)
        assert dm[("GTO1", 2026, 7)] == pytest.approx(126721)
        assert dm[("SLP1", 2026, 7)] == pytest.approx(25011)
        # LaaS/blank-design rows skipped, not crashed on
        assert ("LOAX1", 2026, 7) not in dm


class TestLoaderRobustness:
    def test_missing_tab_degrades(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.side_effect = RuntimeError("no tab")
        assert load_contract_monthly(sheets) == {}

    def test_bad_header_degrades(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [["wrong", "header"], ["x", 1]]
        assert load_contract_monthly(sheets) == {}

    def test_malformed_rows_skipped(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [
            CONTRACT_HEADER,
            ["SLP1", "2026", "7", "25011", "24874", "2.596", "", ""],
            ["", "2026", "7", "1", "1", "1", "", ""],       # no plant
            ["SLP1", "notayear", "7", "1", "1", "1", "", ""],
        ]
        cm = load_contract_monthly(sheets)
        assert list(cm) == [("SLP1", 2026, 7)]

    def test_om_cost_field_parses(self):
        from argia.core.config import PlantConfig
        assert hasattr(PlantConfig, "om_cost_monthly_mxn")
        p = PlantConfig(
            plant_key="X", customer="c", brand="b", site_id="s",
            kwp_dc=1.0, kwp_ac=1.0, lat=None, lon=None,
            expected_factor=0.8, pr_target=0.8, installation_date="",
            secret_api_name="", secret_user_name="", secret_pass_name="",
            weather_plant_id="", datalogger_sn="", datalogger_addr=0,
            active=True, om_cost_monthly_mxn=8000.0)
        assert p.om_cost_monthly_mxn == 8000.0
