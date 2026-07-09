"""Shared report output plumbing: HTML→PDF and the Report_Outbox queue.

Moved verbatim from scripts/report_daily.py (v64) so the finance report
uses the exact same rendering and delivery path — one Chromium print
profile, one outbox schema, no drift. report_daily re-exports these
names for backward compatibility (tests and the notifier contract).
"""

from __future__ import annotations

import os


def html_to_pdf(html_path: str, pdf_path: str) -> None:
    """Print the HTML to PDF with headless Chromium — renders the inline
    SVG charts and web fonts exactly as a browser does."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{os.path.abspath(html_path)}")
        page.wait_for_load_state("networkidle")   # let fonts arrive
        page.pdf(path=pdf_path, format="A4",
                 margin={"top": "12mm", "bottom": "12mm",
                         "left": "10mm", "right": "10mm"},
                 print_background=True)
        browser.close()


OUTBOX_TAB = "Report_Outbox"
OUTBOX_HEADER = ["date_iso", "kind", "pdf_file_id", "html_file_id",
                 "created_utc", "notified_at", "channel"]


def append_outbox(sheets, *, date_iso: str, kind: str,
                  pdf_file_id: str | None, html_file_id: str | None,
                  now_utc_iso: str, channel: str = "reporting") -> None:
    """Queue the uploaded report for e-mail delivery.

    The Apps Script notifier (docs/notifier.gs) scans this APPEND-ONLY tab
    every few minutes, mails rows whose notified_at is empty (PDF attached
    from Drive), and stamps notified_at. Append-only means the stamp can
    never be wiped by a rewrite — unlike the engine-owned Alerts tab, which
    is why alerts use a separate ledger instead.
    """
    sheets.ensure_tab(OUTBOX_TAB)
    sheets.ensure_header(OUTBOX_TAB, OUTBOX_HEADER)
    sheets.append_rows(OUTBOX_TAB, [[
        date_iso, kind, pdf_file_id or "", html_file_id or "",
        now_utc_iso, "", channel,
    ]])
