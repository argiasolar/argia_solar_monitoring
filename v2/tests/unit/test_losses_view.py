"""v264 - the losses page renders from loss_daily rows (no database)."""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))

import losses_view as LV  # noqa: E402

TODAY = dt.date(2026, 9, 28)
PLANTS = {"MEX1": {"portfolio": "PPA"}, "MEX2": {"portfolio": "PPA"}, "MEX3": {"portfolio": "CAPEX"}}
NAME = {"MEX1": "Sag", "MEX2": "Vitalmex", "MEX3": "Sms"}.get


def row(k, day, lost, unav=0.0, heat=0.0, tariff=None, basis="peers"):
    return {"plant_key": k, "prod_date": day, "expected_basis": basis, "peers": "MEX2,MEX3",
            "expected_weather_kwh": 3000.0, "expected_peers_kwh": 2900.0, "expected_kwh": 2900.0,
            "actual_kwh": 2900.0 - lost, "lost_kwh": lost, "unavailability_kwh": unav, "overheating_kwh": heat,
            "underperformance_kwh": lost - unav - heat, "excused_kwh": 0.0, "tariff_mxn": tariff,
            "lost_mxn": None if tariff is None else lost * tariff}


ROWS = [row("MEX1", "2026-09-27", 300, 0, 0, 2.508), row("MEX1", "2026-09-25", 1947.2, 1947.2, 0, 2.508),
        row("MEX1", "2026-09-19", 2615.8, 2615.8, 0, 2.508), row("MEX1", "2026-08-20", 999, 999, 0, 2.508),
        row("MEX2", "2026-09-27", 100, 0, 20, 2.367), row("MEX3", "2026-09-27", 50, 50, 0, None)]


def html():
    return LV.render(ROWS, PLANTS, TODAY, NAME, lambda k: f"/monitoring/{k.lower()}/")


class TestPeriods:
    def test_windows_end_yesterday(self):
        assert [r["prod_date"] for r in LV.period_rows(ROWS, "1d", TODAY)] == ["2026-09-27"] * 3
        assert {r["prod_date"] for r in LV.period_rows(ROWS, "7d", TODAY)} == {"2026-09-27", "2026-09-25"}
        assert "2026-08-20" not in {r["prod_date"] for r in LV.period_rows(ROWS, "30d", TODAY)}
        assert {r["prod_date"] for r in LV.period_rows(ROWS, "mtd", TODAY)} == {"2026-09-27", "2026-09-25", "2026-09-19"}

    def test_sums_price_each_cause_at_the_rows_tariff(self):
        s = LV.sums([r for r in ROWS if r["plant_key"] == "MEX1" and r["prod_date"] >= "2026-09-01"])
        assert s["lost"] == 300 + 1947.2 + 2615.8
        assert round(s["unavailability_mxn"]) == round((1947.2 + 2615.8) * 2.508)
        assert round(s["lost_mxn"]) == round(s["unavailability_mxn"] + s["underperformance_mxn"])


class TestPage:
    def test_sag_leads_with_its_30_day_money(self):
        s = html()
        mxn = (300 + 1947.2 + 2615.8) * 2.508
        assert f"${mxn:,.0f}" in s
        assert s.index("Sag") < s.index("Vitalmex")                # worst first

    def test_capex_is_kwh_only(self):
        s = html()
        assert "kWh only" in s and "CAPEX, no tariff" in s

    def test_every_period_has_a_table_and_a_switch(self):
        s = html()
        for p, en, _ in LV.PERIODS:
            assert f'data-p="{p}"' in s and en in s

    def test_day_by_day_detail_and_the_method_are_there(self):
        s = html()
        assert 'id="loss-mex1"' in s and "Unavailability kWh" in s
        assert "healthy neighbours" in s and "deemed energy" in s

    def test_bilingual_and_no_em_dash(self):
        s = html()
        assert "Perdido" in s and "Indisponibilidad" in s
        assert chr(0x2014) not in s

    def test_empty_table_says_so(self):
        assert "has not run" in LV.render([], PLANTS, TODAY, NAME, str)

    def test_normalise_reads_psql_rows_in_select_order(self):
        r = LV.normalise([["MEX1", "2026-09-27", "peers", "MEX2", "3000", "", "2900", "2600", "300",
                           "0", "0", "300", "0", "2.508", "752.4"]])[0]
        assert r["expected_peers_kwh"] is None and r["lost_kwh"] == 300.0 and r["tariff_mxn"] == 2.508
        assert LV.SELECT_SQL.split(" FROM ")[0].count(",") == len(LV.NUM) + 3


class TestPlantCard:
    def test_ppa_card_in_pesos_with_the_split_and_a_link(self):
        c = LV.plant_card("MEX1", ROWS, TODAY, "/monitoring/losses/#loss-mex1")
        assert "Lost to under-production: yesterday $752 MXN" in c
        assert "unavailability $" in c and "/monitoring/losses/#loss-mex1" in c

    def test_capex_card_in_kwh_without_a_link(self):
        c = LV.plant_card("MEX3", ROWS, TODAY, "")
        assert "50 kWh" in c and "CAPEX, not billed per kWh" in c and "href" not in c

    def test_no_rows_no_card(self):
        assert LV.plant_card("NL1", ROWS, TODAY, "x") == ""


def test_the_loss_page_is_behind_the_financial_gate():
    import auth_core as ac
    assert ac.PREFIX_AREA["/monitoring/losses/"] == "financial"
