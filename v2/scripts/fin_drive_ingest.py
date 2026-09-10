#!/usr/bin/env python3
"""v245 — read the books and the project files from Google Drive (or a
local mirror) into PostgreSQL. Idempotent: every file is keyed by its
content hash (fin_source_file); an unchanged file is skipped, a new
monthly print replaces what the previous one said.

Sources (the folders Tomasz named on 2026-09-09 as the source of truth):
  reporting   ACCOUNTING/Accounting Reporting/2026   -> MMYY Polizas / Auxiliares (CONTPAQi prints),
                                                        Argia_Accounting_Data_MM_YY_Vn (the accountants' workbook)
  overview    REALIZATIONS/RUNNING PROJECTS OVERVIEW -> Argia_Projects_Overview_MX.xlsx (sheet Data)
  pm          PROJECT MANAGEMENT                     -> one ARGIA PROJECT Google Sheet per 'Project ARGnnnn - …' folder
  tracker     ACCOUNTING/Argia Mexico Payables and receivables 2026_V2.xlsx (optional, a file id)

    fin_drive_ingest.py --from-dir /path/to/mirror            # laptop: the Google Drive mount; sandbox: staged copies
    fin_drive_ingest.py --drive                               # server: the service account (folders shared read-only)
    ... --apply                                               # write; without it: parse, check, report, write nothing
    ... --force                                               # re-import files already known by hash

Env (server): ARGIA_FIN_DRIVE_REPORTING / _OVERVIEW / _PM (folder ids), ARGIA_FIN_DRIVE_TRACKER (file id),
GOOGLE_CREDENTIALS (json) or GOOGLE_CREDENTIALS_FILE (default /root/.googlecredentials.json), ARGIA_FIN_ENTITY (ARGIA-MX).
Exit 0 = done, 2 = a source could not be read, 3 = the books failed their own consistency checks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # runs from anywhere, PYTHONPATH or not

from argia.fin import acctbook as AB, books as B, contpaq as CP, pmo_sheet as PS, portfolio as PF, source as SRC   # noqa: E402
from argia.fin.ingest import _lit                                                                  # noqa: E402

ENTITY = os.environ.get("ARGIA_FIN_ENTITY", "ARGIA-MX")
POLIZAS_RE = re.compile(r"^(\d{4}|\d{6}) Polizas .*\.xlsx$", re.I)          # 0726 …, 122023 …, 2025 … (v249: the yearly closes)
AUX_RE = re.compile(r"^(\d{4}|\d{6}) Auxiliares .*\.xlsx$", re.I)
BOOK_RE = re.compile(r"^Argia_Accounting_Data_(\d{2})_(\d{2,4})(?:_V(\d+))?.*\.xls[xm]$", re.I)
SKIP_NAMES = re.compile(r"before", re.I)          # 'Argia_Accounting_Data_12_25_V3 – Before (Accruals Model)': the pre-restatement copy
OVERVIEW_RE = re.compile(r"^Argia_Projects_Overview_MX\.xlsx$", re.I)
TRACKER_RE = re.compile(r"Payables and receivables.*\.xlsx$", re.I)
PROJECT_FOLDER_RE = re.compile(r"^Project ARG(\d{4})\b", re.I)
PMO_TITLE_RE = re.compile(r"ARGIA[ _]PROJECT", re.I)
SKIP_DIRS = {"old", "cambios", "v1", "v2", "v3", "versiones", "archivo", "testing versions", "pmo sandbox"}
PMO_MIN_CODE = int(os.environ.get("ARGIA_FIN_PMO_MIN_CODE", "1000"))   # ARG0000-ARG0012 are the dummy PLD set


# ------------------------------------------------------------------ files
class SourceFile:
    def __init__(self, kind: str, name: str, data: bytes = b"", drive_id: str = "", modified: Optional[dt.datetime] = None,
                 tabs: Optional[Dict[str, List[List]]] = None, folder: str = "", mime: str = "", gids: Optional[Dict[str, str]] = None):
        self.kind, self.name, self.data, self.drive_id, self.modified, self.tabs, self.folder = kind, name, data, drive_id, modified, tabs, folder
        self.mime = mime or _mime_of(name)          # v250: a Google Sheet can be linked cell-deep, an .xlsx only file-deep
        self.gids = gids or {}                      # tab title -> gid, for those cell-deep links

    @property
    def sha(self) -> str:
        if self.tabs is not None:
            return B.sha_of(json.dumps(self.tabs, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        return B.sha_of(self.data)


def _mime_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
            ".xls": "application/vnd.ms-excel",
            ".json": SRC.SHEET_MIME}.get(ext, "")


def _period_key(name: str) -> str:
    m = POLIZAS_RE.match(name) or AUX_RE.match(name)
    if m:
        return B.month_prefix_period(name)
    m = BOOK_RE.match(name)
    if m:
        return f"{B.acctbook_period(name)}-{int(m.group(3) or 0):02d}"
    return ""


class LocalSource:
    """A directory tree that mirrors the Drive folders (the laptop's G: mount,
    or copies staged into a folder). Subfolders named like old versions are
    skipped; the newest period wins."""

    def __init__(self, root: Path):
        self.root = root

    def _walk(self):
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            if any(part.lower() in SKIP_DIRS for part in p.relative_to(self.root).parts[:-1]):
                continue
            yield p

    def latest(self, rx: re.Pattern, year: Optional[int] = None) -> Optional[SourceFile]:
        """The newest period (v249: inside ``year`` when given); same period →
        the last modified file wins ('1224 Polizas Argia1.xlsx' re-export)."""
        best = None
        for p in self._walk():
            if rx.match(p.name) and not SKIP_NAMES.search(p.name):
                k = _period_key(p.name)
                if year and not k.startswith(str(year)):
                    continue
                key = (k, p.stat().st_mtime)
                if best is None or key > best[0]:
                    best = (key, p)
        if not best:
            return None
        p = best[1]
        return SourceFile("", p.name, p.read_bytes(), modified=dt.datetime.fromtimestamp(p.stat().st_mtime), folder=str(p.parent))

    def find(self, rx: re.Pattern) -> Optional[SourceFile]:
        for p in self._walk():
            if rx.search(p.name):
                return SourceFile("", p.name, p.read_bytes(), modified=dt.datetime.fromtimestamp(p.stat().st_mtime), folder=str(p.parent))
        return None

    def pmo_sheets(self) -> List[SourceFile]:
        """A local mirror holds .gsheet stubs only — project workbooks can be
        supplied as JSON exports ({tab: rows}) named like the sheet."""
        out = []
        for p in self._walk():
            if p.suffix.lower() == ".json" and PMO_TITLE_RE.search(p.stem) and PROJECT_FOLDER_RE.match(p.parent.name or ""):
                tabs = json.loads(p.read_text(encoding="utf-8"))
                out.append(SourceFile("pmo_sheet", p.stem, tabs=tabs, modified=dt.datetime.fromtimestamp(p.stat().st_mtime), folder=p.parent.name))
        return out


class DriveSource:
    """The service account reads the shared folders (Drive v3 + Sheets v4)."""

    def __init__(self, reporting: str, overview: str, pm: str, tracker: str = "", history: Sequence[str] = ()):
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        raw = os.environ.get("GOOGLE_CREDENTIALS", "")
        if not raw:
            raw = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", "/root/.googlecredentials.json")).read_text(encoding="utf-8")
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=["https://www.googleapis.com/auth/drive.readonly",
                                                                             "https://www.googleapis.com/auth/spreadsheets.readonly"])
        self.drive = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.reporting, self.overview, self.pm, self.tracker = reporting, overview, pm, tracker
        self.history = [h for h in history if h]

    def _list(self, folder_id: str) -> List[dict]:
        out, token = [], None
        while True:
            resp = self.drive.files().list(q=f"'{folder_id}' in parents and trashed = false",
                                           fields="nextPageToken, files(id, name, mimeType, modifiedTime)", pageSize=200,
                                           pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def _walk(self, folder_id: str, depth: int = 0, path: str = ""):
        for f in self._list(folder_id):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                if f["name"].strip().lower() in SKIP_DIRS or depth > 3:
                    continue
                yield from self._walk(f["id"], depth + 1, path + "/" + f["name"])
            else:
                f["folder"] = path
                yield f

    def _download(self, f: dict, kind: str) -> SourceFile:
        data = self.drive.files().get_media(fileId=f["id"], supportsAllDrives=True).execute()
        return SourceFile(kind, f["name"], data, f["id"], _ts(f.get("modifiedTime")), folder=f.get("folder", ""), mime=f.get("mimeType", ""))

    def latest(self, rx: re.Pattern, year: Optional[int] = None) -> Optional[SourceFile]:
        best = None
        for folder in ([self.reporting] if not year else self.history):
            for f in self._walk(folder):
                if rx.match(f["name"]) and not SKIP_NAMES.search(f["name"]):
                    k = _period_key(f["name"])
                    if year and not k.startswith(str(year)):
                        continue
                    key = (k, f.get("modifiedTime") or "")
                    if best is None or key > best[0]:
                        best = (key, f)
        return self._download(best[1], "") if best else None

    def find(self, rx: re.Pattern) -> Optional[SourceFile]:
        if rx is TRACKER_RE:
            if not self.tracker:
                return None
            f = self.drive.files().get(fileId=self.tracker, fields="id, name, mimeType, modifiedTime", supportsAllDrives=True).execute()
            return self._download(f, "tracker")
        for f in self._walk(self.overview):
            if rx.search(f["name"]) and not f.get("folder"):
                return self._download(f, "overview")
        return None

    def pmo_sheets(self) -> List[SourceFile]:
        out = []
        for folder in self._list(self.pm):
            m = PROJECT_FOLDER_RE.match(folder["name"])
            if folder["mimeType"] != "application/vnd.google-apps.folder" or not m or int(m.group(1)) < PMO_MIN_CODE:
                continue
            for f in self._list(folder["id"]):
                if f["mimeType"] == "application/vnd.google-apps.spreadsheet" and PMO_TITLE_RE.search(f["name"]):
                    meta = self.sheets.spreadsheets().get(spreadsheetId=f["id"], fields="sheets.properties.title,sheets.properties.sheetId").execute()
                    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
                    gids = {s["properties"]["title"]: str(s["properties"].get("sheetId", "")) for s in meta.get("sheets", [])}
                    got = self.sheets.spreadsheets().values().batchGet(spreadsheetId=f["id"], ranges=[f"'{t}'" for t in titles],
                                                                       valueRenderOption="FORMATTED_VALUE").execute()
                    tabs = {t: vr.get("values", []) for t, vr in zip(titles, got.get("valueRanges", []))}
                    out.append(SourceFile("pmo_sheet", f["name"], drive_id=f["id"], modified=_ts(f.get("modifiedTime")), tabs=tabs,
                                          folder=folder["name"], mime=SRC.SHEET_MIME, gids=gids))
        return out


def _ts(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- parsing
def _xlsx_rows(data: bytes, sheet: Optional[str] = None, max_col: Optional[int] = None, read_only: bool = True) -> Dict[str, List[tuple]]:
    import openpyxl
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=read_only, data_only=True)
    names = [sheet] if sheet else wb.sheetnames
    return {n: list(wb[n].iter_rows(values_only=True, max_col=max_col)) for n in names if n in wb.sheetnames}


def _print_rows(data: bytes) -> List[tuple]:
    """A CONTPAQi print: one sheet whose dimension record is broken (read-only
    mode sees 1x1 unless the dimensions are reset). v249: streamed read-only
    with ``reset_dimensions()`` — same rows as a full load, a third of the
    memory (the 2023 auxiliares print is 40 MB; the full load killed the
    ingest on the 4 GB server)."""
    import openpyxl
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    ws.reset_dimensions()
    rows = [tuple(r) + (None,) * (8 - len(r)) for r in ws.iter_rows(values_only=True, max_col=8)]
    while rows and all(c is None for c in rows[-1]):
        rows.pop()
    wb.close()
    return rows


def _rows(sql: str):
    from argia.store.pgq import psql_rows
    return psql_rows("SET statement_timeout='20s'; " + sql)


def _exec(sql: str):
    from argia.store.pgq import psql_exec
    psql_exec("SET statement_timeout='300s';\n" + sql)


def _overview_codes() -> set:
    """v248: codes present in the projects overview are projects for the
    cost-centre catalogue whatever their name looks like; empty without a database."""
    try:
        return {int(r[0]) for r in _rows(f"SELECT DISTINCT code FROM portfolio_project WHERE entity_id = {_lit(ENTITY)};") if r and r[0]}
    except Exception:                          # noqa: BLE001 — dry run on the laptop
        return set()


class Run:
    def __init__(self, apply: bool, force: bool):
        self.apply, self.force = apply, force
        self.known = set()
        if apply or not force:
            try:
                self.known = {r[0] for r in _rows("SELECT sha256 FROM fin_source_file;")}
            except Exception as e:            # noqa: BLE001 — no database (dry run on the laptop)
                print(f"  (no database reachable: {str(e).strip()[:80]})")
        self.notes: List[str] = []
        self.pending: List[str] = []
        self.findings: List[str] = []
        self.stats: Dict[str, int] = {}

    def skip(self, sf: SourceFile) -> bool:
        if sf.sha in self.known and not self.force:
            print(f"  {sf.kind}: {sf.name} — unchanged (sha {sf.sha[:12]}), skipped")
            if sf.drive_id or sf.mime:           # v250: the content is old news, the Drive pointer may be new
                self.write(B.source_touch_sql(sf.sha, sf.drive_id, sf.mime, sf.modified), "source_touch")
                self.commit()
            return True
        return False

    def write(self, sql: str, what: str):
        """Collect; ``commit()`` runs one file's statements in ONE transaction,
        so a half-imported file never exists (and never gets skipped by hash)."""
        n = sql.count(";\n") + (1 if sql.strip() else 0)
        self.stats[what] = self.stats.get(what, 0) + n
        if sql.strip():
            self.pending.append(sql)

    def commit(self):
        if self.apply and self.pending:
            _exec("BEGIN;\n" + "\n".join(self.pending) + "\nCOMMIT;")
        self.pending = []


def ingest_books(run: Run, pol: Optional[SourceFile], aux: Optional[SourceFile], book: Optional[SourceFile],
                 history: bool = False) -> Tuple[Optional[CP.PolizasPrint], Optional[CP.AuxiliaresPrint], Optional[AB.Workbook]]:
    """v249: ``history`` = a past year's close — balances, journals, report
    lines and margins for that period; the account names, the project list
    and the cost-centre catalogue stay those of the current workbook."""
    P = A = W = None
    if aux:
        aux.kind = 'auxiliares'
    if aux and not run.skip(aux):
        A = CP.parse_auxiliares(_print_rows(aux.data))
        bad = [a for a in A.accounts.values() if not a.running_ok]
        if bad:
            run.findings.append(f"auxiliares: running balance disagrees on {len(bad)} account(s): {', '.join(a.account for a in bad[:5])}")
        period = B.period_of(A.period_to) or B.month_prefix_period(aux.name)
        run.write(B.source_file_sql(B.source_file_row(aux.sha, ENTITY, "auxiliares", aux.name, aux.drive_id, aux.modified, period,
                                                     sum(len(a.movements) for a in A.accounts.values()), mime=aux.mime)), "source_file")
        rows = B.balance_rows(ENTITY, A, aux.sha, period)
        run.write(B.balance_sql(rows), "gl_balance")
        run.commit()
        print(f"  auxiliares: {aux.name} — {len(A.accounts)} accounts, {sum(len(a.movements) for a in A.accounts.values())} movements, period {period}")
    if book:
        book.kind = 'acctbook'
    if book and not run.skip(book):
        sheets = _xlsx_rows(book.data)
        W = AB.read_workbook({k: v for k, v in sheets.items() if k in AB.SHEETS})
        period = W.period.label if W.period.year else B.acctbook_period(book.name)
        run.write(B.source_file_sql(B.source_file_row(book.sha, ENTITY, "acctbook", book.name, book.drive_id, book.modified, period, len(W.mapping), mime=book.mime)), "source_file")
        if not history:
            run.write(B.gl_account_sql(B.gl_account_rows(ENTITY, W.mapping, A)), "gl_account")
            run.write(B.biz_case_sql(B.biz_case_rows(ENTITY, W.projects)), "biz_case")
            run.write(B.cost_center_sql(B.cost_center_rows(ENTITY, W.projects, _overview_codes())), "cost_center")
        else:
            run.write(B.upsert("gl_account", B.gl_account_rows(ENTITY, W.mapping, A), ("entity_id", "account"), update=[]), "gl_account_new")   # only accounts the current books no longer carry
        for sheet, lines in (("pl", W.pl), ("bs", W.bs), ("budget", W.budget)):
            run.write(B.report_sql(B.report_rows(ENTITY, period, sheet, lines, book.sha)), "fin_report_line")
        run.write(B.margin_sql(B.margin_rows(ENTITY, period, W.gm, book.sha)), "project_margin")
        if "Balanza" in sheets and A is not None:
            bal = {b.dashed: b for b in CP.parse_balanza(sheets["Balanza"]) if b.level == 4}
            diffs = [a for a, l in A.accounts.items() if a in bal and abs(bal[a].closing - l.closing) > 0.01]
            if diffs:
                run.findings.append(f"balanza vs auxiliares: {len(diffs)} account(s) differ: {', '.join(diffs[:5])}")
        run.commit()
        print(f"  workbook: {book.name} — period {period}, {len(W.mapping)} accounts mapped, {len(W.projects)} business cases, "
              f"PL {len(W.pl)} / BS {len(W.bs)} / budget {len(W.budget)} lines, {len(W.gm)} project margins")
    if pol:
        pol.kind = 'polizas'
    if pol and not run.skip(pol):
        P = CP.parse_polizas(_print_rows(pol.data))
        posted = B.posted_keys(A) if A is not None else None
        if A is not None:
            for f in CP.cross_check(P, A):
                run.findings.append("pólizas vs auxiliares: " + f)
        period = B.period_of(P.period_to) or B.month_prefix_period(pol.name)
        run.write(B.source_file_sql(B.source_file_row(pol.sha, ENTITY, "polizas", pol.name, pol.drive_id, pol.modified, period, P.lines, mime=pol.mime)), "source_file")
        jr, lr = B.journal_rows(ENTITY, P, pol.sha, posted)
        run.write(B.journal_sql(ENTITY, jr, lr, P.period_from, P.period_to), "gl_journal+line")
        run.commit()
        unposted = sum(1 for r in jr if not r["posted"])
        print(f"  pólizas: {pol.name} — {len(P.journals)} journals, {P.lines} lines, {P.period_from}..{P.period_to}"
              + (f", {unposted} not in the auxiliares (unposted)" if unposted else ""))
    return P, A, W


def ingest_overview(run: Run, sf: Optional[SourceFile]):
    if sf:
        sf.kind = 'overview'
    if not sf or run.skip(sf):
        return None
    rows = PF.read_overview(_xlsx_rows(sf.data, "Data", max_col=40)["Data"], "Data")
    run.write(B.source_file_sql(B.source_file_row(sf.sha, ENTITY, "overview", sf.name, sf.drive_id, sf.modified, "", len(rows), mime=sf.mime)), "source_file")
    run.write(B.portfolio_sql(ENTITY, B.portfolio_rows(ENTITY, rows, sf.sha)), "portfolio_project")
    run.commit()
    print(f"  overview: {sf.name} — {len(rows)} projects, {sum(1 for r in rows if r.active)} active")
    return rows


def ingest_tracker(run: Run, sf: Optional[SourceFile]):
    if sf:
        sf.kind = 'tracker'
    if not sf or run.skip(sf):
        return None
    sheets = _xlsx_rows(sf.data)
    name = next((n for n in sheets if n.lower().startswith("payables and receivables")), None)
    if not name:
        run.findings.append(f"tracker: no 'Payables and Receivables' sheet in {sf.name}")
        return None
    items = PF.read_tracker(sheets[name], name)
    run.write(B.source_file_sql(B.source_file_row(sf.sha, ENTITY, "tracker", sf.name, sf.drive_id, sf.modified, "", len(items), mime=sf.mime)), "source_file")
    run.write(B.open_item_sql(ENTITY, B.open_item_rows(ENTITY, items, sf.sha)), "open_item")
    run.commit()
    print(f"  tracker: {sf.name} — {len(items)} items ({sum(1 for i in items if i.side == 'ar')} AR, {sum(1 for i in items if i.side == 'ap')} AP)")
    return items


def ingest_pmo(run: Run, sheets: Sequence[SourceFile]):
    n = 0
    for sf in sheets:
        if run.skip(sf):
            continue
        p = PS.read_project(sf.tabs or {})
        if p is None:
            run.findings.append(f"pmo: {sf.name} — no PROJECT SUMMARY block, skipped")
            continue
        if p.code is not None and p.code < PMO_MIN_CODE:
            continue
        run.write(B.source_file_sql(B.source_file_row(sf.sha, ENTITY, "pmo_sheet", sf.name, sf.drive_id, sf.modified, "", len(p.tasks), mime=sf.mime)), "source_file")
        run.write(B.pmo_sql(*B.pmo_rows(ENTITY, p, sf.sha, sf.drive_id, sf.modified, sf.gids)), "pmo")
        run.commit()
        print(f"  pmo: {p.project_id} {p.name[:40]} — {len(p.tasks)} tasks, {len(p.costs)} costs, {len(p.invoices)} invoices, phase '{p.phase}'")
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-dir", help="local mirror of the Drive folders")
    ap.add_argument("--drive", action="store_true", help="read Google Drive with the service account")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-import files already known by hash")
    ap.add_argument("--only", choices=("books", "overview", "tracker", "pmo"), action="append")
    ap.add_argument("--history", default=os.environ.get("ARGIA_FIN_HISTORY_YEARS", ""), metavar="YEARS",
                    help="v249: past years' closes to load, e.g. 2025,2024,2023 (Drive: the year folders in ARGIA_FIN_DRIVE_HISTORY)")
    a = ap.parse_args(argv)
    if not a.from_dir and not a.drive:
        ap.error("--from-dir DIR or --drive")
    only = set(a.only or ("books", "overview", "tracker", "pmo"))
    src = LocalSource(Path(a.from_dir)) if a.from_dir else DriveSource(
        os.environ.get("ARGIA_FIN_DRIVE_REPORTING", ""), os.environ.get("ARGIA_FIN_DRIVE_OVERVIEW", ""),
        os.environ.get("ARGIA_FIN_DRIVE_PM", ""), os.environ.get("ARGIA_FIN_DRIVE_TRACKER", ""),
        [x.strip() for x in os.environ.get("ARGIA_FIN_DRIVE_HISTORY", "").split(",") if x.strip()])
    run = Run(a.apply, a.force)
    print(f"fin_drive_ingest: entity {ENTITY}, {'APPLY' if a.apply else 'dry run'}, source {'drive' if a.drive else a.from_dir}")
    if a.apply:
        run.write(B.entity_sql(), "entity")
        run.commit()
    try:
        if "books" in only:
            ingest_books(run, src.latest(POLIZAS_RE), src.latest(AUX_RE), src.latest(BOOK_RE))
            for y in [int(x) for x in a.history.split(",") if x.strip().isdigit()]:
                print(f"  history {y}:")
                ingest_books(run, src.latest(POLIZAS_RE, y), src.latest(AUX_RE, y), src.latest(BOOK_RE, y), history=True)
        if "overview" in only:
            ingest_overview(run, src.find(OVERVIEW_RE))
        if "tracker" in only:
            ingest_tracker(run, src.find(TRACKER_RE))
        if "pmo" in only:
            ingest_pmo(run, src.pmo_sheets())
    except Exception as e:                       # noqa: BLE001
        print(f"FAILED: {type(e).__name__}: {e}")
        return 2
    print("  statements: " + ", ".join(f"{k}={v}" for k, v in sorted(run.stats.items())) if run.stats else "  nothing new")
    for f in run.findings:
        print("  finding: " + f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
