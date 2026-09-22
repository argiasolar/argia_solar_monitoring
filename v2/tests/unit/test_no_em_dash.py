"""v261 - no em dash anywhere.

Tomasz: "I do not want to see an em dash anywhere in the portal, report, or
anywhere". Every string that can reach a page, a PDF, a mail or a log is
written with a plain hyphen. This test is the regression guard: it scans
the whole v2 tree (code, templates, fixtures, docs, configs) for the
character in any of its spellings.

The one permitted exception is a line that MATCHES an em dash typed by a
person in an input file (the AR/AP tracker uses it for 'nothing'); such a
line carries the marker  em-dash-ok  and is skipped here.
"""
from __future__ import annotations

import pathlib

V2 = pathlib.Path(__file__).resolve().parents[2]
MARK = "em-dash-ok"
EM = chr(0x2014)
ESC = "\\" + "u2014"            # the Python/JSON escape, spelled so this file does not trip itself
ENT = "&" + "mdash;"
NUM = "&#" + "8212;"
RAW = "\\" + "xe2" + "\\" + "x80" + "\\" + "x94"   # the UTF-8 bytes in a bytes literal
SPELLINGS = (EM, ENT, NUM, ESC, ESC.upper(), RAW)
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}
EXT = {".py", ".html", ".js", ".css", ".sh", ".sql", ".txt", ".json", ".md", ".yml", ".yaml",
       ".svg", ".conf", ".service", ".timer", ".csv", ".env", ".ini", ".toml", ".cfg",
       ".example", ".gs", ".lock"}


def offenders():
    out = []
    for p in V2.rglob("*"):
        if not p.is_file() or p.suffix not in EXT or SKIP_DIRS & set(p.parts):
            continue
        try:
            lines = p.read_text(encoding="utf-8").split("\n")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(lines, 1):
            if MARK in line:
                continue
            if any(s in line for s in SPELLINGS):
                out.append(f"{p.relative_to(V2)}:{i}")
    return out


class TestNoEmDash:
    def test_no_em_dash_reaches_any_output(self):
        bad = offenders()
        assert bad == [], "em dash found (use '-' instead, or tag an INPUT matcher with em-dash-ok):\n" + "\n".join(bad[:40])

    def test_the_exception_is_an_input_matcher_only(self):
        # the tracker accepts a typed em dash as 'nothing' - that line is tagged and is the whole allow-list
        tagged = [p for p in (V2 / "server" / "bundle").rglob("*.py")
                  if MARK in p.read_text(encoding="utf-8")]
        assert [p.name for p in tagged] == ["fin_books.py"]
        src = (V2 / "server" / "bundle" / "fin_books.py").read_text(encoding="utf-8")
        tagged_lines = [l for l in src.split("\n") if MARK in l and EM in l]
        assert len(tagged_lines) == 1 and ".lower() in (" in tagged_lines[0]

    def test_this_guard_sees_every_spelling(self):
        for s in ("a " + EM + " b", "a" + ENT + "b", "a" + NUM + "b", "'" + ESC + "'", "b'" + RAW + "'"):
            assert any(x in s for x in SPELLINGS), s
