"""v196 — the daily PDF report is mailed from the server ('reports'
channel), replacing the Report_Outbox -> Apps Script notifier path."""
import logging
import pathlib

from argia.alerts import subscriptions as subs
from argia.report import report_mail as RM

V2 = pathlib.Path(__file__).resolve().parents[2]


class TestChannel:
    def test_reports_is_a_channel_everywhere(self):
        assert "reports" in subs.CHANNELS
        assert "'reports'" in subs.ENSURE_SQL
        assert "DROP CONSTRAINT IF EXISTS mail_subscription_channel_check" in subs.ENSURE_SQL
        setup = (V2 / "server" / "bundle" / "setup_app.py").read_text(encoding="utf-8")
        assert subs.ENSURE_SQL in setup                      # byte-identical copy
        assert "'reports': 'Daily PDF reports" in setup

    def test_recipients_for_accepts_it(self, monkeypatch):
        import argia.store.pgq as pgq
        monkeypatch.setattr(pgq, "psql_rows", lambda sql: [["a@x", ""]])
        assert subs.recipients_for("reports") == [("a@x", None)]


class TestMail:
    def test_subject_body(self):
        s, b = RM.subject_body("2026-09-04", "morning_yesterday")
        assert s == "[ARGIA] Daily report 2026-09-04 — Morning report"
        assert "yesterday's full day" in b
        s, _ = RM.subject_body("2026-09-04", "evening_today")
        assert s.endswith("Evening report")

    def test_fail_closed_without_subscribers(self, monkeypatch, tmp_path, caplog):
        pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF")
        monkeypatch.setattr(RM, "recipients", lambda: [])
        assert RM.send_report(str(pdf), "2026-09-04", "evening_today") is False
        assert "no enabled 'reports' subscribers" in caplog.text

    def test_sends_pdf_attachment(self, monkeypatch, tmp_path):
        from argia.alerts import emailer
        pdf = tmp_path / "ARGIA_Daily_2026-09-04.pdf"; pdf.write_bytes(b"%PDF-1.4 x")
        monkeypatch.setattr(RM, "recipients", lambda: ["t@x", "u@x"])
        monkeypatch.setattr(emailer, "load_smtp", lambda path=None: {"SMTP_HOST": "h", "SMTP_PORT": "25", "SMTP_USER": "svc@x"})
        got = {}
        monkeypatch.setattr(emailer, "send", lambda msg, cfg, timeout=30: got.setdefault("msg", msg) and True)
        assert RM.send_report(str(pdf), "2026-09-04", "morning_yesterday") is True
        msg = got["msg"]
        assert msg["To"] == "t@x, u@x"
        att = [p for p in msg.iter_attachments()]
        assert len(att) == 1 and att[0].get_filename() == "ARGIA_Daily_2026-09-04.pdf"
        assert att[0].get_content_type() == "application/pdf"

    def test_missing_pdf_or_dry_run(self, monkeypatch, tmp_path, caplog):
        assert RM.send_report("/nope.pdf", "2026-09-04", "evening_today") is False
        pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF")
        monkeypatch.setattr(RM, "recipients", lambda: ["t@x"])
        with caplog.at_level(logging.INFO):
            assert RM.send_report(str(pdf), "2026-09-04", "evening_today", dry_run=True) is True
        assert "[DRY RUN] would mail" in caplog.text


class TestWiring:
    def test_report_daily_mails_then_optionally_queues(self):
        src = (V2 / "scripts" / "report_daily.py").read_text(encoding="utf-8")
        assert "send_report(pdf_path, date_iso, kind)" in src
        assert 'os.environ.get("ARGIA_SHEET_OUTBOX", "1")' in src
        assert src.index("send_report(") < src.index("append_outbox(sheets")
