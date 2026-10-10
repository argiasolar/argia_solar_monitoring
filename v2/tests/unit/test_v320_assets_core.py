"""v320 - equipment register, warranty claims, spare parts: the rules
(argia/prologis/assets.py) and the stdlib spreadsheet writer
(argia/prologis/xlsx.py), against a temporary SQLite store."""
from __future__ import annotations

import datetime as dt
import io
import random
import sqlite3

import pytest

from argia.prologis import assets as A
from argia.prologis import store as S
from argia.prologis import xlsx as XL

SITES = ["TST001", "TST002"]
TODAY = dt.date(2026, 10, 10)


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    yield con
    con.close()


def _eq(**kw):
    d = {"site_code": "TST001", "category": "inverter", "tag": "INV-01", "make": "SolarEdge", "model": "SE100K",
         "serial": "7E100", "qty": "1", "dc_kw": "120.5", "ac_kw": "100", "installed": "2025-03-01",
         "warranty_by": "SolarEdge", "warranty_until": "2037-03-01", "calib_due": "", "notes": ""}
    d.update(kw)
    return d


def _audit(c, action):
    return c.execute("SELECT * FROM audit WHERE action=? ORDER BY id", (action,)).fetchall()


# ------------------------------------------------------------------ pure rules
def test_warranty_state_boundaries():
    assert A.warranty_state("", TODAY) == "none"
    assert A.warranty_state("2026-10-09", TODAY) == "expired"
    assert A.warranty_state("2026-10-10", TODAY) == "expiring"            # the last day still counts
    assert A.warranty_state((TODAY + dt.timedelta(days=90)).isoformat(), TODAY) == "expiring"
    assert A.warranty_state((TODAY + dt.timedelta(days=91)).isoformat(), TODAY) == "active"


def test_calibration_state_only_for_meters_and_sensors():
    assert A.calib_state("inverter", "2020-01-01", TODAY) == "n/a"
    assert A.calib_state("meter", "", TODAY) == "unknown"
    assert A.calib_state("sensor", "2026-10-09", TODAY) == "overdue"
    assert A.calib_state("meter", (TODAY + dt.timedelta(days=30)).isoformat(), TODAY) == "due_soon"
    assert A.calib_state("meter", (TODAY + dt.timedelta(days=31)).isoformat(), TODAY) == "ok"


@pytest.mark.parametrize("change, problem", [
    ({"site_code": "NOPE"}, "unknown site"),
    ({"category": "toaster"}, "unknown category"),
    ({"status": "lost"}, "unknown status"),
    ({"installed": "01/03/2025"}, "installed must be YYYY-MM-DD"),
    ({"warranty_until": "2037-02-30"}, "warranty_until must be YYYY-MM-DD"),
    ({"qty": "2"}, "one unit"),
    ({"qty": "1.5", "serial": ""}, "whole number"),
    ({"qty": "0", "serial": ""}, "qty must be 1 to 100000"),
    ({"dc_kw": "-1"}, "dc_kw out of range"),
    ({"ac_kw": "lots"}, "ac_kw must be a number"),
    ({"serial": "", "tag": "", "model": ""}, "at least a tag"),
])
def test_clean_equipment_lists_problems(change, problem):
    _r, errs = A.clean_equipment(_eq(**change), SITES)
    assert any(problem in e for e in errs), errs


def test_clean_equipment_normalises():
    r, errs = A.clean_equipment(_eq(site_code="tst002", category="INVERTER", dc_kw="1,200.5", qty=""), SITES)
    assert errs == [] and r["site_code"] == "TST002" and r["category"] == "inverter" and r["dc_kw"] == 1200.5 and r["qty"] == 1


CSV_OK = ("﻿site,category,tag,make,model,serial,qty,dc_kw,warranty_until\n"
          "TST001,inverter,INV-01,SolarEdge,SE100K,7E1,1,120,2037-03-01\n"
          ",,,,,,,,\n"
          "TST001,module,ROOF-A,Longi,LR5-72 550,,218,119.9,2050-01-01\n")


def test_parse_import_good_file_skips_blank_rows():
    res = A.parse_import(CSV_OK, SITES)
    assert res.errors == [] and len(res.rows) == 2 and res.rows[1]["qty"] == 218


@pytest.mark.parametrize("text, problem", [
    ("", "empty file"),
    ("site,category,colour\nTST001,inverter,red\n", "unknown columns: colour"),
    ("category,tag\ninverter,INV\n", "missing column 'site'"),
    ("site,category,tag\n", "no data rows"),
    ("site,category,make,serial\nTST001,inverter,SE,X1\nTST002,inverter,se,x1\n", "line 3: serial x1 is already registered"),
    ("site,category,make,serial\nTST001,inverter,SolarEdge,OLD-1\n", "line 2: serial OLD-1 is already registered"),
    ("site,category,tag,installed\nTST001,meter,M1,2025/01/01\nTST009,meter,M2,\n", "line 3: unknown site"),
])
def test_parse_import_refuses(text, problem):
    res = A.parse_import(text, SITES, existing=[("solaredge", "old-1")])
    assert any(problem in e for e in res.errors), res.errors


def test_import_is_all_or_nothing(c):
    bad = A.parse_import("site,category,tag\nTST001,inverter,INV-1\nTST001,nope,INV-2\n", SITES)
    assert bad.errors and len(bad.rows) == 2
    with pytest.raises(ValueError):
        A.import_equipment(c, bad, "op")
    assert c.execute("SELECT count(*) FROM equipment").fetchone()[0] == 0
    assert A.import_equipment(c, A.parse_import(CSV_OK, SITES), "op") == 2
    assert len(_audit(c, "equipment_import")) == 1
    again = A.parse_import(CSV_OK, SITES, A.existing_serials(c))
    assert any("7E1 is already registered" in e for e in again.errors)


def test_dc_check(c):
    assert A.dc_check(A.equipment(c, "TST001"), 619.4) is None
    A.save_equipment(c, _eq(category="module", tag="A", serial="", make="Longi", qty="500", dc_kw="275"), SITES, "op")
    A.save_equipment(c, _eq(category="module", tag="B", serial="", make="Longi", qty="626", dc_kw="344.4"), SITES, "op")
    A.save_equipment(c, _eq(category="module", tag="C", serial="", make="Longi", qty="1", dc_kw="99", status="removed"), SITES, "op")
    tot, share = A.dc_check(A.equipment(c, "TST001", include_removed=True), 619.4)
    assert tot == pytest.approx(619.4) and share == pytest.approx(1.0)      # removed rows do not count


def test_quarters():
    assert A.quarter_bounds("2026-q4") == (dt.date(2026, 10, 1), dt.date(2026, 12, 31))
    assert A.quarter_bounds("2027-Q1") == (dt.date(2027, 1, 1), dt.date(2027, 3, 31))
    assert A.quarter_bounds("2028-Q1")[1] == dt.date(2028, 3, 31)
    assert A.quarter_of(dt.date(2026, 6, 30)) == "2026-Q2" and A.quarter_of(dt.date(2026, 7, 1)) == "2026-Q3"
    for bad in ("2026-Q5", "2026Q4", "", "Q4-2026"):
        with pytest.raises(ValueError):
            A.quarter_bounds(bad)


def _m(day, part, kind, qty, frm="", to="", serial=""):
    return {"day": day, "part_code": part, "kind": kind, "qty": qty, "from_loc": frm, "to_loc": to, "serial": serial}


def test_quarter_report_balances_and_restock():
    moves = [_m("2026-09-20", "INV", "RECEIPT", 4, to="WH"),
             _m("2026-10-02", "INV", "ISSUE", 1, frm="WH"),
             _m("2026-10-03", "INV", "TRANSFER", 2, frm="WH", to="TR"),
             _m("2026-11-05", "INV", "RECEIPT", 3, to="WH"),
             _m("2027-01-02", "INV", "RECEIPT", 50, to="WH")]                # next quarter: ignored
    rows, rest, inq = A.quarter_report(moves, ["INV", "MOD"], [("WH", False), ("TR", True)],
                                       {("INV", "WH"): 11, ("MOD", "WH"): 208}, "2026-Q4")
    wh = next(r for r in rows if r.loc == "WH" and r.part == "INV")
    assert (wh.opening, wh.closing) == (4, 4) and wh.ins == {"RECEIPT": 3} and wh.outs == {"ISSUE": 1, "TRANSFER": 2}
    assert wh.below_min
    tr = next(r for r in rows if r.loc == "TR")
    assert tr.closing == 2 and not tr.below_min
    mod = next(r for r in rows if r.part == "MOD")
    assert mod.closing == 0 and mod.below_min                               # a minimum with no stock is listed
    ri = next(x for x in rest if x.part == "INV")
    assert (ri.min_total, ri.closing_total, ri.transit, ri.order_qty) == (11, 4, 2, 5)
    assert next(x for x in rest if x.part == "MOD").order_qty == 208
    assert len(inq) == 3


def test_quarter_report_invariant_random():
    """opening + in - out == closing for every row, and the closing of one
    quarter is the opening of the next, on random movement histories."""
    rnd = random.Random(320)
    locs = [("W1", False), ("W2", False), ("T", True)]
    for _ in range(25):
        moves = []
        for _k in range(rnd.randint(5, 60)):
            day = (dt.date(2026, 1, 1) + dt.timedelta(days=rnd.randint(0, 540))).isoformat()
            kind = rnd.choice(A.MOVE_KEYS)
            need_from, need_to = A._NEEDS[kind]
            a, b = rnd.sample([x for x, _ in locs], 2)
            moves.append(_m(day, rnd.choice(["P1", "P2"]), kind, rnd.randint(1, 9), a if need_from else "", b if need_to else ""))
        moves.sort(key=lambda m: m["day"])
        prev = None
        for q in ("2026-Q1", "2026-Q2", "2026-Q3", "2026-Q4", "2027-Q1", "2027-Q2"):
            rows, _r, _i = A.quarter_report(moves, ["P1", "P2"], locs, {}, q)
            for r in rows:
                assert r.opening + sum(r.ins.values()) - sum(r.outs.values()) == pytest.approx(r.closing)
            closing = {(r.part, r.loc): r.closing for r in rows}
            if prev is not None:
                opening = {(r.part, r.loc): r.opening for r in rows}
                for k, v in prev.items():
                    assert opening.get(k, 0) == pytest.approx(v)
            prev = closing


# ------------------------------------------------------------------ store: equipment
def test_ensure_is_idempotent_with_default_locations(c):
    A.ensure(c)
    A.ensure(c)
    assert [r["code"] for r in A.locations(c)] == ["WH-CDMX", "TRANSIT"]
    assert A.locations(c)[1]["transit"] == 1


def test_equipment_save_edit_and_duplicate_serial(c):
    eid = A.save_equipment(c, _eq(), SITES, "op", "1.2.3.4")
    with pytest.raises(ValueError, match="already registered"):
        A.save_equipment(c, _eq(serial="7e100", make="solaredge"), SITES, "op")
    A.save_equipment(c, _eq(serial="7E100", make="Huawei"), SITES, "op")    # same serial, other maker: allowed
    A.save_equipment(c, _eq(status="fault", notes="arc fault"), SITES, "op2", eid=eid)
    r = A.equipment_row(c, eid)
    assert r["status"] == "fault" and r["updated_by"] == "op2"
    d = _audit(c, "equipment_edit")[-1]["detail"]
    assert "status: 'in_service' -> 'fault'" in d and "notes: '' -> 'arc fault'" in d
    with pytest.raises(ValueError, match="no such equipment"):
        A.save_equipment(c, _eq(serial="X9"), SITES, "op", eid=999)
    with pytest.raises(sqlite3.IntegrityError):                              # the database refuses it too
        c.execute("INSERT INTO equipment (site_code, category, make, serial, created_by, created_utc) VALUES ('TST001','inverter','SolarEdge','7E100','x','x')")


# ------------------------------------------------------------------ store: warranty claims
def test_claim_lifecycle_denial_needs_owner_ack(c):
    eid = A.save_equipment(c, _eq(), SITES, "op")
    with pytest.raises(ValueError, match="not at that site"):
        A.open_claim(c, "TST002", "SolarEdge", "x", "op", equipment_id=eid)
    with pytest.raises(ValueError, match="supplier"):
        A.open_claim(c, "TST001", " ", "x", "op")
    cl = A.open_claim(c, "TST001", "SolarEdge", "Inverter fault 3x09", "op", equipment_id=eid)
    assert cl["number"] == "WC-0001" and cl["status"] == "DRAFT"
    assert A.open_claim(c, "TST001", "Longi", "hot spot", "op")["number"] == "WC-0002"
    with pytest.raises(ValueError, match="not allowed"):
        A.set_claim_status(c, cl, "APPROVED", "op")
    A.set_claim_status(c, cl, "SUBMITTED", "op", supplier_ref="RMA-77", ts="2026-10-01 10:00:00")
    cl = A.claim(c, "WC-0001")
    assert cl["supplier_ref"] == "RMA-77"
    assert A.turnaround_days(cl, dt.datetime(2026, 10, 11, 10)) == pytest.approx(10)
    with pytest.raises(ValueError, match="reason"):
        A.set_claim_status(c, cl, "DENIED", "op", note="  ")
    assert not A.needs_owner_ack(c, cl)
    A.set_claim_status(c, cl, "DENIED", "op", note="installation damage claimed", ts="2026-10-05 10:00:00")
    cl = A.claim(c, "WC-0001")
    assert A.needs_owner_ack(c, cl) and A.turnaround_days(cl, dt.datetime(2030, 1, 1)) == pytest.approx(4)
    A.ack_denial(c, cl, "mgr", "will discuss at the monthly meeting")
    cl = A.claim(c, "WC-0001")
    assert not A.needs_owner_ack(c, cl) and cl["owner_ack_by"] == "mgr"
    with pytest.raises(ValueError, match="already"):
        A.ack_denial(c, cl, "mgr")
    A.set_claim_status(c, cl, "SUBMITTED", "op", note="appeal with photos")      # re-submission keeps the ack
    cl = A.claim(c, "WC-0001")
    assert not A.needs_owner_ack(c, cl) and cl["decided_utc"] == ""
    A.set_claim_status(c, cl, "DENIED", "op", note="denied again")              # a second denial needs a new ack
    assert A.needs_owner_ack(c, A.claim(c, "WC-0001"))
    A.set_claim_status(c, A.claim(c, "WC-0001"), "CLOSED", "op")
    assert A.needs_owner_ack(c, A.claim(c, "WC-0001"))                          # closing does not hide it
    with pytest.raises(ValueError, match="nothing to acknowledge"):
        A.ack_denial(c, A.claim(c, "WC-0002"), "mgr")
    acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
    assert acts.count("claim_status") == 5 and "claim_owner_ack" in acts and acts.count("claim_open") == 2


def test_claim_compensation_pass_through(c):
    cl = A.open_claim(c, "TST001", "SolarEdge", "x", "op")
    with pytest.raises(ValueError, match="no compensation"):
        A.record_pass_through(c, cl, "2026-10-10", "op")
    A.set_claim_status(c, cl, "SUBMITTED", "op")
    with pytest.raises(ValueError, match="out of range"):
        A.set_claim_status(c, A.claim(c, cl["number"]), "APPROVED", "op", compensation=-5)
    A.set_claim_status(c, A.claim(c, cl["number"]), "APPROVED", "op", compensation=18500.0)
    cl = A.claim(c, cl["number"])
    assert cl["compensation_mxn"] == 18500.0 and cl["passed_through"] == ""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        A.record_pass_through(c, cl, "10/10/2026", "op")
    A.record_pass_through(c, cl, "2026-10-10", "op", "credit note NC-12")
    assert A.claim(c, cl["number"])["passed_through"] == "2026-10-10"
    assert A.turnaround_days(A.claim(c, "WC-0001"), dt.datetime(2030, 1, 1)) is not None


# ------------------------------------------------------------------ store: parts and stock
def _parts(c):
    A.save_part(c, {"code": "inv-se100", "name_en": "Inverter SE100K", "category": "inverter", "make": "SolarEdge",
                    "model": "SE100K", "serialized": "1", "owner": "PROLOGIS"}, "op")
    A.save_part(c, {"code": "MOD-550", "name_en": "Module 550 W", "category": "module", "make": "Longi", "owner": "ARGIA"}, "op")


def test_parts_validation_and_seed(c):
    for bad, msg in (({"code": "x", "name_en": "a", "category": "module"}, "code"),
                     ({"code": "OK1", "name_en": "", "category": "module"}, "English name"),
                     ({"code": "OK1", "name_en": "a", "category": "pump"}, "unknown category"),
                     ({"code": "OK1", "name_en": "a", "category": "module", "owner": "TENANT"}, "owner")):
        with pytest.raises(ValueError, match=msg):
            A.save_part(c, bad, "op")
    items = [{"code": "FUSE-15", "name_en": "Fuse 15 A", "category": "protection", "min": {"WH-CDMX": 40}}]
    assert A.seed_parts(c, items) == 1 and A.seed_parts(c, items) == 0
    assert A.minimums(c) == {("FUSE-15", "WH-CDMX"): 40}
    A.set_min(c, "FUSE-15", "WH-CDMX", 30, "op")
    assert A.seed_parts(c, items) == 0 and A.minimums(c)[("FUSE-15", "WH-CDMX")] == 30   # never overwritten
    with pytest.raises(ValueError, match="no such location"):
        A.set_min(c, "FUSE-15", "WH-MARS", 1, "op")
    with pytest.raises(ValueError, match="out of range"):
        A.set_min(c, "FUSE-15", "WH-CDMX", -1, "op")
    with pytest.raises(ValueError, match="exists"):
        A.add_location(c, "wh-cdmx", "again", "op")
    A.add_location(c, "WH-MTY", "Monterrey", "op")
    assert "WH-MTY" in [r["code"] for r in A.locations(c)]


def test_stock_moves_rules(c):
    _parts(c)
    kw = dict(site_codes=SITES, today=TODAY)
    A.move(c, "RECEIPT", "MOD-550", 10, "op", to_loc="WH-CDMX", note="EPC handover, inspected ok", **kw)
    with pytest.raises(ValueError, match="only 10"):
        A.move(c, "ISSUE", "MOD-550", 11, "op", from_loc="WH-CDMX", site_code="TST001", **kw)
    with pytest.raises(ValueError, match="choose the site"):
        A.move(c, "ISSUE", "MOD-550", 1, "op", from_loc="WH-CDMX", **kw)
    with pytest.raises(ValueError, match="two different"):
        A.move(c, "TRANSFER", "MOD-550", 1, "op", from_loc="WH-CDMX", to_loc="WH-CDMX", **kw)
    with pytest.raises(ValueError, match="reason"):
        A.move(c, "ADJUST_OUT", "MOD-550", 1, "op", from_loc="WH-CDMX", **kw)
    with pytest.raises(ValueError, match="future"):
        A.move(c, "RECEIPT", "MOD-550", 1, "op", to_loc="WH-CDMX", day="2026-10-11", **kw)
    with pytest.raises(ValueError, match="more than 0"):
        A.move(c, "RECEIPT", "MOD-550", 0, "op", to_loc="WH-CDMX", **kw)
    with pytest.raises(ValueError, match="not tracked by serial"):
        A.move(c, "RECEIPT", "MOD-550", 1, "op", to_loc="WH-CDMX", serial="X", **kw)
    with pytest.raises(ValueError, match="unknown part"):
        A.move(c, "RECEIPT", "NOPE", 1, "op", to_loc="WH-CDMX", **kw)
    with pytest.raises(ValueError, match="goes to"):
        A.move(c, "RECEIPT", "MOD-550", 1, "op", to_loc="WH-MARS", **kw)
    A.move(c, "ISSUE", "MOD-550", 2, "op", from_loc="WH-CDMX", site_code="TST001", note="hot spot", **kw)
    A.move(c, "SCRAP", "MOD-550", 1, "op", from_loc="WH-CDMX", note="broken glass", **kw)
    A.move(c, "RECEIPT", "MOD-550", 2, "op", to_loc="WH-CDMX", condition="damaged", note="cracked", **kw)
    assert A.on_hand(c, "MOD-550", "WH-CDMX") == 9 and A.on_hand_total(c, "MOD-550") == 9
    # serialized
    with pytest.raises(ValueError, match="give the serial"):
        A.move(c, "RECEIPT", "INV-SE100", 1, "op", to_loc="WH-CDMX", **kw)
    with pytest.raises(ValueError, match="one unit at a time"):
        A.move(c, "RECEIPT", "INV-SE100", 2, "op", to_loc="WH-CDMX", serial="S1", **kw)
    A.move(c, "RECEIPT", "INV-SE100", 1, "op", to_loc="WH-CDMX", serial="s1", **kw)
    with pytest.raises(ValueError, match="already in stock at WH-CDMX"):
        A.move(c, "RECEIPT", "INV-SE100", 1, "op", to_loc="TRANSIT", serial="S1", **kw)
    with pytest.raises(ValueError, match="is not at TRANSIT"):
        A.move(c, "TRANSFER", "INV-SE100", 1, "op", from_loc="TRANSIT", to_loc="WH-CDMX", serial="S1", **kw)
    A.move(c, "TRANSFER", "INV-SE100", 1, "op", from_loc="WH-CDMX", to_loc="TRANSIT", serial="S1", **kw)
    assert A.serial_locations(A.all_moves(c))[("INV-SE100", "S1")] == "TRANSIT"
    with pytest.raises(ValueError, match="serial tracking cannot change"):
        A.save_part(c, {"code": "INV-SE100", "name_en": "Inverter SE100K", "category": "inverter", "serialized": ""}, "op")
    assert len(_audit(c, "stock_receipt")) == 3


def test_issue_replacing_equipment_keeps_the_serial_trail(c):
    _parts(c)
    kw = dict(site_codes=SITES, today=TODAY)
    old = A.save_equipment(c, _eq(serial="BAD-1"), SITES, "op")
    A.move(c, "RECEIPT", "INV-SE100", 1, "op", to_loc="WH-CDMX", serial="NEW-1", **kw)
    with pytest.raises(ValueError, match="not in service at that site"):
        A.move(c, "ISSUE", "INV-SE100", 1, "op", from_loc="WH-CDMX", serial="NEW-1", site_code="TST002", replaces=old, **kw)
    with pytest.raises(ValueError, match="only an issue"):
        A.move(c, "RETURN", "INV-SE100", 1, "op", to_loc="WH-CDMX", serial="NEW-2", site_code="TST001", replaces=old, **kw)
    mid = A.move(c, "ISSUE", "INV-SE100", 1, "op", from_loc="WH-CDMX", serial="NEW-1", site_code="TST001", replaces=old, **kw)
    o = A.equipment_row(c, old)
    n = A.equipment_row(c, o["replaced_by"])
    assert o["status"] == "removed" and n["serial"] == "NEW-1" and n["tag"] == "INV-01" and n["status"] == "in_service"
    assert n["dc_kw"] == 120.5 and n["installed"] == TODAY.isoformat() and f"stock move #{mid}" in n["notes"]
    assert A.serial_locations(A.all_moves(c))[("INV-SE100", "NEW-1")] == ""             # left the inventory
    assert [r["serial"] for r in A.equipment(c, "TST001")] == ["NEW-1"]
    assert len(_audit(c, "equipment_replace")) == 1


# ------------------------------------------------------------------ xlsx
def test_xlsx_reads_back_with_openpyxl():
    openpyxl = pytest.importorskip("openpyxl")
    data = XL.workbook([("Stock 2026-Q4", [["part", "qty", "note"], ["INV", 11, "a & b <c>"], ["MOD", 2.5, "tab\x01gone"], ["X", None, ""]]),
                        ("Bad/Name:*?", [["only header"]]), ("bad_name___", [["dup"]]), ("Empty", [])])
    wb = openpyxl.load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Stock 2026-Q4", "Bad_Name___", "bad_name____2", "Empty"]
    ws = wb["Stock 2026-Q4"]
    assert [c.value for c in ws[2]] == ["INV", 11, "a & b <c>"]
    assert ws["B3"].value == 2.5 and ws["C3"].value == "tabgone" and ws["B4"].value is None
    assert ws["A1"].font.b and not ws["A2"].font.b
    assert ws.freeze_panes == "A2"


def test_xlsx_needs_a_sheet():
    with pytest.raises(ValueError):
        XL.workbook([])
