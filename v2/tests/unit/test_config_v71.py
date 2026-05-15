"""Tests for Stage 7.1 config additions.

Verifies:
- New optional fields default to None / "" when columns are absent
- New fields populate correctly from sheet rows
- Backward compatibility: old 18-column Plants rows still load
- Garbage/empty values become None, not crashes
- The new headers contain the old headers in order
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from argia.core.config import (
    INVERTERS_HEADER,
    INVERTERS_HEADER_V70,
    PLANTS_HEADER,
    PLANTS_HEADER_V70,
    PLANTS_HEADER_V71,
    InverterConfig,
    PlantConfig,
    Portfolio,
    load_portfolio,
)


# ============================================================
# Header constants
# ============================================================


class TestHeaderConstants:
    def test_plants_header_v70_is_18_cols(self):
        assert len(PLANTS_HEADER_V70) == 18

    def test_plants_header_v71_is_26_cols(self):
        # 18 original + 8 new
        assert len(PLANTS_HEADER_V71) == 26

    def test_plants_header_starts_with_v70(self):
        """The new header must start with the v7.0 columns in the same order,
        so existing sheets stay readable."""
        assert PLANTS_HEADER[:18] == PLANTS_HEADER_V70

    def test_new_plants_columns_appear_after_active(self):
        """The 'active' column must remain in position 17 (0-indexed) for
        sheet backward compat."""
        assert PLANTS_HEADER.index("active") == 17

    def test_v71_new_columns_are_the_8_added(self):
        """The 8 columns added in Stage 7.1 must remain at positions 18-25."""
        new_cols = PLANTS_HEADER_V71[18:]
        expected = [
            "module_count", "module_wp", "string_count",
            "tilt_deg", "azimuth_deg",
            "system_losses_pct", "commissioning_date", "notes",
        ]
        assert new_cols == expected

    def test_inverters_header_v70_is_5_cols(self):
        assert len(INVERTERS_HEADER_V70) == 5

    def test_inverters_header_is_8_cols(self):
        assert len(INVERTERS_HEADER) == 8

    def test_inverters_header_starts_with_v70(self):
        assert INVERTERS_HEADER[:5] == INVERTERS_HEADER_V70


# ============================================================
# Dataclass defaults
# ============================================================


class TestPlantConfigDefaults:
    """All new fields must default to None/'' so PlantConfig still
    constructs with only the 17 original args."""

    def _minimal_plant(self):
        return PlantConfig(
            plant_key="X", customer="", brand="GROWATT", site_id="",
            kwp_dc=0.0, kwp_ac=0.0, lat=None, lon=None,
            expected_factor=0.0, pr_target=0.0, installation_date="",
            secret_api_name="", secret_user_name="", secret_pass_name="",
            weather_plant_id="", datalogger_sn="", datalogger_addr=0,
            active=True,
        )

    def test_constructs_with_only_old_fields(self):
        plant = self._minimal_plant()
        assert plant.module_count is None
        assert plant.module_wp is None
        assert plant.string_count is None
        assert plant.tilt_deg is None
        assert plant.azimuth_deg is None
        assert plant.system_losses_pct is None
        assert plant.commissioning_date == ""
        assert plant.notes == ""

    def test_constructs_with_new_fields(self):
        plant = PlantConfig(
            plant_key="X", customer="", brand="GROWATT", site_id="",
            kwp_dc=0.0, kwp_ac=0.0, lat=None, lon=None,
            expected_factor=0.0, pr_target=0.0, installation_date="",
            secret_api_name="", secret_user_name="", secret_pass_name="",
            weather_plant_id="", datalogger_sn="", datalogger_addr=0,
            active=True,
            module_count=1110, module_wp=540.0, string_count=60,
            tilt_deg=15.0, azimuth_deg=180.0,
            system_losses_pct=14.0, commissioning_date="2024-03-15",
            notes="Test",
        )
        assert plant.module_count == 1110
        assert plant.module_wp == 540.0
        assert plant.notes == "Test"


class TestInverterConfigDefaults:
    def test_constructs_with_only_old_fields(self):
        inv = InverterConfig("X", "SN1", "Inv 1", 100.0, True)
        assert inv.mppt_count is None
        assert inv.strings_per_mppt is None
        assert inv.rated_kw_dc is None

    def test_constructs_with_new_fields(self):
        inv = InverterConfig("X", "SN1", "Inv 1", 100.0, True,
                             mppt_count=6, strings_per_mppt=2,
                             rated_kw_dc=110.0)
        assert inv.mppt_count == 6
        assert inv.strings_per_mppt == 2
        assert inv.rated_kw_dc == 110.0


# ============================================================
# load_portfolio
# ============================================================


def _mock_sheets(plants_rows, inverters_rows):
    """Build a mock SheetsClient whose read_table returns the given rows.

    plants_rows / inverters_rows are lists of dicts (header → value)."""
    sheets = MagicMock()

    def read_table(tab, _range):
        if tab == "Plants":
            return plants_rows
        if tab == "Inverters":
            return inverters_rows
        return []

    sheets.read_table.side_effect = read_table
    return sheets


def _old_plant_row(plant_key="QRO1"):
    """A v7.0-shape Plants row (no new columns)."""
    return {
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


def _full_plant_row(plant_key="QRO1"):
    """A v7.1-shape Plants row (all 26 columns)."""
    row = _old_plant_row(plant_key)
    row.update({
        "module_count": "1110", "module_wp": "540", "string_count": "60",
        "tilt_deg": "15", "azimuth_deg": "180",
        "system_losses_pct": "14", "commissioning_date": "2024-03-15",
        "notes": "South facing array, no shading",
    })
    return row


class TestLoadPortfolioBackwardCompat:
    """Old 18-column sheets must keep working."""

    def test_old_row_loads_without_new_fields(self):
        sheets = _mock_sheets([_old_plant_row()], [])
        portfolio = load_portfolio(sheets)
        plant = portfolio.plants["QRO1"]
        assert plant.kwp_dc == 500.0
        assert plant.module_count is None
        assert plant.module_wp is None
        assert plant.tilt_deg is None
        assert plant.notes == ""

    def test_old_inverter_row_loads_without_new_fields(self):
        sheets = _mock_sheets(
            [_old_plant_row()],
            [{"plant_key": "QRO1", "inverter_sn": "SN1",
              "inverter_label": "Inv 1", "rated_kw": "100", "active": "TRUE"}],
        )
        portfolio = load_portfolio(sheets)
        inv = portfolio.inverters_by_plant["QRO1"][0]
        assert inv.rated_kw == 100.0
        assert inv.mppt_count is None
        assert inv.strings_per_mppt is None
        assert inv.rated_kw_dc is None


class TestLoadPortfolioNewFields:
    def test_full_row_populates_all_new_fields(self):
        sheets = _mock_sheets([_full_plant_row()], [])
        portfolio = load_portfolio(sheets)
        plant = portfolio.plants["QRO1"]
        assert plant.module_count == 1110
        assert plant.module_wp == 540.0
        assert plant.string_count == 60
        assert plant.tilt_deg == 15.0
        assert plant.azimuth_deg == 180.0
        assert plant.system_losses_pct == 14.0
        assert plant.commissioning_date == "2024-03-15"
        assert plant.notes == "South facing array, no shading"

    def test_partial_population_keeps_others_none(self):
        """Filling in only module_count + module_wp should leave others None."""
        row = _old_plant_row()
        row["module_count"] = "1110"
        row["module_wp"] = "540"
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        plant = portfolio.plants["QRO1"]
        assert plant.module_count == 1110
        assert plant.module_wp == 540.0
        assert plant.string_count is None
        assert plant.tilt_deg is None

    def test_garbage_int_becomes_none(self):
        row = _old_plant_row()
        row["module_count"] = "not_a_number"
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        assert portfolio.plants["QRO1"].module_count is None

    def test_garbage_float_becomes_none(self):
        row = _old_plant_row()
        row["tilt_deg"] = "garbage"
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        assert portfolio.plants["QRO1"].tilt_deg is None

    def test_float_in_int_column_coerced(self):
        """Sheets often returns numbers as floats. '1110.0' should become 1110."""
        row = _old_plant_row()
        row["module_count"] = "1110.0"
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        assert portfolio.plants["QRO1"].module_count == 1110

    def test_negative_values_load_as_given(self):
        """The loader doesn't validate ranges — that's Stage 7.2's job.
        Negative tilt is meaningless physically but loads fine."""
        row = _old_plant_row()
        row["tilt_deg"] = "-15"
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        assert portfolio.plants["QRO1"].tilt_deg == -15.0


class TestLoadInvertersNewFields:
    def test_inverter_with_mppt_fields(self):
        sheets = _mock_sheets(
            [_old_plant_row()],
            [{"plant_key": "QRO1", "inverter_sn": "SN1",
              "inverter_label": "Inv 1", "rated_kw": "100", "active": "TRUE",
              "mppt_count": "6", "strings_per_mppt": "2",
              "rated_kw_dc": "110"}],
        )
        portfolio = load_portfolio(sheets)
        inv = portfolio.inverters_by_plant["QRO1"][0]
        assert inv.mppt_count == 6
        assert inv.strings_per_mppt == 2
        assert inv.rated_kw_dc == 110.0


class TestSanity:
    """Higher-level invariants the loader should always honor."""

    def test_kwp_dc_module_consistency_NOT_enforced(self):
        """The loader does NOT enforce kwp_dc ≈ module_count*module_wp/1000.
        That's Stage 7.2's job. This test pins the current behavior so a
        future enforcement is a deliberate decision."""
        row = _old_plant_row()
        row["kwp_dc"] = "100"        # claims 100 kWp DC
        row["module_count"] = "1110"  # but 1110 × 540 Wp = 599 kWp DC
        row["module_wp"] = "540"
        sheets = _mock_sheets([row], [])
        portfolio = load_portfolio(sheets)
        plant = portfolio.plants["QRO1"]
        # Loads both as-given. Stage 7.2 must flag this.
        assert plant.kwp_dc == 100.0
        assert plant.module_count == 1110
        assert plant.module_wp == 540.0

    def test_empty_plants_returns_empty_portfolio(self):
        sheets = _mock_sheets([], [])
        portfolio = load_portfolio(sheets)
        assert portfolio.plants == {}
        assert portfolio.inverters_by_plant == {}

    def test_active_plants_filter(self):
        rows = [_old_plant_row("A"), _old_plant_row("B")]
        rows[1]["active"] = "FALSE"
        sheets = _mock_sheets(rows, [])
        portfolio = load_portfolio(sheets)
        active = portfolio.active_plants()
        assert len(active) == 1
        assert active[0].plant_key == "A"
