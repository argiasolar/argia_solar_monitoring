"""Unit tests — argia.alerts.emailer + argia.alerts.monitor (pure parts)."""

import datetime as dt

from argia.alerts.emailer import build_email, parse_smtp_config
from argia.alerts.monitor import (
    SEV_CRIT,
    SEV_WARN,
    Alert,
    infra_alerts,
    plan_sends,
    plant_alerts,
    recon_alerts,
    render_body,
)

UTC = dt.timezone.utc


# ----------------------------------------------------------- smtp config
def test_parse_smtp_config_complete():
    cfg = parse_smtp_config(
        "SMTP_HOST=smtp.gmail.com\nSMTP_PORT=587\n"
        "SMTP_USER=service@argia.com.mx\nSMTP_PASS=abcd efgh\n# comment\n")
    assert cfg["SMTP_PORT"] == "587"
    assert cfg["SMTP_PASS"] == "abcd efgh"


def test_parse_smtp_config_incomplete_is_none():
    assert parse_smtp_config("SMTP_HOST=x\nSMTP_PORT=587\n") is None
    assert parse_smtp_config("") is None
    assert parse_smtp_config("SMTP_HOST=x\nSMTP_PORT=587\n"
                             "SMTP_USER=u\nSMTP_PASS=\n") is None


def test_build_email():
    msg = build_email("subject", "body", "service@argia.com.mx",
                      ["a@x.mx", "b@x.mx"])
    assert msg["Subject"] == "subject"
    assert "service@argia.com.mx" in msg["From"]
    assert msg["To"] == "a@x.mx, b@x.mx"
    assert "body" in msg.get_content()


# ------------------------------------------------------------ conditions
def test_plant_alerts_only_in_window():
    fresh = {"GTO1": 60.0, "MEX1": 5.0, "QRO1": None}
    assert plant_alerts(fresh, in_window=False) == []
    keys = {a.key for a in plant_alerts(fresh, in_window=True)}
    assert keys == {"plant-stale:GTO1", "plant-dark:QRO1"}


def test_infra_alerts():
    out = infra_alerts([("argia-kpi.service", "exit 1")], 91.0, False)
    keys = {a.key for a in out}
    assert keys == {"unit-failed:argia-kpi.service", "disk-full",
                    "postgres-down"}
    assert not infra_alerts([], 50.0, True)


def test_recon_alerts():
    out = recon_alerts([("GTO1", "2026-08-25", "counters disagree")])
    assert out[0].key == "recon-fail:GTO1:2026-08-25"
    assert out[0].severity == SEV_WARN


# ------------------------------------------------------------ send plan
def _a(key):
    return Alert(key, SEV_CRIT, key, key)


def test_plan_sends_new_alert_fires():
    now = dt.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    send, rec = plan_sends([_a("x")], {}, now)
    assert [a.key for a in send] == ["x"] and rec == []


def test_plan_sends_active_waits_for_resend_window():
    now = dt.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    recent = {"x": (now - dt.timedelta(hours=1), True)}
    old = {"x": (now - dt.timedelta(hours=7), True)}
    assert plan_sends([_a("x")], recent, now)[0] == []
    assert [a.key for a in plan_sends([_a("x")], old, now)[0]] == ["x"]


def test_plan_sends_recovery():
    now = dt.datetime(2026, 8, 27, 12, 0, tzinfo=UTC)
    state = {"gone": (now - dt.timedelta(hours=1), True),
             "already-cleared": (now - dt.timedelta(hours=1), False)}
    send, rec = plan_sends([], state, now)
    assert send == [] and rec == ["gone"]


def test_render_body_sections():
    body = render_body(
        [Alert("k1", SEV_CRIT, "GTO1 stale", "detail1"),
         Alert("k2", SEV_WARN, "disk 90%", "detail2")],
        ["plant-stale:MEX1"], "2026-08-27 07:00")
    assert "CRITICAL:" in body and "GTO1 stale" in body
    assert "WARNING:" in body and "disk 90%" in body
    assert "RECOVERED:" in body and "plant-stale:MEX1" in body
    assert "monitoring.argia.com.mx" in body
