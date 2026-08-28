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


class TestLogoutClearsEveryRealm:
    def test_all_three_pages_share_the_scope_list(self):
        for src in (REP, MON, SET):
            for scope in SCOPES:
                assert scope in src, scope

    def test_logout_no_longer_hits_only_the_root(self):
        for src in (REP, MON, SET):
            assert "fetch('/',{headers:{Authorization:'Basic eDp4'}})" \
                not in src.replace(" ", "")

    def test_uses_allsettled_so_one_404_cannot_abort_it(self):
        for src in (REP, MON, SET):
            assert "Promise.allSettled" in src

    def test_lands_on_the_public_page(self):
        for src in (REP, MON, SET):
            assert "/logged-out.html" in src

    def test_monitoring_pages_have_a_logout_button(self):
        assert 'onclick="argiaLogout()"' in MON
