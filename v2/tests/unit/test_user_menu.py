"""User menu, navigation and the 401 landing page (feedback 2026-08-28).

  1. monitoring had no way back to the reports after the switch
     buttons were removed;
  2. the language switch belongs to the person, not the toolbar;
  3. so does Log out;
  4. Download PDF appeared twice on report pages;
  5. cancelling the sign-in box left the bare nginx 401 wall.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REP = (ROOT / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
MON = (ROOT / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
SET = (ROOT / "server" / "bundle" / "setup_app.py").read_text(encoding="utf-8")
AUTH = (ROOT / "server" / "bundle" /
        "nginx-argia_auth.conf").read_text(encoding="utf-8")


def flat(src):
    """The HTML as the browser sees it, not as Python types it.

    The three generators build the same markup three ways: an f-string
    triple quote (report_gen), and adjacent single-quoted literals with
    escaped inner quotes (monitoring_gen, setup_app).  Collapsing the
    literal joins and the backslashes lets one assertion cover all of
    them instead of three spellings of the same check.
    """
    return re.sub(r"'\s*\n\s*'", "", src).replace("\\'", "'")


class TestWayBackToReports:
    def test_monitoring_controls_link_to_the_reports_root(self):
        i = MON.index("def controls(")
        block = MON[i:i + 1800]
        assert 'href="/"' in block
        assert "Reports" in block

    def test_reports_link_is_same_origin(self):
        """One host now — no hostname, so no second login."""
        assert "https://report.argia.com.mx" not in MON

    def test_setup_page_also_offers_the_way_back(self):
        assert '← Reports' in SET

    def test_logo_still_goes_home(self):
        assert '<a href="/" title="ARGIA reports">' in MON


class TestEverythingPersonalIsInTheMenu:
    def test_all_three_surfaces_have_the_menu(self):
        for src in (REP, MON, SET):
            assert 'class="umenu"' in src
            assert "argiaMenu(" in src

    def test_menu_holds_account_language_and_logout(self):
        for src in (REP, MON, SET):
            i = src.index('class="umenu"')
            menu = flat(src[i:i + 1600])
            assert 'href="/account/"' in menu
            assert "setLang('en')" in menu and "setLang('es')" in menu
            assert 'onclick="argiaLogout()"' in menu

    def test_language_buttons_left_the_toolbar(self):
        """No loose EN/ES buttons outside the menu."""
        for src in (REP, MON, SET):
            s = flat(src)
            assert s.count("setLang('en')") == 1, "one EN button only"
            assert s.count("setLang('es')") == 1

    def test_logout_button_left_the_toolbar(self):
        """Exactly one Log out control.

        Counted on the onclick, not the bare call: the source also
        contains ``function argiaLogout()`` and the definition is not a
        second button.
        """
        for src in (REP, MON, SET):
            assert flat(src).count('onclick="argiaLogout()"') == 1

    def test_menu_closes_on_outside_click_and_escape(self):
        for src in (REP, MON, SET):
            assert "document.addEventListener('click'" in src
            assert "Escape" in src

    def test_chip_shows_a_caret_so_it_reads_as_a_menu(self):
        for src in (REP, MON):
            assert "className='car'" in src or 'class="car"' in src
        assert 'class="car"' in SET

    def test_active_language_is_marked(self):
        for src in (REP, MON, SET):
            assert "lang-btn" in src
            assert "classList.toggle('active'" in src


class TestSinglePdfButton:
    def test_only_the_bottom_one_remains(self):
        assert REP.count("window.print()") == 1

    def test_it_is_the_bottom_row(self):
        i = REP.index("window.print()")
        assert "pdfrow" in REP[max(0, i - 260):i]

    def test_toolbar_has_no_pdf_button(self):
        """chrome_top ends where pdf_bottom begins — the bottom row is
        allowed to print, the toolbar above it is not."""
        i = REP.index("def chrome_top")
        assert "window.print()" not in REP[i:REP.index("def pdf_bottom")]


class TestNoAccessPage:
    def test_nginx_serves_a_body_for_401(self):
        assert "error_page 401 /no-access.html;" in AUTH

    def test_that_body_is_public(self):
        i = AUTH.index("location = /no-access.html")
        assert "auth_basic off" in AUTH[i:i + 120]

    def test_page_is_generated(self):
        assert "def no_access_page" in REP
        assert "write('no-access.html', no_access_page())" in REP

    def test_page_offers_the_way_back(self):
        i = REP.index("def no_access_page")
        body = flat(REP[i:i + 1600])
        assert 'href="/"' in body
        assert "Back to reports / Volver a reportes" in body

    def test_page_returns_to_reports_on_its_own(self):
        i = REP.index("def no_access_page")
        assert 'http-equiv="refresh"' in REP[i:i + 1400]

    def test_bilingual(self):
        i = REP.index("def no_access_page")
        body = REP[i:i + 1400]
        assert "Sin acceso" in body and "No access" in body

    def test_challenge_still_sent_so_the_prompt_appears_first(self):
        """error_page must not rewrite the status to 200 — the browser
        needs the 401 + WWW-Authenticate to offer the sign-in box."""
        assert "error_page 401 =200" not in AUTH
