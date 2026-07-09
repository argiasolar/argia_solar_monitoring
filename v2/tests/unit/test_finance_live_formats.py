"""Regression guards for the v64 live-format incident.

First real Pi run of report_finance showed service=0, actual=0 and
LaaS expected missing: the Sheets API returned FORMATTED values —
US-style dates ("10/1/2024", because Sheets auto-parsed the migration's
"2024-10" strings into date cells) and comma-grouped numbers
("94,668.89") — which the finance loaders' plain float()/[:7] parsing
silently rejected, emptying the whole schedule. These tests feed the
loaders exactly what the live sheet serves; they must parse, not skip.
"""

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.finance.contract import CONTRACT_HEADER, load_contract_monthly
from argia.finance.income import Period, load_kpi_energy
from argia.finance.loans import load_loan_schedule, monthly_debt_service


class TestScheduleLiveFormats:
    def _load(self, ref_month, payment, due="10,365,302.11"):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_table.return_value = [{
            "loan_id": "GTO1-L1", "plant_key": "GTO1",
            "ref_month": ref_month, "installment_no": "22",
            "total_installments": "84", "payment_mxn": payment,
            "payment_ccy": "", "xr": "", "due_after_mxn": due,
        }]
        return load_loan_schedule(sheets)

    def test_us_formatted_date_cell(self):
        # what FORMATTED_VALUE serves after Sheets auto-parsed "2026-07"
        rows = self._load("7/1/2026", "151,558.14")
        assert len(rows) == 1
        assert rows[0].ref_month == "2026-07"
        assert rows[0].payment_mxn == pytest.approx(151558.14)
        assert monthly_debt_service(rows, "2026-07") == {
            "GTO1": pytest.approx(151558.14)}

    def test_iso_datetime_string(self):
        rows = self._load("2026-07-01 00:00:00", "151558.14")
        assert rows[0].ref_month == "2026-07"

    def test_datetime_object(self):
        rows = self._load(dt.datetime(2026, 7, 1), "151558.14")
        assert rows[0].ref_month == "2026-07"

    def test_sheets_serial_number(self):
        # UNFORMATTED_VALUE for 2026-07-01
        serial = (dt.date(2026, 7, 1) - dt.date(1899, 12, 30)).days
        rows = self._load(serial, "151558.14")
        assert rows[0].ref_month == "2026-07"

    def test_plain_ym_string_passthrough(self):
        rows = self._load("2026-07", "151558.14")
        assert rows[0].ref_month == "2026-07"

    def test_comma_grouped_due_balance(self):
        rows = self._load("2026-07", "151,558.14")
        assert rows[0].due_after_mxn == pytest.approx(10365302.11)


class TestKpiLiveFormats:
    def test_us_date_and_comma_energy(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [
            ["date_iso", "plant_key", "energy_kwh"],
            ["7/4/2026", "GTO1", "4,966.20"],
            [dt.datetime(2026, 7, 5), "GTO1", 2558.7],
        ]
        kpi = load_kpi_energy(sheets,
                              Period.from_iso("2026-07-01", "2026-07-07"))
        assert kpi[("GTO1", "2026-07-04")] == pytest.approx(4966.20)
        assert kpi[("GTO1", "2026-07-05")] == pytest.approx(2558.7)

    def test_unparseable_rows_skipped_not_fatal(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [
            ["date_iso", "plant_key", "energy_kwh"],
            ["not a date", "GTO1", "1"],
            ["2026-07-03", "GTO1", "n/a"],
            ["2026-07-03", "SLP1", "100"],
        ]
        kpi = load_kpi_energy(sheets,
                              Period.from_iso("2026-07-01", "2026-07-07"))
        assert kpi == {("SLP1", "2026-07-03"): 100.0}


class TestContractLiveFormats:
    def test_comma_grouped_kwh(self):
        sheets = MagicMock(spec=SheetsClient)
        sheets.read_range.return_value = [
            CONTRACT_HEADER,
            ["GTO1", "2,026", "7", "126,721", "94,698", "1.975", "", ""],
        ]
        cm = load_contract_monthly(sheets)
        row = cm[("GTO1", 2026, 7)]
        assert row.contract_kwh == pytest.approx(94698)
        assert row.design_kwh == pytest.approx(126721)
