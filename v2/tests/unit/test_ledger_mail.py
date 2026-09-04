"""v196 — engine (ledger) alerts are mailed from the server, replacing the
Apps Script notifier that read the Alerts sheet tab.

Locks: only OPEN records without 'email' in channels_sent are candidates;
recipient scoping by plant; identical views share one mail; mailed
records get 'email' marked; no SMTP / no subscribers -> nothing marked
(retry next run); no visible subscriber -> marked handled; the engine
scripts persist the marks; dry-run sends nothing.
"""
import pathlib

from argia.alerts import ledger_mail as LM
from argia.core.alerts_state import AlertRecord, AlertState

V2 = pathlib.Path(__file__).resolve().parents[2]


def rec(i, plant="GTO1", state=AlertState.OPEN, sent="", sev="WARNING", opened="2026-09-04T18:30:07+00:00"):
    return AlertRecord(alert_id=f"ALT-20260904-{i:03d}", alert_key=f"{plant.lower()}:inv:x{i}:inverter_fault",
                       plant_key=plant, inverter_sn=f"X{i}", metric="inverter_fault", severity=sev,
                       state=state, opened_utc=opened, last_seen_utc=opened, resolved_utc="",
                       value=1.0, threshold=0.0, message=f"fault {i}", channels_sent=sent,
                       explanation="check it")


class TestPure:
    def test_candidates_are_open_and_unmailed_oldest_first(self):
        rs = [rec(2, opened="2026-09-04T19:00:00+00:00"), rec(1), rec(3, sent="email"),
              rec(4, state=AlertState.RESOLVED), rec(5, sent="sheet")]
        assert [r.alert_id for r in LM.unmailed(rs)] == ["ALT-20260904-001", "ALT-20260904-005",
                                                        "ALT-20260904-002"]

    def test_subject_and_body_like_the_notifier(self):
        r = rec(1)
        assert LM.subject_for(r) == "[ARGIA] WARNING — GTO1 inverter_fault"
        b = LM.body_for(r)
        assert "Alert:      ALT-20260904-001" in b and "Plant:      GTO1  inverter X1" in b
        assert "Value:      1.0 (threshold 0.0)" in b and b.endswith("fault 1\n\ncheck it\n")

    def test_several_alerts_one_mail(self):
        subj, body = LM.digest_body([rec(1), rec(2, plant="NL1", sev="CRITICAL")])
        assert subj == "[ARGIA] CRITICAL — 2 new alerts (GTO1, NL1)"
        assert body.count("Alert:      ") == 2

    def test_views_by_scope(self):
        alerts = [rec(1, "GTO1"), rec(2, "NL1")]
        views = LM.group_views(alerts, [("all@x", None), ("gto@x", frozenset({"GTO1"})),
                                        ("also@x", None), ("qro@x", frozenset({"QRO1"}))])
        assert views == [(["gto@x"], [alerts[0]]), (["all@x", "also@x"], alerts)]

    def test_mark_mailed(self):
        out = LM.mark_mailed([rec(1, sent="sheet"), rec(2)], {"ALT-20260904-001"})
        assert out[0].channels_sent == "email,sheet" and out[1].channels_sent == ""


class TestIO:
    def test_no_smtp_marks_nothing(self, monkeypatch):
        monkeypatch.setattr(LM, "recipients", lambda: [("a@x", None)])
        from argia.alerts import emailer
        monkeypatch.setattr(emailer, "load_smtp", lambda path=None: None)
        out = LM.mail_new_alerts([rec(1)])
        assert out[0].channels_sent == ""

    def test_no_subscribers_marks_nothing(self, monkeypatch, caplog):
        monkeypatch.setattr(LM, "recipients", lambda: [])
        out = LM.mail_new_alerts([rec(1)])
        assert out[0].channels_sent == "" and "no 'maintenance' subscribers" in caplog.text

    def test_scoped_subscribers_that_see_nothing_mark_handled(self, monkeypatch):
        monkeypatch.setattr(LM, "recipients", lambda: [("qro@x", frozenset({"QRO1"}))])
        out = LM.mail_new_alerts([rec(1, "GTO1")])
        assert out[0].channels_sent == "email"

    def test_send_marks_only_delivered(self, monkeypatch):
        from argia.alerts import emailer
        monkeypatch.setattr(LM, "recipients", lambda: [("all@x", None), ("gto@x", frozenset({"GTO1"}))])
        monkeypatch.setattr(emailer, "load_smtp", lambda path=None: {"SMTP_HOST": "h", "SMTP_PORT": "25", "SMTP_USER": "svc@x"})
        sent = []
        def fake_send(msg, cfg, timeout=30):
            sent.append(msg["To"]); return msg["To"] != "all@x"     # the all-plants mail fails
        monkeypatch.setattr(emailer, "send", fake_send)
        out = LM.mail_new_alerts([rec(1, "GTO1"), rec(2, "NL1")])
        assert sent == ["gto@x", "all@x"]
        assert out[0].channels_sent == "email"          # GTO1 went to gto@x
        assert out[1].channels_sent == ""               # NL1 only in the failed mail -> retry

    def test_dry_run_sends_nothing_marks_nothing(self, monkeypatch, caplog):
        from argia.alerts import emailer
        monkeypatch.setattr(LM, "recipients", lambda: [("all@x", None)])
        monkeypatch.setattr(emailer, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
        import logging
        with caplog.at_level(logging.INFO):
            out = LM.mail_new_alerts([rec(1)], dry_run=True)
        assert out[0].channels_sent == "" and "[DRY RUN] would mail all@x" in caplog.text


class TestWiring:
    def test_engine_scripts_mail_then_persist_the_marks(self):
        for name in ("alerts_daily.py", "alerts_snapshot.py"):
            src = (V2 / "scripts" / name).read_text(encoding="utf-8")
            assert "records = mail_new_alerts(result.records, dry_run=args.dry_run)" in src
            assert "mailed = records != list(result.records)" in src
            assert "write_ledger(sheets, records)" in src
            assert "or mailed" in src
