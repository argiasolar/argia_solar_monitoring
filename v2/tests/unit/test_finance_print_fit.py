"""v254 — the emailed financial report must carry every column.

Tomasz, 2026-09-13: the weekly mailed PDF was truncated while the same page
printed by hand from a browser was complete. Cause: the screen layout is
1080px wide, A4 is ~700px, and the two ways of making the PDF disagree about
what to do with the excess —

* a person pressing Ctrl+P gets Chrome's print dialog, which shrinks to fit;
* ``scripts/financial_mail.py`` renders with ``chromium --print-to-pdf``,
  which has no shrink-to-fit and simply CLIPS whatever overflows the sheet.

So the mail lost debt service, loan position and BOTH DSCR columns — the
covenant numbers — off the right edge, silently, every week.

These tests pin the print stylesheet that makes the page fit on its own. The
first two are cheap and always run; the last actually drives headless chromium
with the production flags and reads the text back out of the PDF, and skips
where no chromium is installed.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from argia.finance.income import Period                      # noqa: E402
from argia.finance.webreport import (                        # noqa: E402
    build_daily_atoms, render_financial_report_html,
)
from tests.unit.test_finance_report import _portfolio, _sheets   # noqa: E402

SRC = (V2 / "argia" / "finance" / "webreport.py").read_text(encoding="utf-8")

# every column of the per-asset table, as the reader sees it
COLUMNS = ("Asset", "Type", "Exp. revenue", "Actual revenue", "O&M",
           "Debt service", "Loan position", "DSCR exp.", "DSCR act.")

# the exact flags scripts/financial_mail.py::render_pdf uses
MAIL_FLAGS = ["--headless=new", "--disable-gpu", "--no-sandbox",
              "--hide-scrollbars", "--virtual-time-budget=8000",
              "--no-pdf-header-footer"]


def _html() -> str:
    data = build_daily_atoms(_sheets(), _portfolio(8000.0),
                             Period.from_iso("2026-07-01", "2026-08-31"))
    return render_financial_report_html(data, "2026-09-13 00:00 UTC")


def _print_block() -> str:
    return SRC.split("@media print", 1)[1].split("</style>", 1)[0]


class TestThePrintStylesheetMakesThePageFit:
    def test_the_sheet_size_and_a_fluid_wrapper_are_declared(self):
        blk = _print_block()
        assert "@page" in blk and "A4" in blk
        assert "margin: 10mm" in blk
        # the 1080px screen cap must be lifted, or the table overflows A4
        assert re.search(r"\.wrap\s*{{\s*max-width:\s*none", blk), blk

    def test_the_kpi_row_is_pinned_to_five_columns(self):
        """Lifting the width cap narrows the grid; without this the five KPI
        cards wrap onto a second row and the report grows a page."""
        assert "repeat(5, 1fr)" in _print_block()

    def test_the_table_is_shrunk_rather_than_left_to_overflow(self):
        blk = _print_block()
        m = re.search(r"table\s*{{\s*font-size:\s*([\d.]+)px", blk)
        assert m, "print CSS must set an explicit table font size"
        assert float(m.group(1)) <= 10.0, m.group(1)

    def test_the_reason_is_written_down_next_to_the_fix(self):
        """The next person to 'tidy up' this block needs to know that a
        browser's shrink-to-fit is what hid the bug for so long."""
        blk = _print_block().lower()
        assert "print-to-pdf" in blk and "clip" in blk


class TestTheRenderedPdfKeepsEveryColumn:
    """The real thing: render exactly as the weekly mail does, read it back."""

    @staticmethod
    def _chromium():
        for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
            p = shutil.which(name)
            if p:
                return p
        for p in Path("/opt/pw-browsers").glob("chromium*/chrome-linux/chrome"):
            return str(p)
        return None

    def _pdf_text(self, html: str) -> str:
        chromium = self._chromium()
        if not chromium or not shutil.which("pdftotext"):
            pytest.skip("needs chromium and pdftotext")
        with tempfile.TemporaryDirectory() as d:
            src, pdf = Path(d) / "r.html", Path(d) / "r.pdf"
            src.write_text(html, encoding="utf-8")
            r = subprocess.run(
                [chromium, *MAIL_FLAGS, f"--print-to-pdf={pdf}", f"file://{src}"],
                capture_output=True, text=True, timeout=180)
            assert pdf.exists() and pdf.stat().st_size > 5000, r.stderr[-400:]
            subprocess.run(["pdftotext", "-layout", str(pdf), str(pdf) + ".txt"],
                           check=True, timeout=60)
            return Path(str(pdf) + ".txt").read_text(encoding="utf-8", errors="ignore")

    def test_every_per_asset_column_survives_the_headless_render(self):
        text = self._pdf_text(_html())
        missing = [c for c in COLUMNS if c not in text]
        assert not missing, (
            f"columns clipped off the emailed PDF: {missing}. The print CSS no "
            f"longer makes the page fit A4 — see the module docstring.")

    def test_the_regression_itself_is_reproducible(self):
        """Guard against a false pass: with the print block removed, the same
        render must LOSE columns. If this stops failing, the test above is no
        longer proving anything."""
        html = _html()
        start = html.index("@media print")
        depth, i = 0, html.index("{", start)
        while True:                                   # walk to the matching brace
            if html[i] == "{":
                depth += 1
            elif html[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        text = self._pdf_text(html[:start] + html[i + 1:])
        assert [c for c in COLUMNS if c not in text], (
            "removing the print CSS no longer truncates the PDF — either the "
            "layout changed or this harness stopped exercising the real path")


# --------------------------------------------------------------------------
# v255 — the page the WEEKLY MAIL actually renders
#
# v254 fixed argia/finance/webreport.py, which is a real page (it is what
# scripts/financial_report_publish.py publishes) but NOT the one that gets
# mailed. scripts/financial_mail.py prints
# /www/hosting/portal.argia.com.mx/www/report/financial/index.html, which the
# portal builds from server/bundle/report_gen.py::financial_body(). Verified on
# pio06: the live page carries two @page rules — portal_chrome's landscape one
# and financial_body's inline portrait one — and the inline block, being later
# in the document, wins. The rendered MediaBox was 594.96 x 841.92 (A4
# portrait), byte-identical to the truncated PDF Tomasz received.
# --------------------------------------------------------------------------

RG_SRC = (V2 / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
PC_SRC = (V2 / "server" / "bundle" / "portal_chrome.py").read_text(encoding="utf-8")


def _fin_print_block() -> str:
    """financial_body()'s own <style> — the rules that win on the mailed page."""
    blk = RG_SRC.split("the mailed PDF is this page printed", 1)[1]
    return blk.split("</style>", 1)[0]


class TestTheMailedPageFitsItsSheet:
    def test_the_nine_column_table_is_printed_landscape(self):
        """194mm of A4 portrait never fitted nine columns. 281mm does."""
        blk = _fin_print_block()
        assert "@page{{size:A4 landscape" in blk, blk[:400]
        assert "size:A4 portrait" not in blk

    def test_the_chrome_and_the_page_now_agree_on_orientation(self):
        """Both stylesheets reach the mailed page; when they disagreed the
        later one won by accident rather than by decision."""
        assert "@page{size:A4 landscape" in PC_SRC          # portal chrome
        assert "@page{{size:A4 landscape" in _fin_print_block()

    def test_the_asset_column_may_wrap_when_printing(self):
        """The safety valve: a tenth column must make the report taller, not
        quietly push a column off the sheet."""
        blk = _fin_print_block()
        assert "td.asset{{white-space:normal" in blk
        assert "#tbl_assets{{width:100%" in blk

    def test_why_it_broke_is_recorded_where_the_rule_lives(self):
        blk = _fin_print_block().lower()
        assert "print-to-pdf" in blk and "clip" in blk

    def test_the_mail_still_renders_this_exact_page(self):
        """If the mail ever points somewhere else, these tests stop meaning
        anything — pin the path and the flags they were written against."""
        mail = (V2 / "scripts" / "financial_mail.py").read_text(encoding="utf-8")
        assert 'os.path.join(WEBROOT, "report", "financial", "index.html")' in mail
        assert "--print-to-pdf=" in mail
        assert "--scale" not in mail      # the CLI has no scale flag; CSS must fit
        assert "write('report/financial/index.html', financial_report())" in \
            (V2 / "server" / "bundle" / "portal_gen.py").read_text(encoding="utf-8")
