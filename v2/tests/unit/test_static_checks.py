"""v263 - static inspection as a test, so it runs on every push.

Two bug classes that no behavioural test catches until the code path runs
on the server:

* a file that does not even compile on the oldest Python in the CI matrix
  (3.10) - many operational scripts and the Pi tools are never imported
  by a test, so a syntax slip there is only found in production;
* the pyflakes findings that are real defects, not style: an undefined
  name (a NameError waiting for its code path), a duplicate dict key
  (the first value is silently lost - v263 found one in the icon table),
  a name redefined before use, a comparison with a literal via ``is``.

Style findings (unused import, unused local, f-string without a
placeholder) are deliberately NOT failures here.
"""
from __future__ import annotations

import pathlib
import py_compile

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
CODE_DIRS = ("argia", "scripts", "server", "pi", "tools")
SKIP_PARTS = {".venv", "__pycache__", ".git"}

# pyflakes message classes that are defects. Everything else is style.
DEFECTS = (
    "UndefinedName", "UndefinedLocal", "UndefinedExport",
    "MultiValueRepeatedKeyLiteral", "MultiValueRepeatedKeyVariable",
    "RedefinedWhileUnused", "ImportStarUsage", "ReturnOutsideFunction",
    "YieldOutsideFunction", "ContinueOutsideLoop", "BreakOutsideLoop",
    "DefaultExceptNotLast", "TwoStarredExpressions", "IsLiteral",
    "StringDotFormatExtraPositionalArguments", "StringDotFormatMissingArgument",
    "PercentFormatMissingArgument", "PercentFormatExtraArguments",
)


def code_files():
    for d in CODE_DIRS:
        for p in sorted((V2 / d).rglob("*.py")):
            if not SKIP_PARTS & set(p.parts):
                yield p


def test_there_is_code_to_check():
    assert sum(1 for _ in code_files()) > 150


@pytest.mark.parametrize("path", list(code_files()), ids=lambda p: str(p.relative_to(V2)))
def test_every_source_file_compiles(path, tmp_path):
    py_compile.compile(str(path), cfile=str(tmp_path / "x.pyc"), doraise=True)


def test_no_pyflakes_defects():
    checker = pytest.importorskip("pyflakes.checker")
    import ast
    bad = []
    for p in code_files():
        src = p.read_text(encoding="utf-8")
        w = checker.Checker(ast.parse(src, filename=str(p)), filename=str(p))
        for m in w.messages:
            if type(m).__name__ in DEFECTS:
                bad.append(f"{p.relative_to(V2)}:{m.lineno}: {m.message % m.message_args}")
    assert bad == [], "real defects found by pyflakes:\n" + "\n".join(bad)


CONTROL_CHARS = "\x08\x07\x0b\x0c\x00"


def test_no_invalid_escape_and_no_hidden_control_character():
    """v263: ask_app's page template had JavaScript regexes inside a normal
    Python string. '\\*' is an invalid escape (a SyntaxWarning today, an error
    in a future Python) and '\\b' is a BACKSPACE in Python, so the JS word
    boundary in the slide-link regex silently became a control character and
    'slide 12' in an answer never turned into a link. Both are caught here."""
    import ast
    import warnings
    bad = []
    for p in code_files():
        src = p.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            compile(src, str(p), "exec", dont_inherit=True)
        # only the escape class: newer Pythons add other SyntaxWarnings (3.14: return in
        # finally) that deserve their own decision, not a red suite on the laptop
        bad += ["%s:%s: %s" % (p.relative_to(V2), x.lineno, x.message) for x in w
                if "escape sequence" in str(x.message)]
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                hit = [hex(ord(c)) for c in CONTROL_CHARS if c in n.value]
                if hit:
                    bad.append("%s:%s: control character %s in a string literal" % (p.relative_to(V2), n.lineno, hit))
    assert bad == [], "\n".join(bad)
