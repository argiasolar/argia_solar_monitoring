"""Session UI: admin-only links, logout across realms, no double-escaping.

Three faults reported 2026-08-28 from one screenshot:
  * the footer read "User &amp; access setup" — t() escapes its input,
    so the literal &amp; got escaped a second time;
  * a non-admin (eduardo) saw that Setup link at all, and clicking it
    raised a password prompt the browser answered with the PREVIOUS
    admin's cached credentials — so he silently became tomasz;
  * Log out only poisoned '/', leaving the cached credential for the
    /setup/ realm alive.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REP = (ROOT / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
MON = (ROOT / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
SET = (ROOT / "server" / "bundle" / "setup_app.py").read_text(encoding="utf-8")
SCOPES = ("'/'", "'/setup/'", "'/account/'", "'/monitoring/'")


class TestNoDoubleEscaping:
    def test_no_html_entities_passed_through_t(self):
        """t() runs html.escape on its arguments, so an entity written
        by hand comes out visible as '&amp;'."""
        import re
        for m in re.finditer(r't\("([^"]*)"', REP):
            assert "&amp;" not in m.group(1), m.group(1)
            assert "&lt;" not in m.group(1)

    def test_setup_link_renamed(self):
        assert 't("Setup","Configuración")' in REP
        assert "User &amp; access setup" not in REP


class TestAdminOnlyLinks:
    def test_setup_link_is_admin_only(self):
        i = REP.index('href="setup/"')
        assert 'class="adminonly"' in REP[i:i + 80]

    def test_hidden_by_default_in_css(self):
        assert ".adminonly{display:none;}" in REP

    def test_revealed_only_inside_the_admin_branch(self):
        """The reveal must sit inside `if(d.admin)`, never outside."""
        for src in (REP, MON):
            i = src.index("if(d.admin)")
            branch = src[i:i + 420]
            assert ".adminonly" in branch

    def test_reveal_needs_whoami_to_answer(self):
        """If whoami fails the links stay hidden — fail closed."""
        for src in (REP, MON):
            assert "catch(()=>{el.remove();})" in src

    def test_not_a_security_boundary_only_a_ui_hint(self):
        """nginx still gates /setup/ with admin.htpasswd — hiding the
        link is about not luring people into a password prompt."""
        auth = (ROOT / "server" / "bundle" /
                "nginx-argia_auth.conf").read_text(encoding="utf-8")
        block = auth[auth.index("location /setup/"):]
        assert "admin.htpasswd" in block[:block.index("}")]


class TestLogoutRaisesNoPrompt:
    """A logout must never issue a request that can answer 401: the
    browser turns that into its own sign-in dialog, which no password
    can satisfy (the page it guards is public). Reported 2026-08-28."""

    def test_logout_makes_no_request_at_all(self):
        for src in (REP, MON, SET):
            i = src.index("function argiaLogout")
            body = src[i:i + 200]
            for forbidden in ("fetch(", "XMLHttpRequest", "Authorization",
                              "Promise.allSettled"):
                assert forbidden not in body, (forbidden, body[:90])

    def test_logout_navigates_straight_to_the_public_page(self):
        for src in (REP, MON, SET):
            i = src.index("function argiaLogout")
            assert "/logged-out.html" in src[i:i + 200]

    def test_logged_out_page_is_outside_the_login(self):
        auth = (ROOT / "server" / "bundle" /
                "nginx-argia_auth.conf").read_text(encoding="utf-8")
        assert "location = /logged-out.html" in auth
        i = auth.index("location = /logged-out.html")
        assert "auth_basic off" in auth[i:i + 120]

    def test_page_is_honest_about_basic_auth(self):
        """No false 'you are signed out everywhere' claim: the browser
        keeps the credential until its windows close."""
        assert "close all browser windows" in REP
        assert "private window" in REP

    def test_monitoring_pages_have_a_logout_button(self):
        assert 'onclick="argiaLogout()"' in MON


class TestOneRealm:
    AUTH = (ROOT / "server" / "bundle" /
            "nginx-argia_auth.conf").read_text(encoding="utf-8")

    def test_single_realm_across_the_whole_site(self):
        """Browsers cache one credential per realm, so a second realm
        means a second prompt when moving between sections."""
        import re
        realms = set(re.findall(r'auth_basic "([^"]+)"', self.AUTH))
        assert realms == {"ARGIA"}, realms

    def test_setup_and_monitoring_no_longer_split_the_realm(self):
        """Only the directives matter — the section comments may still
        say 'ARGIA reporting'."""
        for old in ("ARGIA reporting", "ARGIA setup", "ARGIA monitoring"):
            assert f'auth_basic "{old}"' not in self.AUTH
