#!/usr/bin/env python3
"""Generate the customer invoicing annex (v93) — self-contained HTML.

On-demand (no cron): one HTML annex per plant, covering a selectable
year. The embedded month picker lets the customer read any month of that
year; ``Descargar`` prints to PDF. Fed entirely by the Google Sheet
(KPI_Daily energy/billable + performance columns, Contract_Monthly
tariff). Energía compensada comes from the stamped ``billable_kwh`` (v91
deemed engine) — never recomputed here.

Per-client isolation: ``--channel`` renders an annex for every plant on
that client_channel; ``--plant`` renders a single one.

USAGE
    # one plant, current year, write locally (no upload)
    PYTHONPATH=. python scripts/report_invoice_annex.py --plant MEX2 --dry-run
    # a client channel, a specific year
    PYTHONPATH=. python scripts/report_invoice_annex.py --channel faurecia --year 2026
    # upload to Drive
    PYTHONPATH=. python scripts/report_invoice_annex.py --plant MEX2

EXIT CODES
    0 ok   2 nothing to render   3 config error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile

from argia.core.config import load_portfolio
from argia.core.sheets import SheetsClient
from argia.core.time_utils import now_mx
from argia.finance.annex import build_annex_data, render_annex_html
from argia.finance.income import Period

LOG = logging.getLogger("argia.report.invoice_annex")


def _year_window(year: int) -> Period:
    return Period.from_iso("%04d-01-01" % year, "%04d-12-31" % year)


def previous_month(year: int, month: int):
    """Calendar month before (year, month). Pure — Jan rolls to prior Dec."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def last_complete_month(now) -> str:
    """'YYYY-MM' of the month that has fully ended as of ``now`` (MX). On
    the 1st this is the month that just closed — the invoice period."""
    y, m = previous_month(now.year, now.month)
    return "%04d-%02d" % (y, m)


def month_window(ym: str) -> Period:
    """A single-calendar-month Period from 'YYYY-MM'. Pure."""
    y, m = int(ym[:4]), int(ym[5:7])
    from calendar import monthrange
    return Period.from_iso("%s-01" % ym, "%s-%02d" % (ym, monthrange(y, m)[1]))


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")


def _target_plants(portfolio, args) -> list:
    if args.plant:
        pk = args.plant.upper()
        if pk not in portfolio.plants:
            LOG.error("unknown plant %s", pk)
            return []
        return [pk]
    if args.channel:
        sub = portfolio.for_client_channel(args.channel)
        return sorted(p.plant_key for p in sub.active_plants())
    # default: every PPA plant that shows on financial reports
    return sorted(p.plant_key for p in portfolio.financial_plants())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--plant", default=None, help="single plant key")
    parser.add_argument("--channel", default=None,
                        help="client_channel -> one annex per plant")
    parser.add_argument("--year", type=int, default=None,
                        help="calendar year (default: current MX year)")
    parser.add_argument("--month", default=None,
                        help="single billing month 'YYYY-MM' (invoice mode)")
    parser.add_argument("--last-month", action="store_true",
                        help="the month that just closed (for the 1st-of-"
                             "month cron) — a single-month invoice")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="render locally; no Drive upload")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.error("GOOGLE_SHEET_ID_V2 not set")
        return 3
    try:
        sheets = SheetsClient(sheet_id=sheet_id)
        portfolio = load_portfolio(sheets)
    except Exception as e:  # noqa: BLE001
        LOG.error("bootstrap failed: %s", e)
        return 3

    # Two outputs from one generator:
    #   invoice mode  (--last-month / --month) → the single CLOSED month,
    #                 file "invoice_<plant>_<YYYY-MM>.html". This is the
    #                 1st-of-month cron output.
    #   annex mode    (default / --year)       → the whole year with the
    #                 in-browser month picker, file "annex_<plant>_<year>"
    #                 — the "big report", run on demand.
    ym = None
    if args.last_month:
        ym = last_complete_month(now_mx())
    elif args.month:
        ym = args.month
    if ym is not None:
        window = month_window(ym)
        mode = "invoice"
    else:
        year = args.year or now_mx().year
        window = _year_window(year)
        mode = "annex"

    plants = _target_plants(portfolio, args)
    if not plants:
        LOG.warning("no plants matched — nothing to render")
        return 2

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_annex_")
    os.makedirs(out_dir, exist_ok=True)
    generated_at = now_mx().strftime("%Y-%m-%d %H:%M MX")

    rendered = []
    for pk in plants:
        try:
            payload = build_annex_data(sheets, portfolio, pk, window)
            html = render_annex_html(payload, generated_at)
        except Exception as e:  # noqa: BLE001
            LOG.error("[%s] annex render failed: %s", pk, e)
            continue
        if mode == "invoice":
            base = "invoice_%s_%s" % (pk.lower(), ym)
        else:
            base = "annex_%s_%d" % (pk.lower(), window.start.year)
        path = os.path.join(out_dir, base + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        LOG.info("[%s] rendered %s (%d KiB)", pk, path, len(html) // 1024)
        rendered.append((pk, path))

    if not rendered:
        return 2

    # The self-contained HTML file IS the publishable artifact (Pi + HTML
    # stack). Publishing/serving is the pipeline's job; this script only
    # writes the files to --out-dir. --dry-run is a synonym here (nothing
    # is pushed anywhere), kept for flag consistency with sibling scripts.
    LOG.info("%d annex file(s) written to %s", len(rendered), out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
