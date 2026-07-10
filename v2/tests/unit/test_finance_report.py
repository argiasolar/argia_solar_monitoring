"""Finance report tests: builder assembles the approved-sample numbers
from seed fixtures; renderer carries the logo, both DSCR views and the
provenance footer (drift guard: footer text must come from the
registry, so audit text in the sheet and in the PDF can't diverge)."""

import csv
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argia.core.config import PlantConfig, Portfolio
from argia.core.sheets import SheetsClient
from argia.finance.contract import CONTRACT_HEADER
from argia.finance.income import Period
from argia.finance.loans import LOANS_HEADER, SCHEDULE_HEADER
from argia.finance.provenance import COLUMN_NOTES
from argia.finance.report import (
    build_finance_report_data, render_html,
)

DATA = Path(__file__).resolve().parents[2] / "data" / "finance"

JULY_MTD_ENERGY = {
    "SLP1": 5676.1, "SLP2": 9265.0, "GTO1": 25380.1,
    "MEX1": 14827.7, "NL1": 21441.5, "MEX2": 16737.8,
}
TARIFFS = {"SLP1": 2.596, "SLP2": 2.462, "GTO1": 1.975,
           "MEX1": 2.508, "NL1": 2.022, "MEX2": 2.367}


def _csv_rows(name):
    with open(DATA / name, newline="") as fh:
        return list(csv.DictReader(fh))


def _sheets():
    """Mock SheetsClient serving all four tabs from the seed CSVs plus
    synthetic KPI_Daily rows for Jul 1-7."""
    contract = ([CONTRACT_HEADER] +
                [[r[c] for c in CONTRACT_HEADER]
                 for r in _csv_rows("contract_monthly_seed.csv")])
    kpi = [["date_iso", "plant_key", "energy_kwh"]]
    for plant, total in JULY_MTD_ENERGY.items():
        for d in range(1, 8):
            kpi.append(["2026-07-%02d" % d, plant, total / 7.0])

    def read_range(tab, a1="A1:Z"):
        if tab == "Contract_Monthly":
            return contract
        if tab == "KPI_Daily":
            return kpi
        raise RuntimeError("no such tab: " + tab)

    def read_table(tab, a1="A1:Z"):
        if tab == "Loans":
            return _csv_rows("loans_seed.csv")
        if tab == "Loan_Schedule":
            return _csv_rows("loan_schedule_seed.csv")
        raise RuntimeError("no such tab: " + tab)

    sheets = MagicMock(spec=SheetsClient)
    sheets.read_range.side_effect = read_range
    sheets.read_table.side_effect = read_table
    return sheets


def _plant(pk, om=8000.0):
    return PlantConfig(
        plant_key=pk, customer=pk + " CUSTOMER", brand="growatt",
        site_id="1", kwp_dc=100.0, kwp_ac=90.0, lat=None, lon=None,
        expected_factor=0.8, pr_target=0.8, installation_date="",
        secret_api_name="", secret_user_name="", secret_pass_name="",
        weather_plant_id="", datalogger_sn="", datalogger_addr=0,
        active=True, tariff_mxn_per_kwh=TARIFFS[pk],
        om_cost_monthly_mxn=om)


def _portfolio(om=8000.0):
    plants = [_plant(pk, om) for pk in JULY_MTD_ENERGY]
    p = MagicMock(spec=Portfolio)
    p.active_plants.return_value = plants
    # v74: the financial surfaces read the report-axis accessor; the
    # fixture keeps both in sync (all fixture plants are visible)
    p.financial_plants.return_value = plants
    return p


JULY_MTD = Period.from_iso("2026-07-01", "2026-07-07")
JULY_FULL = Period.from_iso("2026-07-01", "2026-07-31")


class TestBuilder:
    def test_eight_assets_resolved(self):
        data = build_finance_report_data(_sheets(), _portfolio(), JULY_MTD)
        keys = sorted(a.plant_key for a in data.assets)
        assert keys == ["GTO1", "LGTO1", "LOAX1", "MEX1", "MEX2", "NL1",
                        "SLP1", "SLP2"]
        assert {a.typ for a in data.assets
                if a.plant_key in ("LOAX1", "LGTO1")} == {"LaaS"}

    def test_full_month_matches_approved_sample(self):
        data = build_finance_report_data(_sheets(), _portfolio(),
                                         JULY_FULL)
        assert data.expected_total == pytest.approx(1754409.06, abs=1.0)
        assert data.service_total == pytest.approx(1267488.22, abs=0.05)
        assert data.om_total == pytest.approx(48000.0)

    def test_mtd_actual_revenue_and_proration(self):
        data = build_finance_report_data(_sheets(), _portfolio(), JULY_MTD)
        ppa_actual = sum(a.actual_mxn for a in data.assets
                         if a.typ == "PPA")
        assert ppa_actual == pytest.approx(207832.36, abs=0.5)
        assert data.service_total == pytest.approx(
            1267488.22 * 7 / 31, abs=0.05)
        laas = {a.plant_key: a for a in data.assets if a.typ == "LaaS"}
        assert laas["LOAX1"].actual_mxn == pytest.approx(
            26750 * 17.98 * 7 / 31, abs=0.01)

    def test_usd_share_and_fx_neutral_inputs(self):
        data = build_finance_report_data(_sheets(), _portfolio(),
                                         JULY_FULL)
        assert 0.43 < data.usd_service_share < 0.45

    def test_slp1_uses_true_refinanced_payment(self):
        data = build_finance_report_data(_sheets(), _portfolio(),
                                         JULY_FULL)
        slp1 = next(a for a in data.assets if a.plant_key == "SLP1")
        assert slp1.service_mxn == pytest.approx(12500.00)

    def test_missing_om_listed_not_fatal(self):
        data = build_finance_report_data(_sheets(), _portfolio(om=None),
                                         JULY_MTD)
        assert sorted(data.om_plants_missing) == sorted(JULY_MTD_ENERGY)
        assert data.om_total == 0.0


class TestRenderer:
    def _html(self, period=JULY_FULL, om=8000.0):
        data = build_finance_report_data(_sheets(), _portfolio(om), period)
        return render_html(data)

    def test_logo_embedded(self):
        assert "data:image/png;base64," in self._html()

    def test_key_numbers_present(self):
        h = self._html()
        assert "1,754,409" in h
        assert "1,267,488" in h
        assert "12,500" in h

    def test_footer_is_generated_from_registry(self):
        h = self._html()
        # verbatim registry fragments — if provenance.py changes, the
        # footer changes with it (and vice versa this test breaks on
        # hand-edited footer text)
        assert COLUMN_NOTES["Plants"]["om_cost_monthly_mxn"][:30] in h
        assert "projection, not a commitment" in h.replace("\n", " ")
        assert "principal+interest combined" in h

    def test_watch_list_appears_when_dscr_below_one(self):
        h = self._html(period=JULY_MTD)
        # MEX1 MTD runs below 1.0x on real numbers
        assert "Watch: MEX1" in h or "Watch:" in h and "MEX1" in h

    def test_om_missing_note(self):
        h = self._html(om=None)
        assert "om_cost_monthly_mxn is blank" in h

    def test_prorated_label_on_partial_period(self):
        assert "prorated" in self._html(period=JULY_MTD)
        # and absent for an exact full month
        assert "debt &amp; O&amp;M prorated" not in self._html(
            period=JULY_FULL)


class TestHeaderContracts:
    def test_seed_headers_still_match_modules(self):
        # the builder reads the same tabs the migrations wrote
        assert list(_csv_rows("loans_seed.csv")[0]) == LOANS_HEADER
        assert list(_csv_rows("loan_schedule_seed.csv")[0]) == \
            SCHEDULE_HEADER


def test_dscr_definition_in_audit_footer():
    """User audit question 2026-07-09: the portfolio DSCR must state its
    aggregation in the audit text — summed revenue over summed service,
    not an average of per-asset ratios."""
    data = build_finance_report_data(_sheets(), _portfolio(),
                                     Period.from_iso("2026-07-01",
                                                     "2026-07-31"))
    h = render_html(data)
    flat = " ".join(h.split())
    assert "Σ revenue ÷ Σ debt service" in flat
    assert "NOT an average of per-asset ratios" in flat


def test_kwp_and_loan_position_in_pdf(monkeypatch=None):
    """User request 2026-07-10: plant size in the asset name, loan
    position column (paid/total, active loans only)."""
    data = build_finance_report_data(_sheets(), _portfolio(),
                                     Period.from_iso("2026-07-01",
                                                     "2026-07-31"))
    by_key = {a.plant_key: a for a in data.assets}
    assert by_key["GTO1"].installments == "22/84"
    assert by_key["SLP1"].installments == "2/12"   # L1 done, L2 active
    assert by_key["GTO1"].kwp_dc == pytest.approx(100.0)  # fixture value
    assert by_key["LOAX1"].kwp_dc is None          # LaaS: no Plants row
    h = render_html(data)
    assert "Loan position" in h and "22/84" in h and "2/12" in h
    assert "100 kWp" in h
