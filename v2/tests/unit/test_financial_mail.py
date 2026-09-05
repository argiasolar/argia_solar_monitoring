"""Tests: financial mailer windows + the backup chain's contracts.

The mailer's window math is what decides which numbers the mailing
list sees — a wrong previous-month boundary would silently ship a
wrong month-close. The shell scripts are the off-site backup chain;
they must at least parse, and their key invariants (read-only pull,
PGDMP integrity gate, retention counts) are locked as text so a later
edit that drops one fails loudly here first.
"""

import datetime as dt
import pathlib
import subprocess

from scripts.financial_mail import (
    compute_window, mail_body, mail_subject, pdf_name)

V2 = pathlib.Path(__file__).resolve().parents[2]


class TestComputeWindow:
    def test_weekly_is_month_to_date(self):
        d0, d1, label = compute_window("weekly", dt.date(2026, 9, 18))
        assert (d0, d1) == ("2026-09-01", "2026-09-18")
        assert "September 2026 to date" in label

    def test_weekly_on_the_first_is_a_one_day_window(self):
        d0, d1, _ = compute_window("weekly", dt.date(2026, 10, 1))
        assert (d0, d1) == ("2026-10-01", "2026-10-01")

    def test_monthly_is_the_full_previous_month(self):
        d0, d1, label = compute_window("monthly", dt.date(2026, 9, 1))
        assert (d0, d1) == ("2026-08-01", "2026-08-31")
        assert label == "August 2026 — month close"

    def test_monthly_across_year_boundary(self):
        d0, d1, label = compute_window("monthly", dt.date(2027, 1, 1))
        assert (d0, d1) == ("2026-12-01", "2026-12-31")
        assert label == "December 2026 — month close"

    def test_monthly_handles_february(self):
        d0, d1, _ = compute_window("monthly", dt.date(2026, 3, 1))
        assert (d0, d1) == ("2026-02-01", "2026-02-28")

    def test_monthly_fires_late_still_reports_previous_month(self):
        # Persistent=true can fire the timer after a reboot mid-month
        d0, d1, _ = compute_window("monthly", dt.date(2026, 9, 14))
        assert (d0, d1) == ("2026-08-01", "2026-08-31")


class TestMailShape:
    def test_pdf_names(self):
        assert pdf_name("monthly", "2026-08-01", "2026-08-31") == \
            "ARGIA_Financial_2026-08.pdf"
        assert pdf_name("weekly", "2026-09-01", "2026-09-18") == \
            "ARGIA_Financial_2026-09-01_to_2026-09-18.pdf"

    def test_subject_and_body_carry_the_window(self):
        assert mail_subject("August 2026 — month close").startswith(
            "[ARGIA] Financial report")
        body = mail_body("August 2026 — month close",
                         "2026-08-01", "2026-08-31")
        assert "2026-08-01 .. 2026-08-31" in body
        assert "report.argia.com.mx/financial/" in body
        assert "sin IVA" in body
        # v204 (Tomasz): no boilerplate in the mail
        for gone in ("re-derived", "DSCR =", "Manage recipients", "/setup/"):
            assert gone not in body, gone

    def test_pdf_is_a_one_pager(self):
        """v204: the mailed PDF is the financial page printed; the print
        rules keep it on one A4 page — short asset names, 5 tiles in a
        row, no methodology card."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "server" / "bundle" / "report_gen.py"
               ).read_text(encoding="utf-8")
        fin = src[src.index("def financial_page"):src.index("def financial_page") + 12000]
        # the page is an f-string: braces are doubled in the source
        assert "@page{{size:A4 portrait;margin:8mm;}}" in fin
        assert "repeat(5,minmax(0,1fr))!important" in fin
        assert ".audit,.rangebar,.pdfrow,footer{{display:none!important;}}" in fin
        assert "const shortName=(m.name||k).split(" in fin and 'td.asset{{white-space:nowrap;}}' in fin


class TestReportGenWindowParams:
    def test_financial_page_reads_d0_d1_from_url(self):
        # encoding pinned — Windows defaults to cp1250 (v167 lesson)
        src = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
        assert "location.hash" in src and "location.search" in src
        assert "URLSearchParams" in src
        # strict date shape — garbage in the fragment must not stick
        assert r"^\d{{4}}-\d{{2}}-\d{{2}}$" in src


class TestBackupChain:
    def test_shell_scripts_parse(self):
        for rel in ("server/bundle/db_backup.sh",
                    "pi/db_backups/pull_backup.sh"):
            path = V2 / rel
            assert path.exists(), rel
            subprocess.run(["bash", "-n", str(path)], check=True)

    def test_dump_script_verifies_before_publishing(self):
        src = (V2 / "server/bundle/db_backup.sh").read_text()
        assert "PGDMP" in src                      # magic gate
        assert "argia_mont_latest.dump" in src     # stable pull name
        assert "mode=ro" in src                    # auth db opened read-only
        assert "tail -n +4" in src                 # keep 3 local

    def test_pull_script_invariants(self):
        src = (V2 / "pi/db_backups/pull_backup.sh").read_text()
        assert "PGDMP" in src
        assert "BatchMode=yes" in src
        assert "tail -n +15" in src                # 14 daily kept
        assert "tail -n +9" in src                 # 8 weekly kept
        assert "pg_restore -l" in src              # monthly deep test
        assert "ntfy.sh" in src                    # failures reach the phone
        assert "IdentitiesOnlky" not in src        # the typo, never again

    def test_timers_fire_in_mexico_city_time(self):
        for rel in ("server/bundle/argia-finmail-weekly.timer",
                    "server/bundle/argia-finmail-monthly.timer"):
            assert "America/Mexico_City" in (V2 / rel).read_text()
        weekly = (V2 / "server/bundle/argia-finmail-weekly.timer").read_text()
        assert "Fri" in weekly
        monthly = (V2 / "server/bundle/argia-finmail-monthly.timer").read_text()
        assert "*-*-01" in monthly

    def test_finmail_units_use_distinct_lock_names(self):
        # same flock name would make the Fri==1st double fire lose one
        w = (V2 / "server/bundle/argia-finmail-weekly.service").read_text()
        m = (V2 / "server/bundle/argia-finmail-monthly.service").read_text()
        assert "finmailw" in w and "finmailm" in m
