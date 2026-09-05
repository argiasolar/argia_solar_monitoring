"""v197 — the monthly archive runs on the server from PostgreSQL; the
two remaining scheduled GitHub Actions that read the sheet
(v2-watchdog, v2-archive-month) are manual-only."""
import datetime as dt
import pathlib

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
ROOT = V2.parent


@pytest.fixture
def mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "archive_month_pg", V2 / "scripts" / "archive_month_pg.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestPure:
    def test_previous_month_and_bounds(self, mod):
        assert mod.previous_month(dt.date(2026, 9, 2)) == "2026-08"
        assert mod.previous_month(dt.date(2026, 1, 1)) == "2025-12"
        assert mod.month_bounds("2026-12") == ("2026-12-01", "2027-01-01")
        assert mod.month_bounds("2026-08") == ("2026-08-01", "2026-09-01")

    def test_sql_half_open_month(self, mod):
        assert ("WHERE prod_date >= DATE '2026-08-01' AND prod_date < DATE '2026-09-01'"
                in mod.kpi_sql("2026-08"))
        assert "opened_utc >= '2026-08-01' AND opened_utc < '2026-09-01'" in mod.alerts_sql("2026-08")

    def test_file_names_and_row_count(self, mod):
        assert [n for n, _ in mod.file_names("2026-08")] == ["kpi_daily_2026-08.csv",
                                                              "alert_ledger_2026-08.csv"]
        assert mod.csv_row_count("a,b\n1,2\n3,\"x\ny\"\n") == 2
        assert mod.csv_row_count("a,b\n") == 0


class TestExport:
    class Drive:
        def __init__(self):
            self.folders = {}; self.uploaded = []
        def ensure_folder(self, parent, name):
            self.folders[(parent, name)] = f"{parent}/{name}"; return f"{parent}/{name}"
        def upload_file(self, folder, name, path, mime):
            self.uploaded.append((folder, name, open(path, encoding="utf-8").read(), mime)); return "id"
        def find_file(self, folder, name):
            return "id" if any(u[0] == folder and u[1] == name for u in self.uploaded) else None

    def test_dry_run_uploads_nothing(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_fetch_csv", lambda sql: "a\n1\n")
        d = self.Drive()
        assert mod.export(d, "ROOT", "2026-08", apply=False) == 0
        assert d.uploaded == [] and d.folders == {}

    def test_apply_writes_both_csvs_under_year_folder(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_fetch_csv", lambda sql: "a\n1\n" if "daily_production" in sql else "b\n")
        d = self.Drive()
        assert mod.export(d, "ROOT", "2026-08", apply=True) == 0
        assert [(u[0], u[1], u[3]) for u in d.uploaded] == [
            ("ROOT/Monthly_Archive/2026", "kpi_daily_2026-08.csv", "text/csv"),
            ("ROOT/Monthly_Archive/2026", "alert_ledger_2026-08.csv", "text/csv")]
        assert d.uploaded[0][2] == "a\n1\n"

    def test_failed_verify_is_exit_1(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_fetch_csv", lambda sql: "a\n")
        d = self.Drive(); d.find_file = lambda folder, name: None
        assert mod.export(d, "ROOT", "2026-08", apply=True) == 1


class TestSchedules:
    def test_only_the_unit_test_workflow_remains(self):
        # v207.1: every sheet-era Action is gone; nothing but pytest runs on GitHub
        names = sorted(p.name for p in (ROOT / ".github" / "workflows").glob("*.yml"))
        assert names == ["v2-tests.yml"]

    def test_server_unit_runs_the_pg_archive(self):
        svc = (V2 / "server" / "bundle" / "argia-archive-month.service").read_text(encoding="utf-8")
        assert "run_job.sh archive-month archive_month_pg.py --apply" in svc
        tmr = (V2 / "server" / "bundle" / "argia-archive-month.timer").read_text(encoding="utf-8")
        assert "OnCalendar=*-*-02 03:00:00 America/Mexico_City" in tmr and "Persistent=true" in tmr


class TestWatched:
    def test_archive_is_instrumented_and_watched_by_the_mailer(self):
        src = (V2 / "scripts" / "archive_month_pg.py").read_text(encoding="utf-8")
        assert '@instrument("archive_month_pg", write_if=apply_flag_write_if)' in src
        mailer = (V2 / "scripts" / "alert_mailer.py").read_text(encoding="utf-8")
        for u in ("argia-archive-month", "argia-dailyperf", "argia-invoice", "argia-recon-close"):
            assert f'"{u}"' in mailer, u
