"""v322 - the monthly O&M report (argia/prologis/monthly.py): months and
the 10-day deadline, the design yield CSV, the HSE register and its 24-hour
rule, the report numbers (totals add up, partial months, outages over 3
days, response time per work order, the log), freezing at publication,
the spreadsheet, and the digest reminder."""
from __future__ import annotations

import datetime as dt
import io
import json
import pathlib

import pytest

from argia.prologis import alarms as AL
from argia.prologis import monthly as MR
from argia.prologis import registry as R
from argia.prologis import store as S
from argia.prologis import xlsx as XL

V2 = pathlib.Path(__file__).resolve().parents[2]
RG = R.load(str(V2 / "tests" / "fixtures" / "prologis" / "registry.json"))
CODES = [s.code for s in RG.sites]


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    yield con
    con.close()


def U(local: str) -> str:
    return (dt.datetime.fromisoformat(local) + dt.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ months and deadline
def test_month_bounds_and_deadline():
    assert MR.month_bounds("2026-02") == (dt.date(2026, 2, 1), dt.date(2026, 2, 28))
    assert MR.month_bounds("2028-02")[1] == dt.date(2028, 2, 29)
    assert MR.deadline("2026-10") == dt.date(2026, 11, 10) and MR.deadline("2026-12") == dt.date(2027, 1, 10)
    assert MR.prev_month(dt.date(2027, 1, 3)) == "2026-12"
    for bad in ("2026-13", "2026-1", "", "Oct 2026"):
        with pytest.raises(ValueError):
            MR.month_bounds(bad)


@pytest.mark.parametrize("pub_local, today, state", [
    ("", "2026-10-20", "open"), ("", "2026-11-01", "due"), ("", "2026-11-10", "due"), ("", "2026-11-11", "overdue"),
    ("2026-11-10 23:30", "2026-11-20", "met"),            # late evening MX is still the 10th (UTC already the 11th)
    ("2026-11-11 00:10", "2026-11-20", "late"),
])
def test_deadline_state(pub_local, today, state):
    assert MR.deadline_state("2026-10", U(pub_local) if pub_local else "", dt.date.fromisoformat(today)) == state


# ------------------------------------------------------------------ design yield
HEAD = "site," + ",".join(f"m{i}" for i in range(1, 13)) + "\n"


def test_design_csv(c):
    rows, errs = MR.parse_design(HEAD + "TST001," + ",".join(["60000"] * 12) + "\ntst002,1,,3,,,,,,,,,\n", CODES)
    assert errs == [] and len(rows) == 14 and ("TST002", 3, 3.0) in rows
    for text, problem in (("site,jan\nTST001,1\n", "header must be"), (HEAD + "NOPE,1,,,,,,,,,,,\n", "unknown site NOPE"),
                          (HEAD + "TST001,1,,,,,,,,,,,\nTST001,2,,,,,,,,,,,\n", "site TST001 twice"),
                          (HEAD + "TST001,x,,,,,,,,,,,\n", "m1 is not a number"), (HEAD + "TST001,-5,,,,,,,,,,,\n", "m1 out of range"),
                          (HEAD + "TST001,,,,,,,,,,,,\n", "no values")):
        assert any(problem in e for e in MR.parse_design(text, CODES)[1]), problem
    MR.save_design(c, rows, "PVsyst v7 2025", "op")
    MR.save_design(c, [("TST001", 1, 61000.0)], "PVsyst v8", "op")
    dm = MR.design_map(c)
    assert dm[("TST001", 1)] == 61000.0 and dm[("TST001", 2)] == 60000.0 and len(dm) == 14


# ------------------------------------------------------------------ HSE
def test_hse_register_and_24h(c):
    now = dt.datetime(2026, 10, 10, 12, 0)
    kw = dict(site_codes=CODES, now_local=now)
    for args, problem in ((("2026-10-09 10:00", "TST001", "fire", "x", ""), "unknown kind"),
                          (("2026-10-09 10:00", "NOPE", "incident", "x", ""), "unknown site"),
                          (("2026-10-09 10:00", "TST001", "incident", " ", ""), "describe"),
                          (("09/10/2026", "TST001", "incident", "x", ""), "YYYY-MM-DD"),
                          (("2026-10-11 10:00", "TST001", "incident", "x", ""), "future")):
        with pytest.raises(ValueError, match=problem):
            MR.add_hse(c, *args, "op", **kw)
    with pytest.raises(ValueError, match="reported before"):
        MR.add_hse(c, "2026-10-09 10:00", "TST001", "incident", "x", "", "op", reported_local="2026-10-09 09:00", **kw)
    a = MR.add_hse(c, "2026-10-08 10:00", "TST001", "near_miss", "ladder slipped", "tied off", "op", reported_local="2026-10-09 09:59", **kw)
    b = MR.add_hse(c, "2026-10-08 10:00", "TST002", "first_aid", "cut", "", "op", reported_local="2026-10-09 10:01", **kw)
    d = MR.add_hse(c, "2026-10-10 08:00", "", "incident", "vehicle scratch", "", "op", **kw)
    e = MR.add_hse(c, "2026-10-01 08:00", "TST003", "observation", "no gloves", "", "op", **kw)
    rows = {r["id"]: r for r in c.execute("SELECT * FROM hse_events")}
    nowu = dt.datetime(2026, 10, 10, 18, 0)
    assert MR.hse_on_time(rows[a], nowu) is True and MR.hse_on_time(rows[b], nowu) is False
    assert MR.hse_on_time(rows[d], nowu) is True                                  # 4 h old, still inside 24 h
    assert MR.hse_on_time(rows[d], nowu + dt.timedelta(hours=21)) is False
    assert MR.hse_on_time(rows[e], nowu) is None                                  # an observation needs no report
    assert rows[a]["occurred_utc"] == "2026-10-08 16:00:00"


# ------------------------------------------------------------------ the report
def _setup_history(c):
    """A September with one long outage, one short one, alarms and an order."""
    long_ = S.create_ticket(c, "TST001", "Inverter 2 down", "IGBT failure", "OUT_100_500", "op",
                            detected_utc=U("2026-09-10 11:00"), kw_lost=150.0)
    S.set_status(c, long_, "RESPONDED", "op", "crew on site", ts=U("2026-09-10 15:00"))
    c.execute("UPDATE tickets SET eta_date='2026-10-20' WHERE id=?", (long_["id"],))
    short = S.create_ticket(c, "TST002", "String fuse", "", "STRING_25", "op", detected_utc=U("2026-09-12 10:00"), kw_lost=8.0)
    S.set_status(c, short, "IN_PROGRESS", "op", ts=U("2026-09-12 12:00"))
    S.set_status(c, c.execute("SELECT * FROM tickets WHERE id=?", (short["id"],)).fetchone(), "RESOLVED", "op", "fuse replaced", ts=U("2026-09-13 10:00"))
    old = S.create_ticket(c, "TST003", "August comm loss", "", "COMM_2", "op", detected_utc=U("2026-08-01 10:00"))
    S.set_status(c, old, "RESPONDED", "op", ts=U("2026-08-01 12:00"))
    S.set_status(c, c.execute("SELECT * FROM tickets WHERE id=?", (old["id"],)).fetchone(), "RESOLVED", "op", ts=U("2026-08-02 10:00"))
    c.execute("INSERT INTO alarms (site_code, kind, severity, msa_class, kw_lost, detail, detected_utc, last_seen_utc, sample)"
              " VALUES ('TST001','production_loss','critical','OUT_100_500',150,'inverter(s) down: INV-2',?,?,1)",
              (U("2026-09-10 10:55"), U("2026-09-10 10:55")))
    c.commit()
    MR.add_hse(c, "2026-09-15 09:00", "TST001", "near_miss", "ladder", "", "op", reported_local="2026-09-15 12:00",
               site_codes=CODES, now_local=dt.datetime(2026, 10, 1))
    return long_, short


def test_build_september(c):
    long_, short = _setup_history(c)
    rows, _ = MR.parse_design(HEAD + "TST001," + ",".join(["90000"] * 12) + "\n", CODES)
    MR.save_design(c, rows, "PVsyst", "op")
    d = MR.build(c, RG, "2026-09", dt.date(2026, 10, 10), now_utc_dt=dt.datetime(2026, 10, 10, 18))
    json.dumps(d)                                                                 # can be frozen as JSON
    p = d["portfolio"]
    assert p["sites"] == 7 and p["days"] == 30 and d["source"] == "sample" and d["guarantee"] == 0.98
    assert p["kwh"] == pytest.approx(sum(x["kwh"] for x in d["sites"]), abs=0.5)
    assert sum(k for _, k, _ in d["daily"]) == pytest.approx(p["kwh"], abs=1)
    assert all(len(x["daily"]) == 30 for x in d["sites"])
    t1 = next(x for x in d["sites"] if x["code"] == "TST001")
    assert t1["design_kwh"] == 90000.0 and p["design_kwh"] == 90000.0 and p["design_sites"] == 1
    assert next(x for x in d["sites"] if x["code"] == "TST002")["design_kwh"] is None      # never invented
    assert 0.7 < p["pr"] < 0.9 and 0.9 < p["availability"] <= 1 and p["irr_kwh_m2"] > 100
    # outages over 3 days: the long one only (still open at month end -> counted to month end)
    assert [o["number"] for o in d["outages"]] == [long_["number"]]
    o = d["outages"][0]
    assert o["eta"] == "2026-10-20" and o["action"] == "crew on site" and o["days"] == pytest.approx(20.5, abs=0.1)
    # work orders: both September tickets with their response time; the August one is out
    wo = {w["number"]: w for w in d["work_orders"]}
    assert set(wo) == {long_["number"], short["number"]}
    assert wo[long_["number"]]["hours"] == pytest.approx(4.0) and wo[long_["number"]]["clock"] == "met"
    assert wo[short["number"]]["clock"] == "met" and wo[short["number"]]["hours"] == pytest.approx(2.0)
    assert d["response_closed"] == 2 and d["response_met"] == 2
    assert any(x["type"] == "alarm" and "INV-2" in x["text"] for x in d["log"])
    assert any(x["type"] == "ticket" and "fuse replaced" in x["text"] for x in d["log"])
    assert len(d["hse"]) == 1 and d["hse"][0]["on_time"] is True
    assert [x["number"] for x in d["open_items"]["incidents"]] == [long_["number"]]


def test_partial_month_prorates_design(c):
    rows, _ = MR.parse_design(HEAD + "TST001," + ",".join(["31000"] * 12) + "\n", CODES)
    MR.save_design(c, rows, "PVsyst", "op")
    d = MR.build(c, RG, "2026-10", dt.date(2026, 10, 10))
    assert d["portfolio"]["days"] == 9 and d["last_day"] == "2026-10-09"
    assert next(x for x in d["sites"] if x["code"] == "TST001")["design_kwh"] == pytest.approx(9000.0)
    assert MR.build(c, RG, "2026-11", dt.date(2026, 10, 10))["portfolio"]["kwh"] == 0      # nothing yet


def test_publish_freezes(c):
    _setup_history(c)
    today = dt.date(2026, 10, 5)
    with pytest.raises(ValueError, match="not over"):
        MR.publish(c, "2026-10", MR.build(c, RG, "2026-10", today), "x", "op", today)
    d = MR.build(c, RG, "2026-09", today)
    with pytest.raises(ValueError, match="summary"):
        MR.publish(c, "2026-09", d, " ", "op", today)
    MR.publish(c, "2026-09", d, "All sites in operation; INV-2 at site 1 waiting for the RMA.", "op", today)
    with pytest.raises(ValueError, match="already"):
        MR.publish(c, "2026-09", d, "again", "op", today)
    S.create_ticket(c, "TST004", "late entry", "", "OUT_500", "op", detected_utc=U("2026-09-20 10:00"), kw_lost=600.0)
    frozen, is_frozen = MR.report_data(c, RG, "2026-09", today)
    assert is_frozen and len(frozen["work_orders"]) == 2 and frozen["notes"].startswith("All sites")
    fresh = MR.build(c, RG, "2026-09", today)
    assert len(fresh["work_orders"]) == 3                                         # the draft would have changed
    assert MR.deadline_state("2026-09", MR.published(c, "2026-09")["published_utc"], today) in ("met", "late")
    assert c.execute("SELECT count(*) FROM audit WHERE action='monthly_report_publish'").fetchone()[0] == 1


def test_workbook(c):
    openpyxl = pytest.importorskip("openpyxl")
    _setup_history(c)
    d = MR.build(c, RG, "2026-09", dt.date(2026, 10, 10))
    wb = openpyxl.load_workbook(io.BytesIO(XL.workbook(MR.workbook_sheets(d, {s.code: s.name for s in RG.sites}))))
    assert wb.sheetnames == ["Portfolio", "Sites", "Daily", "Outages over 3 days", "Alarms and O&M log", "Work orders", "HSE"]
    sites = list(wb["Sites"].iter_rows(min_row=2, values_only=True))
    assert len(sites) == 7 and sum(r[3] for r in sites) == pytest.approx(d["portfolio"]["kwh"], abs=0.5)
    assert sites[0][1] == "Parque Norte Bldg 1"
    assert wb["Daily"].max_row == 1 + 7 * 30 and wb["Outages over 3 days"].max_row == 2
    port = {r[0]: r[1] for r in wb["Portfolio"].iter_rows(min_row=2, values_only=True)}
    assert port["availability guarantee"] == 0.98 and port["month"] == "2026-09"


def test_digest_reminds_of_the_report_and_guarantee_setting(c):
    s, b, _ = AL.digest(c, RG, dt.datetime(2026, 10, 4, 7, 30), "https://x")
    assert "Monthly report" not in b                                              # before the 5th: quiet
    s, b, _ = AL.digest(c, RG, dt.datetime(2026, 10, 6, 7, 30), "https://x")
    assert "Monthly report 2026-09: NOT PUBLISHED, due by 10 Oct" in b and "OVERDUE" not in b
    s, b, _ = AL.digest(c, RG, dt.datetime(2026, 10, 11, 7, 30), "https://x")
    assert "OVERDUE" in b
    MR.publish(c, "2026-09", MR.build(c, RG, "2026-09", dt.date(2026, 10, 11)), "ok", "op", dt.date(2026, 10, 11))
    assert "Monthly report" not in AL.digest(c, RG, dt.datetime(2026, 10, 12, 7, 30), "https://x")[1]
    with pytest.raises(ValueError, match="between"):
        AL.set_setting(c, "availability_guarantee", "0.5", "ad")
    with pytest.raises(ValueError, match="share"):
        AL.set_setting(c, "availability_guarantee", "98%", "ad")
    AL.set_setting(c, "availability_guarantee", "0.9750", "ad")
    assert AL.setting(c, "availability_guarantee") == "0.975"
