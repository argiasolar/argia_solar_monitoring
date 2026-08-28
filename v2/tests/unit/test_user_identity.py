"""Identity on screen + user profile fields (name, surname, email).

Why this exists (2026-08-28): while testing several accounts it was
impossible to tell who was signed in, and it looked as if a non-admin
had reached /setup/. The nginx log showed the opposite —

    arturo  /setup/  401      <- correctly refused
    tomasz  /setup/  200      <- the browser's cached admin credentials

Basic auth re-sends cached credentials silently, so every page now
names the account it belongs to, and /account/whoami is the single
source for that name.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "server" / "bundle" / "setup_app.py"
MONGEN = ROOT / "server" / "monitoring_gen.py"
REPGEN = ROOT / "server" / "bundle" / "report_gen.py"
SRC = SETUP.read_text(encoding="utf-8")

FNS = ("display_name", "clean_name", "clean_email")


def _load():
    tree = ast.parse(SRC)
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in FNS]
    assert len(body) == len(FNS)
    ns = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]),
                 "<setup_app:identity>", "exec"), ns)
    return ns


NS = _load()
display_name = NS["display_name"]
clean_name = NS["clean_name"]
clean_email = NS["clean_email"]


class TestDisplayName:
    def test_first_and_last(self):
        assert display_name("Eduardo", "Ramirez", "eduardo") == \
            "Eduardo Ramirez"

    def test_only_first(self):
        assert display_name("Eduardo", "", "eduardo") == "Eduardo"

    def test_only_last(self):
        assert display_name("", "Ramirez", "eduardo") == "Ramirez"

    def test_falls_back_to_username(self):
        assert display_name("", "", "arturo") == "arturo"
        assert display_name(None, None, "arturo") == "arturo"

    def test_never_empty_when_username_known(self):
        """The chip must always say something — a blank chip is worse
        than no chip."""
        assert display_name("  ", "  ", "vit") == "vit"

    def test_whitespace_around_parts_ignored(self):
        assert display_name(" Ana ", " Lopez ", "ana") == "Ana Lopez"


class TestCleanName:
    def test_trims_and_collapses(self):
        assert clean_name("  Juan   Carlos ") == "Juan Carlos"

    def test_newlines_collapse(self):
        assert clean_name("Juan\nCarlos") == "Juan Carlos"

    def test_length_capped(self):
        assert len(clean_name("x" * 200)) == 60

    def test_none_is_empty(self):
        assert clean_name(None) == ""


class TestCleanEmail:
    def test_lowercased_and_trimmed(self):
        assert clean_email("  Eduardo@Argia.COM.mx ") == \
            "eduardo@argia.com.mx"

    def test_blank_allowed(self):
        assert clean_email("") == "" and clean_email(None) == ""

    def test_garbage_rejected_not_stored(self):
        for bad in ("nope", "a@b", "a b@c.mx", "@argia.com.mx",
                    "eduardo@", "two@@argia.com.mx"):
            assert clean_email(bad) == "", bad

    def test_plus_addressing_kept(self):
        assert clean_email("ed+solar@argia.com.mx") == \
            "ed+solar@argia.com.mx"


class TestWhoamiEndpoint:
    def test_identity_from_the_proxy_header_only(self):
        body = SRC[SRC.index("def whoami"):SRC.index("def account_change")]
        assert "X-Remote-User" in body
        assert "request.args" not in body and "request.form" not in body

    def test_reports_admin_flag_and_name(self):
        body = SRC[SRC.index("def whoami"):SRC.index("def account_change")]
        for key in ("'user'", "'name'", "'admin'", "'level'"):
            assert key in body

    def test_not_cached(self):
        body = SRC[SRC.index("def whoami"):SRC.index("def account_change")]
        assert "no-store" in body

    def test_lives_under_the_account_prefix(self):
        """So it inherits /account/'s all.htpasswd and cannot answer
        for an unauthenticated caller."""
        assert "@app.get('/account/whoami')" in SRC


class TestProfilePersistence:
    def test_columns_migrated(self):
        for col in ("first_name", "last_name", "email"):
            assert f"ALTER TABLE users ADD COLUMN {col}" in SRC

    def test_add_stores_profile(self):
        body = SRC[SRC.index("def add():"):SRC.index("@app.post('/settings')")]
        assert "first_name,last_name,email" in body.replace(" ", "")
        for f in ("clean_name", "clean_email"):
            assert f in body

    def test_update_stores_profile(self):
        body = SRC[SRC.index("def update():"):SRC.index("@app.post('/suspend')")]
        assert "first_name=?" in body and "email=?" in body

    def test_edit_form_exposes_the_fields(self):
        body = SRC[SRC.index("def edit_form"):SRC.index("MAINT_CATEGORIES")]
        for f in ('name="first_name"', 'name="last_name"', 'name="email"'):
            assert f in body

    def test_users_table_shows_name_column(self):
        body = SRC[SRC.index("def users_table"):SRC.index("def add_form")]
        assert 'data-en="Name"' in body
        assert "display_name(" in body


class TestChipOnEveryPortal:
    MON = MONGEN.read_text(encoding="utf-8")
    REP = REPGEN.read_text(encoding="utf-8")

    def test_both_portals_carry_the_chip(self):
        for src in (self.MON, self.REP):
            assert 'id="whoami"' in src
            assert "/account/whoami" in src

    def test_chip_opens_a_menu_that_reaches_the_account_page(self):
        """The chip stopped being a link in v150: it opens the user
        menu, and My account is the first row of that menu."""
        for src in (self.MON, self.REP):
            i = src.index('id="whoami"')
            assert "argiaMenu(" in src[i - 120:i + 200]
            j = src.index('class="umenu"')
            assert 'href="/account/"' in src[j:j + 400]

    def test_chip_shows_username_when_name_differs(self):
        for src in (self.MON, self.REP):
            assert "d.name!==d.user" in src

    def test_chip_marks_admins(self):
        for src in (self.MON, self.REP):
            assert "d.admin" in src

    def test_chip_removed_when_unidentified(self):
        """No stale name may linger if whoami fails or returns
        nothing — an empty chip would be misleading."""
        for src in (self.MON, self.REP):
            assert src.count("el.remove()") >= 2

    def test_setup_page_names_the_actor_server_side(self):
        body = SRC[SRC.index("def page("):SRC.index("def users_table")]
        assert "X-Remote-User" in body and "whoami" in body
        assert "profile_of" in body
