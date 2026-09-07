"""v226 — maintenance tickets: the pure rules (lifecycle, priority/SLA,
numbering, recipients, the alert bridge, SQL builders) and the mail
integration ("share the progress instead of repeating the warning")."""
from __future__ import annotations

import datetime as dt

import pytest

from argia.maintenance import tickets as TK

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 8, 12, 30, tzinfo=UTC)


def tk(**kw):
    base = dict(id=7, number="TK-NL1-0007", plant_key="NL1", inverter_sn="JGMAE65009", title="Inverter 1 cooling",
                description="", category="inverter/derating", priority="P2", status="IN_PROGRESS", created_by="tomasz",
                assigned_to="juan", created_at="2026-09-06 14:00:00+00", updated_at="2026-09-07 10:00:00+00",
                followers=["arturo"], alert_keys=["nl1:inv:jgmae65009:inverter_temp_high"])
    base.update(kw)
    return TK.Ticket(**base)


class TestLifecycle:
    def test_transitions(self):
        assert TK.can_transition("NEW", "IN_PROGRESS") and TK.can_transition("IN_PROGRESS", "VERIFICATION")
        assert TK.can_transition("VERIFICATION", "RESOLVED") and TK.can_transition("RESOLVED", "CLOSED")
        assert TK.can_transition("RESOLVED", "IN_PROGRESS")            # re-open
        assert not TK.can_transition("NEW", "RESOLVED")                 # no skipping to resolved
        assert not TK.can_transition("CLOSED", "CLOSED") and not TK.can_transition("X", "NEW")
        assert all(s in TK.STATUS_LABEL for s in TK.TRANSITIONS) and all(t in TK.STATUS_LABEL for v in TK.TRANSITIONS.values() for t in v)

    def test_open_states_and_stamps(self):
        assert [s for s in TK.STATUS_LABEL if TK.is_open(s)] == ["NEW", "IN_PROGRESS", "WAITING", "VERIFICATION"]
        assert TK.STATUS_STAMP["RESOLVED"] == "resolved_at" and "NEW" not in TK.STATUS_STAMP

    def test_priorities_and_sla(self):
        assert [p[0] for p in TK.PRIORITIES] == ["P1", "P2", "P3", "P4"]
        assert TK.SLA_RESOLVE_H == {"P1": 4, "P2": 24, "P3": 72, "P4": 0}
        assert TK.PRIORITY_FOR_SEVERITY["CRITICAL"] == "P2" and TK.PRIORITY_FOR_SEVERITY["WARNING"] == "P3"
        assert TK.CATEGORY_FOR_METRIC["inverter_temp_high"] == "inverter/derating"
        assert all(m in TK.CATEGORY_LABEL for m in TK.CATEGORY_FOR_METRIC.values())


class TestNumbering:
    def test_make_and_parse(self):
        assert TK.make_number("nl1", 7) == "TK-NL1-0007" and TK.make_number("", 12345) == "TK-FLEET-12345"
        assert TK.parse_number("tk-nl1-0007") == ("NL1", 7) and TK.parse_number("ALT-1") is None

    def test_next_number_sql_scopes_to_the_plant(self):
        q = TK.next_number_sql("nl1")
        assert "LIKE 'TK-NL1-%'" in q and "max(substring(number from '\\d+$')::int)" in q


class TestPeopleAndAge:
    def test_participants_and_recipients(self):
        t = tk(followers=["arturo", "juan", "tomasz"])
        assert TK.participants(t) == ["tomasz", "juan", "arturo"]
        assert TK.recipients(t, "juan") == ["tomasz", "arturo"]
        assert TK.recipients(tk(assigned_to="", followers=[]), "tomasz") == []

    def test_age_and_format(self):
        assert TK.fmt_age(dt.timedelta(minutes=45)) == "45 min"
        assert TK.fmt_age(dt.timedelta(hours=5, minutes=7)) == "5 h 07"
        assert TK.fmt_age(dt.timedelta(days=2, hours=4)) == "2 d 4 h"
        assert TK.fmt_age(TK.age(tk(), NOW)) == "1 d 22 h"
        assert TK.age(tk(created_at="garbage"), NOW) == dt.timedelta(0)

    def test_sla_state(self):
        assert TK.sla_state(tk(priority="P4"), NOW) == ("none", "planned work, no SLA clock")
        assert TK.sla_state(tk(priority="P2"), NOW)[0] == "breached"                       # 46 h into a 24 h target
        assert TK.sla_state(tk(priority="P3"), NOW)[0] == "ok"                             # 72 h target, 26 h left
        assert TK.sla_state(tk(priority="P3", created_at="2026-09-05 20:00:00+00"), NOW)[0] == "due"   # 6.5 h left
        st, txt = TK.sla_state(tk(priority="P2", status="RESOLVED", resolved_at="2026-09-07 02:00:00+00"), NOW)
        assert st == "ok" and txt.startswith("resolved in 12 h 00")

    def test_progress_line(self):
        ev = TK.Event(1, 7, "", "juan", "comment", "Filters replaced, verifying tomorrow.\nPhotos attached.")
        assert TK.progress_line(tk(), ev, NOW) == ("TK-NL1-0007 · In progress · juan · 1 d 22 h — last update: "
                                                   "Filters replaced, verifying tomorrow. Photos attached.")
        assert TK.progress_line(tk(assigned_to=""), None, NOW) == "TK-NL1-0007 · In progress · 1 d 22 h"

    def test_title_for_alert(self):
        assert TK.title_for_alert("inverter_temp_high", "Plastic Omnium", "Inverter 1") == "Plastic Omnium · Inverter 1: inverter running hot"
        assert TK.title_for_alert("plant_offline", "SAG", "") == "SAG: plant produced nothing"


class TestSql:
    def test_ensure_and_builders(self):
        for tbl in ("ticket", "ticket_follower", "ticket_event", "ticket_attachment", "ticket_alert"):
            assert f"CREATE TABLE IF NOT EXISTS {tbl} (" in TK.ENSURE_SQL
        ins = TK.insert_ticket_sql("TK-NL1-0007", "nl1", "SN", "O'Brien's inverter", "d", "inverter/fault", "P2", "tomasz", "")
        assert "'NL1'" in ins and "'O''Brien''s inverter'" in ins and ins.endswith("RETURNING id;")
        ev = TK.event_sql(7, "juan", "comment", "hi", {"a": 1})
        assert "INSERT INTO ticket_event" in ev and "'{\"a\": 1}'::jsonb" in ev and "UPDATE ticket SET updated_at = now() WHERE id = 7;" in ev
        with pytest.raises(ValueError):
            TK.event_sql(7, "x", "bogus")
        assert "resolved_at = coalesce(resolved_at, now())" in TK.status_sql(7, "RESOLVED")
        assert "coalesce" not in TK.status_sql(7, "NEW")
        assert "lost_kwh = 12.3" in TK.resolution_sql(7, "environment", "cleaned", 12.34)
        assert "lost_kwh = NULL" in TK.resolution_sql(7, "", "", None)
        assert "ON CONFLICT DO NOTHING" in TK.follow_sql(7, "juan") and "DELETE FROM ticket_follower" in TK.follow_sql(7, "juan", False)
        assert "occurrences = ticket_alert.occurrences + 1" in TK.link_alert_sql(7, "k")
        assert "RETURNING id;" in TK.attachment_sql(7, None, "a.jpg", "x.jpg", 10, "image/jpeg", "juan") and " NULL, 'a.jpg'" in TK.attachment_sql(7, None, "a.jpg", "x.jpg", 10, "image/jpeg", "juan")
        assert "SELECT id, number, plant_key" in TK.SELECT_TICKETS and "AS followers" in TK.SELECT_TICKETS and "AS alert_keys" in TK.SELECT_TICKETS

    def test_rows_from_csv_keeps_newlines(self):
        rows = TK.rows_from_csv('id,ticket_id,ts,actor,kind,body,meta\n1,7,2026-09-07 10:00:00+00,juan,comment,"line 1\nline 2","{""a"": 1}"\n')
        ev = TK.event_from_row(rows[0])
        assert ev.body == "line 1\nline 2" and ev.meta == {"a": 1} and ev.kind == "comment"
        t = TK.ticket_from_row({"id": "7", "number": "TK-NL1-0007", "plant_key": "NL1", "title": "x", "followers": "a,b",
                                "alert_keys": "", "lost_kwh": "12.5", "status": "WAITING"})
        assert t.followers == ["a", "b"] and t.alert_keys == [] and t.lost_kwh == 12.5 and t.open


class TestAlertBridge:
    def test_briefs_only_for_open_tickets(self):
        b = TK.briefs_by_alert([tk(), tk(id=8, number="TK-NL1-0008", status="CLOSED", alert_keys=["k2"])], {7: "on it"})
        assert list(b) == ["nl1:inv:jgmae65009:inverter_temp_high"]
        assert b["nl1:inv:jgmae65009:inverter_temp_high"].number == "TK-NL1-0007" and b["nl1:inv:jgmae65009:inverter_temp_high"].last_update == "on it"

    def test_load_open_briefs_is_fail_soft(self):
        def boom(sql):
            raise RuntimeError("no db")
        assert TK.load_open_briefs(boom) == {}
        csv_t = ("id,number,plant_key,inverter_sn,title,description,category,priority,status,created_by,assigned_to,created_at,"
                 "updated_at,started_at,verification_at,resolved_at,closed_at,root_cause,resolution,lost_kwh,followers,alert_keys\n"
                 "7,TK-NL1-0007,NL1,SN,t,,other,P2,IN_PROGRESS,tomasz,juan,2026-09-06 14:00:00+00,2026-09-07 10:00:00+00,,,,,,,,arturo,k1\n")
        csv_e = "ticket_id,body\n7,Filters replaced\n"
        out = TK.load_open_briefs(lambda sql: csv_e if "ticket_event" in sql else csv_t)
        assert out["k1"].last_update == "Filters replaced" and out["k1"].assigned_to == "juan"


class TestMailIntegration:
    """An alert with an open ticket is reported as the ticket's progress,
    never as a repeated warning."""

    def _rec(self, i, key, plant="NL1", sn="JGMAE65009", sev="WARNING", sent=""):
        from argia.core.alerts_state import AlertRecord, AlertState
        return AlertRecord(alert_id=f"ALT-{i}", alert_key=key, plant_key=plant, inverter_sn=sn, metric="inverter_temp_high",
                           severity=sev, state=AlertState.OPEN, opened_utc="2026-09-07T12:30:00+00:00", last_seen_utc="",
                           resolved_utc="", value=72.0, threshold=70.0, message=f"{plant} {sn}: day-peak 72 degC [WARNING]",
                           channels_sent=sent, explanation="")

    def test_in_hand_replaces_the_warning(self):
        from argia.alerts import ledger_mail as LM, naming
        n = naming.Names({"NL1": "Plastic Omnium"}, {("NL1", "JGMAE65009"): "Inverter 1"})
        briefs = {"k1": TK.TicketBrief("TK-NL1-0007", "IN_PROGRESS", "P2", "juan", "2026-09-06 14:00:00+00", "Filters replaced", True)}
        new = [self._rec(1, "k1"), self._rec(2, "k2")]
        subj, text, html = LM.render_mail(new, n, still_open=new, when_mx="2026-09-08 06:30", now_utc=NOW, tickets=briefs)
        assert subj == "[ARGIA] 8 Sep — 1 warning (Plastic Omnium)"                       # k1 is not counted as new
        assert "In hand — open maintenance tickets\n  Plastic Omnium: TK-NL1-0007 · In progress · juan · 1 d 22 h — last update: Filters replaced (inverter running hot — Inverter 1 (JGMAE65009))" in text
        assert text.count("ALT-1") == 0 and "ALT-2" in text                                 # the handled alert is not listed as new
        assert "Still open" not in text                                                       # nor as still open
        assert 'href="https://portal.argia.com.mx/maintenance/t/TK-NL1-0007/"' in html
        # nothing new but a ticket in hand: the subject says so
        subj, text, _ = LM.render_mail([], n, still_open=[self._rec(1, "k1", sent="email")], when_mx="2026-09-08 06:30", now_utc=NOW, tickets=briefs)
        assert subj == "[ARGIA] 8 Sep — nothing new — 1 ticket in hand"

    def test_closed_ticket_does_not_hide_an_alert(self):
        from argia.alerts import ledger_mail as LM
        briefs = {"k1": TK.TicketBrief("TK-NL1-0007", "CLOSED", "P2", "", "2026-09-01", "", False)}
        subj, text, _ = LM.render_mail([self._rec(1, "k1")], None, when_mx="2026-09-08 06:30", now_utc=NOW, tickets=briefs)
        assert "1 warning" in subj and "In hand" not in text

    def test_scripts_wire_the_bridge(self):
        import pathlib
        v2 = pathlib.Path(__file__).resolve().parents[2]
        d = (v2 / "scripts/alerts_daily.py").read_text(encoding="utf-8")
        assert "tickets = _ticket_briefs()" in d and "_attach_to_tickets(result.opened + result.touched, open_tickets" in d
        assert "tickets=tickets)" in d
        s = (v2 / "scripts/alerts_snapshot.py").read_text(encoding="utf-8")
        assert "TK.load_open_briefs()" in s and "tickets=tickets)" in s
        mg = (v2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert "TICKET_BY_ALERT" in mg and '/maintenance/new/?alert=' in mg and 'data-en="Ticket"' in mg

    def test_attach_to_tickets_dry_run_counts(self, monkeypatch):
        import importlib
        import sys
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
        ad = importlib.import_module("alerts_daily")
        open_t = [tk(alert_keys=["k1"], inverter_sn="JGMAE65009")]
        # by key (k1) and by asset (k2 is the same inverter) — both land on the ticket; k3 is another inverter
        assert ad._attach_to_tickets([self._rec(1, "k1"), self._rec(2, "k2"), self._rec(3, "k3", sn="OTHER")], open_t, dry_run=True) == 2
        assert ad._attach_to_tickets([self._rec(1, "k1")], [], dry_run=True) == 0
        # verification: dry run decides but changes nothing
        v = tk(status="VERIFICATION", alert_keys=["k1"])
        assert ad._verify_tickets([v], [], [self._rec(1, "k1")], NOW, dry_run=True) == 0


class TestV227:
    """v227: e-mail participants, reply-by-mail, verification by the
    data, asset matching, statistics."""

    def test_email_identities(self):
        assert TK.is_email("tech@contractor.mx") and not TK.is_email("juan") and not TK.is_email("a@b")
        assert TK.valid_email(" Tech@Contractor.MX ") == "tech@contractor.mx" and TK.valid_email("nope") == ""
        t = tk(followers=["arturo", "tech@contractor.mx"])
        assert TK.recipients(t, "tomasz") == ["juan", "arturo", "tech@contractor.mx"]
        from argia.maintenance import notify as N
        assert N.address_of("tech@contractor.mx", lambda u: "") == "tech@contractor.mx"
        assert N.address_of("juan", lambda u: "juan@x") == "juan@x"
        subj, text, html = N.render(t, "Juan Perez", "Status: New → In progress", "", "Plastic Omnium", "Inverter 1", "Juan Perez")
        assert subj == "[TK-NL1-0007] Plastic Omnium: Inverter 1 cooling — In progress"
        assert "Reply to this mail to add a comment" in text and "/maintenance/t/TK-NL1-0007/" in html

    def test_parse_ts_is_python310_safe(self):
        assert TK.parse_ts("2026-09-06 14:00:00+00") == dt.datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
        assert TK.parse_ts("2026-09-06 14:00:00.123456+00") == dt.datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
        assert TK.parse_ts("2026-09-06T14:00:00+00:00") == dt.datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
        assert TK.parse_ts("2026-09-06 08:00:00-06") == dt.datetime(2026, 9, 6, 14, 0, tzinfo=UTC)
        assert TK.parse_ts("garbage") is None and TK.parse_ts("") is None

    def test_reply_parsing(self):
        assert TK.reply_ticket_number("Re: [TK-NL1-0007] Plastic Omnium: cooling — In progress") == "TK-NL1-0007"
        assert TK.reply_ticket_number("hello") is None
        body = "Filters replaced today.\nPhotos attached.\n\nOn Mon, Sep 7, 2026 at 9:00 AM ARGIA Monitoring <service@argia.com.mx> wrote:\n> Status: New\n> …"
        assert TK.strip_reply(body) == "Filters replaced today.\nPhotos attached."
        assert TK.strip_reply("> quoted only\n> more") == ""
        assert TK.strip_reply("ok\n-- \nJuan\nARGIA") == "ok"

    def test_verify_decision(self):
        now = NOW
        seen_old = {"k1": now - dt.timedelta(days=3)}
        v = tk(status="VERIFICATION", alert_keys=["k1"])
        assert TK.verify_decision(v, [], [], seen_old, now) == ("RESOLVED", "no linked alert open or seen for 2 days — resolved by the data")
        assert TK.verify_decision(v, [], ["k1"], seen_old, now)[0] == "IN_PROGRESS"           # recurred today
        assert TK.verify_decision(v, ["k1"], [], seen_old, now) == (None, "waiting: 1 linked alert(s) still open in the ledger")
        assert TK.verify_decision(v, [], [], {"k1": now - dt.timedelta(hours=20)}, now)[0] is None   # too recent
        assert TK.verify_decision(tk(status="IN_PROGRESS", alert_keys=["k1"]), [], [], {}, now) == (None, "")
        assert TK.verify_decision(tk(status="VERIFICATION", alert_keys=[]), [], [], {}, now) == (None, "")   # people resolve these

    def test_matching_ticket_by_asset(self):
        a = tk(id=1, number="TK-NL1-0001", inverter_sn="SN1", created_at="2026-09-01 10:00:00+00")
        b = tk(id=2, number="TK-NL1-0002", inverter_sn="SN1", created_at="2026-09-05 10:00:00+00")
        p = tk(id=3, number="TK-NL1-0003", inverter_sn="", created_at="2026-09-05 10:00:00+00")
        assert TK.matching_ticket([a, b, p], "nl1", "SN1").number == "TK-NL1-0002"     # newest on the asset
        assert TK.matching_ticket([a, b, p], "NL1", "").number == "TK-NL1-0003"        # plant-level alert -> plant-level ticket
        assert TK.matching_ticket([a, b, p], "NL1", "SN9") is None and TK.matching_ticket([], "NL1", "SN1") is None

    def test_status_timeline_and_stats_and_chart(self):
        t1 = tk(id=1, number="TK-NL1-0001", status="RESOLVED", created_at="2026-09-01 10:00:00+00", resolved_at="2026-09-03 10:00:00+00")
        t2 = tk(id=2, number="TK-NL1-0002", status="IN_PROGRESS", created_at="2026-09-05 10:00:00+00")
        evs = [TK.Event(1, 1, "2026-09-02 10:00:00+00", "juan", "status", "", {"from": "NEW", "to": "IN_PROGRESS"}),
               TK.Event(2, 1, "2026-09-03 10:00:00+00", "monitoring", "status", "", {"from": "IN_PROGRESS", "to": "RESOLVED"}),
               TK.Event(3, 2, "2026-09-06 10:00:00+00", "juan", "status", "", {"from": "NEW", "to": "IN_PROGRESS"})]
        series = TK.status_timeline([t1, t2], evs, 8, NOW)
        by_day = {d.isoformat(): c for d, c in series}
        assert by_day["2026-09-01"] == {"NEW": 1} and by_day["2026-09-02"] == {"IN_PROGRESS": 1}
        assert by_day["2026-09-03"] == {} and by_day["2026-09-05"] == {"NEW": 1} and by_day["2026-09-08"] == {"IN_PROGRESS": 1}
        st = TK.stats([t1, t2], NOW, weeks=2)
        assert st["n_open"] == 1 and st["n_resolved"] == 1 and st["mttr_h"] == 48.0
        assert st["by_plant"] == [("NL1", 1)] and st["by_priority"] == [("P2", 1)] and st["over_sla"] == 1
        assert sum(o for _w, o, _r in st["weeks"]) == 2 and sum(r for _w, _o, r in st["weeks"]) == 1
        svg = TK.status_chart_svg(series)
        assert svg.startswith("<svg") and "<polygon" in svg and "In progress" in svg and TK.status_chart_svg([]) == ""

    def test_ingester_parses_a_reply(self):
        import sys
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))
        import ticket_mail_in as MI
        raw = (b"From: Juan Perez <juan@x>\r\nTo: service@argia.com.mx\r\nSubject: Re: [TK-NL1-0007] Plastic Omnium: cooling\r\n"
               b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
               b"Filters replaced.\r\n\r\nOn Mon wrote:\r\n> old\r\n--B\r\nContent-Type: image/png\r\nContent-Disposition: attachment; filename=\"a.png\"\r\n"
               b"Content-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n--B\r\nContent-Type: application/x-msdownload\r\n"
               b"Content-Disposition: attachment; filename=\"run.exe\"\r\nContent-Transfer-Encoding: base64\r\n\r\nAAAA\r\n--B--\r\n")
        number, sender, text, files = MI.parse_message(raw)
        assert (number, sender, text) == ("TK-NL1-0007", "juan@x", "Filters replaced.")
        assert [f[0] for f in files] == ["a.png"]
        t = tk(followers=["tech@contractor.mx"])
        assert MI.may_comment(t, "juan@x", lambda u: {"juan": "juan@x"}.get(u, "")) == "juan"
        assert MI.may_comment(t, "tech@contractor.mx", lambda u: "") == "tech@contractor.mx"
        assert MI.may_comment(t, "stranger@x", lambda u: "") is None
        assert MI.imap_config({"SMTP_HOST": "h"}) is None
        assert MI.imap_config({"IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASS": "p"})["folder"] == "INBOX"

    def test_daily_job_wiring(self):
        import pathlib
        v2 = pathlib.Path(__file__).resolve().parents[2]
        d = (v2 / "scripts/alerts_daily.py").read_text(encoding="utf-8")
        assert "open_tickets = _open_tickets()" in d and "_verify_tickets(open_tickets, result.records, result.opened + result.touched, now_utc" in d
        assert "TK.matching_ticket(open_tickets, r.plant_key, r.inverter_sn" in d
        assert "tickets = _ticket_briefs()            # after the attach" in d
        pm = (v2 / "scripts/daily_perf_mail.py").read_text(encoding="utf-8")
        assert '(extra or {}).get("ticket") or _ISSUE_WHY.get(head, "")' in pm and "opened_utc, message, alert_key FROM alert_ledger" in pm
        pg = (v2 / "server/bundle/portal_gen.py").read_text(encoding="utf-8")
        assert pg.index("('ags',") < pg.index("('maint',") < pg.index("('setup',")
        assert "argia-ticket-mail" in (v2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert "ticket_mail_in.py" in (v2 / "server/bundle/argia-ticket-mail.service").read_text(encoding="utf-8")
