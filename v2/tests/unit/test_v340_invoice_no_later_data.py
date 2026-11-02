"""v340: an invoice carries no data after the invoiced month.

Tomasz, 2 Nov 2026: "please for god sake remove the november consumption
from the October month!!" The October invoices were rendered on 2 Nov and
embed the whole year, so 1-2 Nov production showed in the annual chart,
the January-December table and the year-to-date total.
"""
from unittest.mock import MagicMock

import pytest

from argia.core.config import PlantConfig, Portfolio
from argia.finance.annex import (ATOM_WIDTH, annual_rollup, build_annex_data,
                                 rollup_month)
from argia.finance.income import Period
from scripts import report_invoice_annex as ria

YEAR = Period.from_iso("2026-01-01", "2026-12-31")


def _plant():
    return PlantConfig(
        plant_key="GTO1", customer="Taigene", brand="growatt",
        site_id="1", kwp_dc=600.0, kwp_ac=500.0, lat=None, lon=None,
        expected_factor=0.8, pr_target=0.85, installation_date="",
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True, tariff_mxn_per_kwh=1.975)


def _sheets():
    head = ["date_iso", "plant_key", "energy_kwh", "billable_kwh",
            "expected_kwh", "design_kwh", "pr", "availability",
            "soiling_loss_pct", "cloud_coverage_pct"]
    kpi = [head,
           ["2026-10-30", "GTO1", "3000", "3000", "3100", "3050", "0.8", "1", "0.01", "20"],
           ["2026-10-31", "GTO1", "3200", "3200", "3100", "3050", "0.8", "1", "0.01", "20"],
           ["2026-11-01", "GTO1", "1900", "1900", "3100", "3050", "0.8", "1", "0.01", "20"],
           ["2026-11-02", "GTO1", "1998.7", "1998.7", "3100", "3050", "0.8", "1", "0.01", "20"]]
    contract = [["plant_key", "year", "month", "design_kwh", "contract_kwh",
                 "tariff_mxn", "fixed_income_ccy", "ccy"]]

    def read_range(tab, a1="A1:Z"):
        return {"KPI_Daily": kpi, "Contract_Monthly": contract}.get(tab, [])

    sc = MagicMock()
    sc.read_range.side_effect = read_range
    return sc


def _portfolio():
    p = MagicMock(spec=Portfolio)
    p.plants = {"GTO1": _plant()}
    return p


HISTORY = {"2026-10": {"kwh": 102141.5, "penalty": 0.0, "income": 201729.46},
           "2026-11": {"kwh": 3898.7, "penalty": 0.0, "income": 7700.0}}


class TestInvoiceCutoff:
    def _oct(self):
        return build_annex_data(_sheets(), _portfolio(), "GTO1", YEAR,
                                history=dict(HISTORY), data_until="2026-10-31")

    def test_no_atom_after_the_invoiced_month_has_data(self):
        d = self._oct()
        late = [a for day, a in zip(d["days"], d["atoms"]) if day > "2026-10-31"]
        assert late and all(a == [None] * ATOM_WIDTH for a in late)

    def test_axis_still_runs_to_december(self):
        d = self._oct()
        assert d["days"][0] == "2026-01-01" and d["days"][-1] == "2026-12-31"

    def test_october_days_are_kept(self):
        d = self._oct()
        i = d["days"].index("2026-10-31")
        assert d["atoms"][i][0] == 3200.0

    def test_november_rollup_is_empty(self):
        r = rollup_month(self._oct(), "2026-11")
        assert r["has_data"] is False and r["billable_kwh"] == 0.0

    def test_later_history_months_are_dropped(self):
        d = self._oct()
        assert "2026-11" not in d["history"] and "2026-10" in d["history"]

    def test_year_total_equals_january_to_october(self):
        rows = annual_rollup(self._oct())
        jan_dec = sum(r["billable_kwh"] for r in rows)
        jan_oct = sum(r["billable_kwh"] for r in rows if r["ym"] <= "2026-10")
        assert jan_dec == jan_oct == 102141.5
        assert sum(r["amount_mxn"] or 0 for r in rows) == pytest.approx(201729.46)

    def test_without_cutoff_the_year_annex_is_unchanged(self):
        """annex mode (whole year, no cutoff) still shows every day"""
        d = build_annex_data(_sheets(), _portfolio(), "GTO1", YEAR)
        assert rollup_month(d, "2026-11")["measured_kwh"] == 3898.7
        assert d["data_until"] is None


@pytest.mark.parametrize("ym,end", [("2026-10", "2026-10-31"), ("2026-11", "2026-11-30"),
                                    ("2026-12", "2026-12-31"), ("2028-02", "2028-02-29"),
                                    ("2027-02", "2027-02-28")])
def test_month_end_iso(ym, end):
    assert ria._month_end_iso(ym) == end


@pytest.mark.parametrize("argv,expect", [(["--month", "2026-10"], "2026-10-31"),
                                         (["--year", "2026"], None)])
def test_main_passes_the_cutoff_only_in_invoice_mode(monkeypatch, tmp_path, argv, expect):
    seen = []
    pf = _portfolio()
    pf.financial_plants = lambda: [_plant()]
    monkeypatch.setattr(ria, "open_sheets", lambda: object())
    monkeypatch.setattr(ria, "load_portfolio", lambda s: pf)
    monkeypatch.setattr(ria, "reconciliation_gate", lambda ym, plants: (list(plants), []))
    monkeypatch.setattr(ria, "load_invoicing_overview", lambda y: {})

    def fake_build(sheets, portfolio, pk, window, history=None, data_until=None):
        seen.append(data_until)
        return {}
    monkeypatch.setattr(ria, "build_annex_data", fake_build)
    monkeypatch.setattr(ria, "render_annex_html", lambda payload, gen, default_ym=None: "<html></html>")
    assert ria.main(argv + ["--out-dir", str(tmp_path)]) == 0
    assert seen == [expect]
