#!/usr/bin/env python3
"""PPA monthly performance report (v374): PostgreSQL -> /ppa-report/ on portal.argia.com.mx.

    ppa_monthly_gen.py [OUTROOT]                      # every month: pages, Excel and PDFs (drafts re-rendered
                                                      # only when their numbers changed)
    ppa_monthly_gen.py --month 2026-10 [--no-pdf]     # one month
    ppa_monthly_gen.py --publish 2026-10 --by tomasz  # freeze a complete month (setup_app's Publish button)

A draft is computed from the data on every run. Publishing freezes the month: the numbers (JSON), the PDFs and
the Excel go to STORE/published/YYYY-MM/ and are only ever copied from there afterwards, so a published month
never changes - the Prologis monthly report rule (argia/prologis/monthly.py). Publishing twice is refused.

Read-only on PostgreSQL. The report is ARGIA staff and invited stakeholders only: the auth area 'ppa_report'
(auth_core.PREFIX_AREA '/ppa-report/'). Customer data: the output lives on the server, never in the repo.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import os
import shutil
import sys
import tempfile
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.dirname(HERE)
for _p in (V2, os.path.join(V2, "server", "bundle"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from argia.kpi import design_model as DM                     # noqa: E402
from argia.report import ppa_monthly as PM                   # noqa: E402
from argia.report import ppa_monthly_view as V               # noqa: E402

LOG = logging.getLogger("ppa_monthly_gen")
OUTROOT = "/www/hosting/portal.argia.com.mx/www"
STORE = os.environ.get("ARGIA_PPA_REPORT_DIR", "/opt/argia/ppa_report")
SECTION = "ppa-report"
PPA = "(SELECT plant_key FROM plant WHERE portfolio = 'PPA' AND active)"
SQL = {
    "plants": "SELECT plant_key, customer, kwp_dc, tariff_mxn_per_kwh, sla_target FROM plant "
              "WHERE portfolio = 'PPA' AND active ORDER BY plant_key",
    "daily": f"SELECT plant_key, prod_date, energy_kwh, irradiance_kwh_m2, expected_kwh, billable_kwh, cloud_cover_pct, "
             f"availability, avail_coverage FROM daily_production WHERE plant_key IN {PPA} ORDER BY 1, 2",
    "contract": f"SELECT plant_key, year, month, contract_kwh, tariff_mxn FROM contract_monthly WHERE plant_key IN {PPA}",
    "invoices": f"SELECT plant_key, ref_month, amount_mxn, produced_kwh, billable_kwh, penalty_kwh, check_status "
                f"FROM invoicing WHERE plant_key IN {PPA}",
    "losses": f"SELECT plant_key, prod_date, lost_kwh, unavailability_kwh, overheating_kwh, underperformance_kwh, "
              f"excused_kwh, lost_mxn FROM loss_daily WHERE plant_key IN {PPA}",
    "events": f"SELECT plant_key, start_ts, end_ts, category, cost_mxn, deemed_kwh, note FROM maintenance_event "
              f"WHERE plant_key IN {PPA}",
    "tickets": f"SELECT number, plant_key, category, priority, status, created_at, started_at, resolved_at, closed_at, "
               f"title FROM ticket WHERE plant_key IN {PPA}",
    "sat": f"SELECT plant_key, check_date, status, drift_pct FROM satellite_check WHERE plant_key IN {PPA}",
}
OPTIONAL = {"invoices", "losses", "events", "tickets", "sat"}    # a missing table must not stop the report


def fetch() -> Dict[str, List[Dict]]:
    from argia.store.pgq import psql_csv
    out = {}
    for k, sql in SQL.items():
        try:
            out[k] = list(csv.DictReader(io.StringIO(psql_csv("SET statement_timeout = '120s'; " + sql))))
        except RuntimeError as exc:
            if k not in OPTIONAL:
                raise
            LOG.warning("%s unavailable (%s): the report goes on without it", k, str(exc)[:120])
            out[k] = []
    return out


def context(rows: Dict[str, List[Dict]], designs=None) -> PM.Context:
    import portal_chrome as C
    plants = [dict(p, name=C.display_name(p["customer"])) for p in rows["plants"]]
    return PM.Context(plants, rows["daily"], rows["contract"], rows["invoices"], rows["losses"], rows["events"],
                      rows["tickets"], rows["sat"], designs if designs is not None else DM.load())


# ------------------------------------------------------------------ files
def write_if_changed(path: str, data) -> bool:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if os.path.exists(path):
        with open(path, "rb") as fh:
            if fh.read() == raw:
                return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, path)
    return True


def _read(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def render_pdf(html_doc: str, pdf_path: str, chromium: Optional[str]) -> bool:
    if not chromium:
        return False
    from invoice_publish import html_to_pdf
    with tempfile.TemporaryDirectory() as td:
        h = os.path.join(td, "report.html")
        with open(h, "w", encoding="utf-8") as fh:
            fh.write(html_doc)
        tmp = os.path.join(td, "report.pdf")
        if not html_to_pdf(h, tmp, chromium):
            return False
        os.makedirs(os.path.dirname(pdf_path), exist_ok=True)
        shutil.copyfile(tmp, pdf_path + ".tmp")
        os.replace(pdf_path + ".tmp", pdf_path)
    return True


def published_dir(store: str, ym: str) -> str:
    return os.path.join(store, "published", ym)


def load_published(store: str, ym: str) -> Optional[Dict]:
    p = os.path.join(published_dir(store, ym), "report.json")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def files_for(rep: Dict, ctx: Optional[PM.Context], today: str, chromium: Optional[str], out_dir: str,
              pdf: bool = True) -> Dict[str, bool]:
    """report.xlsx, report-en.pdf, report-es.pdf for one month into out_dir."""
    from argia.report import ppa_monthly_xlsx as X
    daily = PM.daily_rows(ctx, rep["month"], today) if ctx is not None else []
    done = {"xlsx": write_if_changed(os.path.join(out_dir, "report.xlsx"), X.workbook(rep, daily))}
    if pdf:
        for code in ("en", "es"):
            done[f"pdf_{code}"] = render_pdf(V.print_document(rep, code, today),
                                             os.path.join(out_dir, f"report-{code}.pdf"), chromium)
    return done


# ------------------------------------------------------------------ pages
def page(rep: Dict, today: str, months: List[str]) -> str:
    import portal_chrome as C
    ym = rep["month"]
    i = months.index(ym) if ym in months else -1
    links = {"pdf_en": "report-en.pdf", "pdf_es": "report-es.pdf", "xlsx": "report.xlsx", "index": f"/{SECTION}/",
             "prev": f"/{SECTION}/{months[i - 1]}/" if i > 0 else None,
             "next": f"/{SECTION}/{months[i + 1]}/" if 0 <= i < len(months) - 1 else None}
    form = ""
    if rep["complete"] and not rep.get("published"):
        form = (f'<a class="adminonly" href="/setup/ppa-report/?month={ym}">'
                f'{C.t("Publish this month", "Publicar este mes")}</a>')
    return C.page(f"PPA report {ym}", V.body(rep, V.web_lang(), today, links, form), section=SECTION,
                  extra_head=f"<style>{V.CSS}</style>")


def index_page(cards: List[Dict], today: str) -> str:
    import portal_chrome as C
    return C.page("PPA monthly report", V.index_body(cards, V.web_lang(), today), section=SECTION,
                  extra_head=f"<style>{V.CSS}</style>")


def card(rep: Dict, today: str) -> Dict:
    P = rep["portfolio"]
    status = ("published" if rep.get("published") else "mtd" if not rep["complete"]
              else "late" if PM.deadline_applies(rep["month"]) and today > rep["deadline"] else "draft")
    return {"month": rep["month"], "href": f"/{SECTION}/{rep['month']}/", "status": status,
            "actual_kwh": P["actual_kwh"], "promise": P["promise"], "epi": P["epi"], "sun_index": P["sun_index"],
            "lost_mxn": (P.get("loss") or {}).get("lost_mxn")}


# ------------------------------------------------------------------ the runs
def generate(ctx: PM.Context, outroot: str, store: str, today: str, only: Optional[str] = None, pdf: bool = True,
             chromium: Optional[str] = None) -> Dict[str, int]:
    first = ctx.first_month()
    if not first:
        LOG.warning("no PPA production data: nothing to report")
        return {"months": 0}
    months = PM.month_range(first, today[:7])
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {"months": 0, "rendered": 0, "published": 0}
    cards = []
    for ym in months:
        out_dir = os.path.join(outroot, SECTION, ym)
        pub = load_published(store, ym)
        if pub is not None:
            rep = pub
            if only in (None, ym):
                for f in ("report.xlsx", "report-en.pdf", "report-es.pdf"):
                    src = os.path.join(published_dir(store, ym), f)
                    if os.path.exists(src):
                        with open(src, "rb") as fh:
                            write_if_changed(os.path.join(out_dir, f), fh.read())
            stats["published"] += 1
        else:
            rep = PM.build(ctx, ym, today, now)
            if only in (None, ym):
                dg = PM.digest(rep)
                mark = os.path.join(out_dir, ".digest")
                old = _read(mark)
                need = dg != old or not all(os.path.exists(os.path.join(out_dir, f)) for f in
                                            (("report.xlsx", "report-en.pdf", "report-es.pdf") if pdf else ("report.xlsx",)))
                if need:
                    files_for(rep, ctx, today, chromium, out_dir, pdf)
                    write_if_changed(mark, dg + "\n")
                    stats["rendered"] += 1
        if only in (None, ym):
            write_if_changed(os.path.join(out_dir, "index.html"), page(rep, today, months))
        cards.append(card(rep, today))
        stats["months"] += 1
    write_if_changed(os.path.join(outroot, SECTION, "index.html"), index_page(list(reversed(cards)), today))
    return stats


def publish(ctx: PM.Context, outroot: str, store: str, ym: str, by: str, today: str,
            chromium: Optional[str], now: Optional[str] = None) -> Dict:
    """Freeze a complete month. Refuses a month in progress and a month already published."""
    if load_published(store, ym) is not None:
        raise SystemExit(f"{ym} is already published - a published month never changes")
    now = now or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep = PM.freeze(PM.build(ctx, ym, today, now), by, now)
    d = published_dir(store, ym)
    tmp = d + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    done = files_for(rep, ctx, today, chromium, tmp, pdf=True)
    if not all(done.get(k) for k in ("pdf_en", "pdf_es")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"{ym}: the PDF did not render - nothing was published")
    with open(os.path.join(tmp, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(rep, fh, ensure_ascii=False, indent=1, default=str)
    os.makedirs(os.path.dirname(d), exist_ok=True)
    os.replace(tmp, d)
    generate(ctx, outroot, store, today, only=ym, pdf=False, chromium=chromium)
    return rep["published"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("outroot", nargs="?", default=OUTROOT)
    ap.add_argument("--store", default=STORE)
    ap.add_argument("--month")
    ap.add_argument("--publish", metavar="YYYY-MM")
    ap.add_argument("--by", default="")
    ap.add_argument("--today", help="YYYY-MM-DD (tests); default: today in Mexico City")
    ap.add_argument("--no-pdf", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    today = a.today or (dt.datetime.now(dt.timezone.utc) + PM.MX_OFFSET).date().isoformat()
    from invoice_publish import find_chromium
    chromium = None if a.no_pdf else find_chromium()
    if not a.no_pdf and not chromium:
        LOG.warning("no chromium: pages and Excel only, PDFs pending")
    ctx = context(fetch())
    if a.publish:
        if not a.by:
            raise SystemExit("--publish needs --by USER")
        info = publish(ctx, a.outroot, a.store, a.publish, a.by, today, chromium)
        print(f"PUBLISHED {a.publish} by {info['by']} at {info['utc']} digest {info['digest']} "
              f"{ {True: 'on time', False: 'after the day-10 deadline', None: '(history, no deadline)'}[info['on_time']]}")
        return 0
    stats = generate(ctx, a.outroot, a.store, today, a.month, not a.no_pdf, chromium)
    print(f"ppa_monthly_gen: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
