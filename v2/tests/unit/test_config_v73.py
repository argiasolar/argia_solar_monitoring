"""Tests for Stage 7.3 config additions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.core.config import (
    PLANTS_HEADER,
    PLANTS_HEADER_V70,
    PLANTS_HEADER_V71,
    PlantConfig,
    load_portfolio,
)


# ============================================================
# Headers
# ============================================================


class TestHeaders:
    def test_v70_18_cols(self):
        assert len(PLANTS_HEADER_V70) == 18

    def test_v71_26_cols(self):
        assert len(PLANTS_HEADER_V71) == 26

    def test_v73_28_cols(self):
        assert len(PLANTS_HEADER) == 28

    def test_new_columns_after_v71(self):
        new_cols = PLANTS_HEADER[26:]
        assert new_cols == ["pr_baseline", "tariff_mxn_per_kwh"]

    def test_v73_strictly_extends_v71(self):
        assert PLANTS_HEADER[:26] == PLANTS_HEADER_V71


# ============================================================
# Defaults
# ============================================================


class TestDefaults:
    def _plant(self, **overrides):
        defaults = dict(
            plant_key="X", customer="", brand="GROWATT", site_id="",
            kwp_dc=0.0, kwp_ac=0.0, lat=None, lon=None,
            expected_factor=0.0, pr_target=0.0, installation_date="",
            secret_api_name="", secret_user_name="", secret_pass_name="",
            weather_plant_id="", datalogger_sn="", datalogger_addr=0,
            active=True,
        )
        defaults.update(overrides)
        return PlantConfig(**defaults)

    def test_pr_baseline_defaults_none(self):
        plant = self._plant()
        assert plant.pr_baseline is None
        assert plant.tariff_mxn_per_kwh is None

    def test_can_construct_with_new_fields(self):
        plant = self._plant(pr_baseline=0.82, tariff_mxn_per_kwh=2.5)
        assert plant.pr_baseline == 0.82
        assert plant.tariff_mxn_per_kwh == 2.5


# ============================================================
# Loading
# ============================================================


def _mock_sheets(plants_rows, inverters_rows):
    sheets = MagicMock()
    def read_table(tab, _range):
        return plants_rows if tab == "Plants" else (
            inverters_rows if tab == "Inverters" else []
        )
    sheets.read_table.side_effect = read_table
    return sheets


def _plant_row(plant_key="QRO1", **overrides):
    row = {
        "plant_key": plant_key, "customer": "Cust", "brand": "SOLAREDGE",
        "site_id": "12345", "kwp_dc": "500", "kwp_ac": "400",
        "lat": "20.5", "lon": "-100.2",
        "expected_factor": "0.78", "pr_target": "0.80",
        "installation_date": "2024-01-01",
        "secret_api_name": "SOLAREDGE_API_KEY",
        "secret_user_name": "", "secret_pass_name": "",
        "weather_plant_id": "", "datalogger_sn": "", "datalogger_addr": "0",
        "active": "TRUE",
    }
    row.update(overrides)
    return row


class TestLoading:
    def test_loads_new_optional_fields(self):
        row = _plant_row(pr_baseline="0.82", tariff_mxn_per_kwh="2.5")
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        plant = portfolio.plants["QRO1"]
        assert plant.pr_baseline == 0.82
        assert plant.tariff_mxn_per_kwh == 2.5

    def test_missing_new_fields_become_none(self):
        sheets = _mock_sheets([_plant_row()], [])
        portfolio = load_portfolio(sheets)
        plant = portfolio.plants["QRO1"]
        assert plant.pr_baseline is None
        assert plant.tariff_mxn_per_kwh is None

    def test_garbage_pr_baseline_becomes_none(self):
        row = _plant_row(pr_baseline="not_a_number")
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        assert portfolio.plants["QRO1"].pr_baseline is None


# ============================================================
# Sanity warnings (best-effort, don't crash)
# ============================================================


class TestSanityWarnings:
    def test_kwp_dc_zero_warns(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        row = _plant_row(kwp_dc="0")
        sheets = _mock_sheets([row], [])
        load_portfolio(sheets)
        assert any("kwp_dc is 0" in m for m in caplog.messages)

    def test_kwp_dc_below_ac_warns(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        # kwp_dc < kwp_ac is implausible
        row = _plant_row(kwp_dc="100", kwp_ac="400")
        sheets = _mock_sheets([row], [])
        load_portfolio(sheets)
        assert any("kwp_dc" in m and "kwp_ac" in m for m in caplog.messages)

    def test_module_math_mismatch_warns(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        # kwp_dc=100 but 1110 × 540 / 1000 = 599 → big mismatch
        row = _plant_row(kwp_dc="100", module_count="1110", module_wp="540")
        sheets = _mock_sheets([row], [])
        load_portfolio(sheets)
        assert any("disagrees" in m or "module" in m.lower() for m in caplog.messages)

    def test_inverter_rated_kw_zero_warns(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        sheets = _mock_sheets(
            [_plant_row()],
            [{"plant_key": "QRO1", "inverter_sn": "SN1",
              "inverter_label": "Inv 1", "rated_kw": "0", "active": "TRUE"}],
        )
        load_portfolio(sheets)
        assert any("rated_kw is 0" in m for m in caplog.messages)
