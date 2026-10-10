"""A minimal .xlsx writer (v320) - stdlib only.

The Prologis platform runs on the server's system Python, which has no
openpyxl; the proposal promises reports "also as a spreadsheet". This
writes plain workbooks: one or more sheets of rows, the first row bold
and frozen, numbers as numbers, text as inline strings, column widths
from the content. Nothing else (no formulas, no merged cells).

    data = workbook([("Summary", [["Part", "Qty"], ["Inverter", 11]])])

Pure: returns bytes. Tests read the result back with openpyxl.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import List, Sequence, Tuple
from xml.sax.saxutils import escape

_BAD = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]")
_SHEET_BAD = re.compile(r"[\[\]:*?/\\]")


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref: str, v, bold: bool) -> str:
    st = ' s="1"' if bold else ""
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        v = "yes" if v else "no"
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):      # NaN / inf are not valid cell values
            return ""
        return f'<c r="{ref}"{st}><v>{repr(float(v)) if isinstance(v, float) else v}</v></c>'
    t = escape(_BAD.sub("", str(v)))
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{t}</t></is></c>'


def _sheet(rows: Sequence[Sequence]) -> str:
    widths: List[int] = []
    out = []
    for ri, row in enumerate(rows):
        cells = []
        for ci, v in enumerate(row):
            cells.append(_cell(f"{_col(ci)}{ri + 1}", v, ri == 0))
            ln = len(str(v)) if v is not None else 0
            if ci >= len(widths):
                widths.append(ln)
            else:
                widths[ci] = max(widths[ci], ln)
        out.append(f'<row r="{ri + 1}">{"".join(cells)}</row>')
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(60, max(8, w + 2))}" customWidth="1"/>' for i, w in enumerate(widths))
    pane = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
            '</sheetView></sheetViews>') if rows else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{pane}{"<cols>" + cols + "</cols>" if cols else ""}<sheetData>{"".join(out)}</sheetData></worksheet>')


def workbook(sheets: Sequence[Tuple[str, Sequence[Sequence]]]) -> bytes:
    if not sheets:
        raise ValueError("at least one sheet")
    names: List[str] = []
    for name, _rows in sheets:
        n = _SHEET_BAD.sub("_", name)[:31] or "Sheet"
        base, k = n, 2
        while n.lower() in (x.lower() for x in names):
            n = f"{base[:28]}_{k}"
            k += 1
        names.append(n)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" '
                             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(len(sheets)))
                   + '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                   + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                             f'Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(sheets)))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                   '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                   '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
                   '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                   '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
                   '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   '</styleSheet>')
        for i, (_n, rows) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", _sheet(rows))
    return buf.getvalue()
