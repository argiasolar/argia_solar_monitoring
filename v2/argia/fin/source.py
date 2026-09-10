"""v250 — "where does this number come from, and where do I change it?"

Every row the portal shows was read out of one file, one sheet, one row.
The ingest stores those three things (``src_sheet``, ``src_row``,
``src_gid``) next to the data; this module turns them into a link a
person can click and a sentence a person can act on (Tomasz, 2026-09-10:
"if an invoice is 741 days past due I want to click and land on the very
document the module read that from").

How deep the link goes depends on the file:

* a **Google Sheet** (the ARGIA PROJECT workbooks) → straight to the
  cell: ``/edit#gid=<tab>&range=A<row>``;
* an **.xlsx in Drive** (the tracker, the projects overview, the
  accountants' prints) → the file opens, and the label says which sheet
  and row to look at — Drive has no cell anchor for a binary file. The
  day one of those becomes a Google Sheet the ingest records its tab ids
  and the same link deepens on its own, no code change;
* **no Drive id yet** (the file was read from a local mirror, or the
  service account cannot see the folder) → a Drive search for the file
  name, which is still one click from the file.

Pure: everything here takes plain values and returns strings.
"""
from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import quote

SHEET_MIME = "application/vnd.google-apps.spreadsheet"

# who owns the file — whether editing it is a sensible answer at all
OWNER = {
    "tracker": ("the office manager", "la office manager"),
    "overview": ("the portfolio owner", "quien lleva el overview"),
    "pmo_sheet": ("the project manager", "el project manager"),
    "polizas": ("the accountants", "contabilidad"),
    "auxiliares": ("the accountants", "contabilidad"),
    "acctbook": ("the accountants", "contabilidad"),
}
# a file the portal reads but nobody at ARGIA should edit to fix a number
READ_ONLY = ("polizas", "auxiliares", "acctbook")


def is_sheet(mime: str) -> bool:
    return (mime or "").strip() == SHEET_MIME


def url(drive_id: str = "", mime: str = "", name: str = "", gid: str = "", row: int = 0) -> str:
    """The best link the stored pointer allows (see the module docstring)."""
    drive_id = (drive_id or "").strip()
    if not drive_id:
        return f"https://drive.google.com/drive/search?q={quote(name or '')}" if name else ""
    if is_sheet(mime):
        anchor = ""
        if gid:
            anchor = f"#gid={quote(str(gid))}" + (f"&range=A{int(row)}" if row else "")
        return f"https://docs.google.com/spreadsheets/d/{quote(drive_id)}/edit{anchor}"
    return f"https://drive.google.com/file/d/{quote(drive_id)}/view"


def exact(mime: str = "", gid: str = "", row: int = 0) -> bool:
    """True when the link lands on the cell itself, not just the file."""
    return bool(is_sheet(mime) and gid and row)


def where(name: str, sheet: str = "", row: int = 0, column: str = "", value: str = "") -> Tuple[str, str]:
    """The sentence under the link: file, sheet, row, and — when the caller
    knows it — the column and value the portal read. (EN, ES)."""
    en = [name or "the source file"]
    es = [name or "el archivo de origen"]
    if sheet:
        en.append(f"sheet “{sheet}”")
        es.append(f"hoja «{sheet}»")
    if row:
        en.append(f"row {int(row)}")
        es.append(f"fila {int(row)}")
    if column:
        en.append(f"column “{column}”" + (f" = {value}" if value else ""))
        es.append(f"columna «{column}»" + (f" = {value}" if value else ""))
    return " · ".join(en), " · ".join(es)


def owner(kind: str) -> Tuple[str, str]:
    return OWNER.get(kind or "", ("the file's owner", "quien lleva el archivo"))


def editable(kind: str) -> bool:
    """The books are the accountants' output: a wrong figure there is fixed
    in CONTPAQi and arrives with the next close, never by editing the print."""
    return (kind or "") not in READ_ONLY


def pointer(row: Optional[dict], column: str = "", value: str = "") -> Optional[dict]:
    """One place that assembles everything a page needs to show a pointer.

    ``row`` carries what the queries select: ``src_name`` (file), ``src_kind``,
    ``src_drive_id``, ``src_mime``, ``src_sheet``, ``src_row``, ``src_gid``.
    Returns None when there is no row, or the row has no source at all.
    """
    if not row:
        return None
    name = (row.get("src_name") or "").strip()
    kind = (row.get("src_kind") or "").strip()
    if not name and not row.get("src_drive_id"):
        return None
    try:
        rn = int(row.get("src_row") or 0)
    except (TypeError, ValueError):
        rn = 0
    gid = str(row.get("src_gid") or "")
    mime = row.get("src_mime") or ""
    en, es = where(name, row.get("src_sheet") or "", rn, column, value)
    return {"url": url(row.get("src_drive_id") or "", mime, name, gid, rn), "en": en, "es": es,
            "exact": exact(mime, gid, rn), "kind": kind, "editable": editable(kind), "name": name}
