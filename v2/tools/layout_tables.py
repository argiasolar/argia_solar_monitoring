"""Pure helpers for the string tables printed on the installation drawings (v388).

Used by tools/string_layout_build.py; kept apart so the parsing is unit-tested without
the drawing PDFs (customer data, never in the repo).

Taigene rev. V8.1 (20.11.2026) prints one row per string ("cadena"):
    Cadena 2: 19 paneles  String 1 - MPPT 2
and one header per inverter table with its module count:
    INVERSOR 1 (GROWATT MAX 124 KW) ... Numero de Módulos FV: 271
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

CADENA = re.compile(r"Cadena\s*(\d+)\s*:\s*(\d+)\s*paneles\s*String\s*(\d+)\s*-\s*MPPT\s*(\d+)", re.I)
MODULE_COUNT = re.compile(r"N[uú]mero de M[oó]dulos FV\s*:\s*(\d+)", re.I)


def cadenas(text: str) -> Dict[int, Tuple[int, int, int]]:
    """{cadena: (panels, string, mppt)} from the drawing's table text. A number twice is an error."""
    out: Dict[int, Tuple[int, int, int]] = {}
    for n, panels, s, m in CADENA.findall(text):
        n = int(n)
        if n in out:
            raise ValueError(f"cadena {n} twice in the drawing table")
        out[n] = (int(panels), int(s), int(m))
    return out


def module_counts(text: str) -> List[int]:
    """Module count of each inverter table, in reading order (INVERSOR 1, 2, ...)."""
    return [int(x) for x in MODULE_COUNT.findall(text)]


def split_by_inverter(cad: Dict[int, Tuple[int, int, int]], counts: List[int]) -> Dict[int, List[int]]:
    """{inverter number: [cadenas]}: consecutive cadenas, each inverter closed when its panels add up to
    the count its table header prints. Raises when the numbers do not land exactly (a misread table)."""
    nums = sorted(cad)
    if nums != list(range(1, len(nums) + 1)):
        raise ValueError(f"cadenas not 1..{len(nums)}: missing {sorted(set(range(1, max(nums) + 1)) - set(nums))}")
    out: Dict[int, List[int]] = {}
    i = 0
    for inv, want in enumerate(counts, start=1):
        got, mine = 0, []
        while i < len(nums) and got < want:
            got += cad[nums[i]][0]
            mine.append(nums[i])
            i += 1
        if got != want:
            raise ValueError(f"INVERSOR {inv}: cadenas {mine} add up to {got} panels, the table says {want}")
        out[inv] = mine
    if i != len(nums):
        raise ValueError(f"cadenas {nums[i:]} belong to no inverter table")
    return out


def growatt_input(mppt: int, string: int, per_mppt: int = 2) -> int:
    """Growatt MAX string input number (currentString n): n = per_mppt * (MPPT - 1) + String."""
    if mppt < 1 or not 1 <= string <= per_mppt:
        raise ValueError(f"no Growatt input for String {string} - MPPT {mppt}")
    return per_mppt * (mppt - 1) + string


def line_cells(hlines):
    """Module outlines drawn as loose lines rather than boxes (Taigene V8.1, inverter 5 single-row strips).

    hlines: [(x0, x1, y, inv)] horizontal lines of one colour each. Lines with the same x span are one
    column of modules; two neighbouring lines at a module's height apart (0.3 - 0.8 of its width) close
    a cell. Duplicates (< 0.5 pt apart) count once. -> [(x0, y0, x1, y1, inv)]"""
    cols: Dict[Tuple[int, int, str], List[float]] = {}
    for x0, x1, y, inv in hlines:
        cols.setdefault((round(x0), round(x1), inv), []).append(y)
    out = []
    for (x0, x1, inv), ys in sorted(cols.items()):
        w = x1 - x0
        uniq: List[float] = []
        for y in sorted(ys):
            if not uniq or y - uniq[-1] >= 0.5:
                uniq.append(y)
        for a, b in zip(uniq, uniq[1:]):
            if 0.3 * w <= b - a <= 0.8 * w:
                out.append((float(x0), a, float(x1), b, inv))
    return out
