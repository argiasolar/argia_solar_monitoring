"""v321 - the Prologis alarm engine (argia/prologis/alarms.py): business
hours and Mexican public holidays, the alarm rules on synthetic snapshots,
open / escalate / clear, triage and its 4-business-hour clock, the outbox
and its mail-safety rules, the daily review log, and full engine runs on
the SAMPLE metering of the synthetic registry."""
from __future__ import annotations

import datetime as dt
import pathlib
import random

import pytest

from argia.prologis import alarms as AL
from argia.prologis import registry as R
from argia.prologis import store as S

V2 = pathlib.Path(__file__).resolve().parents[2]
RG = R.load(str(V2 / "tests" / "fixtures" / "prologis" / "registry.json"))


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    yield con
    con.close()


def L(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def U(local: str) -> str:
    """MX local -> stored UTC string."""
    return (L(local) + dt.timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ business hours
def test_mexican_public_holidays():
    h = AL.mx_holidays(2026)
    assert h == {dt.date(2026, 1, 1), dt.date(2026, 2, 2), dt.date(2026, 3, 16), dt.date(2026, 5, 1),
                 dt.date(2026, 9, 16), dt.date(2026, 11, 16), dt.date(2026, 12, 25)}
    assert dt.date(2030, 10, 1) in AL.mx_holidays(2030) and dt.date(2027, 10, 1) not in AL.mx_holidays(2027)
    assert AL.mx_holidays(2027) >= {dt.date(2027, 2, 1), dt.date(2027, 3, 15), dt.date(2027, 11, 15)}


@pytest.mark.parametrize("start, hours, due", [
    ("2026-10-06 10:00", 4, "2026-10-06 14:00"),          # Tuesday, inside
    ("2026-10-06 16:30", 4, "2026-10-07 11:30"),          # spills to the next morning
    ("2026-10-09 16:00", 4, "2026-10-12 11:00"),          # Friday afternoon -> Monday
    ("2026-10-10 02:00", 4, "2026-10-12 13:00"),          # Saturday night -> Monday
    ("2026-10-06 06:00", 4, "2026-10-06 13:00"),          # before opening
    ("2026-10-06 18:00", 4, "2026-10-07 13:00"),          # at closing
    ("2026-09-15 17:00", 4, "2026-09-17 12:00"),          # 16 Sep is a holiday
    ("2026-11-13 17:30", 4, "2026-11-17 12:30"),          # Friday, then the November holiday Monday
    ("2026-12-24 15:00", 9, "2026-12-28 15:00"),          # Christmas Day off
])
def test_add_business_hours(start, hours, due):
    assert AL.add_business_hours(L(start), hours) == L(due)


def test_business_hours_round_trip_random():
    rnd = random.Random(321)
    for _ in range(400):
        start = dt.datetime(2026, 1, 1) + dt.timedelta(minutes=rnd.randint(0, 365 * 24 * 60))
        h = rnd.choice([0.5, 1, 4, 9, 13.25, 40])
        end = AL.add_business_hours(start, h)
        assert AL.business_hours_between(start, end) == pytest.approx(h, abs=1e-6)
        assert AL.is_business_day(end.date()) and AL.BIZ_START <= end.time() <= AL.BIZ_END
    assert AL.business_hours_between(L("2026-10-09 17:00"), L("2026-10-12 10:00")) == pytest.approx(2)
    assert AL.business_hours_between(L("2026-10-12 10:00"), L("2026-10-09 17:00")) == 0


# ------------------------------------------------------------------ rules
def snap(**kw):
    d = dict(site_code="TST001", kwp=600.0, daylight=True, kw=300.0, kw_expected=320.0, basis="sample", data_age_min=5.0)
    d.update(kw)
    return AL.Snapshot(**d)


def kinds(s):
    f, ev = AL.evaluate(s)
    return {x.kind: x for x in f}, ev


def test_healthy_site_raises_nothing_but_is_evaluated():
    f, ev = kinds(snap())
    assert f == {} and ev == {"comm_loss", "production_loss", "frozen_data"}


@pytest.mark.parametrize("age, meter, cls, sev", [(None, None, "COMM_1", "critical"), (45, None, "COMM_1", "critical"),
                                                 (45, 120.0, "COMM_2", "warning"), (45, 0.0, "COMM_1", "critical")])
def test_comm_loss_classes(age, meter, cls, sev):
    f, _ = kinds(snap(data_age_min=age, kw=None, meter_kw=meter))
    assert f["comm_loss"].msa_class == cls and f["comm_loss"].severity == sev
    assert "production_loss" not in f                       # no data is never a production loss


def test_comm_loss_at_night_only_with_a_heartbeat():
    f, ev = kinds(snap(daylight=False, data_age_min=600, kw=None))
    assert f == {} and "comm_loss" not in ev                 # no false CRITICAL at night
    f, ev = kinds(snap(daylight=False, data_age_min=170, kw=None, night_heartbeat=True))
    assert f == {} and "comm_loss" in ev
    f, _ = kinds(snap(daylight=False, data_age_min=200, kw=None, night_heartbeat=True))
    assert f["comm_loss"].msa_class == "COMM_1"


def test_production_loss_ratio_and_class():
    f, _ = kinds(snap(kw=190.0, kw_expected=320.0))          # 59 % of expected
    assert f["production_loss"].kw_lost == 130.0 and f["production_loss"].msa_class == "OUT_100_500"
    assert f["production_loss"].severity == "critical"
    assert "production_loss" not in kinds(snap(kw=193.0, kw_expected=320.0))[0]      # 60.3 %
    f, _ = kinds(snap(kw=10.0, kw_expected=25.0, kwp=100.0))
    assert f["production_loss"].msa_class == "STRING_25" and f["production_loss"].severity == "warning"
    assert kinds(snap(kw=0.0, kw_expected=20.0))[0] == {}   # below 5 % of kWp expected: dawn, not judged


def test_clear_sky_basis_needs_inverter_status():
    f, ev = kinds(snap(basis="clear_sky", kw=50.0, kw_expected=400.0))       # clouds look like a loss
    assert "production_loss" not in f and "production_loss" not in ev
    f, ev = kinds(snap(basis="clear_sky", kw=250.0, kw_expected=400.0, inverters_down=(("INV-02", 200.0),)))
    assert f["production_loss"].kw_lost == pytest.approx(133.3, abs=0.1) and "INV-02" in f["production_loss"].detail


def test_das_checks_meter_sensor_frozen():
    f, ev = kinds(snap(meter_kw=250.0, kw=300.0))
    assert f["meter_mismatch"].severity == "warning" and "meter_mismatch" in ev
    assert "meter_mismatch" not in kinds(snap(meter_kw=280.0, kw=300.0))[0]
    assert "meter_mismatch" not in kinds(snap(meter_kw=0.0, kw=20.0))[1]     # too little production to compare
    f, _ = kinds(snap(irr_sensor=5.0, ghi_clear=900.0, kw=300.0))
    assert "sensor_implausible" in f
    f, _ = kinds(snap(irr_sensor=1500.0, ghi_clear=900.0))
    assert "sensor_implausible" in f
    assert "sensor_implausible" not in kinds(snap(irr_sensor=600.0, ghi_clear=900.0))[0]
    assert "frozen_data" in kinds(snap(frozen_n=6))[0] and "frozen_data" not in kinds(snap(frozen_n=5))[0]


def test_daily_underperformance():
    assert AL.evaluate_day(840, 1000, 1.0).kind == "underperformance"
    assert AL.evaluate_day(850, 1000, 1.0) is None
    assert AL.evaluate_day(100, 1000, 0.94) is None          # missing data is never a measured loss
    assert AL.evaluate_day(0, 0, 1.0) is None


# ------------------------------------------------------------------ open, escalate, clear
def _apply(c, s, t, res=None):
    res = res or AL.RunResult()
    f, ev = AL.evaluate(s)
    AL.apply(c, s, U(t), f, ev, res)
    return res


def test_open_escalate_clear(c):
    r = _apply(c, snap(kw=280.0, kw_expected=320.0, kwp=600.0, inverters_down=(("INV-3", 40.0),)), "2026-10-06 10:00")
    assert len(r.opened) == 1
    a = AL.alarm(c, r.opened[0])
    assert a["severity"] == "warning" and a["msa_class"] == "STRING_25"
    r = _apply(c, snap(kw=150.0, kw_expected=320.0, inverters_down=(("INV-3", 40.0), ("INV-4", 300.0))), "2026-10-06 10:05")
    assert r.opened == [] and r.escalated == [a["id"]]
    a = AL.alarm(c, a["id"])
    assert a["severity"] == "critical" and a["msa_class"] == "OUT_100_500" and a["detected_utc"] == U("2026-10-06 10:00")
    _apply(c, snap(daylight=False, kw=0.0), "2026-10-06 19:30")                 # night: not evaluable, stays open
    assert AL.open_alarms(c)[0]["misses"] == 0
    _apply(c, snap(), "2026-10-07 09:00")
    assert len(AL.open_alarms(c)) == 1                                           # one clean run is not enough
    r = _apply(c, snap(), "2026-10-07 09:05")
    assert r.cleared == [a["id"]] and AL.open_alarms(c) == []
    a = AL.alarm(c, a["id"])
    assert a["triage"] == ""                                                      # lasted a day: still needs triage


def test_short_blip_clears_itself(c):
    r = _apply(c, snap(data_age_min=40, kw=None), "2026-10-06 11:00")
    _apply(c, snap(), "2026-10-06 11:05")
    _apply(c, snap(), "2026-10-06 11:10")
    a = AL.alarm(c, r.opened[0])
    assert a["cleared_utc"] and a["triage"] == "self_cleared" and a["triaged_by"] == "system"
    assert AL.triage_state(a, L("2026-10-08 00:00")) == "auto"


def test_triage_actions_and_clock(c):
    r = _apply(c, snap(kw=150.0), "2026-10-09 16:00")                       # Friday 16:00 -> due Monday 11:00
    a = AL.alarm(c, r.opened[0])
    assert AL.triage_due_local(a["detected_utc"]) == L("2026-10-12 11:00")
    assert AL.triage_state(a, L(U("2026-10-12 10:59"))) == "running"
    assert AL.triage_state(a, L(U("2026-10-12 11:01"))) == "breached"
    with pytest.raises(ValueError, match="reason"):
        AL.triage(c, a, "dismiss", "op")
    with pytest.raises(ValueError, match="no such ticket"):
        AL.triage(c, a, "link", "op", ticket_number="PL-0404")
    num = AL.triage(c, a, "ticket", "op", "crew informed")
    t = c.execute("SELECT * FROM tickets WHERE number=?", (num,)).fetchone()
    assert t["detected_utc"] == a["detected_utc"] and t["sla_class"] == "OUT_100_500" and t["kw_lost"] == a["kw_lost"]
    assert f"From alarm #{a['id']}" in t["description"]
    a = AL.alarm(c, a["id"])
    assert a["ticket_id"] == t["id"] and a["triage"] == "ticket"
    with pytest.raises(ValueError, match="already triaged"):
        AL.triage(c, a, "dismiss", "op", "x")
    r2 = _apply(c, snap(site_code="TST002", data_age_min=90, kw=None), "2026-10-09 16:05")
    b = AL.alarm(c, r2.opened[0])
    assert AL.triage(c, b, "link", "op", "same storm", ticket_number=num.lower()) == num
    S.set_status(c, t, "CLOSED", "op")
    r3 = _apply(c, snap(site_code="TST003", data_age_min=90, kw=None), "2026-10-09 16:10")
    with pytest.raises(ValueError, match="not open"):
        AL.triage(c, AL.alarm(c, r3.opened[0]), "link", "op", ticket_number=num)
    assert len(c.execute("SELECT * FROM audit WHERE action='alarm_triage'").fetchall()) == 2


# ------------------------------------------------------------------ settings, recipients, outbox
def test_settings_and_preferences(c):
    assert AL.setting(c, "mail_mode") == "dry_run"
    with pytest.raises(ValueError):
        AL.set_setting(c, "mail_mode", "loud", "ad")
    with pytest.raises(ValueError, match="not an e-mail"):
        AL.set_setting(c, "desk_emails", "desk@argia.com.mx, nonsense", "ad")
    AL.set_setting(c, "desk_emails", "Desk@argia.com.mx; desk@argia.com.mx\nops@argia.com.mx", "ad")
    assert AL.setting(c, "desk_emails") == "desk@argia.com.mx, ops@argia.com.mx"
    with pytest.raises(ValueError):
        AL.set_setting(c, "nope", "1", "ad")
    for u, role, pref in (("m1", "manager", "critical"), ("m2", "manager", "all"), ("v1", "viewer", "all"), ("m3", "manager", "")):
        S.create_user(c, u, u, f"{u}@prologis.test", "Prologis", role, "t")
        if pref:
            AL.set_mail_pref(c, u, pref)
    S.set_disabled(c, "v1", True, "ad")
    with pytest.raises(ValueError):
        AL.set_mail_pref(c, "m1", "sms")
    assert AL.recipients(c, "critical") == ["desk@argia.com.mx", "ops@argia.com.mx", "m1@prologis.test", "m2@prologis.test"]
    assert AL.recipients(c, "digest") == ["desk@argia.com.mx", "ops@argia.com.mx", "m2@prologis.test"]
    assert AL.recipients(c, "owner") == ["desk@argia.com.mx", "ops@argia.com.mx", "m1@prologis.test", "m2@prologis.test"]
    acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
    assert acts.count("setting") == 1 and acts.count("alarm_mail_pref") == 3


def test_outbox_never_mails_sample_or_dry_run(c):
    sent = []

    def fake(msg, cfg):
        sent.append(msg["To"])
        return "fail" not in msg["To"]
    cfg = {"SMTP_HOST": "h", "SMTP_PORT": "587", "SMTP_USER": "service@argia.com.mx", "SMTP_PASS": "x"}
    assert AL.queue(c, "alarm", "a", "s", "b", [], False) and c.execute("SELECT status FROM outbox").fetchone()[0] == "no_recipient"
    AL.queue(c, "alarm", "b", "s", "b", ["a@x.test"], False)
    AL.queue(c, "alarm", "c", "s", "b", ["a@x.test"], True)
    assert AL.deliver(c, cfg, fake) == (0, 0) and sent == []                  # dry-run sends nothing
    AL.set_setting(c, "mail_mode", "live", "ad")
    AL.queue(c, "alarm", "d", "s", "b", ["a@x.test"], True)                    # SAMPLE: never mailed, even live
    AL.queue(c, "alarm", "e", "s", "b", ["ok@x.test"], False)
    AL.queue(c, "alarm", "f", "s", "b", ["fail@x.test"], False)
    assert AL.deliver(c, None, fake) == (0, 0)                                 # no SMTP config: stays pending
    assert AL.deliver(c, cfg, fake) == (1, 1) and sent == ["ok@x.test", "fail@x.test"]
    st = dict(c.execute("SELECT ref, status FROM outbox").fetchall())
    assert st == {"a": "no_recipient", "b": "dry_run", "c": "sample", "d": "sample", "e": "sent", "f": "failed"}
    assert AL.deliver(c, cfg, fake) == (0, 0)                                  # nothing is sent twice


# ------------------------------------------------------------------ daily review
def test_daily_review_rules(c):
    today = dt.date(2026, 10, 10)
    st = AL.review_status(c, today, 5)
    assert {s for _, s in st} == {"before_start"}
    with pytest.raises(ValueError, match="future"):
        AL.save_review(c, "2026-10-11", "op", "x", 7, {}, today)
    with pytest.raises(ValueError, match="3 days late"):
        AL.save_review(c, "2026-10-06", "op", "x", 7, {}, today)
    with pytest.raises(ValueError, match="what was found"):
        AL.save_review(c, "2026-10-08", "op", " ", 7, {}, today)
    AL.save_review(c, "2026-10-08", "op", "no findings", 7, {"sites": []}, today)
    assert AL.setting(c, "review_start") == "2026-10-08"
    with pytest.raises(ValueError, match="already reviewed"):
        AL.save_review(c, "2026-10-08", "op", "again", 7, {}, today)
    AL.save_review(c, "2026-10-10", "op", "INV-2 at site A low, ticket opened", 7, {}, today)
    st = dict(AL.review_status(c, today, 5))
    assert st == {"2026-10-10": "on_time", "2026-10-09": "missing", "2026-10-08": "late",
                  "2026-10-07": "before_start", "2026-10-06": "before_start"}
    assert AL.review_compliance(st.items()) == pytest.approx(1 / 3)
    assert dict(AL.review_status(c, dt.date(2026, 10, 11), 1)) == {"2026-10-11": "today"}


# ------------------------------------------------------------------ full runs on SAMPLE data
def test_run_on_sample_inverter_fault_day(c):
    AL.set_setting(c, "desk_emails", "desk@argia.com.mx", "ad")
    out = AL.run(c, RG, L("2026-10-06 09:00"))
    assert out["sites"] == 7 and out["opened"] == 0
    out = AL.run(c, RG, L("2026-10-06 12:00"))                                # TST005 loses a third from 10:00
    op = AL.open_alarms(c)
    assert out["opened"] == 1 and [(a["site_code"], a["kind"], a["sample"]) for a in op] == [("TST005", "production_loss", 1)]
    assert op[0]["severity"] == "warning" and op[0]["msa_class"] == "STRING_100" and op[0]["kw_lost"] == pytest.approx(50.8, abs=0.5)
    assert out["queued"] == 0 and c.execute("SELECT count(*) FROM outbox WHERE kind='alarm'").fetchone()[0] == 0  # warnings wait for the digest
    assert c.execute("SELECT count(*) FROM outbox WHERE kind='digest'").fetchone()[0] == 1          # the 09:00 run sent the day's digest
    assert AL.run(c, RG, L("2026-10-06 12:05"))["opened"] == 0                 # deduplicated
    AL.run(c, RG, L("2026-10-07 07:25"))
    assert c.execute("SELECT count(*) FROM outbox WHERE kind='digest'").fetchone()[0] == 1          # not before 07:30
    AL.run(c, RG, L("2026-10-07 08:00"))                                      # digest once after 07:30
    AL.run(c, RG, L("2026-10-07 08:05"))
    dg = c.execute("SELECT * FROM outbox WHERE kind='digest' ORDER BY id").fetchall()[1:]
    assert "Underperformance: day below 85% of expected - Parque Lago Bldg 5" in dg[0]["body"]   # yesterday's lost third
    assert "Daily performance review 06 Oct: log not started yet" in dg[0]["body"]
    assert not [a for a in AL.open_alarms(c) if a["kind"] == "production_loss"]     # cleared in the morning sun
    assert len(dg) == 1 and "Open alarms: " in dg[0]["body"] and dg[0]["status"] in ("dry_run", "sample")
    AL.run(c, RG, L("2026-10-07 11:00"))
    AL.run(c, RG, L("2026-10-07 11:05"))
    assert all(a["kind"] != "production_loss" for a in AL.open_alarms(c))     # fault gone the next day


def test_run_mails_a_new_critical_alarm_once(c):
    AL.set_setting(c, "desk_emails", "desk@argia.com.mx", "ad")
    AL.set_setting(c, "last_digest_day", "2026-10-06", "system")              # the day's digest already went

    def live_like(site, now_local, i, n):                                    # a measured-data source, not SAMPLE
        if site.code == "TST001":
            return snap(site_code="TST001", kwp=site.kwp, kw=100.0, kw_expected=400.0, basis="irradiance")
        return snap(site_code=site.code, kwp=site.kwp, basis="irradiance")
    out = AL.run(c, RG, L("2026-10-06 21:00"), snapshot=live_like)             # an evening alarm still mails at once
    assert out["opened"] == 1 and out["queued"] == 1
    m = c.execute("SELECT * FROM outbox").fetchone()
    assert m["status"] == "dry_run" and m["to_addrs"] == "desk@argia.com.mx" and "[SAMPLE]" not in m["subject"]
    assert "CRITICAL Production loss - Parque Norte Bldg 1" in m["subject"] and "Estimated loss: 300 kW" in m["body"]
    assert "Triage due by: 07 Oct 2026 13:00" in m["body"]
    assert AL.run(c, RG, L("2026-10-06 21:05"), snapshot=live_like)["queued"] == 0
    assert AL.open_alarms(c)[0]["notified_utc"]


def test_run_on_sample_comm_loss_and_locked_store_cli(c, tmp_path, monkeypatch, capsys):
    AL.run(c, RG, L("2026-10-07 14:00"))
    a = [x for x in AL.open_alarms(c) if x["kind"] == "comm_loss"]
    assert len(a) == 1 and a[0]["site_code"] == "TST003" and a[0]["msa_class"] == "COMM_1"
    AL.run(c, RG, L("2026-10-07 22:00"))                                      # night: stays open, no false clear
    assert [x["id"] for x in AL.open_alarms(c) if x["kind"] == "comm_loss"] == [a[0]["id"]]
    AL.run(c, RG, L("2026-10-08 10:00"))
    AL.run(c, RG, L("2026-10-08 10:05"))
    assert not [x for x in AL.open_alarms(c) if x["kind"] == "comm_loss"]
