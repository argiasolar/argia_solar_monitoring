"""The inverter registry (v229): which serial number is which inverter.

``data/inverter_registry.json`` pins, per plant, every inverter's serial
number and the name the manufacturer portal gives it. The monitoring
``inverter`` table must agree with it — otherwise "Inverter 1" in a
mail points a technician at the wrong machine (2026-09-07: Plastic
Omnium's 1/3/4 and Taigene's 5/6 were swapped, Hirschmann's and Tetra
Pak's 5th inverter were not in the table at all).

Everything here is pure; the script and drift_check do the I/O.

    reg   = load()                          # the JSON
    problems = validate(reg)                # the file itself is sane
    findings = compare(reg, table_rows)     # table vs registry
    sql   = apply_sql(reg, table_rows)      # idempotent fixes
    findings = vendor_diff(reg, "GTO2", [(sn, name), ...])   # live list vs registry
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "inverter_registry.json"

# (plant_key, inverter_sn, inverter_label, active)
TableRow = Tuple[str, str, str, bool]

_NUM = re.compile(r"(?:^|[\s_\-#.])(\d+)\s*$")     # a number at the end, not the tail of a serial
_LABEL = re.compile(r"^Inverter \d+$")


def load(path: Optional[Path] = None) -> dict:
    with open(path or REGISTRY_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def plants(reg: dict) -> Dict[str, Dict[str, dict]]:
    return reg.get("plants") or {}


def number_of(label: str) -> Optional[int]:
    """'Inversor 3' / 'Inverter 3' / 'Budenheim 3' / 'Inversor_3' -> 3;
    a bare serial ('JNMDEXH011') -> None."""
    m = _NUM.search((label or "").strip())
    return int(m.group(1)) if m else None


def is_active(entry: dict) -> bool:
    return bool(entry.get("active", True))


def validate(reg: dict) -> List[str]:
    """The file's own rules — one line per violation, empty when sane."""
    out: List[str] = []
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(reg.get("verified", ""))):
        out.append("missing or malformed 'verified' date")
    for pk, invs in plants(reg).items():
        if not invs:
            out.append(f"{pk}: no inverters")
            continue
        labels = [e.get("label", "") for e in invs.values()]
        for sn, e in invs.items():
            lab = e.get("label", "")
            if sn != sn.strip().upper() or " " in sn:
                out.append(f"{pk} {sn!r}: serial must be upper-case, no spaces")
            if not _LABEL.match(lab):
                out.append(f"{pk} {sn}: label {lab!r} must be 'Inverter N'")
            vn, on = number_of(e.get("vendor_label", "")), number_of(lab)
            if e.get("vendor_label") and vn is not None and vn != on:
                out.append(f"{pk} {sn}: our number {on} differs from the vendor's {vn} ({e.get('vendor_label')!r})")
            if not is_active(e) and "note" not in e:
                out.append(f"{pk} {sn}: inactive without a note saying why")
        active_labels = [e.get("label") for e in invs.values() if is_active(e)]
        dup = sorted({x for x in active_labels if active_labels.count(x) > 1})
        if dup:
            out.append(f"{pk}: duplicate active labels {dup}")
        if labels != sorted(labels, key=lambda x: number_of(x) or 0):
            out.append(f"{pk}: list the inverters in number order")
    return out


def compare(reg: dict, rows: Iterable[TableRow]) -> List[str]:
    """Monitoring table vs registry. One line per difference; the
    active flag is reported, never fixed by apply_sql (that is an
    operational decision)."""
    table = {(pk, sn.strip()): (lab or "", bool(act)) for pk, sn, lab, act in rows}
    out: List[str] = []
    for pk, invs in plants(reg).items():
        for sn, e in invs.items():
            row = table.pop((pk, sn), None)
            want = e.get("label", "")
            if row is None:
                out.append(f"{pk} {sn}: not in the monitoring table (vendor calls it {e.get('vendor_label') or want!r})")
                continue
            lab, act = row
            if lab != want:
                out.append(f"{pk} {sn}: table says {lab!r}, the vendor's name is {e.get('vendor_label') or want!r} -> {want!r}")
            if act != is_active(e):
                out.append(f"{pk} {sn}: table active={act}, registry active={is_active(e)}")
    for (pk, sn), (lab, _act) in sorted(table.items()):
        if pk in plants(reg):
            out.append(f"{pk} {sn}: in the monitoring table ({lab!r}) but not in the registry")
    return out


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def apply_sql(reg: dict, rows: Iterable[TableRow]) -> List[str]:
    """Idempotent statements that make the table's labels match and add
    missing inverters (an insert needs ``rated_kw`` in the registry).
    Existing rows keep their active flag and rating; running it twice
    changes nothing."""
    table = {(pk, sn.strip()): (lab or "", bool(act)) for pk, sn, lab, act in rows}
    out: List[str] = []
    for pk, invs in plants(reg).items():
        for sn, e in invs.items():
            want = e.get("label", "")
            row = table.get((pk, sn))
            if row is None:
                kw = e.get("rated_kw")
                if kw is None:
                    out.append(f"-- {pk} {sn}: cannot insert, registry has no rated_kw")
                    continue
                out.append(f"INSERT INTO inverter (plant_key, inverter_sn, inverter_label, rated_kw, active)"
                           f" VALUES ({_txt(pk)}, {_txt(sn)}, {_txt(want)}, {float(kw)}, {'true' if is_active(e) else 'false'})"
                           f" ON CONFLICT (plant_key, inverter_sn) DO UPDATE SET inverter_label = EXCLUDED.inverter_label;")
            elif row[0] != want:
                out.append(f"UPDATE inverter SET inverter_label = {_txt(want)} WHERE plant_key = {_txt(pk)}"
                           f" AND inverter_sn = {_txt(sn)} AND inverter_label IS DISTINCT FROM {_txt(want)};")
    return out


def vendor_diff(reg: dict, plant_key: str, vendor: Sequence[Tuple[str, str]]) -> List[str]:
    """A live vendor device list (serial, name) vs the registry: a
    serial the vendor has that we do not (a replacement — Tetra Pak's
    Inverter 4 changed serial once), one we have that the vendor no
    longer lists, and a number that moved."""
    invs = plants(reg).get(plant_key) or {}
    seen = set()
    out: List[str] = []
    for sn, name in vendor:
        sn = (sn or "").strip().upper()
        if not sn:
            continue
        seen.add(sn)
        e = invs.get(sn)
        if e is None:
            out.append(f"{plant_key} {sn}: the vendor lists it as {name!r} but the registry does not know it (replacement?)")
            continue
        vn, on = number_of(name), number_of(e.get("label", ""))
        if vn is not None and vn != on:
            out.append(f"{plant_key} {sn}: the vendor now calls it {name!r}, the registry says {e.get('label')!r}")
    for sn in invs:
        if sn not in seen:
            out.append(f"{plant_key} {sn}: in the registry but the vendor no longer lists it")
    return out


def as_table(reg: dict) -> List[TableRow]:
    """The registry in table shape (for tests and dry runs)."""
    return [(pk, sn, e.get("label", ""), is_active(e)) for pk, invs in plants(reg).items() for sn, e in invs.items()]
