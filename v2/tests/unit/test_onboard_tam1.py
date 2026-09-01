"""onboard_tam1 — the pure builders behind the TAM1 backfill.

The script writes three PG tables and three sheet tabs from one set of
builders; these tests pin the honesty rules: never overwrite existing
rows, never write today's partial day, never invent days before first
production, and keep real zero days (an outage must show as 0, not as
a hole).
"""
import re
import sys
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

import onboard_tam1 as O                                   # noqa: E402


class TestDesignScaling:
    def test_scale_is_the_dc_ratio(self):
        assert O.DC_SCALE == pytest.approx(364.65 / 357.0)

    def test_august_design_scales_the_helioscope_value(self):
        assert O.design_kwh(8) == pytest.approx(
            58151.6 * 364.65 / 357.0, abs=0.1)

    def test_annual_design_is_about_549_mwh(self):
        total = sum(O.design_kwh(m) for m in range(1, 13))
        assert 548_000 < total < 551_000


class TestMonthDays:
    def test_days_before_first_production_are_dropped(self):
        got = O.month_days("2026-05", [0.0] * 12 + [1501.4, 1663.9])
        assert got[0] == ("2026-05-13", 1501.4)
        assert all(d >= "2026-05-13" for d, _ in got)

    def test_a_zero_day_after_start_is_kept_as_zero(self):
        got = O.month_days("2026-06", [100.0, 0.0, 50.0],
                           first_production="2026-06-01")
        assert ("2026-06-02", 0.0) in got

    def test_todays_partial_day_is_never_written(self):
        got = O.month_days("2026-09", [480.2, 0.0],
                           today_iso="2026-09-01")
        assert got == []

    def test_padded_31_slot_arrays_do_not_invent_dates(self):
        got = O.month_days("2026-06", [1.0] * 31,
                           first_production="2026-06-01")
        assert len(got) == 30            # June has 30 days
        assert got[-1][0] == "2026-06-30"


class TestSheetRow:
    HDR = ["plant_key", "customer", "brand", "extra_col"]

    def test_orders_by_live_header_and_blanks_unknowns(self):
        row = O.sheet_row(self.HDR, {"plant_key": "TAM1",
                                     "brand": "GROWATT"})
        assert row == ["TAM1", "", "GROWATT", ""]

    def test_a_field_with_no_column_refuses_loudly(self):
        with pytest.raises(ValueError):
            O.sheet_row(["plant_key"], {"plant_key": "TAM1",
                                        "portfolio": "CAPEX"})

    def test_the_real_plant_fields_fit_the_real_header(self):
        # live header captured 2026-09-01 from the Plants tab
        hdr = ['plant_key', 'customer', 'brand', 'site_id', 'kwp_dc',
               'kwp_ac', 'lat', 'lon', 'expected_factor', 'pr_target',
               'installation_date', 'secret_api_name',
               'secret_user_name', 'secret_pass_name',
               'weather_plant_id', 'datalogger_sn', 'datalogger_addr',
               'active', 'module_count', 'module_wp', 'string_count',
               'tilt_deg', 'azimuth_deg', 'notes', 'tariff_mxn_per_kwh',
               'kwp_dc_override', 'kwp_dc_check', 'pr_stc_model',
               'gamma_pmax', 'monitoring_class', 'p90_annual_kwh',
               'contracted_kwh', 'date_interconnection',
               'billing_scheme', 'module_model', 'pr_baseline',
               'om_cost_monthly_mxn', 'portfolio', 'show_dashboard',
               'show_daily_report', 'show_financial', 'client_channel']
        row = O.sheet_row(hdr, O.PLANT_FIELDS)
        assert row[0] == "TAM1"
        assert row[hdr.index("portfolio")] == "CAPEX"
        assert row[hdr.index("kwp_dc")] == 364.65


class TestSqlSafety:
    DAYS = [("2026-05-13", 1501.4), ("2026-05-14", 1663.9)]

    def test_daily_production_never_overwrites(self):
        assert "ON CONFLICT (plant_key, prod_date) DO NOTHING" in \
            O.daily_production_sql(self.DAYS)

    def test_billable_equals_energy_in_the_backfill(self):
        sql = O.daily_production_sql(self.DAYS)
        assert "1501.4,1501.4" in sql.replace(" ", "")

    def test_snapshot_and_reconciliation_are_insert_only(self):
        assert "DO NOTHING" in O.snapshot_sql(self.DAYS)
        assert "DO NOTHING" in O.reconciliation_sql(self.DAYS)

    def test_contract_table_writes_design_only(self):
        sql = O.contract_table_sql()
        assert "design_kwh" in sql
        assert "tariff" not in sql       # CAPEX: no tariff invented

    def test_plant_investment_is_the_offer_total(self):
        assert "7004698" in O.plant_sql()


class TestMonthsBetween:
    def test_spans_the_year_boundary(self):
        assert O.months_between("2026-11", "2027-01") == \
            ["2026-11", "2026-12", "2027-01"]

    def test_single_month(self):
        assert O.months_between("2026-05", "2026-05") == ["2026-05"]


class TestInverters:
    def test_four_units_seventyfive_kw_each(self):
        assert len(O.INVERTERS) == 4
        assert O.INVERTER_RATED_KW * len(O.INVERTERS) == 300

    def test_serials_match_the_portal(self):
        sns = {sn for sn, _ in O.INVERTERS}
        assert sns == {"JNMAE7D006", "JNMAE5X00K",
                       "JNMAE7D007", "JNMAE5X00L"}
