"""Mail the daily PDF report from the server (v196).

Until 2026-09-02 the Apps Script notifier read ``Report_Outbox`` and
mailed the morning ("yesterday") and evening ("today") PDFs to the
'reporting' recipients. It died silently; the sheet path is being
retired anyway. report_daily now mails the PDF it just rendered to the
'reports' channel of mail_subscription (managed in /setup/, portal users
only) — fail CLOSED like the notifier: no subscriber, no mail, logged.
Never raises: the report is already rendered and uploaded; a mail
problem must not turn the run red.
"""
from __future__ import annotations

import logging
import os
from typing import List, Tuple

LOG = logging.getLogger("argia.report.report_mail")

CHANNEL = "reports"
LABEL = {"morning_yesterday": "Morning report — yesterday's full day",
         "evening_today": "Evening report — today so far"}


def subject_body(date_iso: str, kind: str) -> Tuple[str, str]:
    """Pure."""
    label = LABEL.get(kind, kind)
    subject = f"[ARGIA] Daily report {date_iso} — {label.split(' — ')[0]}"
    body = (f"ARGIA fleet daily report for {date_iso}.\n"
            f"{label}.\n\nThe PDF is attached; the HTML edition is in the "
            f"Reports folder on Drive.\n\n— ARGIA Monitoring (service@argia.com.mx)\n")
    return subject, body


def recipients() -> List[str]:
    from argia.alerts import subscriptions
    return [e for e, _ in subscriptions.only_portal(
        subscriptions.recipients_for(CHANNEL), subscriptions.portal_emails())]


def send_report(pdf_path: str, date_iso: str, kind: str,
                dry_run: bool = False) -> bool:
    """True when the mail went out (or would, in dry-run)."""
    from argia.alerts import emailer
    if not pdf_path or not os.path.exists(pdf_path):
        LOG.warning("report mail: no PDF to attach — nothing sent")
        return False
    try:
        rcpt = recipients()
    except Exception as e:  # noqa: BLE001
        LOG.warning("report mail: recipients unavailable (%s) — not sent", e)
        return False
    if not rcpt:
        LOG.warning("report mail: no enabled '%s' subscribers — not sent "
                    "(subscribe in /setup/)", CHANNEL)
        return False
    subject, body = subject_body(date_iso, kind)
    if dry_run:
        LOG.info("[DRY RUN] would mail %s to %s", subject, ", ".join(rcpt))
        return True
    cfg = emailer.load_smtp()
    if not cfg:
        LOG.warning("report mail: no SMTP config — not sent")
        return False
    msg = emailer.build_email(subject, body, cfg["SMTP_USER"], rcpt)
    with open(pdf_path, "rb") as fh:
        msg.add_attachment(fh.read(), maintype="application", subtype="pdf",
                           filename=os.path.basename(pdf_path))
    if emailer.send(msg, cfg):
        LOG.info("report mail: %s -> %d recipient(s)", subject, len(rcpt))
        return True
    LOG.error("report mail: send FAILED for %s", subject)
    return False
