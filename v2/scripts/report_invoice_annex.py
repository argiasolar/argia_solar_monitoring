#!/usr/bin/env python3
"""Generate the customer invoicing annex (v93) - self-contained HTML.

On-demand (no cron): one HTML annex per plant, covering a selectable
year. The embedded month picker lets the customer read any month of that
year; ``Descargar`` prints to PDF. Fed entirely by the Google Sheet
(KPI_Daily energy/billable + performance columns, Contract_Monthly
tariff). Energía compensada comes from the stamped ``billable_kwh`` (v91
deemed engine) - never recomputed here.

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


def _month_end_iso(ym: str) -> str:
    """Last calendar day of YYYY-MM as ISO. Pure."""
    import calendar
    y, m = int(ym[:4]), int(ym[5:7])
    return "%04d-%02d-%02d" % (y, m, calendar.monthrange(y, m)[1])


def _year_window(year: int) -> Period:
    return Period.from_iso("%04d-01-01" % year, "%04d-12-31" % year)


def previous_month(year: int, month: int):
    """Calendar month before (year, month). Pure - Jan rolls to prior Dec."""
    return (year - 1, 12) if month == 1 else (year, month - 1)


def last_complete_month(now) -> str:
    """'YYYY-MM' of the month that has fully ended as of ``now`` (MX). On
    the 1st this is the month that just closed - the invoice period."""
    y, m = previous_month(now.year, now.month)
    return "%04d-%02d" % (y, m)


def month_window(ym: str) -> Period:
    """A single-calendar-month Period from 'YYYY-MM'. Pure."""
    y, m = int(ym[:4]), int(ym[5:7])
    from calendar import monthrange
    return Period.from_iso("%s-01" % ym, "%s-%02d" % (ym, monthrange(y, m)[1]))


def month_books(ym: str):
    """{plant: {"close": {...}, "daily_sum": x, "daily_days": n}} from
    PostgreSQL for the invariant gate; None off-server (no PG: the annex
    rules still run, the books rules cannot)."""
    try:
        from argia.store import pg_mirror
        if not pg_mirror.enabled():
            return None
        from argia.store.pgq import psql_rows
        out = {}
        for r in psql_rows(
                "SELECT plant_key, billing_kwh, closed_at IS NOT NULL FROM reconciliation_monthly"
                f" WHERE ref_month = DATE '{ym}-01';"):
            if len(r) >= 3 and r[0]:
                out.setdefault(r[0], {})["close"] = {
                    "kwh": float(r[1]) if r[1] not in ("", None) else None, "closed": r[2] == "t"}
        for r in psql_rows(
                "SELECT plant_key, sum(energy_kwh), count(DISTINCT prod_date) FILTER (WHERE energy_kwh IS NOT NULL)"
                f" FROM daily_production WHERE prod_date BETWEEN DATE '{ym}-01' AND DATE '{_month_end_iso(ym)}'"
                " GROUP BY 1;"):
            if len(r) >= 3 and r[0]:
                b = out.setdefault(r[0], {})
                b["daily_sum"] = float(r[1]) if r[1] not in ("", None) else None
                b["daily_days"] = int(float(r[2] or 0))
        return out
    except Exception as e:  # noqa: BLE001
        LOG.error("books unreadable for the invoice gate (%s) - every invoice HELD", e)
        return {"__unreadable__": str(e)}


def invoice_violations(ym: str, payload: dict, books, pk: str) -> list:
    """Every invariant for one plant's invoice annex. ``books`` None =
    off-server: annex rules only. Pure."""
    from argia.finance import invariants as INV
    if books is None:
        return INV.check_annex(payload, ym, payload.get("kwp"))
    if "__unreadable__" in books:
        return [INV.Violation("BOOKS", "reconciliation / daily table unreadable: " + books["__unreadable__"])]
    b = books.get(pk, {})
    return INV.check_invoice(ym, payload, b.get("close"), b.get("daily_sum"), b.get("daily_days", 0))


HELD_DIR = "_held"


def hold_previous(out_dir: str, base: str) -> None:
    """A held invoice must not leave an older copy published: move this
    plant's html / pdf for the month out of the served folder."""
    import shutil
    for ext in (".html", ".pdf"):
        src = os.path.join(out_dir, base + ext)
        if os.path.exists(src):
            os.makedirs(os.path.join(out_dir, HELD_DIR), exist_ok=True)
            shutil.move(src, os.path.join(out_dir, HELD_DIR, base + ext))
            LOG.error("moved the previous %s%s out of the published folder", base, ext)


def report_path(out_dir: str, ym: str) -> str:
    return os.path.join(out_dir, "invariants_%s.json" % ym.replace("-", ""))


def write_report(out_dir: str, ym: str, report: dict) -> None:
    """{factura_name: [violation, ...]} - [] = every rule holds. Read by
    the invoices index to say why a closed plant has no invoice."""
    import json
    path = report_path(out_dir, ym)
    merged = {}
    try:                                   # a single-plant run keeps the others' results
        with open(path, encoding="utf-8") as f:
            merged = (json.load(f) or {}).get("plants") or {}
    except (OSError, ValueError):
        pass
    merged.update(report)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ym": ym, "checked_at": now_mx().strftime("%Y-%m-%d %H:%M MX"), "plants": merged},
                  f, indent=1, ensure_ascii=False)
    held = sorted(k for k, v in report.items() if v)
    LOG.info("invariants %s: %d checked, %d held%s", ym, len(report), len(held),
             (" (" + ", ".join(held) + ")") if held else "")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")


def reconciliation_gate(ym: str, plants: list) -> tuple:
    """(allowed, blocked) for invoice mode - v3 item 12: no invoice annex
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
        LOG.error("reconciliation gate unreadable (%s) - refusing to "
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
                             "month cron) - a single-month invoice")
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
    #                 - the "big report", run on demand.
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

    # v340: an invoice shows nothing after the invoiced month (rendered on
    # 2 Nov, the October invoices had carried 1-2 Nov production)
    data_until = _month_end_iso(ym) if mode == "invoice" else None

    plants = _target_plants(portfolio, args)
    if mode == "invoice":
        plants, blocked = reconciliation_gate(ym, plants)
        for pk in blocked:
            LOG.warning("[%s] %s reconciliation NOT closed - invoice "
                        "annex BLOCKED (close the month on the recon "
                        "board first)", pk, ym)
    if not plants:
        LOG.warning("no plants matched - nothing to render")
        if mode == "invoice" and blocked and args.out_dir:
            write_report(args.out_dir, ym, {FACTURA_NAME.get(pk, pk): [
                f"CLOSED: the {ym} reconciliation is not closed"] for pk in blocked})
        return 2

    out_dir = args.out_dir or tempfile.mkdtemp(prefix="argia_annex_")
    os.makedirs(out_dir, exist_ok=True)
    generated_at = now_mx().strftime("%Y-%m-%d %H:%M MX")

    # the invoicing authority for the year (ARGIA Solar workbook);
    # {} degrades to atoms with a logged warning
    history_all = load_invoicing_overview(window.start.year)

    books = month_books(ym) if mode == "invoice" else None
    report = {}
    if mode == "invoice":
        for pk in blocked:
            report[FACTURA_NAME.get(pk, pk)] = [f"CLOSED: the {ym} reconciliation is not closed"]
    rendered = []
    for pk in plants:
        try:
            payload = build_annex_data(sheets, portfolio, pk, window,
                                       history=history_all.get(pk),
                                       data_until=data_until)
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
        if mode == "invoice":
            # v341: the arithmetic gate - one broken rule, no invoice
            try:
                bad = invoice_violations(ym, payload, books, pk)
            except Exception as e:  # noqa: BLE001 - a check that cannot run holds the invoice
                from argia.finance.invariants import Violation
                bad = [Violation("ERROR", f"the checks could not run: {e!r}")]
            report[FACTURA_NAME.get(pk, pk)] = [str(v) for v in bad]
            if bad:
                for v in bad:
                    LOG.error("[%s] %s INVOICE HELD - %s", pk, ym, v)
                hold_previous(out_dir, base)
                continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        LOG.info("[%s] rendered %s (%d KiB)", pk, path, len(html) // 1024)
        rendered.append((pk, path))

    if mode == "invoice":
        write_report(out_dir, ym, report)
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
