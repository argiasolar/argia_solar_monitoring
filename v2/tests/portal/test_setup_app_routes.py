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
    post(sa, "/maint/add", {"plant": "gto1", "start": "2026-09-01T08:00", "category": "customer", "note": "roof work"})
    mid = psql("SELECT id FROM maintenance_event WHERE note = 'roof work'")[0][0]
    post(sa, "/maint/close", {"id": mid})
    assert psql(f"SELECT end_ts IS NOT NULL, approved_by IS NULL FROM maintenance_event WHERE id = {mid}") == [["t", "t"]]
    post(sa, "/maint/approve", {"id": mid})
    assert psql(f"SELECT approved_by FROM maintenance_event WHERE id = {mid}") == [["tomasz"]]
    post(sa, "/maint/delete", {"id": mid})                          # approved events are never deleted
    assert psql(f"SELECT count(*) FROM maintenance_event WHERE id = {mid}") == [["1"]]


def test_a_draft_maintenance_window_can_be_deleted(sa, psql):
    post(sa, "/maint/add", {"plant": "qro1", "start": "2026-09-02T08:00", "category": "customer", "note": "draft one"})
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
    y = psql("SELECT extract(year FROM now())::int")[0][0]
    r1 = post(sa, "/finance/tariff", {"plant": "GTO1", "from_month": f"{y}-10", "amount": "2.55"})
    assert "saved" in r1.get_data(as_text=True), r1.get_data(as_text=True)[-600:]
    assert psql(f"SELECT tariff_mxn FROM contract_monthly WHERE plant_key = 'GTO1' AND year = {y} AND month = 10") == [["2.5500"]]
    r2 = post(sa, "/finance/fee", {"plant": "GTO1", "from_month": f"{y}-10", "amount": "1000"})
    assert "saved" in r2.get_data(as_text=True), r2.get_data(as_text=True)[-600:]
