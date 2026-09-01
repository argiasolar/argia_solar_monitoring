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
import logging
import os
import shutil
import subprocess
import sys

LOG = logging.getLogger("argia.invoice_publish")

WEBROOT = "/www/hosting/monitoring.argia.com.mx/www"
CHROMIUM = ("chromium", "chromium-browser", "google-chrome")

PLANT_CLIENT = {
    "gto1": "TAIGENE (Leon, GTO)",
    "mex1": "SAG (CDMX)",
    "mex2": "VITALMEX (CDMX)",
    "nl1": "PLASTIC OMNIUM (Monterrey, NL)",
    "slp1": "QUIMICA COYOACAN (SLP)",
    "slp2": "HOLIDAY INN EXPRESS (SLP)",
}


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


def scan_months(out_root: str):
    """{ym: [(plant, has_html, has_pdf)]} for everything published."""
    out = {}
    for d in sorted(glob.glob(os.path.join(out_root, "[0-9]" * 4 + "-"
                                           + "[0-9]" * 2))):
        ym = os.path.basename(d)
        rows = []
        for h in sorted(glob.glob(os.path.join(d, "invoice_*_%s.html" % ym))):
            plant = os.path.basename(h)[len("invoice_"):-len("_%s.html" % ym)]
            rows.append((plant, True,
                         os.path.exists(h[:-5] + ".pdf")))
        if rows:
            out[ym] = rows
    return out


def render_index(months, blocked_now=None, generated_at=""):
    """The /invoices/ page: newest month first, one row per plant,
    PDF + HTML links. Pure — tested against fixed inputs."""
    blocked_now = blocked_now or {}
    parts = []
    for ym in sorted(months, reverse=True):
        rows = []
        for plant, has_html, has_pdf in months[ym]:
            client = PLANT_CLIENT.get(plant, plant.upper())
            pdf = (f'<a class="btn" href="{ym}/invoice_{plant}_{ym}.pdf" '
                   f'download>PDF</a>' if has_pdf else
                   '<span class="mut">PDF pending</span>')
            web = (f'<a class="btn" href="{ym}/invoice_{plant}_{ym}.html">'
                   f'{_esc("View / Ver")}</a>' if has_html else "")
            rows.append(f"<tr><td>{plant.upper()}</td><td>{_esc(client)}"
                        f"</td><td>{pdf} {web}</td></tr>")
        for plant, why in sorted(blocked_now.get(ym, [])):
            rows.append(f'<tr><td>{plant.upper()}</td>'
                        f'<td>{_esc(PLANT_CLIENT.get(plant, ""))}</td>'
                        f'<td><span class="blocked">{_esc(why)}</span>'
                        f"</td></tr>")
        parts.append(
            f"<h2>{ym}</h2><table><thead><tr><th>Planta</th>"
            f"<th>Cliente</th><th></th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")
    body = "".join(parts) or ("<p>No annexes published yet. / "
                              "Aún no hay anexos publicados.</p>")
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Anexos de facturación — ARGIA</title><style>
body{{margin:0;background:#f6f7f8;color:#202124;
font-family:"Segoe UI",system-ui,sans-serif;font-size:15px}}
.wrap{{max-width:760px;margin:0 auto;padding:26px 18px 48px}}
h1{{font-size:21px;letter-spacing:.14em;text-transform:uppercase}}
h2{{font-size:16px;margin:26px 0 8px}}
table{{width:100%;border-collapse:collapse;background:#fff;
border:1px solid #e0e3e7;border-radius:10px}}
th,td{{text-align:left;padding:9px 12px;border-bottom:1px solid #eef0f2}}
th{{color:#5f6368;font-weight:500;font-size:12.5px}}
.btn{{display:inline-block;border:1px solid #dadce0;border-radius:7px;
padding:4px 12px;text-decoration:none;color:#202124;background:#fff}}
.btn:hover{{border-color:#b9bec4}}
.mut{{color:#9aa0a6;font-size:13px}}
.blocked{{color:#a50e0e;font-size:13px}}
.sub{{color:#5f6368;font-size:13px;margin:2px 0 18px}}
a.back{{color:#1a73e8;text-decoration:none}}</style></head><body>
<div class="wrap"><p><a class="back" href="/">← Reports / Reportes</a></p>
<h1>Anexos de facturación</h1>
<div class="sub">Invoice annexes · energía PPA · un anexo por
planta y mes cerrado. Cada mes nuevo aparece automáticamente el
día 1 después del cierre de conciliación. ·
Generated {_esc(generated_at)}</div>
{body}
</div></body></html>"""


def _esc(s):
    return _html.escape(str(s), quote=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--month", default=None, help="'YYYY-MM'")
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

    if args.last_month:
        from argia.core.time_utils import now_mx
        ym = ria.last_complete_month(now_mx())
    elif args.month:
        ym = args.month
    else:
        LOG.error("give --month YYYY-MM or --last-month")
        return 3

    out_dir = os.path.join(args.out_root, ym)
    os.makedirs(out_dir, exist_ok=True)
    rc = ria.main(["--month", ym, "--out-dir", out_dir])
    if rc == 3:
        return rc                      # config error — nothing rendered

    chromium = None if args.no_pdf else find_chromium()
    if not args.no_pdf and not chromium:
        LOG.warning("no chromium found — HTML only, PDFs pending")
    if chromium:
        for h in sorted(glob.glob(os.path.join(out_dir,
                                               "invoice_*_%s.html" % ym))):
            html_to_pdf(h, h[:-5] + ".pdf", chromium)

    from argia.core.time_utils import now_mx as _now
    idx = render_index(scan_months(args.out_root),
                       generated_at=_now().strftime("%Y-%m-%d %H:%M MX"))
    with open(os.path.join(args.out_root, "index.html"), "w",
              encoding="utf-8") as f:
        f.write(idx)
    LOG.info("index written; months published: %s",
             sorted(scan_months(args.out_root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
