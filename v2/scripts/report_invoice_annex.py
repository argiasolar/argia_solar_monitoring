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
from argia.core.sheets import open_sheets
from argia.core.time_utils import now_mx
from argia.finance.annex import (FACTURA_NAME, build_annex_data,
                                 load_invoicing_overview,
                                 render_annex_html)
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


def reconciliation_gate(ym: str, plants: list) -> tuple:
    """(allowed, blocked) for invoice mode — v3 item 12: no invoice annex
    for a month whose reconciliation is not CLOSED (PASS auto-closes on
    the 1st at 06:10 MX; REVIEW/FAIL need a manual close).

    On the server the gate FAILS CLOSED: if the close table cannot be
    read, nothing is invoiced (the alert mailer will already be
    complaining). Off-server (Pi/CI, no PG) the gate is inert.
    """
    try:
        from argia.store import pg_mirror
        if not pg_mirror.enabled():
            return list(plants), []
        from argia.store.pgq import psql_rows
        closed = {r[0] for r in psql_rows(
            "SELECT plant_key FROM reconciliation_monthly"
            f" WHERE ref_month = DATE '{ym}-01'"
            " AND closed_at IS NOT NULL;") if r and r[0]}
    except Exception as e:  # noqa: BLE001
        LOG.error("reconciliation gate unreadable (%s) — refusing to "
                  "invoice ANY plant for %s (fail-closed)", e, ym)
        return [], list(plants)
    allowed = [p for p in plants if p in closed]
    blocked = [p for p in plants if p not in closed]
    return allowed, blocked


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

    try:
        sheets = open_sheets()          # v199: NullSheets once retired
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
        # v158: an invoice embeds the WHOLE year (the old Looker factura
        # shows year-to-date results and the January–December table on
        # every monthly invoice) and opens on the invoiced month.
        window = _year_window(int(ym[:4]))
        mode = "invoice"
    else:
        year = args.year or now_mx().year
        window = _year_window(year)
        mode = "annex"

    plants = _target_plants(portfolio, args)
    if mode == "invoice":
        plants, blocked = reconciliation_gate(ym, plants)
        for pk in blocked:
            LOG.warning("[%s] %s reconciliation NOT closed — invoice "
                        "annex BLOCKED (close the month on the recon "
                        "board first)", pk, ym)
    if not plants:
        LOG.warning("no plants matched — nothing to render")
        return 2

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_annex_")
    os.makedirs(out_dir, exist_ok=True)
    generated_at = now_mx().strftime("%Y-%m-%d %H:%M MX")

    # the invoicing authority for the year (ARGIA Solar workbook);
    # {} degrades to atoms with a logged warning
    history_all = load_invoicing_overview(window.start.year)

    rendered = []
    for pk in plants:
        try:
            payload = build_annex_data(sheets, portfolio, pk, window,
                                       history=history_all.get(pk))
            html = render_annex_html(payload, generated_at,
                                     default_ym=ym)
        except Exception as e:  # noqa: BLE001
            LOG.error("[%s] annex render failed: %s", pk, e)
            continue
        if mode == "invoice":
            base = "factura_%s_%s" % (FACTURA_NAME.get(pk, pk),
                                      ym.replace("-", ""))
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
