"""Email the financial report to the mailing list (pio06 only).

Tomasz, 2026-09-02: "send financial report to the mailing list every
friday EOD (23:59)" and "every EOD of the 1st day of the month", plus
"store reports ... maybe instead of Pi store it in google drive".

What one run does:
  1. picks the reporting window in America/Mexico_City time:
       --mode weekly   -> month-to-date (1st .. today)     [Fri 23:59]
       --mode monthly  -> the just-closed previous month   [1st 23:59]
  2. prints the LIVE financial page to PDF with headless chromium,
     straight from the webroot file (no HTTP, no auth dance); the page
     computes any window client-side from its embedded daily atoms, so
     the window is passed as a #d0=..&d1=.. fragment (report_gen reads
     it since v166),
  3. mails the PDF to the enabled 'financial' channel of
     ``mail_subscription`` (managed in /setup/, portal users only),
  4. archives a copy to Google Drive Archive/Financial_Reports/<YYYY>/
     (the folder tree the invoicing archive already lives in) and to
     the local backup dir, which the Pi mirrors nightly.

The financial page is PPA-internal: recipients on that list see loan
and DSCR figures, so the list — not this script — is the access
control, exactly as it is for alert mail. A tiny PDF (< 20 kB) means
chromium rendered a blank/failed page: the run aborts without sending
rather than mailing garbage.

Fires from systemd timers (argia-finmail-weekly / -monthly) with
OnCalendar in the America/Mexico_City timezone. On a Friday that is
also the 1st both fire: two different mails (month close + new MTD),
which is intended. Dry-run renders and reports but sends nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.alerts import emailer
from argia.core.time_utils import MX_TZ
from argia.store import pg_mirror

LOG = logging.getLogger("argia.financial_mail")

WEBROOT = "/www/hosting/monitoring.argia.com.mx/www"
CHROMIUM = ("chromium", "chromium-browser", "google-chrome")
MIN_PDF_BYTES = 20_000
BACKUP_REPORTS_DIR = os.environ.get(
    "ARGIA_BACKUP_REPORTS_DIR", "/root/argia_backups/reports")

MONTHS = ("January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November",
          "December")


def compute_window(mode: str, today: dt.date):
    """(d0, d1, human label) for the reporting window — pure.

    weekly  -> month-to-date through ``today``
    monthly -> the previous calendar month, complete
    """
    if mode == "monthly":
        first_this = today.replace(day=1)
        d1 = first_this - dt.timedelta(days=1)
        d0 = d1.replace(day=1)
        label = "%s %d — month close" % (MONTHS[d0.month - 1], d0.year)
    else:
        d0 = today.replace(day=1)
        d1 = today
        label = "%s %d to date (through %s)" % (
            MONTHS[d0.month - 1], d0.year, d1.isoformat())
    return d0.isoformat(), d1.isoformat(), label


def pdf_name(mode: str, d0: str, d1: str) -> str:
    if mode == "monthly":
        return "ARGIA_Financial_%s.pdf" % d0[:7]
    return "ARGIA_Financial_%s_to_%s.pdf" % (d0, d1)


def mail_subject(label: str) -> str:
    return "[ARGIA] Financial report — %s" % label


def mail_body(label: str, d0: str, d1: str) -> str:
    return (
        "Attached: the ARGIA financial report for %s\n"
        "(window %s .. %s, MXN, sin IVA).\n\n"
        "Live version with date picker:\n"
        "  https://report.argia.com.mx/financial/\n\n"
        "Figures are re-derived from the monitoring database at send "
        "time.\nDSCR = (revenue - O&M) / debt service for the window.\n\n"
        "Manage recipients: https://report.argia.com.mx/setup/\n"
        % (label, d0, d1))


def find_chromium():
    import shutil
    for name in CHROMIUM:
        p = shutil.which(name)
        if p:
            return p
    return None


def render_pdf(html_path: str, d0: str, d1: str, pdf_path: str) -> bool:
    chromium = find_chromium()
    if not chromium:
        LOG.error("no chromium binary found — cannot render PDF")
        return False
    url = "file://" + os.path.abspath(html_path) + "#d0=%s&d1=%s" % (d0, d1)
    r = subprocess.run(
        [chromium, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--virtual-time-budget=8000",
         "--no-pdf-header-footer", "--print-to-pdf=" + pdf_path, url],
        capture_output=True, text=True, timeout=180)
    ok = (r.returncode == 0 and os.path.exists(pdf_path)
          and os.path.getsize(pdf_path) >= MIN_PDF_BYTES)
    if not ok:
        LOG.error("pdf render failed: rc=%d size=%s %s", r.returncode,
                  os.path.getsize(pdf_path) if os.path.exists(pdf_path)
                  else "none", (r.stderr or "")[-200:])
    return ok


def recipients():
    """Enabled 'financial' channel subscribers (v176), portal accounts
    only. No plant scoping here — the financial report is one document
    and its recipient list IS the access control."""
    from argia.alerts import subscriptions
    return [e for e, _ in subscriptions.only_portal(
        subscriptions.recipients_for("financial"),
        subscriptions.portal_emails())]


def archive_drive(pdf_path: str, name: str, year: str):
    """Upload to Archive/Financial_Reports/<YYYY>/ — best-effort."""
    root = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if not root:
        LOG.warning("GOOGLE_ARCHIVE_FOLDER_ID not set — no Drive copy")
        return False
    try:
        from argia.core.drive import DriveClient
        d = DriveClient()
        folder = d.ensure_folder(d.ensure_folder(root, "Financial_Reports"),
                                 year)
        d.upload_file(folder, name, pdf_path, "application/pdf")
        return True
    except Exception as e:  # noqa: BLE001
        LOG.error("Drive archive failed: %s: %s", type(e).__name__, e)
        return False


def archive_local(pdf_path: str, name: str) -> bool:
    """Copy into the backup dir the Pi pulls nightly — best-effort."""
    import shutil
    try:
        os.makedirs(BACKUP_REPORTS_DIR, exist_ok=True)
        shutil.copyfile(pdf_path, os.path.join(BACKUP_REPORTS_DIR, name))
        return True
    except OSError as e:
        LOG.error("local archive failed: %s", e)
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="financial report mailer")
    parser.add_argument("--mode", choices=("weekly", "monthly"),
                        required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0

    today = dt.datetime.now(MX_TZ).date()
    d0, d1, label = compute_window(args.mode, today)
    name = pdf_name(args.mode, d0, d1)
    LOG.info("mode=%s window=%s..%s -> %s", args.mode, d0, d1, name)

    html = os.path.join(WEBROOT, "financial", "index.html")
    if not os.path.exists(html):
        LOG.error("financial page not found at %s", html)
        return 1
    pdf_path = os.path.join("/tmp", name)
    if not render_pdf(html, d0, d1, pdf_path):
        return 1
    LOG.info("rendered %s (%d bytes)", pdf_path,
             os.path.getsize(pdf_path))

    rcpt = recipients()
    if not rcpt:
        LOG.error("no enabled 'financial' subscribers — not sending")
        return 1
    cfg = emailer.load_smtp()
    if not cfg:
        LOG.error("no SMTP config (/root/.argia_mail) — not sending")
        return 1

    if args.dry_run:
        LOG.info("dry-run: would mail %s to %s, archive to Drive + %s",
                 name, rcpt, BACKUP_REPORTS_DIR)
        return 0

    msg = emailer.build_email(mail_subject(label),
                              mail_body(label, d0, d1),
                              cfg["SMTP_USER"], rcpt)
    with open(pdf_path, "rb") as fh:
        msg.add_attachment(fh.read(), maintype="application",
                           subtype="pdf", filename=name)
    if not emailer.send(msg, cfg):
        LOG.error("send FAILED — report not delivered")
        return 1
    LOG.info("sent %s to %d recipient(s)", name, len(rcpt))

    drive_ok = archive_drive(pdf_path, name, d0[:4])
    local_ok = archive_local(pdf_path, name)
    LOG.info("archive: drive=%s local=%s",
             "OK" if drive_ok else "FAILED",
             "OK" if local_ok else "FAILED")
    # the mail went out — archive failures are logged, not fatal
    return 0


if __name__ == "__main__":
    sys.exit(main())
