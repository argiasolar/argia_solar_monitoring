"""v263 - every scheduled job runs for real against the seeded database.

Each job's main() is called in its safe mode (--dry-run, or writing into a
temp dir) against the private PostgreSQL from conftest.py, exactly as the
systemd timer calls it on pio06. The test asserts the exit code AND a line
the job prints only when it did its work - a job that silently skips
("nothing to do") does not pass.

Jobs that need a vendor API, Google, IMAP or the internet cannot run here;
they are listed in NOT_RUNNABLE_HERE with the reason, and
tests/contracts/test_inventory.py makes sure every job on the server is
either in JOBS below or in that list - a new job cannot be forgotten.
"""
from __future__ import annotations

import importlib
import logging
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

# the switches pio06 runs with (pinned here: the root conftest pins the legacy sheet values)
SERVER_ENV = {
    "ARGIA_PG_MIRROR": "1",
    "ARGIA_TELEMETRY_SOURCE": "pg", "ARGIA_KPI_SOURCE": "pg", "ARGIA_FINANCE_SOURCE": "pg",
    "ARGIA_INVOICING_SOURCE": "pg", "ARGIA_ALERTS_SOURCE": "pg", "ARGIA_DASHBOARD_SOURCE": "pg",
    "ARGIA_CONFIG_SOURCE": "pg", "ARGIA_KPI_WRITE": "pg",
    "ARGIA_SHEET_TELEMETRY": "0", "ARGIA_SHEET_OUTBOX": "0",
}

# (script, argv, regex that proves the job did its work). {tmp} -> a temp dir.
JOBS = [
    ("alerts_snapshot", ["--dry-run"], r"MEX3: no telemetry"),
    ("alerts_daily", ["--dry-run"], r"\[DRY RUN\] no rows written"),
    ("kpi_eod", [], r"Stamped \d+ billable_kwh"),
    ("recon_snapshot", ["--dry-run"], r"DONE: reconciliation rows upserted=\d+ dry_run=True"),
    ("recon_close", ["--dry-run"], r"DONE: \d{4}-\d{2} months-rows"),
    ("thermal_daily", ["--dry-run"], r"DONE: [1-9]\d* inverter-days evaluated"),
    ("alert_mailer", ["--dry-run"], r"active=\d+ to_send=\d+"),
    ("daily_perf_mail", ["--dry-run"], r"dry-run: HTML preview at"),
    ("financial_report_publish", ["--out", "{tmp}/fin.html"], r"rendered .*: \d+ KiB, 6 plants"),
    ("cfe_engine_push", ["--dry-run"], r"dry-run: valid, nothing pushed"),
    ("report_daily", ["--dry-run", "--html-only", "--out-dir", "{tmp}"], r"HTML written: .*ARGIA_Daily_"),
    ("dashboard_update", [], r"\[dry-run\] PostgreSQL: would rewrite dashboard_inverter"),
    ("dashboard_html_publish", ["--out", "{tmp}/dash.html"], r"rendered .*plants=\["),
    ("portfolio_export", ["--out", "{tmp}/portfolio.json"], r"11 plants, 22 inverters"),
    ("invoice_publish", ["--last-month", "--out-root", "{tmp}", "--no-pdf"], r"index written; months published"),
    ("fin_savio_recon", [], r"SAVIO_ONLY|TRACKER_ONLY|MATCH|findings"),
]

# job script -> why it cannot run in a test (it still gets the static checks
# and whatever unit tests cover its pure parts)
NOT_RUNNABLE_HERE = {
    "telemetry_5m": "calls the Growatt / Huawei / SolarEdge APIs",
    "string_daily": "calls the Growatt API for string data",
    "satellite_check": "calls the satellite irradiance service on the internet",
    "soiling_check": "reads the Google Sheet",
    "archive_month_pg": "uploads to Google Drive",
    "telemetry_archive": "uploads to Google Drive",
    "fin_drive_ingest": "reads Google Drive",
    "ags_ingest": "downloads the Golden Standard from the web",
    "client_reports_publish": "uploads to the clients' FTP",
    "ticket_mail_in": "reads the IMAP mailbox",
    "financial_mail": "prints the financial page to PDF with Chromium and mails it",
    "cfe_ingest.py": "loads the CSVs the office Pi uploads (server/bundle)",
    "db_backup.sh": "pg_dump of the production database",
    "drift_check": "compares /etc and /opt on pio06 with the repo",
}


def _run(script, argv, env, monkeypatch, capsys, caplog):
    for k, v in {**SERVER_ENV, **{k: env[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")}}.items():
        monkeypatch.setenv(k, v)
    caplog.set_level(logging.INFO)
    mod = importlib.import_module(script)
    try:
        rc = mod.main(argv)
    except SystemExit as e:              # argparse / explicit exits
        rc = e.code
    out = capsys.readouterr()
    return (0 if rc is None else rc), out.out + out.err + caplog.text


@pytest.mark.parametrize("script, argv, proof", JOBS, ids=[j[0] for j in JOBS])
def test_job_runs_and_does_its_work(script, argv, proof, fresh_db, tmp_path, monkeypatch, capsys, caplog):
    argv = [a.replace("{tmp}", str(tmp_path)) for a in argv]
    rc, text = _run(script, argv, fresh_db, monkeypatch, capsys, caplog)
    assert rc == 0, f"{script} exited {rc}:\n{text[-3000:]}"
    assert re.search(proof, text), f"{script} ran but did not show /{proof}/:\n{text[-3000:]}"
    assert "Traceback" not in text, text[-3000:]


def test_dry_run_jobs_leave_the_database_untouched(fresh_db, psql, tmp_path, monkeypatch, capsys, caplog):
    tables = ("alert_ledger", "reconciliation_daily", "reconciliation_monthly", "thermal_daily", "invoicing")
    before = {t: psql(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {t} t")[0] for t in tables}
    for script, argv, _ in JOBS:
        if "--dry-run" in argv:
            _run(script, [a.replace("{tmp}", str(tmp_path)) for a in argv], fresh_db, monkeypatch, capsys, caplog)
    after = {t: psql(f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) FROM {t} t")[0] for t in tables}
    assert before == after, "a --dry-run job wrote to the database"
