"""Credential normalisation in the setup app.

Background (2026-08-28): two new users could not log in although the
database rows, the bcrypt hashes and every htpasswd file were correct.
An end-to-end probe on pio06 with a throwaway account showed the two
ways a correct-looking account still returns 401:

    exact username + password      -> 200
    CAPITALISED username           -> 401   (nginx compares byte-wise)
    password with trailing space   -> 401   (hashed with the space)

setup_app.py imports flask, which the test venv does not carry, so the
two helpers are lifted out with ast — the tests therefore exercise the
code that actually ships.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "server" / "bundle" / "setup_app.py"
WANTED = ("clean_username", "clean_password")


def _load():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    body = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in WANTED]
    assert len(body) == len(WANTED), f"helpers missing from {SRC.name}"
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]),
                 "<setup_app:creds>", "exec"), ns)
    return ns


NS = _load()
clean_username = NS["clean_username"]
clean_password = NS["clean_password"]


class TestCleanUsername:
    def test_lowercased(self):
        assert clean_username("Eduardo") == "eduardo"
        assert clean_username("ARTURO") == "arturo"

    def test_whitespace_trimmed(self):
        assert clean_username("  eduardo \n") == "eduardo"
        assert clean_username("Eduardo ") == "eduardo"

    def test_email_style_kept(self):
        assert clean_username(" Eduardo@Argia.com.MX ") == \
            "eduardo@argia.com.mx"

    def test_empty_and_none(self):
        assert clean_username(None) == ""
        assert clean_username("   ") == ""

    def test_idempotent(self):
        once = clean_username(" Eduardo ")
        assert clean_username(once) == once


class TestCleanPassword:
    def test_trailing_space_stripped(self):
        """The bug: a pasted password kept its trailing space, was
        hashed with it, and every login then returned 401."""
        assert clean_password("Sol4r-2026 ") == "Sol4r-2026"

    def test_leading_space_stripped(self):
        assert clean_password("  Sol4r-2026") == "Sol4r-2026"

    def test_newline_from_paste_stripped(self):
        assert clean_password("Sol4r-2026\r\n") == "Sol4r-2026"

    def test_inner_spaces_preserved(self):
        """Only the edges are trimmed — a passphrase stays intact."""
        assert clean_password(" correct horse battery ") == \
            "correct horse battery"

    def test_case_and_symbols_untouched(self):
        for pw in ("aB3!$%&+=/x", "ÁrbolÑ2026", "tab\tinside"):
            assert clean_password(pw) == pw

    def test_blank_means_generate(self):
        """Empty after trimming must stay falsy so /add generates a
        one-time password instead of hashing whitespace."""
        assert clean_password("   ") == ""
        assert not clean_password(None)


class TestAddUserWiring:
    """The helpers only help if /add actually calls them."""

    def _add_src(self):
        tree = ast.parse(SRC.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "add")
        return ast.dump(fn)

    def test_add_uses_both_helpers(self):
        src = self._add_src()
        assert "'clean_username'" in src.replace('"', "'")
        assert "'clean_password'" in src.replace('"', "'")

    def test_add_does_not_hash_raw_form_value(self):
        """Guards the regression: hash_pw must never see request.form
        output that skipped clean_password()."""
        src = SRC.read_text(encoding="utf-8")
        add = src[src.index("def add():"):src.index("@app.post('/settings')")]
        assert "request.form.get('password') or ''" not in add
