"""v320 - ARGIA for Prologis: the Assets pages end to end (Flask test
client, temp SQLite, synthetic registry). Equipment register with CSV
import and export, warranty claims with the Prologis acknowledgement of a
denial, spare parts with movements and the quarterly report (page, CSV,
spreadsheet), roles, export, both languages, no em dash."""
from __future__ import annotations

import csv
import importlib
import io
import json
import pathlib
import re
import sys
import zipfile

import pytest

pytest.importorskip("flask", reason="flask not installed here")

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

from argia.prologis import assets as A     # noqa: E402
from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402

NEW_PW = "Sunny-rooftops-2026"
PAGES = ("/assets/", "/assets/claims/", "/assets/spares/", "/assets/spares/report")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX / "registry.json"))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    return app_mod, pws, tmp_path


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    return m.group(1) if m else ""


def sign_in(app_mod, user, pw):
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": user, "password": pw, "next": "/"})
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    code = TOTP.code_at(secret, int(__import__("time").time() // 30))
    cl.post("/mfa", data={"code": code, "pending": secret, "csrf": _csrf(page), "next": "/"})
    page = cl.get("/password").get_data(as_text=True)
    cl.post("/password", data={"pw1": NEW_PW, "pw2": NEW_PW, "csrf": _csrf(page)})
    assert cl.get("/").status_code == 200
    return cl


def _clean(h: str) -> str:
    return re.sub(r"<script.*?</script>", "", h, flags=re.S)


def _ok(h: str, where: str = "") -> None:
    assert chr(0x2014) not in h and "None" not in _clean(h) and "Traceback" not in h, where


def test_assets_need_login(env):
    app_mod, _, _ = env
    cl = app_mod.app.test_client()
    for path in PAGES + ("/assets/eq/new", "/assets/import", "/assets/equipment.csv", "/assets/claims/WC-0001/"):
        r = cl.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_viewer_and_manager_see_but_cannot_change(env):
    app_mod, pws, _ = env
    for role in ("viewer", "manager"):
        cl = sign_in(app_mod, role, pws[role])
        for path in PAGES:
            h = cl.get(path).get_data(as_text=True)
            _ok(h, path)
            assert "Assets" in h and 'action="/assets/spares/move"' not in h and "Add equipment" not in h, path
        tok = _csrf(cl.get("/password").get_data(as_text=True))
        assert cl.get("/assets/eq/new").status_code == 403 and cl.get("/assets/import").status_code == 403
        for path, data in (("/assets/eq/save", {"site_code": "TST001", "category": "inverter", "tag": "X"}),
                           ("/assets/claims/new", {"site": "TST001", "supplier": "X"}),
                           ("/assets/spares/move", {"kind": "RECEIPT"}), ("/assets/spares/part", {"code": "X1"}),
                           ("/assets/spares/min", {}), ("/assets/spares/location", {})):
            assert cl.post(path, data={**data, "csrf": tok}).status_code == 403, (role, path)
        assert cl.get("/assets/equipment.csv").status_code == 200           # Owner visibility: the register is theirs


def test_operator_register_import_and_export(env):
    app_mod, pws, tmp = env
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get("/assets/").get_data(as_text=True)
    assert "No equipment registered yet" in h
    form = op.get("/assets/eq/new?site=TST002").get_data(as_text=True)
    assert 'value="TST002" selected' in form
    tok = _csrf(form)
    base = {"site_code": "TST001", "category": "inverter", "tag": "INV-01", "make": "SolarEdge", "model": "SE100K", "serial": "7E1",
            "qty": "1", "dc_kw": "120", "warranty_until": "2026-11-01", "status": "in_service", "csrf": tok}
    r = op.post("/assets/eq/save", data=base)
    assert r.status_code == 302 and r.headers["Location"].endswith("/assets/?site=TST001")
    dup = op.post("/assets/eq/save", data={**base, "tag": "INV-02"}).get_data(as_text=True)
    assert "already registered" in dup and 'value="INV-02"' in dup                 # the form keeps what was typed
    assert "must be YYYY-MM-DD" in op.post("/assets/eq/save", data={**base, "serial": "7E2", "installed": "1/1/2025"}).get_data(as_text=True)
    op.post("/assets/eq/save", data={**base, "category": "meter", "tag": "MTR", "serial": "M-1", "make": "Janitza", "dc_kw": "", "calib_due": "2020-01-01"})
    h = op.get("/assets/?site=TST001").get_data(as_text=True)
    _ok(h)
    assert "7E1" in h and "Expires 2026-11-01" in h and "Calibration overdue 2020-01-01" in h
    assert "No module DC registered for this site yet" in h
    # CSV import: one bad row refuses the whole file
    bad = "site,category,tag,make,serial,qty,dc_kw\nTST001,module,ROOF-A,Longi,,1126,619.4\nTST001,inverter,INV-09,SolarEdge,7E1,1,\n"
    r = op.post("/assets/import", data={"file": (io.BytesIO(bad.encode()), "eq.csv"), "csrf": tok}, content_type="multipart/form-data")
    h = r.get_data(as_text=True)
    assert "Nothing imported: 1 problem(s)" in h and "line 3: serial 7E1 is already registered" in h
    c = S.connect(str(tmp / "pl.db"))
    assert c.execute("SELECT count(*) FROM equipment").fetchone()[0] == 2
    good = bad.replace("7E1,1,", "7E9,1,")
    r = op.post("/assets/import", data={"file": (io.BytesIO(good.encode("utf-8-sig")), "eq.csv"), "csrf": tok}, content_type="multipart/form-data")
    assert "2 rows imported" in r.get_data(as_text=True)
    h = op.get("/assets/?site=TST001").get_data(as_text=True)
    assert "619.4 kW</b> = <b>100.0%" in h                                   # the module DC matches the site kWp
    tpl = op.get("/assets/import_template.csv").get_data(as_text=True)
    assert tpl.splitlines()[0] == ",".join(A.IMPORT_COLUMNS)
    rows = list(csv.DictReader(io.StringIO(op.get("/assets/equipment.csv").get_data(as_text=True))))
    assert len(rows) == 4 and {r["serial"] for r in rows} >= {"7E1", "7E9", "M-1"}
    acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
    assert {"equipment_add", "equipment_import", "equipment_import_refused", "equipment_export"} <= set(acts)
    site = op.get("/sites/TST001/").get_data(as_text=True)
    assert "1126" in site and "PV modules" in site and "warranties expiring or expired" in site
    eid = c.execute("SELECT id FROM equipment WHERE serial='7E1'").fetchone()[0]
    ed = op.get(f"/assets/eq/{eid}").get_data(as_text=True)
    assert 'value="7E1"' in ed and op.get("/assets/eq/abc").status_code == 404 and op.get("/assets/eq/999").status_code == 404


def test_denied_claim_is_flagged_until_prologis_acknowledges(env):
    app_mod, pws, tmp = env
    op = sign_in(app_mod, "operator", pws["operator"])
    tok = _csrf(op.get("/assets/claims/").get_data(as_text=True))
    assert "Not opened: no ticket PL-0099" in op.post("/assets/claims/new", data={"site": "TST001", "supplier": "SolarEdge", "ticket": "PL-0099", "csrf": tok}).get_data(as_text=True)
    r = op.post("/assets/claims/new", data={"site": "TST001", "supplier": "SolarEdge", "fault": "Error 3x09 after storm", "csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("/assets/claims/WC-0001/")
    h = op.get("/assets/claims/WC-0001/").get_data(as_text=True)
    _ok(h)
    assert "Error 3x09 after storm" in h and 'value="SUBMITTED"' in h
    op.post("/assets/claims/WC-0001/status", data={"to": "SUBMITTED", "ref": "RMA-555", "csrf": tok})
    assert "a denial needs the supplier" in op.post("/assets/claims/WC-0001/status", data={"to": "DENIED", "csrf": tok}).get_data(as_text=True)
    op.post("/assets/claims/WC-0001/status", data={"to": "DENIED", "note": "outside warranty: surge", "csrf": tok})
    assert op.post("/assets/claims/WC-0001/ack", data={"csrf": tok}).status_code == 403   # ARGIA cannot acknowledge for Prologis
    mgr = sign_in(app_mod, "manager", pws["manager"])
    home = mgr.get("/").get_data(as_text=True)
    assert "1 warranty claim(s) denied by the supplier" in home
    h = mgr.get("/assets/claims/WC-0001/").get_data(as_text=True)
    assert "RMA-555" in h and "outside warranty: surge" in h and "Acknowledge" in h
    mt = _csrf(h)
    assert mgr.post("/assets/claims/WC-0001/status", data={"to": "CLOSED", "csrf": mt}).status_code == 403
    mgr.post("/assets/claims/WC-0001/comment", data={"body": "Please appeal", "csrf": mt})
    mgr.post("/assets/claims/WC-0001/ack", data={"note": "seen", "csrf": mt})
    h = mgr.get("/assets/claims/WC-0001/").get_data(as_text=True)
    assert "Denial acknowledged by" in h and "Please appeal" in h and "Acknowledge</button>" not in h
    assert "denied by the supplier" not in mgr.get("/").get_data(as_text=True)
    assert "already acknowledged" in mgr.post("/assets/claims/WC-0001/ack", data={"csrf": mt}).get_data(as_text=True)
    # appeal, approval with compensation, pass-through
    op.post("/assets/claims/WC-0001/status", data={"to": "SUBMITTED", "note": "appeal", "csrf": tok})
    op.post("/assets/claims/WC-0001/status", data={"to": "APPROVED", "comp": "18,500", "csrf": tok})
    lst = op.get("/assets/claims/").get_data(as_text=True)
    assert "MXN 18,500.00" in lst and "to pass through" in lst
    op.post("/assets/claims/WC-0001/pass", data={"day": "2026-10-09", "note": "NC-1", "csrf": tok})
    assert "passed through 2026-10-09" in op.get("/assets/claims/").get_data(as_text=True)
    assert "not allowed" in op.post("/assets/claims/WC-0001/status", data={"to": "DENIED", "note": "x", "csrf": tok}).get_data(as_text=True)
    assert op.get("/assets/claims/WC-0404/").status_code == 404
    c = S.connect(str(tmp / "pl.db"))
    acts = [r["action"] for r in c.execute("SELECT action FROM audit WHERE target='WC-0001'")]
    assert acts.count("claim_status") == 4 and {"claim_open", "claim_owner_ack", "claim_comment", "claim_pass_through"} <= set(acts)


def test_spares_moves_and_quarterly_report(env):
    openpyxl = pytest.importorskip("openpyxl")
    app_mod, pws, tmp = env
    seed = tmp / "parts.json"
    seed.write_text(json.dumps([
        {"code": "INV-SE100", "name_en": "Inverter SE100K", "name_es": "Inversor SE100K", "category": "inverter", "make": "SolarEdge",
         "model": "SE100K", "serialized": "1", "owner": "PROLOGIS", "min": {"WH-CDMX": 2}},
        {"code": "MOD-550", "name_en": "Module 550 W", "category": "module", "owner": "PROLOGIS", "min": {"WH-CDMX": 20}}]))
    assert app_mod.main(["x", "--seed-parts", str(seed)]) == 0
    assert app_mod.main(["x", "--seed-parts", str(seed)]) == 0             # second run adds nothing
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get("/assets/spares/").get_data(as_text=True)
    _ok(h)
    assert "INV-SE100" in h and "<b style=\"color:var(--red, #b2443c)\">0</b>" in h      # below minimum shows red
    tok = _csrf(h)

    def mv(**d):
        return op.post("/assets/spares/move", data={**d, "csrf": tok}).get_data(as_text=True)
    assert "Recorded" in mv(kind="RECEIPT", part="MOD-550", to="WH-CDMX", qty="25", note="EPC handover")
    assert "Recorded" in mv(kind="RECEIPT", part="MOD-550", to="WH-CDMX", qty="3", damaged="1", note="cracked glass")
    assert "Recorded" in mv(kind="RECEIPT", part="INV-SE100", to="WH-CDMX", qty="1", serial="sn-1")
    assert "Not recorded: only 28" in mv(kind="ISSUE", part="MOD-550", **{"from": "WH-CDMX"}, qty="30", site="TST001")
    assert "Not recorded: this part is tracked by serial number" in mv(kind="ISSUE", part="INV-SE100", **{"from": "WH-CDMX"}, qty="1", site="TST001")
    # a ticket to link the issue to
    c = S.connect(str(tmp / "pl.db"))
    tk = S.create_ticket(c, "TST001", "Hot spot", "", "STRING_25", "op")
    assert "Not recorded: no ticket PL-0404" in mv(kind="ISSUE", part="MOD-550", **{"from": "WH-CDMX"}, qty="2", site="TST001", ticket="PL-0404")
    assert "Recorded" in mv(kind="ISSUE", part="MOD-550", **{"from": "WH-CDMX"}, qty="2", site="TST001", ticket=tk["number"])
    assert "Recorded" in mv(kind="SCRAP", part="MOD-550", **{"from": "WH-CDMX"}, qty="3", note="damaged on receipt")
    assert "Recorded" in mv(kind="TRANSFER", part="INV-SE100", **{"from": "WH-CDMX"}, to="TRANSIT", qty="1", serial="SN-1")
    assert "Saved" in op.post("/assets/spares/min", data={"part": "MOD-550", "loc": "WH-CDMX", "min": "21", "csrf": tok}).get_data(as_text=True)
    assert "Not saved: give a minimum" in op.post("/assets/spares/min", data={"part": "MOD-550", "loc": "WH-CDMX", "csrf": tok}).get_data(as_text=True)
    assert "Added" in op.post("/assets/spares/location", data={"code": "WH-MTY", "name": "Monterrey", "csrf": tok}).get_data(as_text=True)
    assert "Saved" in op.post("/assets/spares/part", data={"code": "FUSE-15", "name_en": "Fuse 15 A", "category": "protection", "owner": "ARGIA", "csrf": tok}).get_data(as_text=True)
    assert "Not saved: code" in op.post("/assets/spares/part", data={"code": "?", "name_en": "x", "category": "module", "csrf": tok}).get_data(as_text=True)
    h = op.get("/assets/spares/").get_data(as_text=True)
    _ok(h)
    assert "WH-MTY" in h and "SN-1" in h and "cracked glass" in h and tk["number"] in h
    q = A.quarter_of(app_mod.today_mx())
    rep = op.get("/assets/spares/report").get_data(as_text=True)
    _ok(rep)
    assert f"Quarterly inventory report · {q}" in rep and "Received damaged" in rep and "below minimum" in rep
    assert "Hot spot" not in rep and tk["number"] in rep                    # issues list their ticket
    x = op.get(f"/assets/spares/report?q={q}&fmt=xlsx")
    assert x.status_code == 200 and x.headers["Content-Disposition"].endswith(f"prologis_inventory_{q}.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(x.data))
    assert wb.sheetnames == [f"Stock {q}", "Movements", "Restocking"]
    hdr = [cl.value for cl in wb[f"Stock {q}"][1]]
    rows = {(r[0], r[3]): dict(zip(hdr, r)) for r in wb[f"Stock {q}"].iter_rows(min_row=2, values_only=True)}
    mod = rows[("MOD-550", "WH-CDMX")]
    assert mod["in_receipt"] == 28 and mod["out_issue"] == 2 and mod["out_scrap"] == 3 and mod["closing"] == 23 and mod["minimum"] == 21
    assert mod["opening"] + mod["in_receipt"] - mod["out_issue"] - mod["out_scrap"] == mod["closing"]
    assert rows[("INV-SE100", "TRANSIT")]["closing"] == 1 and rows[("INV-SE100", "WH-CDMX")]["below_minimum"] == "yes"
    restock = {r[0]: r for r in wb["Restocking"].iter_rows(min_row=2, values_only=True)}
    assert restock["INV-SE100"][5] == 1 and restock["MOD-550"][5] == 0      # 2 min - 0 in warehouse - 1 in transit
    assert wb["Movements"].max_row == 1 + 6
    cs = op.get(f"/assets/spares/report?q={q}&fmt=csv").get_data(as_text=True)
    assert cs.splitlines()[0].startswith("part,name,owner,location,opening")
    assert op.get("/assets/spares/report?q=2026-Q9").status_code == 400
    acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
    assert {"stock_receipt", "stock_issue", "stock_scrap", "stock_transfer", "stock_min", "location_add", "part_add",
            "inventory_report_export"} <= set(acts)


def test_export_overview_and_spanish(env):
    app_mod, pws, _ = env
    ad = sign_in(app_mod, "admin", pws["admin"])
    tok = _csrf(ad.get("/assets/claims/").get_data(as_text=True))
    ad.post("/assets/claims/new", data={"site": "TST002", "supplier": "Longi", "csrf": tok})
    z = zipfile.ZipFile(io.BytesIO(ad.get("/export.zip").data))
    assert {"equipment.csv", "warranty_claims.csv", "warranty_claim_events.csv", "spare_parts.csv", "stock_locations.csv",
            "stock_minimums.csv", "stock_movements.csv"} <= set(z.namelist())
    assert "Longi" in z.read("warranty_claims.csv").decode() and "WH-CDMX" in z.read("stock_locations.csv").decode()
    home = ad.get("/").get_data(as_text=True)
    assert "open warranty claims" in home and "spare parts below minimum" in home and 'href="/assets/"' in home
    ad.get("/lang/es")
    for path in PAGES + ("/", "/assets/claims/WC-0001/", "/assets/import", "/assets/eq/new", "/sites/TST001/"):
        h = ad.get(path).get_data(as_text=True)
        _ok(h, path)
        assert "Activos" in h, path
    assert "Registro de equipos" in ad.get("/assets/").get_data(as_text=True)
    assert "Reporte trimestral de inventario" in ad.get("/assets/spares/report").get_data(as_text=True)
