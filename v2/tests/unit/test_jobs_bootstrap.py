"""v206.6 - every job wired to a systemd unit bootstraps through
open_sheets(); none reads GOOGLE_SHEET_ID_V2 or builds a SheetsClient by
hand. client_reports_publish.py did, and failed at every tick from the
moment v205 removed the id from the server env."""
import pathlib
import re

V2 = pathlib.Path(__file__).resolve().parents[2]
UNITS = V2 / "server" / "bundle"

# sheet-era jobs whose units are disabled on pio06 and go with v207
LEGACY = {"kpi_pg_mirror.py"}


def wired_scripts():
    out = set()
    for unit in UNITS.glob("*.service"):
        for m in re.finditer(r"run_job\.sh\s+\S+\s+(\S+\.py)", unit.read_text(encoding="utf-8")):
            out.add(m.group(1))
    return sorted(out)


def test_units_reference_existing_scripts():
    names = wired_scripts()
    assert len(names) >= 15
    missing = [n for n in names if not (V2 / "scripts" / n).exists()]
    assert missing == []


def test_no_wired_job_demands_the_workbook_id():
    offenders = []
    for n in wired_scripts():
        if n in LEGACY:
            continue
        src = (V2 / "scripts" / n).read_text(encoding="utf-8")
        if "GOOGLE_SHEET_ID_V2" in src.replace("ENV: GOOGLE_SHEET_ID_V2", "") \
                and "open_sheets()" not in src:
            offenders.append(n)
        if "SheetsClient(sheet_id" in src:
            offenders.append(n)
    assert offenders == []


def test_client_reports_publish_bootstraps_like_report_daily():
    src = (V2 / "scripts" / "client_reports_publish.py").read_text(encoding="utf-8")
    assert "client = open_sheets()" in src
    assert "GOOGLE_SHEET_ID_V2 not set" not in src
