"""v335 - the overview greeting in correct Spanish.

Tomasz's manual render showed "Buen tarde, Mariana": Spanish needs
"Buenos días / Buenas tardes / Buenas noches" (plural, feminine for tarde
and noche). English unchanged.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

pytest.importorskip("flask", reason="flask not installed here")
V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2))
import prologis_app as A                                    # noqa: E402
import prologis_ui as UI                                    # noqa: E402


@pytest.mark.parametrize("hour,en,es", [(0, "Good morning", "Buenos días"), (11, "Good morning", "Buenos días"),
                                        (12, "Good afternoon", "Buenas tardes"), (18, "Good afternoon", "Buenas tardes"),
                                        (19, "Good evening", "Buenas noches"), (23, "Good evening", "Buenas noches")])
def test_greeting_by_hour(hour, en, es):
    assert A.greeting(hour, UI.T("en")) == en
    assert A.greeting(hour, UI.T("es")) == es


def test_no_broken_spanish_left_in_the_page_code():
    src = (V2 / "server" / "bundle" / "prologis_app.py").read_text(encoding="utf-8")
    assert '"Buen")' not in src and "Buen tarde" not in src
