#!/usr/bin/env python3
"""demo.argia.com.mx - the e-mailed PDF reports, as examples (v288).

Tomasz, 2026-10-02: "inside Annexes tab add pregenerated pdf file examples
of all type of pdf reports that are shared via email for the whole
portfolio". One run makes, from the demo data (ARGIA SOLAR names, every
plant PPA):

* the daily performance report - morning edition (the last KPI day) and
  evening edition (today so far) - the same renderer report_daily mails;
* the financial report - weekly (month to date through the last measured
  day) and monthly (the month just closed) - the same page financial_mail
  prints;
* the monthly invoice annex of every plant for the month just closed -
  the same renderer report_invoice_annex uses (example: no reconciliation
  gate, every plant gets one).

Safety, the same as demo_gen: every psql reads the demo views in a
read-only session (refuses to run without them), every HTML is scrubbed
and scanned for every real customer name AND every real customer logo
image before it is printed; one hit = nothing is published, the last
good set stays. No upload, no mail, no Drive, no queue: the PDFs land in
DEMO_ANNEXES (server only, never in git) with a manifest.json that
demo_gen turns into the Reports > Annexes tab.

    /root/argia_v2/v2/.venv/bin/python /opt/argia/bundle/demo_annexes.py [/opt/argia/demo/annexes]
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import demo_gen as DG                                   # noqa: E402

OUT_DEFAULT = DG.DEMO_ANNEXES
CHROMIUM = ('chromium', 'chromium-browser', 'google-chrome', 'chrome-headless-shell')
MIN_PDF_BYTES = 5000
# the switches the server jobs run with: PostgreSQL everywhere, no workbook
SERVER_ENV = {
    'ARGIA_PG_MIRROR': '1',
    'ARGIA_TELEMETRY_SOURCE': 'pg', 'ARGIA_KPI_SOURCE': 'pg', 'ARGIA_FINANCE_SOURCE': 'pg',
    'ARGIA_INVOICING_SOURCE': 'pg', 'ARGIA_ALERTS_SOURCE': 'pg', 'ARGIA_DASHBOARD_SOURCE': 'pg',
    'ARGIA_CONFIG_SOURCE': 'pg', 'ARGIA_KPI_WRITE': 'pg',
    'ARGIA_SHEET_TELEMETRY': '0', 'ARGIA_SHEET_OUTBOX': '0',
}
NO_SECRETS = ('GOOGLE_SHEET_ID_V2', 'ARGIA_SOLAR_SHEET_ID', 'GOOGLE_CREDENTIALS', 'GOOGLE_CREDENTIALS_FILE',
              'GOOGLE_ARCHIVE_FOLDER_ID', 'SMTP_HOST', 'SMTP_USER', 'SMTP_PASS', 'NTFY_TOPIC', 'ANTHROPIC_API_KEY')
MONTHS_EN = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
             'September', 'October', 'November', 'December')
MONTHS_ES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
             'septiembre', 'octubre', 'noviembre', 'diciembre')


# ------------------------------------------------------------------ pure
def previous_month(day):
    """'YYYY-MM' of the month before ``day``'s month. Pure."""
    first = day.replace(day=1)
    return (first - dt.timedelta(days=1)).strftime('%Y-%m')


def last_friday_mail(asof):
    """The last Friday mail worth showing: the most recent Friday on or
    before ``asof`` that is at least a week into its month (early in a month
    the real mail covers a day or two - a poor example). Pure."""
    d = asof
    while d.weekday() != 4 or d.day < 7:
        d -= dt.timedelta(days=1)
    return d


def windows(asof, today):
    """The financial windows, the same rules as financial_mail: weekly =
    month to date as of a Friday mail, monthly = the month just closed.
    -> {'weekly': (d0, d1), 'monthly': (d0, d1)}. Pure."""
    ym = previous_month(today)
    y, m = int(ym[:4]), int(ym[5:])
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
    fri = last_friday_mail(asof)
    return {'weekly': (fri.replace(day=1).isoformat(), fri.isoformat()),
            'monthly': ('%s-01' % ym, last.isoformat())}


def slug_name(demo_name):
    """'ARGIA SOLAR 3' -> 'ARGIA_SOLAR_3'. Pure."""
    return re.sub(r'[^A-Za-z0-9]+', '_', demo_name).strip('_')


def gate(text, rules, uris):
    """Scrub-free check: [(token, context)] for every real name or real logo
    image left in a rendered report. Pure."""
    return DG.leaks(text, rules) + [(nm, 'its logo image') for nm in DG.logo_leaks(text, uris)]


# ------------------------------------------------------------- rendering
def find_chromium():
    env = os.environ.get('ARGIA_CHROMIUM', '').strip()
    if env and os.path.exists(env):
        return env
    for name in CHROMIUM:
        p = shutil.which(name)
        if p:
            return p
    return None


def chromium_pdf(html_path, pdf_path, fragment=''):
    """Print one HTML file to PDF with headless chromium (the way
    financial_mail and invoice_publish do). True when a real PDF came out."""
    chromium = find_chromium()
    if not chromium:
        print('demo_annexes: no chromium binary found', file=sys.stderr)
        return False
    url = 'file://' + os.path.abspath(html_path) + (('#' + fragment) if fragment else '')
    r = subprocess.run([chromium, '--headless=new', '--disable-gpu', '--no-sandbox', '--hide-scrollbars',
                        '--virtual-time-budget=8000', '--no-pdf-header-footer',
                        '--print-to-pdf=' + pdf_path, url], capture_output=True, text=True, timeout=180)
    return r.returncode == 0 and os.path.exists(pdf_path) and os.path.getsize(pdf_path) >= MIN_PDF_BYTES


RENDER = chromium_pdf      # tests replace it (CI has no chromium)


# --------------------------------------------------------------- reports
def daily_reports(asof, today):
    """[(group, file_base, title_en, title_es, period, html)] for both editions."""
    from argia.core.config import load_portfolio
    from argia.core.sheets import open_sheets
    from argia.report.daily import build_report_data, render_html
    sheets = open_sheets()
    portfolio = load_portfolio(sheets)
    out = []
    for edition, day, en, es in (('morning', asof, 'Daily report - morning edition', 'Reporte diario - edición de la mañana'),
                                 ('evening', today, 'Daily report - evening edition', 'Reporte diario - edición de la tarde')):
        data = build_report_data(sheets, portfolio, day.isoformat())
        if not any(p.energy_kwh is not None for p in data.plants) and edition == 'evening':
            data = build_report_data(sheets, portfolio, asof.isoformat())   # nothing measured yet today
            day = asof
        out.append(('daily', f'ARGIA_SOLAR_Daily_{edition}_{day.isoformat()}', en, es, day.isoformat(),
                    render_html(data), ''))
    return out


def financial_reports(PG, asof, today):
    win = windows(asof, today)
    page = PG.financial_report()
    out = []
    for mode, en, es in (('weekly', 'Financial report - weekly (month to date)', 'Reporte financiero - semanal (mes a la fecha)'),
                         ('monthly', 'Financial report - monthly (month close)', 'Reporte financiero - mensual (cierre de mes)')):
        d0, d1 = win[mode]
        name = f'ARGIA_SOLAR_Financial_{d0[:7]}' if mode == 'monthly' else f'ARGIA_SOLAR_Financial_{d0}_to_{d1}'
        out.append(('financial', name, en, es, f'{d0} .. {d1}', page, f'd0={d0}&d1={d1}'))
    return out


def invoice_annexes(keys, demo_names, today):
    """One invoice annex per plant for the month just closed (no
    reconciliation gate: these are examples)."""
    import base64
    import demo_brand
    from argia.core.config import load_portfolio
    from argia.core.sheets import open_sheets
    from argia.core.time_utils import now_mx
    from argia.finance.annex import build_annex_data, load_invoicing_overview, render_annex_html
    from argia.finance.income import Period
    ym = previous_month(today)
    year = int(ym[:4])
    window = Period.from_iso('%04d-01-01' % year, '%04d-12-31' % year)
    sheets = open_sheets()
    portfolio = load_portfolio(sheets)
    history = load_invoicing_overview(year)
    logo = 'data:image/png;base64,' + base64.b64encode(demo_brand.logo_png()).decode()
    generated = now_mx().strftime('%Y-%m-%d %H:%M MX')
    y, m = int(ym[:4]), int(ym[5:])
    out = []
    for k in keys:
        payload = build_annex_data(sheets, portfolio, k, window, history=history.get(k))
        payload['client_logo'] = logo                     # never the customer's logo
        payload['factura_name'] = slug_name(demo_names[k])
        html = render_annex_html(payload, generated, default_ym=ym)
        out.append(('invoice', f'Factura_{slug_name(demo_names[k])}_{ym.replace("-", "")}',
                    f'{demo_names[k]} - invoice annex {MONTHS_EN[m - 1]} {y}',
                    f'{demo_names[k]} - anexo de factura {MONTHS_ES[m - 1]} {y}', ym, html, ''))
    return out


# ------------------------------------------------------------------ main
def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    out = os.path.abspath(argv[1] if len(argv) > 1 else OUT_DEFAULT)
    stage = out + '.staging'
    import fcntl
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    lock = open(out + '.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('demo_annexes: another run is in progress - skipped (nothing changed)')
        return 0
    for k in NO_SECRETS:                                   # nothing here may reach Google, mail or a model
        os.environ.pop(k, None)
    os.environ.update(SERVER_ENV)
    os.environ['PGOPTIONS'] = DG.DEMO_OPTS
    if not DG.demo_schema_ok():
        print('demo_annexes: REFUSED - demo views missing or search_path/read-only not in effect; nothing published',
              file=sys.stderr)
        return 2
    DG.setup_paths()
    rules = DG.build_rules(DG.real_names())
    uris = DG.client_logo_uris()
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    PG, keys, demo_names = DG.load_portal(stage)
    from argia.core.time_utils import now_mx
    today = now_mx().date()
    asof = dt.date.fromisoformat(PG.RG.asof)
    reports = (daily_reports(asof, today) + financial_reports(PG, asof, today)
               + invoice_annexes(keys, demo_names, today))
    items, found, failed = [], [], []
    for group, base, en, es, period, text, fragment in reports:
        text, _ = DG.scrub(DG.demo_text(text), rules, demo_names)
        hits = gate(text, rules, uris)
        if hits:
            found += [(base, tok, ctx) for tok, ctx in hits]
            continue
        hp = os.path.join(stage, base + '.html')
        with open(hp, 'w', encoding='utf-8') as fh:
            fh.write(text)
        pdf = os.path.join(stage, base + '.pdf')
        if not RENDER(hp, pdf, fragment):
            failed.append(base)
            continue
        os.remove(hp)
        items.append({'group': group, 'file': base + '.pdf', 'title_en': en, 'title_es': es, 'period': period})
    if found or failed:
        for base, tok, ctx in found[:20]:
            print(f'demo_annexes: LEAK {base}: {tok!r} in ...{ctx}...', file=sys.stderr)
        for base in failed:
            print(f'demo_annexes: PDF FAILED {base}', file=sys.stderr)
        print(f'demo_annexes: REFUSED - {len(found)} leak(s), {len(failed)} PDF failure(s); nothing published',
              file=sys.stderr)
        shutil.rmtree(stage, ignore_errors=True)
        return 1
    # only the PDFs and the manifest are published (the staging folder also
    # holds the portal pages load_portal wrote - they are not needed here)
    final = out + '.new'
    shutil.rmtree(final, ignore_errors=True)
    os.makedirs(final)
    for it in items:
        shutil.move(os.path.join(stage, it['file']), os.path.join(final, it['file']))
    with open(os.path.join(final, 'manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump({'generated': now_mx().strftime('%Y-%m-%d %H:%M MX'), 'items': items}, fh, ensure_ascii=False, indent=1)
    shutil.rmtree(stage, ignore_errors=True)
    old = out + '.old'
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(out):
        os.replace(out, old)
    os.replace(final, out)
    shutil.rmtree(old, ignore_errors=True)
    n = {g: sum(1 for it in items if it['group'] == g) for g in ('daily', 'financial', 'invoice')}
    print(f'demo_annexes: wrote {len(items)} PDFs under {out} (daily {n["daily"]}, financial {n["financial"]}, '
          f'invoice {n["invoice"]}; 0 leaks)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
