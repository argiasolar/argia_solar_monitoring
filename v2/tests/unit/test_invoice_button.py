"""The invoice button and its access path (2026-09-01 close feature)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REP = (ROOT / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
UNIT = (ROOT / "server" / "bundle" / "argia-invoice.service").read_text(encoding="utf-8")


class TestTheButtonExists:
    def test_on_the_financial_page_toolbar(self):
        i = REP.index("def financial_page")
        block = REP[i:i + 2600]
        assert 'href="/invoices/"' in block
        assert "Invoice annexes" in block

    def test_on_the_landing_page_footer(self):
        i = REP.index('class="flinks"')
        block = REP[i:i + 700]
        assert 'href="invoices/"' in block

    def test_as_a_full_card_on_the_landing_nav(self):
        """Feedback 2026-09-01: the footer link alone was invisible —
        'I still cant see the invoice button'. Now a first-class card
        beside Financial Report."""
        i = REP.index('class="nav navbig"')
        block = REP[i:i + 1400]
        assert 'href="invoices/"' in block
        assert "Invoice Annexes" in block

    def test_on_the_reconciliation_board(self):
        MON = (ROOT / "server" / "monitoring_gen.py").read_text(
            encoding="utf-8")
        i = MON.index("def recon_page")
        block = MON[i:i + 2000]
        assert 'href="/invoices/"' in block

    def test_it_is_not_admin_gated_in_the_markup(self):
        """financial users are not all admins; nginx+auth_core gate it
        by the financial grant, the markup must not hide it further.
        The anchor carries no class at all — the neighbouring Setup
        link is the adminonly one."""
        assert '<a href="invoices/">' in REP


class TestTheTimerRunsThePublisher:
    def test_publisher_not_the_bare_generator(self):
        assert "invoice_publish.py --last-month" in UNIT

    def test_documented_manual_regeneration(self):
        assert "--month" in UNIT
