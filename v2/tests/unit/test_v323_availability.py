"""v323 - MSA availability (argia/prologis/availability.py) and its pages:
the formula on 5-minute slots checked by hand, exclusions (accepted vs
claimed, per incident or per site), the 150 W/m2 rule, liquidated damages
and bonus with their caps, contract years and the analysis due date, the
registers' rules, the suggestions the data proves, the monthly report
using it, and the web flow (ARGIA records and claims, Prologis decides)."""
from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import re
import sys

import pytest

from argia.prologis import assets as A
from argia.prologis import availability as AV
from argia.prologis import monthly as MR
from argia.prologis import registry as R
from argia.prologis import store as S
from argia.prologis import totp as TOTP

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "prologis"
RG = R.load(str(FIX / "registry.json"))
OP = [s.code for s in RG.operating]
T0 = dt.datetime(2026, 10, 6, 10, 0)


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    yield con
    con.close()


def slots(n, start=T0):
    return [start + dt.timedelta(minutes=5 * k) for k in range(n)]


def iv(a, b):
    return AV.Interval(T0 + dt.timedelta(minutes=5 * a), None if b is None else T0 + dt.timedelta(minutes=5 * b))


# ------------------------------------------------------------------ the formula
def test_formula_by_hand():
    r = AV.compute(100.0, slots(12), [AV.Inc(1, 30.0, iv(0, 6))], [])
    assert r.h_ttp == pytest.approx(1.0) and r.gross_kwh_eq == pytest.approx(15.0)
    assert r.availability == pytest.approx(1 - 15 / 100)
    r = AV.compute(100.0, slots(12), [AV.Inc(1, 30.0, iv(0, 6))], [AV.Excl(9, None, iv(0, 2))])
    assert r.excluded_kwh_eq == pytest.approx(5.0) and r.net_kwh_eq == pytest.approx(10.0)
    assert r.availability == pytest.approx(0.90) and r.per_incident[1] == (pytest.approx(0.5), pytest.approx(1 / 6, abs=1e-3))


def test_exclusion_for_one_incident_and_overlap_cap():
    incs = [AV.Inc(1, 60.0, iv(0, 12)), AV.Inc(2, 60.0, iv(0, None))]
    r = AV.compute(100.0, slots(12), incs, [AV.Excl(5, 1, iv(0, 12))])
    assert r.gross_kwh_eq == pytest.approx(100.0)                   # 120 kW down is capped at the plant's 100 kW
    assert r.net_kwh_eq == pytest.approx(60.0)                       # only incident 1 is excluded
    assert AV.compute(100.0, [], incs, []).availability is None      # no sun, no availability
    assert AV.compute(100.0, slots(12), [AV.Inc(1, 300.0, iv(0, 12))], []).availability == 0.0


def test_only_hours_above_150_w_m2_count():
    site = RG.site("TST001")
    flat = lambda s, d: [("09:55", 151.0), ("10:00", 150.0), ("10:05", 900.0), ("23:00", 0.0)]   # noqa: E731
    got = AV.sunny_slots(site, dt.datetime(2026, 10, 6), dt.datetime(2026, 10, 8), flat)
    assert [t.strftime("%d %H:%M") for t in got] == ["06 09:55", "06 10:05", "07 09:55", "07 10:05"]
    h = AV.sunny_slots(site, dt.datetime(2026, 10, 6), dt.datetime(2026, 10, 7))
    assert 4 <= len(h) * AV.SLOT_H <= 11 and all(7 <= t.hour <= 18 for t in h)


def test_sunny_hours_equals_the_slots_on_random_windows():
    import random
    rnd = random.Random(323)
    for _ in range(40):
        s = RG.operating[rnd.randrange(len(RG.operating))]
        a = dt.datetime(2026, 1, 1) + dt.timedelta(minutes=rnd.randint(0, 300 * 1440))
        b = a + dt.timedelta(minutes=rnd.randint(1, 20 * 1440))
        assert AV.sunny_hours(s, a, b) == pytest.approx(len(AV.sunny_slots(s, a, b)) * AV.SLOT_H)


# ------------------------------------------------------------------ LD and bonus
def test_liquidated_damages_and_bonus():
    assert AV.annual_loss(0.97, 0.98, 1_000_000, 2.0) == pytest.approx(2.0 * (1_000_000 / 0.99 - 1_000_000))
    assert AV.annual_loss(0.98, 0.98, 1_000_000, 2.0) == 0.0
    amt, raw, capped = AV.liquidated_damages(0.97, 0.98, 1_000_000, 2.0, 500_000)
    assert amt == pytest.approx(raw) and not capped and raw == pytest.approx(20202.02, abs=0.01)
    amt, raw, capped = AV.liquidated_damages(0.97, 0.98, 1_000_000, 2.0, 50_000)
    assert amt == 10_000 and capped                                   # 20 % of the annual fees
    b, braw, bcap = AV.bonus(0.99, 1_000_000, 2.0, 500_000)
    assert b == pytest.approx(10101.01, abs=0.01) and not bcap
    assert AV.bonus(0.99, 1_000_000, 2.0, 50_000)[0] == 5_000          # 10 % of the annual fees
    assert AV.bonus(0.98, 1_000_000, 2.0, 50_000)[0] == 0.0 and AV.liquidated_damages(0.99, 0.98, 1e6, 2.0, 5e4)[0] == 0.0


def test_contract_years_and_due_dates():
    yrs = AV.contract_years(dt.date(2026, 11, 1), dt.date(2027, 12, 1))
    assert yrs == [(dt.date(2026, 11, 1), dt.date(2027, 11, 1)), (dt.date(2027, 11, 1), dt.date(2028, 11, 1))]
    assert AV.contract_years(dt.date(2026, 11, 1), dt.date(2026, 10, 31)) == []
    assert AV.analysis_due(dt.date(2027, 11, 1)) == dt.date(2028, 3, 31)
    assert AV.analysis_due(dt.date(2027, 3, 15)) == dt.date(2027, 6, 30)
    assert AV.analysis_due(dt.date(2027, 9, 30)) == dt.date(2027, 12, 31)
    assert AV.contract_years(dt.date(2028, 2, 29), dt.date(2029, 3, 2))[1][0] == dt.date(2029, 3, 1)


# ------------------------------------------------------------------ registers
def test_incident_and_exclusion_rules(c):
    kw = dict(site_codes=OP, site_kwp=619.4)
    for args, problem in ((("NOPE", "INV", 10, "2026-10-06 10:00", ""), "unknown site"),
                          (("TST001", "INV", 700, "2026-10-06 10:00", ""), "not above the site kWp"),
                          (("TST001", "INV", 10, "2026-10-06 10:00", "2026-10-06 09:00"), "after the start"),
                          (("TST001", "", 10, "2026-10-06 10:00", ""), "name the component"),
                          (("TST001", "INV", 10, "6/10/2026", ""), "YYYY-MM-DD")):
        with pytest.raises(ValueError, match=problem):
            AV.add_incident(c, *args, "", "op", **kw)
    eid = A.save_equipment(c, {"site_code": "TST001", "category": "inverter", "tag": "INV-02", "make": "SolarEdge", "model": "SE100K",
                               "serial": "S2", "dc_kw": "120"}, OP, "op")
    other = A.save_equipment(c, {"site_code": "TST002", "category": "inverter", "tag": "INV-01", "serial": "S9", "dc_kw": "50"}, OP, "op")
    with pytest.raises(ValueError, match="not at that site"):
        AV.add_incident(c, "TST001", "", None, "2026-10-06 10:00", "", "", "op", equipment_id=other, **kw)
    iid = AV.add_incident(c, "TST001", "", None, "2026-10-06 10:00", "", "IGBT", "op", equipment_id=eid, **kw)
    r = c.execute("SELECT * FROM unavail_incidents WHERE id=?", (iid,)).fetchone()
    assert r["dc_kw"] == 120 and r["component"] == "INV-02 SolarEdge SE100K S2" and r["end_utc"] == ""
    assert r["start_utc"] == "2026-10-06 16:00:00"
    with pytest.raises(ValueError, match="after the start"):
        AV.close_incident(c, iid, "2026-10-06 09:00", "op")
    AV.close_incident(c, iid, "2026-10-08 12:00", "op")
    with pytest.raises(ValueError, match="already closed"):
        AV.close_incident(c, iid, "2026-10-09 12:00", "op")
    for args, problem in ((("TST001", "storm", "2026-10-06 10:00", "2026-10-07 10:00", "x"), "unknown category"),
                          (("TST001", "owner", "2026-10-06 10:00", "2026-10-07 10:00", " "), "reason"),
                          (("TST001", "owner", "2026-10-07 10:00", "2026-10-06 10:00", "x"), "after the start")):
        with pytest.raises(ValueError, match=problem):
            AV.claim_exclusion(c, *args, "op", site_codes=OP)
    with pytest.raises(ValueError, match="not at that site"):
        AV.claim_exclusion(c, "TST002", "b_rma", "2026-10-06 10:00", "2026-10-07 10:00", "x", "op", incident_id=iid, site_codes=OP)
    x = AV.claim_exclusion(c, "", "b_rma", "2026-10-06 12:00", "2026-10-07 12:00", "RMA 555", "op", incident_id=iid, site_codes=OP)
    assert c.execute("SELECT site_code FROM exclusions WHERE id=?", (x,)).fetchone()[0] == "TST001"
    with pytest.raises(ValueError, match="needs a reason"):
        AV.decide_exclusion(c, x, False, "mgr")
    AV.decide_exclusion(c, x, True, "mgr", "ok")
    with pytest.raises(ValueError, match="already decided"):
        AV.decide_exclusion(c, x, False, "mgr", "changed my mind")
    acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
    assert {"unavail_add", "unavail_close", "exclusion_claim", "exclusion_decide"} <= set(acts)


def test_site_result_accepted_vs_claimed_and_report(c):
    s = RG.site("TST001")
    iid = AV.add_incident(c, "TST001", "INV-02", 200.0, "2026-09-10 00:00", "2026-09-13 00:00", "fault", "op", site_codes=OP, site_kwp=s.kwp)
    x1 = AV.claim_exclusion(c, "TST001", "a_quote", "2026-09-10 00:00", "2026-09-11 00:00", "quote waiting", "op", incident_id=iid, site_codes=OP)
    AV.claim_exclusion(c, "", "utility", "2026-09-11 00:00", "2026-09-12 00:00", "CFE outage", "op", site_codes=OP)
    a, b = dt.datetime(2026, 9, 1), dt.datetime(2026, 10, 1)
    raw = AV.site_result(c, s, a, b, ())
    acc0 = AV.site_result(c, s, a, b, ("accepted",))
    cl = AV.site_result(c, s, a, b, ("accepted", "claimed"))
    assert raw.availability == acc0.availability < cl.availability < 1
    AV.decide_exclusion(c, x1, True, "mgr")
    acc1 = AV.site_result(c, s, a, b, ("accepted",))
    assert acc0.availability < acc1.availability < cl.availability
    day = [t for t in AV.sunny_slots(s, a, b) if t.date() == dt.date(2026, 9, 12)]
    assert cl.net_kwh_eq == pytest.approx(len(day) * AV.SLOT_H * 200.0)          # only the 3rd day is not excluded
    assert AV.site_result(c, RG.site("TST002"), a, b, ()).availability == 1.0     # other site untouched
    d = MR.build(c, RG, "2026-09", dt.date(2026, 10, 10))
    t1 = next(x for x in d["sites"] if x["code"] == "TST001")
    assert t1["msa_availability"] == pytest.approx(acc1.availability, abs=1e-5)
    assert t1["msa_availability_claimed"] == pytest.approx(cl.availability, abs=1e-5)
    assert d["portfolio"]["exclusions_pending"] == 1 and d["portfolio"]["msa_availability"] < 1


def test_suggestions_from_approvals_and_claims(c):
    t = S.create_ticket(c, "TST001", "Inverter down", "", "OUT_100_500", "op", detected_utc="2026-10-06 16:00:00",
                        kw_lost=100.0, needs_approval=True)
    S.set_approval(c, t, True, "mgr", ts="2026-10-07 16:00:00")
    eid = A.save_equipment(c, {"site_code": "TST001", "category": "inverter", "tag": "INV-03", "serial": "S3", "dc_kw": "100"}, OP, "op")
    cl = A.open_claim(c, "TST001", "SolarEdge", "x", "op", equipment_id=eid)
    A.set_claim_status(c, cl, "SUBMITTED", "op", ts="2026-10-08 16:00:00")
    A.set_claim_status(c, A.claim(c, cl["number"]), "APPROVED", "op", ts="2026-10-20 16:00:00")
    iid = AV.add_incident(c, "TST001", "", None, "2026-10-06 10:00", "", "x", "op", equipment_id=eid, ticket_id=t["id"],
                          site_codes=OP, site_kwp=619.4)
    sg = {x["category"]: x for x in AV.suggestions(c)}
    assert sg["a_quote"]["end_utc"] == "2026-10-07 16:00:00" and sg["a_quote"]["incident"] == iid
    assert sg["b_rma"]["start_utc"] == "2026-10-08 16:00:00" and sg["b_rma"]["end_utc"] == "2026-10-20 16:00:00"
    AV.claim_exclusion(c, "", "b_rma", "2026-10-08 10:00", "2026-10-20 10:00", sg["b_rma"]["reason"], "op", incident_id=iid, site_codes=OP)
    assert [x["category"] for x in AV.suggestions(c)] == ["a_quote"]


# ------------------------------------------------------------------ pages
NEW_PW = "Sunny-rooftops-2026"


@pytest.fixture
def env(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    sys.path.insert(0, str(V2 / "server" / "bundle"))
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX / "registry.json"))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    con = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(con, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    con.close()
    return app_mod, pws, tmp_path


def _csrf(h):
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', h)
    return m.group(1) if m else ""


def sign_in(app_mod, user, pw):
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": user, "password": pw, "next": "/"})
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    cl.post("/mfa", data={"code": TOTP.code_at(secret, int(__import__("time").time() // 30)), "pending": secret, "csrf": _csrf(page), "next": "/"})
    page = cl.get("/password").get_data(as_text=True)
    cl.post("/password", data={"pw1": NEW_PW, "pw2": NEW_PW, "csrf": _csrf(page)})
    assert cl.get("/").status_code == 200
    return cl


def _ok(h, where=""):
    assert chr(0x2014) not in h and "None" not in re.sub(r"<script.*?</script>", "", h, flags=re.S) and "Traceback" not in h, where


def test_pages_argia_records_prologis_decides(env):
    app_mod, pws, tmp = env
    m = MR.prev_month(app_mod.today_mx())
    a, _b = MR.month_bounds(m)
    op = sign_in(app_mod, "operator", pws["operator"])
    h = op.get(f"/availability/?m={m}").get_data(as_text=True)
    _ok(h)
    assert "MSA availability" in h and "100.000%" in h and "Record unavailability" in h
    tok = _csrf(h)
    st, en = f"{a:%Y-%m}-05 08:00", f"{a:%Y-%m}-07 18:00"
    assert "Not recorded: DC kW" in op.post("/availability/incident", data={"site": "TST001", "component": "INV-2", "start": st, "csrf": tok}).get_data(as_text=True)
    h = op.post("/availability/incident", data={"site": "TST001", "component": "INV-2", "dc_kw": "200", "start": st, "end": en,
                                                  "cause": "IGBT", "m": m, "csrf": tok}).get_data(as_text=True)
    assert "Recorded." in h and "INV-2" in h
    h = op.post("/availability/exclusion", data={"site": "TST001", "incident": "1", "category": "b_rma", "start": st, "end": f"{a:%Y-%m}-06 18:00",
                                                   "reason": "RMA 777 at SolarEdge", "m": m, "csrf": tok}).get_data(as_text=True)
    assert "Claimed - waiting for Prologis" in h and "Claimed by ARGIA, awaiting Prologis" in h
    assert op.post("/availability/exclusion/1/decide", data={"ok": "1", "csrf": tok}).status_code == 403   # ARGIA cannot accept its own claim
    mgr = sign_in(app_mod, "manager", pws["manager"])
    h = mgr.get(f"/availability/?m={m}").get_data(as_text=True)
    _ok(h)
    assert "Record unavailability" not in h and "Accept" in h
    mt = _csrf(h)
    assert mgr.post("/availability/incident", data={"csrf": mt}).status_code == 403
    assert "Not saved: a rejection needs a reason" in mgr.post("/availability/exclusion/1/decide", data={"ok": "0", "csrf": mt}).get_data(as_text=True)
    h = mgr.post("/availability/exclusion/1/decide", data={"ok": "1", "note": "RMA confirmed", "m": m, "csrf": mt}).get_data(as_text=True)
    assert "Accepted" in h and "RMA confirmed" in h
    rep = op.get(f"/reports/{m}/").get_data(as_text=True)
    assert "MSA availability" in rep and f'/availability/?m={m}' in rep
    # annual analysis: terms by the admin only
    assert op.post("/availability/terms", data={"site": "TST001", "effective": "2026-01-01", "csrf": tok}).status_code == 403
    ad = sign_in(app_mod, "admin", pws["admin"])
    h = ad.get("/availability/annual").get_data(as_text=True)
    assert "Contract terms not set" in h
    at = _csrf(h)
    assert "Not saved: kWh rate out of range" in ad.post("/availability/terms", data={"site": "TST001", "effective": "2026-01-01", "rate": "-1", "csrf": at}).get_data(as_text=True)
    h = ad.post("/availability/terms", data={"site": "TST001", "effective": f"{a.year - 1}-{a.month:02d}-01", "rate": "2.10", "fee": "400000", "csrf": at}).get_data(as_text=True)
    _ok(h)
    assert "2.10" in h and "Liquidated damages" in h and "Analysis due" in h
    con = S.connect(str(tmp / "pl.db"))
    assert tuple(con.execute("SELECT kwh_rate, annual_fee FROM contract_terms WHERE site_code='TST001'").fetchone()) == (2.1, 400000.0)
    ad.get("/lang/es")
    for path in (f"/availability/?m={m}", "/availability/annual"):
        h = ad.get(path).get_data(as_text=True)
        _ok(h, path)
        assert "Disponibilidad" in h
