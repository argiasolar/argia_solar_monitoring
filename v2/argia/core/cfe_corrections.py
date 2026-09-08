"""Cell-level corrections to ``cfe_tariff`` (v239).

``data/cfe_corrections.json`` is the register: which cells are wrong in
the seed, the correct value, who confirmed it. The table is corrected
AT SOURCE so every consumer reads the same number; the register is what
drift_check compares the table against every morning, and what the
apply script writes. Pure functions here; ``scripts/cfe_corrections.py``
and drift_check do the I/O.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

REGISTER_PATH = Path(__file__).resolve().parents[2] / "data" / "cfe_corrections.json"
_YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TOL = 1e-6

# (tariff_code, region, charge_type, "YYYY-MM") -> value
Cells = Dict[Tuple[str, str, str, str], float]


def load(path: Optional[Path] = None) -> dict:
    with open(path or REGISTER_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def validate(reg: dict) -> List[str]:
    out: List[str] = []
    ids = set()
    for c in reg.get("corrections") or []:
        cid = c.get("id", "?")
        if cid in ids:
            out.append(f"{cid}: duplicate id")
        ids.add(cid)
        for k in ("tariff_code", "region", "charge_type", "months", "value", "status", "confirmed_by", "basis"):
            if k not in c:
                out.append(f"{cid}: missing {k}")
        if c.get("status") not in ("confirmed", "proposed"):
            out.append(f"{cid}: status must be confirmed or proposed")
        for m in c.get("months") or []:
            if not _YM.match(str(m)):
                out.append(f"{cid}: bad month {m!r}")
        if not isinstance(c.get("value"), (int, float)) or c.get("value", -1) < 0:
            out.append(f"{cid}: value must be a non-negative number")
    return out


def wanted(reg: dict, include_proposed: bool = False) -> Cells:
    """The cells the register asserts, {key: value}."""
    out: Cells = {}
    for c in reg.get("corrections") or []:
        if c.get("status") != "confirmed" and not include_proposed:
            continue
        for m in c.get("months") or []:
            out[(c["tariff_code"], c["region"], c["charge_type"], m)] = float(c["value"])
    return out


def compare(reg: dict, rows: Iterable[Tuple[str, str, str, str, str]]) -> List[str]:
    """Table vs register: a line per confirmed cell that is missing or
    differs. ``rows`` = (code, region, charge, 'YYYY-MM', value)."""
    table = {(r[0], r[1], r[2], r[3]): float(r[4]) for r in rows if len(r) >= 5 and r[4] not in ("", None)}
    out: List[str] = []
    for key, want in sorted(wanted(reg).items()):
        have = table.get(key)
        if have is None:
            out.append(f"{'/'.join(key)}: not in cfe_tariff (want {want:g})")
        elif abs(have - want) > TOL:
            out.append(f"{'/'.join(key)}: {have:g} in cfe_tariff, register says {want:g}")
    return out


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def apply_sql(reg: dict, rows: Iterable[Tuple[str, str, str, str, str]]) -> List[str]:
    """One UPDATE per confirmed cell whose table value differs — never
    touches a cell that already matches, never inserts (a cell the seed
    does not have is reported by compare, not invented here)."""
    table = {(r[0], r[1], r[2], r[3]): float(r[4]) for r in rows if len(r) >= 5 and r[4] not in ("", None)}
    out: List[str] = []
    for (code, region, charge, ym), want in sorted(wanted(reg).items()):
        have = table.get((code, region, charge, ym))
        if have is None or abs(have - want) <= TOL:
            continue
        out.append(f"UPDATE cfe_tariff SET value_mxn = {want!r} WHERE tariff_code = {_txt(code)} AND region = {_txt(region)}"
                   f" AND charge_type = {_txt(charge)} AND month = DATE '{ym}-01' AND value_mxn IS DISTINCT FROM {want!r};")
    return out


def select_sql(reg: dict, include_proposed: bool = True) -> str:
    """The rows compare/apply need — only the register's cells."""
    keys = wanted(reg, include_proposed=include_proposed)
    codes = sorted({k[0] for k in keys})
    if not codes:
        return "SELECT tariff_code, region, charge_type, to_char(month,'YYYY-MM'), value_mxn::text FROM cfe_tariff WHERE false;"
    return ("SELECT tariff_code, region, charge_type, to_char(month,'YYYY-MM'), value_mxn::text FROM cfe_tariff"
            " WHERE tariff_code IN (" + ", ".join(_txt(c) for c in codes) + ")"
            " AND to_char(month,'YYYY') IN (" + ", ".join(_txt(y) for y in sorted({k[3][:4] for k in keys})) + ");")
