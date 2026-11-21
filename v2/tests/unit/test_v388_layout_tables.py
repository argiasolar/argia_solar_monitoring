"""v388 - drawing string tables (tools/layout_tables.py), used by tools/string_layout_build.py for
Taigene rev. V8.1. The drawing itself is customer data and never in the repo: the text below is
synthetic, written in the drawing's format."""
from __future__ import annotations

import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "tools"))
import layout_tables as T  # noqa: E402

TABLE = """INVERSOR 1 (GROWATT MAX 124 KW) INVERSOR 2 (GROWATT MAX 124 KW)
Numero de Módulos FV: 37 Numero de Módulos FV: 36
Cadena 1: 18 paneles String 1 - MPPT 1 Cadena 3: 18 paneles String 1 - MPPT 1
Cadena 2: 19 paneles String 1 - MPPT 2 Cadena 4: 18 paneles String 2 - MPPT 1
INVERSOR 3 (GROWATT MAC 60 KW)
Numero de Modulos FV: 36
Cadena 5: 18 paneles String 3 - MPPT 3
Cadena 6:18 paneles String 4 - MPPT 3
"""


def test_rows_are_read_in_any_column_order():
    c = T.cadenas(TABLE)
    assert c == {1: (18, 1, 1), 2: (19, 1, 2), 3: (18, 1, 1), 4: (18, 2, 1), 5: (18, 3, 3), 6: (18, 4, 3)}


def test_module_counts_in_reading_order():
    assert T.module_counts(TABLE) == [37, 36, 36]


def test_split_closes_each_inverter_on_its_printed_count():
    assert T.split_by_inverter(T.cadenas(TABLE), [37, 36, 36]) == {1: [1, 2], 2: [3, 4], 3: [5, 6]}


@pytest.mark.parametrize("counts, msg", [
    ([36, 37, 36], "add up to 37 panels, the table says 36"),      # a misread count never shifts silently
    ([37, 36], "belong to no inverter table"),
    ([37, 36, 36, 18], "add up to 0 panels"),
])
def test_split_refuses_a_table_that_does_not_add_up(counts, msg):
    with pytest.raises(ValueError, match=msg):
        T.split_by_inverter(T.cadenas(TABLE), counts)


def test_gaps_and_duplicates_are_errors():
    with pytest.raises(ValueError, match="missing"):
        T.split_by_inverter({1: (18, 1, 1), 3: (18, 2, 1)}, [36])
    with pytest.raises(ValueError, match="twice"):
        T.cadenas("Cadena 1: 18 paneles String 1 - MPPT 1 Cadena 1: 18 paneles String 2 - MPPT 1")


def test_growatt_input_numbering():
    # Growatt MAX: currentString n = 2 (MPPT - 1) + String; checked against live Taigene data 21 Nov 2026
    # (MPPT 1 current = input 1 + input 2, MPPT 8 = input 15)
    assert [T.growatt_input(m, s) for m, s in ((1, 1), (1, 2), (2, 1), (8, 1), (8, 2))] == [1, 2, 3, 15, 16]
    for bad in ((0, 1), (1, 3), (1, 0)):
        with pytest.raises(ValueError):
            T.growatt_input(*bad)


def test_line_drawn_modules_become_cells():
    # one column of 3 modules (14 x 7 pt) drawn as 4 loose lines, one line doubled; a stray far line ignored
    lines = [(10.0, 24.0, y, "INV5") for y in (100.0, 107.1, 107.2, 114.2, 121.3, 160.0)]
    cells = T.line_cells(lines)
    assert [(round(a, 1), round(b, 1)) for _x0, a, _x1, b, _i in cells] == [(100.0, 107.1), (107.1, 114.2), (114.2, 121.3)]
    assert {c[4] for c in cells} == {"INV5"} and all(c[2] - c[0] == 14 for c in cells)
    # two inverters in the same x span stay apart
    assert len(T.line_cells([(10.0, 24.0, 0.0, "A"), (10.0, 24.0, 7.0, "B")])) == 0


def test_builder_uses_the_table_reader_and_no_v6_overlay():
    src = (V2 / "tools" / "string_layout_build.py").read_text(encoding="utf-8")
    assert "LT.cadenas(" in src and "LT.split_by_inverter(" in src and "LT.growatt_input(" in src
    assert "gto1_v6_shapes" not in src and "GTO1_drawing_v6" not in src      # V8.1 colours every inverter
    assert chr(0x2014) not in src and chr(0x2013) not in src
