"""Self-service profile editing on /account/ (name, surname, email).

Same identity rule as the password route: the account edited comes
from the basic-auth name nginx forwards, never from the form — a user
can only ever edit themselves.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "server" / "bundle" / "setup_app.py"
REPGEN = ROOT / "server" / "bundle" / "report_gen.py"
SRC = SETUP.read_text(encoding="utf-8")
BODY = SRC[SRC.index("def account_profile"):SRC.index("def healthz")]


def _fn(name):
    fn = next(n for n in ast.walk(ast.parse(SRC))
              if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.dump(fn)


class TestProfileRoute:
    def test_route_exists(self):
        assert "@app.post('/account/profile')" in SRC

    def test_identity_from_header_not_form(self):
        assert "X-Remote-User" in BODY
        assert "request.form.get('username')" not in BODY

    def test_update_scoped_to_signed_in_user(self):
        assert "WHERE username=? AND disabled=0" in BODY

    def test_only_profile_columns_touched(self):
        """Must not be able to grant itself access or admin."""
        for forbidden in ("is_admin", "level", "reports", "plant_admin",
                          "hash"):
            assert f"{forbidden}=?" not in BODY

    def test_csrf_checked(self):
        assert "check_csrf" in BODY

    def test_bad_email_rejected_not_silently_dropped(self):
        assert "does not look valid" in BODY

    def test_values_sanitised(self):
        assert "clean_name" in BODY and "clean_email" in BODY


class TestAccountPageShowsProfile(object):
    PAGE = SRC[SRC.index("def account_page"):SRC.index("@app.get('/account/')")]

    def test_form_has_the_three_fields(self):
        for f in ('name="first_name"', 'name="last_name"',
                  'name="email"'):
            assert f in self.PAGE

    def test_fields_prefilled_from_db(self):
        assert "SELECT first_name,last_name,email" in self.PAGE

    def test_username_is_shown_but_not_editable(self):
        assert "username cannot be changed" in self.PAGE
        assert 'name="username"' not in self.PAGE

    def test_password_form_still_present(self):
        assert 'action="change"' in self.PAGE

    def test_password_success_hides_the_forms(self):
        """After a password change the browser holds stale
        credentials — re-showing the forms would just fail."""
        assert "pw_done" in self.PAGE


class TestSinglePlaceForPassword:
    REP = REPGEN.read_text(encoding="utf-8")

    def test_no_password_link_in_the_footer(self):
        assert "Change my password" not in self.REP

    def test_identity_chip_is_the_only_entry_point(self):
        assert self.REP.count('href="/account/"') == 1
        assert 'id="whoami"' in self.REP
