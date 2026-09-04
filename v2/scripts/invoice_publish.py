"""Publish invoice annexes to the report site (pio06 only).

The chain on the 1st of each month (argia-invoice.timer, 07:30 MX,
after the 06:10 close):

    report_invoice_annex --month <closed>  ->  HTML per PPA plant
    headless chromium --print-to-pdf       ->  true PDF next to it
    render_index                           ->  /invoices/ landing page

Everything lands under <webroot>/invoices/<YYYY-MM>/, which nginx
serves to the 'financial' area only. The annex generator's fail-closed
reconciliation gate stays in charge: a plant whose month is not CLOSED
produces no annex, and the index shows it as blocked rather than
silently missing.

PDF needs a JS-capable renderer (the annex fills its numbers from
embedded day atoms in JS), hence chromium and not weasyprint. When no
chromium is available the HTMLs still publish and the index says the
PDF is pending — degraded, never silent.

Usage:
    invoice_publish.py --month 2026-08 [--out-root DIR] [--no-pdf]
    invoice_publish.py --last-month        # the cron form
"""

from __future__ import annotations

import argparse
import glob
import html as _html
import re
import logging
import os
import shutil
import subprocess
import sys

LOG = logging.getLogger("argia.invoice_publish")

WEBROOT = "/www/hosting/monitoring.argia.com.mx/www"
CHROMIUM = ("chromium", "chromium-browser", "google-chrome")

# factura_name -> (plant_key, display). The factura name is the file
# identity (factura_TAIGENE_202608.pdf), per Tomasz 2026-09-01.
FACTURA_CLIENT = {
    "TAIGENE": ("GTO1", "TAIGENE (Leon, GTO)"),
    "SAG": ("MEX1", "SAG (CDMX)"),
    "VITALMEX": ("MEX2", "VITALMEX (CDMX)"),
    "PLASTIC_OMNIUM": ("NL1", "PLASTIC OMNIUM (Monterrey, NL)"),
    "QUIMICA_COYOACAN": ("SLP1", "QUIMICA COYOACAN (SLP)"),
    "HOLIDAY_INN": ("SLP2", "HOLIDAY INN EXPRESS (SLP)"),
}


INVOICE_TOL_PCT = 0.05
"""Invoiced kWh vs the closed billing basis: anything beyond five
hundredths of a percent is a real disagreement, not rounding."""


def invoice_check(billable_kwh, billing_kwh, tol_pct=INVOICE_TOL_PCT):
    """(status, delta_kwh, delta_pct) of the invoice vs the close.

    Tomasz, 2026-09-01: "going forward always check last month invoice
    vs the last day status, keep it somewhere as invoicing." The close
    row's billing_kwh IS the month-end vendor-counter position, so this
    is that check, run at publish time and stored per plant-month.
    """
    if billable_kwh is None or billing_kwh in (None, 0):
        return "NO_BASIS", None, None
    delta = float(billable_kwh) - float(billing_kwh)
    pct = 100.0 * delta / float(billing_kwh)
    return ("OK" if abs(pct) <= tol_pct else "MISMATCH",
            round(delta, 3), round(pct, 4))


def register_upsert_sql(pk, ym, h, kwh, amount, tariff, billing, dk, dp,
                        status) -> str:
    """The register row for one invoiced plant-month. v192: carries the
    produced / penalty / expected split too, so the register can serve
    the annex the way Invoicing_Overview did. PURE."""
    def _n(v, nd=3):
        return "NULL" if v is None else repr(round(float(v), nd))
    produced = h.get("kwh")
    penalty = h.get("penalty") or 0.0
    expected = h.get("expected")
    return (
        "INSERT INTO invoicing (plant_key, ref_month, billable_kwh,"
        " tariff_mxn, amount_mxn, billing_kwh, delta_kwh, delta_pct,"
        " check_status, produced_kwh, penalty_kwh, expected_kwh, source)"
        " VALUES ("
        f"'{pk}', DATE '{ym}-01', {kwh:.3f},"
        f" {tariff if tariff is not None else 'NULL'},"
        f" {round(amount, 2) if amount is not None else 'NULL'},"
        f" {billing if billing is not None else 'NULL'},"
        f" {dk if dk is not None else 'NULL'},"
        f" {dp if dp is not None else 'NULL'},"
        f" '{status}', {_n(produced)}, {_n(penalty)}, {_n(expected)},"
        " 'invoice_publish') ON CONFLICT (plant_key, ref_month) DO UPDATE"
        " SET billable_kwh = EXCLUDED.billable_kwh,"
        " tariff_mxn = EXCLUDED.tariff_mxn,"
        " amount_mxn = EXCLUDED.amount_mxn,"
        " billing_kwh = EXCLUDED.billing_kwh,"
        " delta_kwh = EXCLUDED.delta_kwh,"
        " delta_pct = EXCLUDED.delta_pct,"
        " check_status = EXCLUDED.check_status,"
        " produced_kwh = EXCLUDED.produced_kwh,"
        " penalty_kwh = EXCLUDED.penalty_kwh,"
        " expected_kwh = COALESCE(EXCLUDED.expected_kwh, invoicing.expected_kwh),"
        " source = EXCLUDED.source,"
        " published_at = now();")


def record_invoicing(ym, names, history):
    """Write the invoicing register rows for one month and return
    {factura_name: (kwh, mxn, status)} for the index.

    Source of billed kWh and MXN: the ARGIA Solar Invoicing_Overview
    slice (``history``) — what was actually invoiced. The check
    compares that against the reconciliation close's billing_kwh, so a
    factura can never silently drift from the closed month. Off-server
    (no PG) records nothing and returns the history values so the
    index still shows them."""
    rows = {}
    y, m = int(ym[:4]), int(ym[5:7])
    have_pg = False
    try:
        from argia.store import pg_mirror
        from argia.store.pgq import psql_rows, psql_exec
        have_pg = pg_mirror.enabled()
    except Exception:                                  # noqa: BLE001
        pass
    if have_pg:
        from argia.finance.invoicing_pg import ENSURE_SQL
        psql_exec(ENSURE_SQL)          # v192: + produced/penalty/expected
        closing = {r[0]: float(r[1]) for r in psql_rows(
            "SELECT plant_key, billing_kwh FROM reconciliation_monthly"
            f" WHERE ref_month = DATE '{ym}-01'"
            " AND billing_kwh IS NOT NULL;") if len(r) >= 2 and r[1]}
        tariffs = {r[0]: float(r[1]) for r in psql_rows(
            "SELECT plant_key, tariff_mxn FROM contract_monthly"
            f" WHERE year = {y} AND month = {m}"
            " AND tariff_mxn IS NOT NULL;") if len(r) >= 2 and r[1]}
    else:
        closing, tariffs = {}, {}

    for name in sorted(names):
        pk = FACTURA_CLIENT.get(name, ("", ""))[0]
        h = (history.get(pk) or {}).get(ym) if pk else None
        if not h or h.get("kwh") is None:
            continue
        kwh = float(h["kwh"]) + float(h.get("penalty") or 0.0)
        amount = h.get("income")
        billing = closing.get(pk)
        status, dk, dp = invoice_check(kwh, billing)
        if status == "NO_BASIS" and not have_pg:
            status = "XLSX"                    # index-only, unrecorded
        if status == "MISMATCH":
            LOG.error("INVOICING CHECK %s %s: billed %.1f kWh vs close"
                      " %.1f (%+0.3f%%) — investigate before sending",
                      pk, ym, kwh, billing, dp)
        rows[name] = (kwh, round(amount, 2) if amount else None, status)
        if not have_pg:
            continue
        tariff = tariffs.get(pk)
        psql_exec(register_upsert_sql(pk, ym, h, kwh, amount, tariff,
                                      billing, dk, dp, status))
    return rows


def all_records():
    """{ym: {factura_name: (kwh, mxn, status)}} — every register row,
    so the index shows amounts for every month, not just the newest."""
    out = {}
    try:
        from argia.store import pg_mirror
        from argia.store.pgq import psql_rows
        if not pg_mirror.enabled():
            return out
        by_pk = {v[0]: k for k, v in FACTURA_CLIENT.items()}
        for r in psql_rows(
                "SELECT plant_key, to_char(ref_month, 'YYYY-MM'),"
                " billable_kwh, amount_mxn, check_status FROM invoicing"
                " WHERE billable_kwh IS NOT NULL;"):   # v192: not EXPECTED_ONLY
            if len(r) < 5 or r[0] not in by_pk:
                continue
            out.setdefault(r[1], {})[by_pk[r[0]]] = (
                float(r[2]) if r[2] else None,
                float(r[3]) if r[3] else None, r[4])
    except Exception as e:                             # noqa: BLE001
        LOG.warning("register unreadable for the index: %s", e)
    return out


def push_to_drive(ym, out_dir):
    """Mirror the month's PDFs into the shared drive the daily reports
    already live on: <archive>/Invoicing/<YYYY-MM>/. That is where
    Tomasz looks for customer documents ("I see report but not the
    folder", 2026-09-01). Best-effort: no Drive credentials, no drama
    — the website copy under /invoices/ is the system of record."""
    try:
        import glob as _glob
        from argia.core.drive import DriveClient
        root = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
        if not root or not os.environ.get("GOOGLE_CREDENTIALS"):
            LOG.info("drive push skipped (no archive folder/creds)")
            return 0
        d = DriveClient()
        folder = d.ensure_folder(d.ensure_folder(root, "Invoicing"), ym)
        n = 0
        for p in sorted(_glob.glob(os.path.join(
                out_dir, "factura_*_%s.pdf" % ym.replace("-", "")))):
            d.upload_file(folder, os.path.basename(p), p,
                          "application/pdf")
            n += 1
        LOG.info("drive push: %d PDF(s) -> Invoicing/%s", n, ym)
        return n
    except Exception as e:                             # noqa: BLE001
        LOG.error("drive push failed (publish unaffected): %s", e)
        return 0


def build_month_zip(out_dir, ym):
    """facturas_<yyyymm>.zip with every PDF of the month — the
    one-click "download all plants" button (Tomasz 2026-09-01).
    Rebuilt from scratch on every publish so it can never carry a
    stale factura. Returns the number of PDFs bundled (0 = no zip)."""
    import zipfile
    stamp = ym.replace("-", "")
    pdfs = sorted(glob.glob(os.path.join(out_dir,
                                         "factura_*_%s.pdf" % stamp)))
    zpath = os.path.join(out_dir, "facturas_%s.zip" % stamp)
    if not pdfs:
        if os.path.exists(zpath):
            os.remove(zpath)           # never leave a stale bundle
        return 0
    tmp = zpath + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in pdfs:
            z.write(p, os.path.basename(p))
    os.replace(tmp, zpath)
    LOG.info("bundled %d PDF(s) -> %s", len(pdfs),
             os.path.basename(zpath))
    return len(pdfs)


def month_zips(out_root):
    """{ym: True} for months that have a facturas bundle."""
    out = {}
    for z in glob.glob(os.path.join(out_root, "*", "facturas_*.zip")):
        stamp = os.path.basename(z)[len("facturas_"):-len(".zip")]
        if len(stamp) == 6 and stamp.isdigit():
            out["%s-%s" % (stamp[:4], stamp[4:])] = True
    return out


def find_chromium():
    for name in CHROMIUM:
        p = shutil.which(name)
        if p:
            return p
    return None


def html_to_pdf(html_path: str, pdf_path: str, chromium: str) -> bool:
    """Render one annex to PDF. The virtual-time budget lets the page's
    JS fill the month before printing."""
    r = subprocess.run(
        [chromium, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", "--virtual-time-budget=5000",
         "--no-pdf-header-footer",
         "--print-to-pdf=" + pdf_path, "file://" + os.path.abspath(html_path)],
        capture_output=True, text=True, timeout=120)
    ok = r.returncode == 0 and os.path.exists(pdf_path) \
        and os.path.getsize(pdf_path) > 5000
    if not ok:
        LOG.error("pdf render failed for %s: rc=%d %s", html_path,
                  r.returncode, (r.stderr or "")[-200:])
    return ok


FACTURA_RX = re.compile(r"factura_(.+)_(\d{6})\.html$")


def scan_months(out_root: str):
    """{ym: [(factura_name, has_html, has_pdf)]} for everything
    published, from the factura_<CLIENT>_<yyyymm> naming (v158)."""
    out = {}
    for d in sorted(glob.glob(os.path.join(out_root, "[0-9]" * 4 + "-"
                                           + "[0-9]" * 2))):
        ym = os.path.basename(d)
        rows = []
        for h in sorted(glob.glob(os.path.join(d, "factura_*.html"))):
            m = FACTURA_RX.search(os.path.basename(h))
            if not m or m.group(2) != ym.replace("-", ""):
                continue
            rows.append((m.group(1), True,
                         os.path.exists(h[:-5] + ".pdf")))
        if rows:
            out[ym] = rows
    return out


def render_index(months, blocked_now=None, records=None, zips=None,
                 generated_at=""):
    """The /invoices/ page. Bilingual like the rest of the site
    (data-en/data-es + the stored language, EN shown to EN users, ES to
    ES users); the PDFs themselves are always Spanish. Each month
    carries a summary line and one row per client with the RECORDED
    invoiced kWh and MXN (the 'invoicing' register). Pure."""
    blocked_now = blocked_now or {}
    records = records or {}
    zips = zips or {}
    MES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
           "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre",
           "Diciembre"]
    MEN = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November",
           "December"]

    def t(en, es):
        return (f'<span data-en="{_esc(en)}" data-es="{_esc(es)}">'
                f"{_esc(en)}</span>")

    parts = []
    for ym in sorted(months, reverse=True):
        y, m = ym[:4], int(ym[5:7])
        rows = []
        tot_k = tot_m = 0.0
        n_rows = 0
        for name, has_html, has_pdf in months[ym]:
            client = FACTURA_CLIENT.get(name, ("", name))[1]
            kwh, mxn, chk = records.get(ym, {}).get(
                name, (None, None, None))
            if kwh:
                tot_k += kwh
            if mxn:
                tot_m += mxn
            n_rows += 1
            kwh_td = "&mdash;" if kwh is None else f"{kwh:,.1f}"
            mxn_td = "&mdash;" if mxn is None else f"${mxn:,.2f}"
            flag = ('' if chk in (None, "OK", "XLSX") else
                    f' <span class="blocked">{_esc(chk)}</span>')
            base = f"{ym}/factura_{name}_{ym.replace('-', '')}"
            pdf = (f'<a class="btn" href="{base}.pdf" download>PDF</a>'
                   if has_pdf else
                   f'<span class="mut">{t("PDF pending", "PDF pendiente")}'
                   "</span>")
            web = (f'<a class="btn" href="{base}.html">'
                   f'{t("View", "Ver")}</a>' if has_html else "")
            rows.append(f"<tr><td>{_esc(client)}</td>"
                        f'<td class="num">{kwh_td}</td>'
                        f'<td class="num">{mxn_td}{flag}</td>'
                        f"<td>{pdf} {web}</td></tr>")
        for name, why in sorted(blocked_now.get(ym, [])):
            rows.append(f"<tr><td>{_esc(FACTURA_CLIENT.get(name, ('', name))[1])}"
                        f'</td><td class="num">&mdash;</td>'
                        f'<td class="num">&mdash;</td>'
                        f'<td><span class="blocked">{_esc(why)}</span>'
                        f"</td></tr>")
        dlall = ""
        if zips.get(ym):
            dlall = (f' · <a class="btn zip" href="{ym}/facturas_'
                     f'{ym.replace("-", "")}.zip" download>'
                     f'{t("Download all (ZIP)", "Descargar todo (ZIP)")}'
                     "</a>")
        summary = (f'<div class="msum">{n_rows} '
                   f'{t("annexes", "anexos")} · {tot_k:,.0f} kWh · '
                   f"${tot_m:,.2f} MXN {t('excl. VAT', 'sin IVA')}"
                   f"{dlall}</div>")
        parts.append(
            f'<h2><span data-en="{_esc(MEN[m])} {y}"'
            f' data-es="{_esc(MES[m])} {y}">{_esc(MEN[m])} {y}</span></h2>'
            f"{summary}"
            f"<table><thead><tr><th>{t('Client', 'Cliente')}</th>"
            f"<th>kWh</th><th>MXN {t('excl. VAT', 'sin IVA')}</th>"
            f"<th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")
    body = "".join(parts) or (
        f"<p>{t('No annexes published yet.', 'Aún no hay anexos publicados.')}</p>")
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Anexos de facturación — ARGIA</title><style>
body{{margin:0;background:#f6f7f8;color:#202124;
font-family:"Segoe UI",system-ui,sans-serif;font-size:15px}}
.wrap{{max-width:820px;margin:0 auto;padding:26px 18px 48px}}
h1{{font-size:21px;letter-spacing:.14em;text-transform:uppercase}}
h2{{font-size:16px;margin:28px 0 2px}}
.msum{{color:#5f6368;font-size:13px;margin:2px 0 8px}}
table{{width:100%;border-collapse:collapse;background:#fff;
border:1px solid #e0e3e7;border-radius:10px}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #eef0f2}}
th{{color:#5f6368;font-weight:500;font-size:12.5px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.btn{{display:inline-block;border:1px solid #dadce0;border-radius:7px;
padding:4px 12px;text-decoration:none;color:#202124;background:#fff}}
.btn:hover{{border-color:#b9bec4}}
.btn.zip{{border-color:#1c2733;font-weight:600}}
.mut{{color:#9aa0a6;font-size:13px}}
.blocked{{color:#a50e0e;font-size:13px}}
.sub{{color:#5f6368;font-size:13px;margin:2px 0 18px}}
a.back{{color:#1a73e8;text-decoration:none}}</style></head><body>
<div class="wrap"><p><a class="back" href="/"
 data-en="← Reports" data-es="← Reportes">← Reports</a></p>
<h1><span data-en="Invoice annexes"
 data-es="Anexos de facturación">Invoice annexes</span></h1>
<div class="sub"><span data-en="One annex per plant and closed month ·
 the PDF is always in Spanish · new months appear automatically on the
 1st after the reconciliation close."
 data-es="Un anexo por planta y mes cerrado · el PDF siempre en
 español · cada mes nuevo aparece automáticamente el día 1 tras el
 cierre de conciliación.">One annex per plant and closed month · the
 PDF is always in Spanish · new months appear automatically on the 1st
 after the reconciliation close.</span> ·
 Generated {_esc(generated_at)}</div>
{body}
</div>
<script>
(function(){{
  let l="en";
  try{{l=localStorage.getItem("argia_lang")||"en";}}catch(e){{}}
  document.querySelectorAll("[data-en]").forEach(e=>{{
    e.textContent=e.dataset[l]||e.dataset.en;}});
}})();
</script>
</body></html>"""


def _esc(s):
    return _html.escape(str(s), quote=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--month", default=None, help="'YYYY-MM'")
    parser.add_argument("--months", default=None,
                        help="comma list of 'YYYY-MM' (backfill runs)")
    parser.add_argument("--last-month", action="store_true")
    parser.add_argument("--out-root", default=os.path.join(WEBROOT,
                                                           "invoices"))
    parser.add_argument("--no-pdf", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import report_invoice_annex as ria
    from argia.finance.annex import load_invoicing_overview

    if args.last_month:
        from argia.core.time_utils import now_mx
        yms = [ria.last_complete_month(now_mx())]
    elif args.months:
        yms = [m.strip() for m in args.months.split(",") if m.strip()]
    elif args.month:
        yms = [args.month]
    else:
        LOG.error("give --month YYYY-MM, --months, or --last-month")
        return 3

    chromium = None if args.no_pdf else find_chromium()
    if not args.no_pdf and not chromium:
        LOG.warning("no chromium found — HTML only, PDFs pending")

    history_by_year = {}
    for ym in yms:
        out_dir = os.path.join(args.out_root, ym)
        os.makedirs(out_dir, exist_ok=True)
        rc = ria.main(["--month", ym, "--out-dir", out_dir])
        if rc == 3:
            return rc                  # config error — nothing rendered

        if chromium:
            pat = "factura_*_%s.html" % ym.replace("-", "")
            for h in sorted(glob.glob(os.path.join(out_dir, pat))):
                html_to_pdf(h, h[:-5] + ".pdf", chromium)

        build_month_zip(out_dir, ym)
        push_to_drive(ym, out_dir)

        year = ym[:4]
        if year not in history_by_year:
            history_by_year[year] = load_invoicing_overview(year)
        published = [n for n, _h, _p in
                     scan_months(args.out_root).get(ym, [])]
        record_invoicing(ym, set(published), history_by_year[year])

    from argia.core.time_utils import now_mx as _now
    idx = render_index(scan_months(args.out_root),
                       records=all_records(),
                       zips=month_zips(args.out_root),
                       generated_at=_now().strftime("%Y-%m-%d %H:%M MX"))
    with open(os.path.join(args.out_root, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(idx)
    LOG.info("index written; months published: %s",
             sorted(scan_months(args.out_root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
