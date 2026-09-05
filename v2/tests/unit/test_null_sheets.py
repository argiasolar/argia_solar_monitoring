"""v199 — the finish-line contract: a job bootstraps without
GOOGLE_SHEET_ID_V2 once every switch is on PostgreSQL; a stray sheet
call then fails loudly; a half-retired configuration fails at bootstrap."""
import pathlib

import pytest

from argia.core import sheets as S

V2 = pathlib.Path(__file__).resolve().parents[2]

ALL_PG = {"ARGIA_TELEMETRY_SOURCE": "pg", "ARGIA_KPI_SOURCE": "pg",
          "ARGIA_FINANCE_SOURCE": "pg", "ARGIA_INVOICING_SOURCE": "pg",
          "ARGIA_ALERTS_SOURCE": "pg", "ARGIA_DASHBOARD_SOURCE": "pg",
          "ARGIA_CONFIG_SOURCE": "pg", "ARGIA_KPI_WRITE": "pg",
          "ARGIA_SHEET_TELEMETRY": "0", "ARGIA_SHEET_OUTBOX": "0"}


class TestStillNeeded:
    def test_fresh_env_needs_nothing(self):
        """v205: the code defaults are pg; a bare env has no reason."""
        assert S.sheet_still_needed({}) == []
        r = S.sheet_still_needed({"ARGIA_CONFIG_SOURCE": "sheet", "ARGIA_KPI_WRITE": "sheet",
                                  "ARGIA_SHEET_TELEMETRY": "1", "ARGIA_SHEET_OUTBOX": "1"})
        assert "ARGIA_CONFIG_SOURCE=sheet" in r and "ARGIA_KPI_WRITE=sheet" in r
        assert "ARGIA_SHEET_TELEMETRY=on" in r and "ARGIA_SHEET_OUTBOX=on" in r
        assert "ARGIA_SHEET_JOBLOG=on" not in r          # joblog default is off

    def test_a_shadow_env_still_needs_the_sheet(self):
        env = dict(ALL_PG); env["ARGIA_SHEET_TELEMETRY"] = "1"; env["ARGIA_KPI_WRITE"] = "both"
        assert S.sheet_still_needed(env) == ["ARGIA_KPI_WRITE=both", "ARGIA_SHEET_TELEMETRY=on"]

    def test_all_pg_needs_nothing(self):
        assert S.sheet_still_needed(ALL_PG) == []


class TestOpenSheets:
    def test_id_set_gives_a_real_client(self, monkeypatch):
        made = {}
        monkeypatch.setattr(S, "SheetsClient", lambda sheet_id: made.setdefault("id", sheet_id))
        assert S.open_sheets({"GOOGLE_SHEET_ID_V2": " abc "}) == "abc"

    def test_unset_and_retired_gives_null(self):
        assert isinstance(S.open_sheets(ALL_PG), S.NullSheets)

    def test_unset_but_needed_fails_at_bootstrap(self):
        env = dict(ALL_PG); env["ARGIA_FINANCE_SOURCE"] = "sheet"
        with pytest.raises(S.SheetsRequired, match="ARGIA_FINANCE_SOURCE=sheet"):
            S.open_sheets(env)

    def test_null_client_fails_loudly_with_tab_and_method(self):
        n = S.NullSheets()
        with pytest.raises(S.SheetsRetired, match=r"read_table\('Plants'\)"):
            n.read_table("Plants", "A1:ZZ")
        with pytest.raises(S.SheetsRetired, match="write_values"):
            n.write_values("Alerts", "A2:O3", [])
        assert n.sheet_id == "" and repr(n) == "NullSheets()"


class TestJobsBootstrapThroughTheDoor:
    JOBS = ["telemetry_5m", "kpi_eod", "alerts_daily", "alerts_snapshot", "report_daily",
            "report_client_daily", "dashboard_update", "dashboard_html_publish",
            "financial_report_publish", "report_invoice_annex", "recon_snapshot",
            "recon_close", "string_daily", "telemetry_archive"]

    def test_no_live_job_requires_the_id_itself(self):
        for j in self.JOBS:
            src = (V2 / "scripts" / f"{j}.py").read_text(encoding="utf-8")
            assert "open_sheets()" in src, j
            assert 'GOOGLE_SHEET_ID_V2 not set' not in src and "GOOGLE_SHEET_ID_V2 is not set" not in src, j
            assert "SheetsClient(sheet_id=" not in src and "SheetsClient(sheet_id)" not in src, j
