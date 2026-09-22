"""v196 - engine (ledger) alerts are mailed from the server, replacing the
Apps Script notifier that read the Alerts sheet tab.

Locks: only OPEN records without 'email' in channels_sent are candidates;
recipient scoping by plant; identical views share one mail; mailed
records get 'email' marked; no SMTP / no subscribers -> nothing marked
(retry next run); no visible subscriber -> marked handled; the engine
scripts persist the marks; dry-run sends nothing.
"""
import pathlib

import pytest

from argia.alerts import ledger_mail as LM
from argia.core.alerts_state import AlertRecord, AlertState


def _toks(record):
    """v257: channel tokens, ignoring the mailed@<iso> stamp."""
    return {t.strip() for t in (record.channels_sent or "").split(",")
            if t.strip() and not t.strip().startswith("mailed@")}


def _stamp(record):
    from argia.alerts.ledger_mail import last_mailed_at
    return last_mailed_at(record)



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

    def test_one_alert_one_mail(self):
        r = rec(1)
        # v217: metric as a phrase; without a names table the code stays
        subj, text, html = LM.render_mail([r], when_mx="2026-09-04 12:30")
        assert subj == "[ARGIA] 4 Sep - 1 warning (GTO1)"
        assert "WARNING - new\n  GTO1\n    inverter reports a fault code - inverter X1\n      inverter X1: fault 1  (ALT-20260904-001)" in text
        assert "What these mean" in text and "inverter reports a fault code:" in text
        assert text.endswith("Portal: https://portal.argia.com.mx/monitoring/")

    def test_several_alerts_one_mail(self):
        subj, text, html = LM.render_mail([rec(1), rec(2, plant="NL1", sev="CRITICAL")], when_mx="2026-09-04 12:30")
        assert subj == "[ARGIA] 4 Sep - 1 critical, 1 warning (GTO1, NL1)"
        assert text.index("CRITICAL - new") < text.index("NL1") < text.index("WARNING - new") < text.index("GTO1")
        assert text.count("(ALT-20260904-") == 2
        assert html.count(">CRITICAL<") == 2 and html.count(">WARNING<") == 2      # bar + pill each

    def test_views_by_scope(self):
        alerts = [rec(1, "GTO1"), rec(2, "NL1")]
        views = LM.group_views(alerts, [("all@x", None), ("gto@x", frozenset({"GTO1"})),
                                        ("also@x", None), ("qro@x", frozenset({"QRO1"}))])
        assert views == [(["gto@x"], [alerts[0]]), (["all@x", "also@x"], alerts)]

    def test_mark_mailed(self):
        out = LM.mark_mailed([rec(1, sent="sheet"), rec(2)], {"ALT-20260904-001"})
        # v257: the channel list now also carries WHEN it was mailed, so a
        # still-open CRITICAL can be re-mailed on a cadence.
        assert _toks(out[0]) >= {"email", "sheet"} and _stamp(out[0]) is not None
        assert out[1].channels_sent == ""


class TestIO:
    @pytest.fixture(autouse=True)
    def _filter_available(self, monkeypatch):
        # v217: no PG here -> the filter would hold everything (fail closed);
        # these tests are about the mailing, so give it a loaded filter
        from argia.alerts import subscriptions
        monkeypatch.setattr(subscriptions, "load_excluded_plants", lambda: frozenset({"QRO1"}))

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
        assert _toks(out[0]) >= {"email"} and _stamp(out[0]) is not None

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
        assert _toks(out[0]) >= {"email"} and _stamp(out[0]) is not None   # GTO1 went to gto@x
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
            assert "records = mail_new_alerts(result.records, dry_run=args.dry_run," in src
            assert "mailed = records != list(result.records)" in src
        # v223: intraday CRITICAL only; the morning run carries WARNINGs + still-open
        assert 'severities=("CRITICAL",)' in (V2 / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")
        assert "morning=True" in (V2 / "scripts" / "alerts_daily.py").read_text(encoding="utf-8")
        for name in ("alerts_daily.py", "alerts_snapshot.py"):
            src = (V2 / "scripts" / name).read_text(encoding="utf-8")
            assert "write_ledger(sheets, records)" in src
            assert "or mailed" in src


class TestGroupedByPlant:
    """v204 (Tomasz): 'make it divided by plants visually easier to recognize';
    v223: CRITICAL first, plant -> issue type -> inverters on one line,
    explanations once at the bottom, still-open reminder."""

    def _alerts(self):
        return [rec(1, "SLP2"), rec(2, "NL1", sev="CRITICAL"), rec(3, "NL1"),
                rec(5, "GTO1", sev="CRITICAL")]

    def test_grouped_critical_plants_first(self):
        g = LM.grouped(self._alerts())
        assert [(sev, pk) for sev, pk, _ in g] == [("CRITICAL", "GTO1"), ("CRITICAL", "NL1"), ("WARNING", "NL1"), ("WARNING", "SLP2")]
        mets = dict((pk, m) for sev, pk, m in g if sev == "CRITICAL")
        assert [x[0] for x in mets["NL1"]] == ["inverter_fault"] and [a.alert_id for a in mets["NL1"][0][2]] == ["ALT-20260904-002"]

    def test_text_groups_plant_issue_inverters(self):
        labels = {"NL1": "Plastic Omnium", "SLP2": "Holiday Inn Express"}
        subj, text, html = LM.render_mail(self._alerts(), labels, when_mx="2026-09-04 06:30")
        assert subj == "[ARGIA] 4 Sep - 2 critical, 2 warnings (GTO1, Holiday Inn Express, Plastic Omnium)"
        assert "CRITICAL - new\n  GTO1\n" in text and "  Plastic Omnium (NL1)\n    inverter reports a fault code - inverter X2" in text
        assert text.index("CRITICAL - new") < text.index("WARNING - new") < text.index("Holiday Inn Express (SLP2)")
        assert text.count("What these mean") == 1 and text.count("inverter reports a fault code:") == 1   # once, not per alert
        assert "Still open" not in text                                                      # nothing older

    def test_inverters_of_one_issue_share_a_line(self):
        rs = [rec(1, "GTO1"), rec(2, "GTO1"), rec(3, "GTO1")]
        subj, text, html = LM.render_mail(rs, when_mx="2026-09-04 06:30")
        assert "    inverter reports a fault code - inverter X1, inverter X2, inverter X3\n" in text
        assert text.count("      inverter X") == 3 and html.count("font-size:12px") >= 3

    def test_inverters_in_label_order(self):
        from argia.alerts import naming
        n = naming.Names({}, {("GTO1", "X1"): "Inverter 10", ("GTO1", "X2"): "Inverter 2", ("GTO1", "X3"): "Inverter 1"})
        subj, text, html = LM.render_mail([rec(1, "GTO1"), rec(2, "GTO1"), rec(3, "GTO1")], n)
        assert "- Inverter 1 (X3), Inverter 2 (X2), Inverter 10 (X1)\n" in text

    def test_message_is_cleaned(self):
        from argia.alerts import naming
        n = naming.Names({"GTO2": "Hirschmann"}, {("GTO1", "X1"): "Inverter 1"})
        r = rec(1, "GTO1", sev="WARNING")
        r = r.__class__(**{**r.__dict__, "message": "GTO1 X1: day-peak temperature 65.1 degC - no output loss [WARNING]"})
        assert LM.clean_message(r, n) == "day-peak temperature 65.1 degC - no output loss"
        r2 = rec(2, "GTO2", sev="CRITICAL")
        r2 = r2.__class__(**{**r2.__dict__, "inverter_sn": "", "message": "[GTO2] produced 1765 kWh vs 2899 expected (61%) - below 70%"})
        assert LM.clean_message(r2, n) == "produced 1765 kWh vs 2899 expected (61%) - below 70%"

    def test_still_open_reminder_and_no_news_mail(self):
        import datetime as dt
        now = dt.datetime(2026, 9, 7, 12, 31, tzinfo=dt.timezone.utc)
        old = [rec(9, "NL1", sev="CRITICAL", opened="2026-08-26T17:00:00+00:00"),
               rec(10, "NL1", opened="2026-09-05T12:30:00+00:00"),
               rec(11, "PORTFOLIO", sev="CRITICAL")]          # a leftover digest row is never listed
        old[2] = old[2].__class__(**{**old[2].__dict__, "metric": "daily_digest"})
        new = [rec(1, "GTO1")]
        subj, text, html = LM.render_mail(new, {"NL1": "Plastic Omnium"}, still_open=old + new, when_mx="2026-09-07 06:30", now_utc=now)
        assert "Still open from previous days: 1 critical / 1 warning" in text
        assert "  CRITICAL · Plastic Omnium: inverter reports a fault code (inverter X9) - 12 d" in text
        assert "  WARNING · Plastic Omnium: inverter reports a fault code (inverter X10) - 2 d" in text
        assert "daily digest" not in text and "ALT-20260904-001" in text          # the new one is not "still open"
        subj, text, html = LM.render_mail([], {"NL1": "Plastic Omnium"}, still_open=old, when_mx="2026-09-07 06:30", now_utc=now)
        assert subj == "[ARGIA] 7 Sep - nothing new - 1 critical still open" and "What these mean" not in text

    def test_html_escapes_and_colours(self):
        alerts = self._alerts()
        alerts[0] = alerts[0].__class__(**{**alerts[0].__dict__, "message": "<b>x</b> & y"})
        subj, text, html = LM.render_mail(alerts, {"NL1": "Plastic Omnium"})
        assert html.count("border-left:6px solid") == 2                     # one bar per severity
        assert "Plastic Omnium (NL1)" in html and "&lt;b&gt;x&lt;/b&gt; &amp; y" in html
        assert "#c5221f" in html and "#a05c00" in html

    def test_unmailed_filters_severities_and_digest(self):
        rs = [rec(1, sev="WARNING"), rec(2, sev="CRITICAL"), rec(3, sev="INFO")]
        rs.append(rec(4, "PORTFOLIO", sev="CRITICAL").__class__(**{**rec(4, "PORTFOLIO", sev="CRITICAL").__dict__, "metric": "daily_digest"}))
        assert [r.alert_id for r in LM.unmailed(rs)] == ["ALT-20260904-001", "ALT-20260904-002"]
        assert [r.alert_id for r in LM.unmailed(rs, ("CRITICAL",))] == ["ALT-20260904-002"]

    def test_morning_mail_without_news_only_for_open_criticals(self, monkeypatch, caplog):
        import logging
        from argia.alerts import subscriptions
        monkeypatch.setattr(subscriptions, "load_excluded_plants", lambda: frozenset({"QRO1"}))
        monkeypatch.setattr(LM, "recipients", lambda: [("all@x", None)])
        with caplog.at_level(logging.INFO):
            LM.mail_new_alerts([rec(1, sent="email", sev="CRITICAL")], dry_run=True, morning=True, when_mx="2026-09-07 06:30")
        assert "would mail all@x: [ARGIA] 7 Sep - nothing new - 1 critical still open" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.INFO):
            LM.mail_new_alerts([rec(1, sent="email", sev="WARNING")], dry_run=True, morning=True, when_mx="2026-09-07 06:30")
        assert "would mail" not in caplog.text                                  # open warnings alone: silence
        with caplog.at_level(logging.INFO):
            LM.mail_new_alerts([rec(2, sev="WARNING")], dry_run=True, severities=("CRITICAL",))
        assert "would mail" not in caplog.text                                  # intraday: a WARNING waits for the morning

    def test_short_customer(self):
        assert LM.short_customer("PLASTIC OMNIUM PPA land (Monterrey, NL)") == "Plastic Omnium"
        assert LM.short_customer("HOLIDAY INN EXPRESS, Turistica Arizona PPA roof (SLP, SLP)") == "Holiday Inn Express"
        assert LM.short_customer("TAIGENE PPA roof (Leon, GTO)") == "Taigene"
        assert LM.short_customer("SAG PPA roof (CDMX, MEX)") == "SAG"      # acronyms stay upper
        assert LM.short_customer("") == ""

    def test_mailer_sends_html_and_filters_portfolios(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "argia" / "alerts" / "ledger_mail.py"
               ).read_text(encoding="utf-8")
        assert "emailer.build_html_email(subject, body, html, cfg[\"SMTP_USER\"], emails)" in src
        assert "subscriptions.is_mailable(r.plant_key, excluded)" in src


class TestRemailAnUnresolvedCritical:
    """v257 - SAG went dark at 12:46 on 2026-09-16, one mail went out at
    13:30, and nothing more was said while the plant sat at zero all
    afternoon. A CRITICAL that is still open is still costing money."""

    import datetime as _dt
    T0 = _dt.datetime(2026, 9, 16, 19, 30, tzinfo=_dt.timezone.utc)   # 13:30 MX

    def _rec(self, **kw):
        from argia.core.alerts_state import AlertRecord, AlertState
        base = dict(alert_id="a1", alert_key="mex1:plant:plant_offline", plant_key="MEX1",
                    inverter_sn="", metric="plant_offline", severity="CRITICAL",
                    state=AlertState.OPEN, opened_utc=self.T0.isoformat(),
                    last_seen_utc=self.T0.isoformat(), resolved_utc="", value=0.0,
                    threshold=None, message="MEX1: ALL 3 at 0 W", channels_sent="")
        base.update(kw)
        return AlertRecord(**base)

    def test_the_stamp_round_trips(self):
        import datetime as dt
        from argia.alerts.ledger_mail import last_mailed_at, stamp_mailed
        r = stamp_mailed(self._rec(), self.T0)
        assert "email" in r.channels_sent and last_mailed_at(r) == self.T0
        assert last_mailed_at(self._rec(channels_sent="email")) is None
        assert last_mailed_at(self._rec(channels_sent="email,mailed@nonsense")) is None

    def test_it_is_re_mailed_once_the_cadence_has_passed(self):
        import datetime as dt
        from argia.alerts.ledger_mail import due_for_remail, stamp_mailed
        r = stamp_mailed(self._rec(), self.T0)
        assert due_for_remail([r], self.T0 + dt.timedelta(hours=2), 15) == []
        assert len(due_for_remail([r], self.T0 + dt.timedelta(hours=3), 16)) == 1
        assert len(due_for_remail([r], self.T0 + dt.timedelta(hours=5), 18)) == 1

    def test_never_outside_daylight(self):
        """Nobody is woken at 03:00 for a plant that cannot produce anyway."""
        import datetime as dt
        from argia.alerts.ledger_mail import due_for_remail, stamp_mailed
        r = stamp_mailed(self._rec(), self.T0)
        late = self.T0 + dt.timedelta(hours=12)
        assert due_for_remail([r], late, 3) == []
        assert due_for_remail([r], late, 6) == []
        assert due_for_remail([r], late, 19) == []
        assert len(due_for_remail([r], late, 7)) == 1
        assert len(due_for_remail([r], late, 18)) == 1

    def test_only_open_criticals_and_never_the_unmailed_or_the_digest(self):
        import datetime as dt
        from argia.core.alerts_state import AlertState
        from argia.alerts.ledger_mail import DIGEST_METRIC, due_for_remail, stamp_mailed
        later = self.T0 + dt.timedelta(hours=4)
        assert due_for_remail([stamp_mailed(self._rec(severity="WARNING"), self.T0)], later, 16) == []
        assert due_for_remail([stamp_mailed(self._rec(state=AlertState.RESOLVED), self.T0)], later, 16) == []
        assert due_for_remail([stamp_mailed(self._rec(metric=DIGEST_METRIC), self.T0)], later, 16) == []
        # never mailed at all -> unmailed() owns it, not the re-mailer
        assert due_for_remail([self._rec()], later, 16) == []

    def test_re_mailing_pushes_the_clock_forward_so_it_does_not_repeat_every_tick(self):
        import datetime as dt
        from argia.alerts.ledger_mail import due_for_remail, stamp_mailed
        r = stamp_mailed(self._rec(), self.T0)
        t1 = self.T0 + dt.timedelta(hours=4)
        assert len(due_for_remail([r], t1, 16)) == 1
        r2 = stamp_mailed(r, t1)                    # it just went out again
        assert due_for_remail([r2], t1 + dt.timedelta(minutes=30), 17) == []
        assert len(due_for_remail([r2], t1 + dt.timedelta(hours=3), 18)) == 1
