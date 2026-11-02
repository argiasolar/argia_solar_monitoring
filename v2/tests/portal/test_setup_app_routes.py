"""v263 - the Setup app (people, plants, finance, CFE, system, account),
route by route, against the private PostgreSQL.

Before v263 not one of its 35 URLs was exercised by a test: user
management, passwords, mail subscriptions, maintenance windows and every
finance edit (O&M, PR baseline, SLA, loans, fees, tariffs) could break
unnoticed. Each POST here checks the database afterwards, not only the
HTTP status - a form that answers 200 but saves nothing fails.
"""
from __future__ import annotations

import importlib
import pathlib
import sqlite3
import sys

import pytest

pytest.importorskip("flask", reason="flask not installed here (CI and pio06 have it)")

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))

ADMIN = {"X-Remote-User": "tomasz"}
PLAIN = {"X-Remote-User": "maria"}


@pytest.fixture
def sa(fresh_db, tmp_path, monkeypatch):
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH"):
        monkeypatch.setenv(k, fresh_db[k])
    monkeypatch.setenv("ARGIA_AUTH_DIR", str(tmp_path / "auth"))
    monkeypatch.setenv("ARGIA_WEBROOT", str(tmp_path / "www"))
    monkeypatch.setenv("ARGIA_ENV_FILE", str(tmp_path / "argia_env"))
    monkeypatch.setenv("ARGIA_BACKUP_DIR", str(tmp_path / "backups"))
    (tmp_path / "www").mkdir()
    (tmp_path / "argia_env").write_text("ARGIA_PG_MIRROR=1\n")
    import setup_app
    mod = importlib.reload(setup_app)
    mod.app.config["TESTING"] = True
    monkeypatch.setattr(mod, "sync", lambda *a, **k: None)          # htpasswd files for nginx: not under test here
    monkeypatch.setattr(mod, "_fin_regen", lambda: True)            # report regeneration is tested in test_portal_site
    c = mod.db()
    c.execute("INSERT INTO users(username,hash,level,reports,is_admin,first_name,last_name,email)"
              " VALUES('tomasz','x','argia','',1,'Tomasz','Z','tomasz@example.invalid')")
    c.execute("INSERT INTO users(username,hash,level,reports,is_admin,first_name,email)"
              " VALUES('maria','x','custom','gto1',0,'Maria','maria@example.invalid')")
    c.commit()
    c.close()
    return mod


def post(sa, path, data, headers=ADMIN):
    return sa.app.test_client().post(path, data={"csrf": sa.CSRF, **data}, headers=headers)


def users(sa):
    c = sqlite3.connect(sa.DB_PATH)
    rows = {r[0]: r[1:] for r in c.execute("SELECT username, disabled, is_admin, email FROM users")}
    c.close()
    return rows


# ------------------------------------------------------------ pages
@pytest.mark.parametrize("path", ["/", "/people/", "/plants/", "/finance/", "/cfe/", "/system/", "/account/", "/finance"])
def test_every_drawer_opens_for_an_admin(sa, path):
    r = sa.app.test_client().get(path, headers=ADMIN)
    assert r.status_code == 200, (path, r.status_code)
    assert "Traceback" not in r.get_data(as_text=True)


def test_health_and_whoami(sa):
    c = sa.app.test_client()
    assert c.get("/healthz").status_code == 200
    who = c.get("/account/whoami", headers=ADMIN).get_json()
    assert who and who.get("user") in ("tomasz", None) or "tomasz" in str(who)


def test_a_non_admin_sees_no_management(sa):
    body = sa.app.test_client().get("/people/", headers=PLAIN).get_data(as_text=True)
    assert "no management rights" in body.lower()
    body = sa.app.test_client().get("/finance/", headers=PLAIN).get_data(as_text=True)
    assert "Admins only" in body or "no management rights" in body.lower()


def test_a_post_without_the_form_token_changes_nothing(sa):
    before = users(sa)
    r = sa.app.test_client().post("/suspend", data={"username": "maria"}, headers=ADMIN)
    assert r.status_code == 409
    assert users(sa) == before


# ------------------------------------------------------------ people
def test_add_then_suspend_then_delete_a_user(sa):
    r = post(sa, "/add", {"username": "nuevo", "first_name": "Nuevo", "email": "nuevo@example.invalid", "level": "custom"})
    assert r.status_code == 200 and "User nuevo created" in r.get_data(as_text=True)
    assert "One-time password" in r.get_data(as_text=True)
    assert users(sa)["nuevo"][0] == 0
    post(sa, "/suspend", {"username": "nuevo"})
    assert users(sa)["nuevo"][0] == 1
    post(sa, "/delete", {"username": "nuevo"})
    assert "nuevo" not in users(sa)


def test_a_duplicate_user_is_refused(sa):
    r = post(sa, "/add", {"username": "maria"})
    assert "already exists" in r.get_data(as_text=True)


def test_the_last_admin_cannot_be_removed(sa):
    post(sa, "/delete", {"username": "tomasz"})
    post(sa, "/suspend", {"username": "tomasz"})
    u = users(sa)
    assert "tomasz" in u and u["tomasz"][0] == 0 and u["tomasz"][1] == 1


def test_update_a_profile_and_reset_a_password(sa):
    post(sa, "/update", {"username": "maria", "first_name": "María", "email": "maria.new@example.invalid", "level": "custom"})
    assert users(sa)["maria"][2] == "maria.new@example.invalid"
    r = post(sa, "/password", {"username": "maria"})
    assert "One-time password" in r.get_data(as_text=True)


def test_own_profile_change(sa):
    post(sa, "/account/profile", {"first_name": "Tom", "last_name": "Z", "email": "t2@example.invalid"}, headers=ADMIN)
    assert users(sa)["tomasz"][2] == "t2@example.invalid"


# ------------------------------------------------------------ mail subscriptions
def test_subscribe_toggle_unsubscribe(sa, psql):
    post(sa, "/mail/add", {"username": "maria", "channel": "daily"})
    rows = psql("SELECT enabled FROM mail_subscription WHERE email = 'maria@example.invalid' AND channel = 'daily'")
    assert rows == [["t"]]
    post(sa, "/mail/toggle", {"email": "maria@example.invalid", "channel": "daily"})
    assert psql("SELECT enabled FROM mail_subscription WHERE email = 'maria@example.invalid' AND channel = 'daily'") == [["f"]]
    post(sa, "/mail/delete", {"email": "maria@example.invalid", "channel": "daily"})
    assert psql("SELECT count(*) FROM mail_subscription WHERE email = 'maria@example.invalid'") == [["0"]]


def test_an_unknown_channel_saves_nothing(sa, psql):
    n = psql("SELECT count(*) FROM mail_subscription")
    post(sa, "/mail/add", {"username": "maria", "channel": "spam"})
    assert psql("SELECT count(*) FROM mail_subscription") == n


# ------------------------------------------------------------ maintenance windows
def test_maintenance_window_lifecycle(sa, psql):
    post(sa, "/maint/add", {"plant": "gto1", "start": "2026-09-01T08:00", "category": "customer", "note": "roof work",
                            "deemed_kwh": "1200"})
    mid = psql("SELECT id FROM maintenance_event WHERE note = 'roof work'")[0][0]
    post(sa, "/maint/close", {"id": mid})
    assert psql(f"SELECT end_ts IS NOT NULL, approved_by IS NULL FROM maintenance_event WHERE id = {mid}") == [["t", "t"]]
    post(sa, "/maint/approve", {"id": mid})
    assert psql(f"SELECT approved_by FROM maintenance_event WHERE id = {mid}") == [["tomasz"]]
    post(sa, "/maint/delete", {"id": mid})                          # approved events are never deleted
    assert psql(f"SELECT count(*) FROM maintenance_event WHERE id = {mid}") == [["1"]]


def test_a_draft_maintenance_window_can_be_deleted(sa, psql):
    post(sa, "/maint/add", {"plant": "qro1", "start": "2026-09-02T08:00", "category": "customer", "note": "draft one",
                            "deemed_kwh": "0"})
    mid = psql("SELECT id FROM maintenance_event WHERE note = 'draft one'")[0][0]
    post(sa, "/maint/delete", {"id": mid})
    assert psql(f"SELECT count(*) FROM maintenance_event WHERE id = {mid}") == [["0"]]


def test_invalid_maintenance_input_saves_nothing(sa, psql):
    n = psql("SELECT count(*) FROM maintenance_event")
    post(sa, "/maint/add", {"plant": "nowhere", "start": "2026-09-01T08:00"})
    post(sa, "/maint/add", {"plant": "gto1", "start": "yesterday"})
    assert psql("SELECT count(*) FROM maintenance_event") == n


# ------------------------------------------------------------ finance edits
def test_plant_level_finance_edits_are_saved_and_audited(sa, psql):
    post(sa, "/finance/om", {"plant": "GTO1", "amount": "12345"})
    post(sa, "/finance/prbaseline", {"plant": "GTO1", "value": "0.83"})
    post(sa, "/finance/sla", {"plant": "GTO1", "value": "0.97"})
    assert psql("SELECT om_cost_monthly_mxn, pr_baseline, sla_target FROM plant WHERE plant_key = 'GTO1'") == [["12345.00", "0.8300", "0.9700"]]
    actions = {r[0] for r in psql("SELECT action FROM finance_audit WHERE username = 'tomasz'")}
    assert {"om", "pr_baseline", "sla_target"} <= actions


def test_out_of_range_finance_values_are_refused(sa, psql):
    before = psql("SELECT pr_baseline, sla_target FROM plant WHERE plant_key = 'GTO1'")
    r = post(sa, "/finance/prbaseline", {"plant": "GTO1", "value": "1.7"})
    assert "nothing saved" in r.get_data(as_text=True)
    post(sa, "/finance/sla", {"plant": "GTO1", "value": "0.2"})
    post(sa, "/finance/om", {"plant": "NOPE", "amount": "10"})
    assert psql("SELECT pr_baseline, sla_target FROM plant WHERE plant_key = 'GTO1'") == before


def test_loan_edits(sa, psql):
    post(sa, "/finance/principal", {"loan_id": "L-GTO1", "amount": "5100000"})
    assert psql("SELECT principal_mxn FROM loan WHERE loan_id = 'L-GTO1'") == [["5100000.00"]]
    last = psql("SELECT to_char(max(ref_month), 'YYYY-MM') FROM loan_schedule WHERE loan_id = 'L-GTO1'")[0][0]
    post(sa, "/finance/payments", {"loan_id": "L-GTO1", "from_month": last, "amount": "81000"})
    assert psql(f"SELECT payment_mxn FROM loan_schedule WHERE loan_id = 'L-GTO1' AND to_char(ref_month,'YYYY-MM') = '{last}'") == [["81000.00"]]
    n = int(psql("SELECT count(*) FROM loan_schedule WHERE loan_id = 'L-GTO1'")[0][0])
    post(sa, "/finance/truncate", {"loan_id": "L-GTO1", "from_month": last})
    assert int(psql("SELECT count(*) FROM loan_schedule WHERE loan_id = 'L-GTO1'")[0][0]) < n


def test_fee_and_tariff_from_a_month(sa, psql):
    # v339: next month, not a fixed October - the form refuses past months by design
    # (min_month = this month), and this test broke when the calendar reached November
    y, m = (int(x) for x in psql("SELECT extract(year FROM now() + interval '1 month')::int,"
                                 " extract(month FROM now() + interval '1 month')::int")[0])
    ym = f"{y}-{m:02d}"
    r1 = post(sa, "/finance/tariff", {"plant": "GTO1", "from_month": ym, "amount": "2.55"})
    assert "nothing saved" not in r1.get_data(as_text=True), r1.get_data(as_text=True)[-600:]
    assert psql(f"SELECT tariff_mxn FROM contract_monthly WHERE plant_key = 'GTO1' AND year = {y} AND month = {m}") == [["2.5500"]]
    r2 = post(sa, "/finance/fee", {"plant": "GTO1", "from_month": ym, "amount": "1000"})
    assert "saved" in r2.get_data(as_text=True), r2.get_data(as_text=True)[-600:]



# ------------------------------------------------------------ v342: the PPA calendar
def test_a_customer_event_without_kwh_uses_the_contract_rule(sa, psql):
    post(sa, "/maint/add", {"plant": "nl1", "start": "2026-10-11T06:00", "end": "2026-10-11T20:00",
                            "category": "customer", "note": "no kwh"})
    assert psql("SELECT deemed_kwh IS NULL FROM maintenance_event WHERE note = 'no kwh'") == [["t"]]
    for bad in ("-5", "abc", "nan"):
        post(sa, "/maint/add", {"plant": "nl1", "start": "2026-10-11T06:00", "category": "customer",
                                "note": "bad kwh", "deemed_kwh": bad})
    assert psql("SELECT count(*) FROM maintenance_event WHERE note = 'bad kwh'") == [["0"]]
    post(sa, "/maint/add", {"plant": "nl1", "start": "2026-10-11T06:00", "end": "2026-10-11T05:00",
                            "category": "argia", "note": "backwards"})
    assert psql("SELECT count(*) FROM maintenance_event WHERE note = 'backwards'") == [["0"]]


def test_the_calendar_logs_a_past_customer_event_and_returns_to_itself(sa, psql):
    back = "/setup/plants/calendar?plant=nl1&month=2026-10"
    r = post(sa, "/maint/add", {"plant": "nl1", "start": "2026-10-11T06:00", "end": "2026-10-11T20:00",
                                "category": "customer", "deemed_kwh": "1859", "note": "customer shutdown",
                                "back": back})
    assert r.status_code == 303 and r.headers["Location"].startswith(back + "&m=")
    row = psql("SELECT plant_key, deemed_kwh, approved_by IS NULL FROM maintenance_event WHERE note = 'customer shutdown'")
    assert row == [["NL1", "1859.0", "t"]]
    mid = psql("SELECT id FROM maintenance_event WHERE note = 'customer shutdown'")[0][0]
    r = post(sa, "/maint/approve", {"id": mid, "back": back})
    assert r.headers["Location"].startswith(back + "&m=")
    page = sa.app.test_client().get("/plants/calendar?plant=nl1&month=2026-10&day=11", headers=ADMIN).get_data(as_text=True)
    assert "Traceback" not in page and "PPA calendar" in page
    assert f"#{mid} customer 1,859.0 kWh agreed" in page
    assert 'name="deemed_kwh"' in page and 'value="2026-10-11T06:00"' in page


def test_the_calendar_only_returns_to_itself(sa):
    r = post(sa, "/maint/add", {"plant": "nl1", "start": "2026-10-11T06:00", "category": "argia",
                                "note": "evil", "back": "https://evil.example/x"})
    assert r.headers["Location"].startswith("/setup/plants/#maintenance".split("#")[0])


def test_the_calendar_is_for_admins_only(sa):
    body = sa.app.test_client().get("/plants/calendar?plant=nl1&month=2026-10", headers=PLAIN).get_data(as_text=True)
    assert "no management rights" in body and "calg" not in body


def test_calendar_pure_helpers(sa):
    assert sa.shift_month("2026-01", -1) == "2025-12" and sa.shift_month("2026-12", 1) == "2027-01"
    assert sa.event_days("2026-10-11 01:01", "2026-10-11 23:00", "2026-10") == ["2026-10-11"]
    assert sa.event_days("2026-09-30 20:00", "2026-10-02 08:00", "2026-10") == ["2026-10-01", "2026-10-02"]
    assert len(sa.event_days("2026-10-30 08:00", "", "2026-10")) == 2              # ongoing: to the month end
    days = {"2026-10-11": {"energy": 0.0, "billable": None}, "2026-10-12": {"energy": 2684.2, "billable": 2684.2}}
    ev = [{"id": "2", "start": "2026-10-11 01:01", "end": "2026-10-11 23:00", "cat": "customer", "kwh": 1859.0,
           "appr": "tomasz", "note": "", "days": ["2026-10-11"]}]
    s = sa.calendar_summary(days, ev, {"produced": 61035.0, "penalty": 0.0, "amount": 123412.77})
    assert s["agreed"] == 1859.0 and s["reissue"] is True
    s = sa.calendar_summary(days, ev, {"produced": 61035.0, "penalty": 1859.0, "amount": 127171.67})
    assert s["reissue"] is False
    # v344: no agreed figure -> the contract rule, annual / 12 / days in the month
    rule = sa.contract_rule(12, 1000973.0, "2026-10")
    assert round(rule["daily"], 1) == 2690.8
    assert sa.contract_rule(10, 862160.0, "2024-10") is None                    # an incomplete contract year
    ev_rule = [dict(ev[0], kwh=None)]
    s = sa.calendar_summary(days, ev_rule, {"produced": 61035.0, "penalty": 1859.0, "amount": 127171.67}, rule)
    assert s["agreed"] == 2690.8 and s["reissue"] is True
    # a partial day: the share of daylight, minus what was measured in it
    days2 = {"2026-10-12": {"energy": 2684.2, "billable": 2684.2}}
    part = {"id": "9", "start": "2026-10-12 06:00", "end": "2026-10-12 13:00", "cat": "customer", "kwh": None,
            "appr": "tomasz", "note": "", "days": ["2026-10-12"]}
    assert sa.daylight_share("2026-10-12 06:00", "2026-10-12 13:00", "2026-10-12") == 0.5
    assert sa.event_compensation(part, days2, rule) == round(2690.78 * 0.5 - 2684.2 * 0.5, 1)
    assert sa.event_compensation(dict(part, cat="argia"), days2, rule) == 0.0
    assert sa.event_compensation(dict(part, appr=""), days2, rule) == 0.0


def test_changing_the_kwh_of_an_approved_event_sends_it_back_to_draft(sa, psql):
    back = "/setup/plants/calendar?plant=mex1&month=2026-10"
    post(sa, "/maint/add", {"plant": "mex1", "start": "2026-10-24T06:00", "end": "2026-10-24T20:00",
                            "category": "customer", "deemed_kwh": "1986.8", "note": "sag day"})
    mid = psql("SELECT id FROM maintenance_event WHERE note = 'sag day'")[0][0]
    post(sa, "/maint/approve", {"id": mid})
    r = post(sa, "/maint/kwh", {"id": mid, "deemed_kwh": "", "back": back})
    assert r.headers["Location"].startswith(back + "&m=")
    row = psql(f"SELECT deemed_kwh IS NULL, approved_by IS NULL, note FROM maintenance_event WHERE id = {mid}")[0]
    assert row[:2] == ["t", "t"] and "compensated kWh 1986.8 kWh -> contract rule" in row[2]
    post(sa, "/maint/kwh", {"id": mid, "deemed_kwh": "2651"})
    assert psql(f"SELECT deemed_kwh FROM maintenance_event WHERE id = {mid}") == [["2651.0"]]
    for bad in ("-1", "x"):
        post(sa, "/maint/kwh", {"id": mid, "deemed_kwh": bad})
    assert psql(f"SELECT deemed_kwh FROM maintenance_event WHERE id = {mid}") == [["2651.0"]]


# ------------------------------------------------------------ v343: names + recalculate button
def test_customer_names_beside_the_codes(sa, psql):
    assert sa.short_customer("SAG PPA roof (CDMX, MEX)") == "SAG"
    assert sa.short_customer("HOLIDAY INN EXPRESS, Turistica Arizona PPA roof (SLP, SLP)") == \
        "HOLIDAY INN EXPRESS, Turistica Arizona"
    assert sa.short_customer("") == ""
    assert sa.plabel("mex1", {"mex1": "SAG"}) == "SAG (MEX1)" and sa.plabel("mex1", {}) == "MEX1"
    names = sa.plant_names()
    assert names and any(pk in sa.ppa_plants() for pk in names)
    page = sa.app.test_client().get("/plants/calendar?plant=nl1&month=2026-10", headers=ADMIN).get_data(as_text=True)
    for pk, n in names.items():
        if pk in sa.ppa_plants():
            assert f"{n} ({pk.upper()})" in page, (pk, n)


def test_recalculate_preview_then_apply(sa, monkeypatch):
    calls = []

    class R:
        def __init__(self, out, rc=0):
            self.stdout, self.stderr, self.returncode = out, "", rc

    def fake(args, timeout):
        calls.append(args)
        if args[-1] == "dry":
            return R("2026-10: plant | ...\n  NL1 |  0.0 -> 1,859.0 | ...   CHANGES\ndry run - nothing written (add --apply)\n")
        return R("Running as unit: argia-reissue-202610-1.service\n")
    monkeypatch.setattr(sa, "_sh", fake)
    back = "/setup/plants/calendar?plant=nl1&month=2026-10"
    page = post(sa, "/plants/calendar/reissue", {"month": "2026-10", "back": back}).get_data(as_text=True)
    assert calls[-1][-2:] == ["2026-10", "dry"]
    assert "NL1 |  0.0 -&gt; 1,859.0" in page and 'name="confirm"' in page and "Apply" in page
    r = post(sa, "/plants/calendar/reissue/apply", {"month": "2026-10", "back": back})   # no confirm tick
    assert len(calls) == 1 and "not+confirmed" in r.headers["Location"].replace("%20", "+")
    r = post(sa, "/plants/calendar/reissue/apply", {"month": "2026-10", "back": back, "confirm": "yes"})
    assert calls[-1][0] == "systemd-run" and calls[-1][-3:] == [sa.REISSUE_SH, "2026-10", "apply"]
    assert any(a.startswith("ARGIA_ACTOR=tomasz") for a in calls[-1])
    assert r.status_code == 303 and r.headers["Location"].startswith(back + "&m=")


def test_recalculate_refuses_a_failed_preview_and_bad_input(sa, monkeypatch):
    class R:
        stdout, stderr, returncode = "Traceback ...", "", 1
    monkeypatch.setattr(sa, "_sh", lambda args, timeout: R())
    page = post(sa, "/plants/calendar/reissue", {"month": "2026-10"}).get_data(as_text=True)
    assert "did not finish cleanly" in page and 'name="confirm"' not in page
    r = post(sa, "/plants/calendar/reissue", {"month": "2026-13"})
    assert "invalid+month" in r.headers["Location"].replace("%20", "+")
    r = post(sa, "/plants/calendar/reissue", {"month": "2026-10"}, headers=PLAIN)
    assert r.status_code != 303 or "calendar" not in r.headers.get("Location", "")


def test_recalculate_log_shows_the_newest_run_of_the_month(sa, tmp_path, monkeypatch):
    log = ("=== 2026-11-02 10:00:00 reissue 2026-10 dry by tomasz\nold\n=== done rc=0\n"
           "=== 2026-11-02 11:00:00 reissue 2026-09 apply by tomasz\nsep\n=== done rc=0\n"
           "=== 2026-11-02 12:00:00 reissue 2026-10 apply by tomasz\nregister updated: MEX1, NL1\n")
    assert sa.log_block(log, "2026-10").endswith("register updated: MEX1, NL1")
    assert sa.log_block(log, "2026-08") == ""
    f = tmp_path / "reissue.log"
    f.write_text(log)
    monkeypatch.setattr(sa, "REISSUE_LOG", str(f))
    assert "RUNNING" in sa.reissue_status("2026-10")
    f.write_text(log + "=== done rc=0\n")
    assert "RUNNING" not in sa.reissue_status("2026-10") and "register updated" in sa.reissue_status("2026-10")
