"""v303 - fewer, truer alert mails (Tomasz, 2026-10-03).

"I am getting a lot of warnings which most probably doesn't even qualify
for warnings - and if there is open ticket stop sending me the same
status over and over again - remind me about all tickets once a week or
when the status changes" + "yes do it" (the frozen-readings rule for the
alert jobs).

Locks:
* grading: a flag without a measured loss is INFO (portal only), the
  data_stale of a few hours is INFO, CRITICAL and measured-loss WARNINGs
  are untouched; both scripts grade before reconcile;
* reconcile keeps the worst of INFO < WARNING < CRITICAL for one key and
  re-arms the mail when an INFO escalates;
* an alert with an open ticket is not mailed, not re-mailed, does not
  trigger the morning "nothing new" mail and is not repeated in the 19:00
  mail (the plant shows the ticket number instead);
* a ticket's creation and status changes also reach the maintenance
  subscribers; the Monday reminder lists every open ticket;
* frozen logger readings are not samples for the acute or daily tier.

Every test here fails on v302.
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
import sys

import pytest

from argia.alerts import grading, ledger_mail as LM, naming
from argia.alerts.engine import Candidate, reconcile_alerts
from argia.core.alerts_state import AlertRecord, AlertState
from argia.maintenance import tickets as TK

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))
NOW = dt.datetime(2026, 10, 5, 13, 10, tzinfo=dt.timezone.utc)      # Monday 07:10 MX


def cand(metric, sev, key=None, msg=None):
    return Candidate(alert_key=key or f"nl1:inv:sn1:{metric}", plant_key="NL1", inverter_sn="SN1", metric=metric,
                     severity=sev, value=1.0, threshold=None, message=msg or f"NL1 SN1: {metric} [{sev}]")


# ------------------------------------------------------------- grading
class TestGrading:
    @pytest.mark.parametrize("metric", ["inverter_temp_high", "inverter_silent", "energy_daily_pct", "plant_twin_yield"])
    @pytest.mark.parametrize("tier", ["acute", "daily"])
    def test_flags_without_a_loss_are_info_in_both_tiers(self, metric, tier):
        c = grading.grade(cand(metric, "WARNING"), tier)
        assert c.severity == "INFO" and c.message.endswith("[INFO]")

    def test_the_hysteresis_line_of_the_19h_mail_is_info(self):
        # "day-peak temperature 61.0 degC - still above the clear level 60 [WARNING]" (SLP1, SAG)
        c = grading.grade(cand("inverter_temp_high", "WARNING",
                               msg="SLP1 X: day-peak temperature 61.0 degC - still above the clear level 60 [WARNING]"), "daily")
        assert c.severity == "INFO" and c.message.endswith("clear level 60 [INFO]")

    def test_data_stale_is_info_intraday_and_warning_for_a_whole_day(self):
        assert grading.grade(cand("data_stale", "WARNING"), "acute").severity == "INFO"
        assert grading.grade(cand("data_stale", "WARNING"), "daily").severity == "WARNING"

    @pytest.mark.parametrize("metric", ["inverter_relative", "string_fault", "inverter_fault", "vendor_flag"])
    def test_measured_losses_and_faults_stay_warning(self, metric):
        assert grading.grade(cand(metric, "WARNING"), "daily").severity == "WARNING"

    @pytest.mark.parametrize("metric", ["inverter_temp_high", "inverter_silent", "energy_daily_pct", "data_stale"])
    def test_critical_is_never_touched(self, metric):
        c = cand(metric, "CRITICAL")
        assert grading.grade(c, "acute") is c and grading.grade(c, "daily") is c

    def test_unknown_tier_is_an_error(self):
        with pytest.raises(ValueError):
            grading.grade(cand("data_stale", "WARNING"), "hourly")

    def test_both_scripts_grade_before_reconcile(self):
        for name, tier in (("alerts_daily.py", "daily"), ("alerts_snapshot.py", "acute")):
            src = (V2 / "scripts" / name).read_text(encoding="utf-8")
            assert 'grading.grade_all(' in src and f'tier="{tier}"' in src
            assert src.index("grading.grade_all(") < src.index("reconcile_alerts(ledger")

    def test_info_has_its_own_explanation_prefix(self):
        from argia.alerts.explanations import explain
        assert explain("inverter_temp_high", "INFO").startswith("For the record - no energy loss measured")


def ledger(*recs):
    from argia.core.alerts_state import AlertsLedger
    return AlertsLedger(records=list(recs))


def rec(i, key, metric="inverter_temp_high", sev="WARNING", sent="", state=AlertState.OPEN, plant="NL1", seen=""):
    return AlertRecord(alert_id=f"ALT-{i}", alert_key=key, plant_key=plant, inverter_sn="SN1", metric=metric,
                       severity=sev, state=state, opened_utc="2026-09-28T12:00:00+00:00", last_seen_utc=seen,
                       resolved_utc="", value=70.0, threshold=65.0, message=f"{metric} [{sev}]",
                       channels_sent=sent, explanation="")


class TestReconcile:
    def test_worst_of_info_warning_critical_wins_for_one_key(self):
        k = "nl1:inv:sn1:data_stale"
        for order in ([cand("data_stale", "INFO", k), cand("data_stale", "WARNING", k)],
                      [cand("data_stale", "WARNING", k), cand("data_stale", "INFO", k)]):
            r = reconcile_alerts(ledger(), order, NOW)
            assert [x.severity for x in r.opened] == ["WARNING"]

    def test_an_open_warning_is_lowered_by_the_daily_tier_and_re_armed_on_escalation(self):
        k = "nl1:inv:sn1:inverter_temp_high"
        r1 = reconcile_alerts(ledger(rec(1, k, sent="email,mailed@2026-09-28T12:30:00Z")),
                              [grading.grade(cand("inverter_temp_high", "WARNING", k), "daily")], NOW)
        assert r1.records[0].severity == "INFO" and "email" in r1.records[0].channels_sent    # no mail on the way down
        r2 = reconcile_alerts(ledger(*r1.records), [cand("inverter_temp_high", "CRITICAL", k)], NOW)
        assert r2.records[0].severity == "CRITICAL" and r2.records[0].channels_sent == ""      # mailed again on the way up
        assert LM.unmailed(r2.records) == [r2.records[0]]


# --------------------------------------------------------- ticketed alerts
BRIEF = TK.TicketBrief("TK-NL1-0001", "IN_PROGRESS", "P2", "arturo", "2026-08-27 15:00:00+00", "", True)


class TestTicketedAlertsAreNotMailed:
    @pytest.fixture(autouse=True)
    def _io(self, monkeypatch):
        from argia.alerts import emailer, subscriptions
        monkeypatch.setattr(subscriptions, "load_excluded_plants", lambda: frozenset())
        monkeypatch.setattr(LM, "recipients", lambda: [("all@x", None)])
        monkeypatch.setattr(LM, "plant_labels", lambda: naming.Names({}))
        monkeypatch.setattr(emailer, "load_smtp", lambda path=None: {"SMTP_HOST": "h", "SMTP_PORT": "25", "SMTP_USER": "svc@x"})
        self.sent = []
        monkeypatch.setattr(emailer, "send", lambda msg, cfg, timeout=30: self.sent.append(msg["Subject"]) or True)

    def test_a_new_ticketed_alert_is_marked_but_not_mailed(self, caplog):
        with caplog.at_level(logging.INFO):
            out = LM.mail_new_alerts([rec(1, "k1", sev="CRITICAL")], tickets={"k1": BRIEF}, now_utc=NOW, now_mx_hour=10)
        assert self.sent == []
        assert "email" in out[0].channels_sent and LM.last_mailed_at(out[0]) == NOW         # not picked up again
        assert "belong to open tickets - not mailed (TK-NL1-0001)" in caplog.text

    def test_a_ticketed_critical_is_not_re_mailed_every_3_hours(self):
        old, seen = "email,mailed@2026-10-05T06:00:00Z", (NOW - dt.timedelta(minutes=5)).isoformat()
        out = LM.mail_new_alerts([rec(1, "k1", sev="CRITICAL", sent=old, seen=seen)], tickets={"k1": BRIEF}, now_utc=NOW, now_mx_hour=10)
        assert self.sent == [] and out[0].channels_sent == old
        # without the ticket the same alert IS re-mailed (v257 unchanged)
        LM.mail_new_alerts([rec(1, "k1", sev="CRITICAL", sent=old, seen=seen)], tickets={}, now_utc=NOW, now_mx_hour=10)
        assert len(self.sent) == 1

    def test_a_critical_not_seen_since_the_morning_is_not_re_mailed(self):
        """SAG 26 + 29 Sep: yesterday's energy verdict (06:30) went out again
        at 09:00, 12:00 and 15:00 MX - nothing new each time."""
        old = "email,mailed@2026-10-05T06:00:00Z"
        morning = rec(1, "mex1:plant:energy_daily_pct", metric="energy_daily_pct", sev="CRITICAL", sent=old,
                      plant="MEX1", seen="2026-10-05T12:30:10+00:00")
        assert LM.due_for_remail([morning], NOW + dt.timedelta(hours=2), 9) == []
        acute = rec(2, "mex1:plant:plant_offline", metric="plant_offline", sev="CRITICAL", sent=old, plant="MEX1",
                    seen=(NOW + dt.timedelta(hours=2) - dt.timedelta(minutes=3)).isoformat())
        assert LM.due_for_remail([morning, acute], NOW + dt.timedelta(hours=2), 9) == [acute]   # still being seen
        assert LM.REMAIL_SEEN_WITHIN_MIN == 40.0

    def test_no_morning_mail_when_only_ticketed_criticals_are_open(self):
        LM.mail_new_alerts([rec(1, "k1", sev="CRITICAL", sent="email")], morning=True, when_mx="2026-10-05 06:30",
                           now_utc=NOW, tickets={"k1": BRIEF})
        assert self.sent == []                                # was "5 Oct - nothing new - 1 ticket in hand"
        LM.mail_new_alerts([rec(1, "k1", sev="CRITICAL", sent="email")], morning=True, when_mx="2026-10-05 06:30",
                           now_utc=NOW, tickets={})
        assert self.sent == ["[ARGIA] 5 Oct - nothing new - 1 critical still open"]   # an unticketed one still reminds

    def test_a_closed_ticket_hides_nothing(self):
        closed = TK.TicketBrief("TK-NL1-0001", "CLOSED", "P2", "", "2026-08-27", "", False)
        LM.mail_new_alerts([rec(1, "k1", sev="CRITICAL")], tickets={"k1": closed}, now_utc=NOW, now_mx_hour=10)
        assert len(self.sent) == 1


# ------------------------------------------------------------ 19:00 mail
class TestEveningMail:
    def _data(self, alerts):
        from scripts import daily_perf_mail as dpm
        plants = [("NL1", "Plastic Omnium", 800.0), ("MEX1", "SAG", 597.78)]
        return dpm, dpm.summarize(plants, {"NL1": (3000.0, 5.0, 4), "MEX1": (2400.0, 5.0, 6)}, {"NL1": 4, "MEX1": 6},
                                  {}, {}, alerts, [], dt.date(2026, 10, 2), "19:00")

    def test_ticketed_issues_are_not_repeated_the_plant_names_the_ticket(self):
        t = {"message": "derating", "ticket": "In hand: TK-NL1-0001 · In progress", "ticket_no": "TK-NL1-0001"}
        alerts = [("inverter_temp_high:NL1:SN1", "CRITICAL", None, t),
                  ("inverter_temp_high:NL1:SN3", "CRITICAL", None, dict(t, ticket_no="TK-NL1-0002")),
                  ("inverter_relative:MEX1:SN2", "WARNING", None, {"message": "77% of peers"})]
        dpm, d = self._data(alerts)
        rows = {r["key"]: r for r in d["rows"]}
        assert rows["NL1"]["status"] == "ticket TK-NL1-0001, TK-NL1-0002" and rows["NL1"]["cls"] == "hand"
        assert rows["MEX1"]["status"] == "check"
        assert [i["key"] if "key" in i else i["who"] for i in d["issues"]] and len(d["issues"]) == 1
        assert d["in_hand"] == ["TK-NL1-0001", "TK-NL1-0002"]
        assert dpm.mail_subject(d).endswith("1 plant(s) need attention")                  # was 2
        text, html = dpm.render_text(d), dpm.render_html(d)
        note = "Open maintenance tickets, not repeated here: TK-NL1-0001, TK-NL1-0002"
        assert note in text and note in html and "In hand:" not in text and "derating" not in text

    def test_ledger_issues_tags_the_ticket_number(self, monkeypatch):
        from argia.store import pgq
        from scripts import daily_perf_mail as dpm
        monkeypatch.setattr(pgq, "psql_rows", lambda sql: [["inverter_temp_high", "NL1", "SN1", "CRITICAL",
                                                           "2026-08-27T15:00:00+00:00", "hot", "k1"]])
        monkeypatch.setattr(dpm, "ticket_briefs", lambda: {"k1": BRIEF})
        (key, sev, _seen, extra), = dpm.ledger_issues()
        assert extra["ticket_no"] == "TK-NL1-0001"

    def test_info_alerts_never_reach_the_evening_mail(self):
        src = (V2 / "scripts" / "daily_perf_mail.py").read_text(encoding="utf-8")
        assert "severity IN ('WARNING','CRITICAL')" in src


# ------------------------------------------------- status-change mails
def ticket(i=1, plant="NL1", status="IN_PROGRESS", created="2026-08-27 15:00:00+00", updated="2026-09-20 15:00:00+00",
           prio="P2", sn="SN1", assigned="arturo", followers=(), keys=()):
    return TK.Ticket(i, f"TK-{plant}-{i:04d}", plant, sn, "Inverter derating", "", "inverter/fault", prio, status,
                     "tomasz", assigned, created, updated, followers=list(followers), alert_keys=list(keys))


class TestStatusWatchers:
    RC = [("tomasz@argia.com.mx", None), ("eduardo@argia.com.mx", frozenset({"GTO1"})),
          ("ex@old.com", None), ("Arturo@Argia.com.mx", frozenset({"NL1"}))]
    PORTAL = frozenset({"tomasz@argia.com.mx", "eduardo@argia.com.mx", "arturo@argia.com.mx"})

    def test_scoped_portal_subscribers_of_the_plant(self):
        from argia.maintenance import notify as N
        assert N.status_watchers("NL1", self.RC, self.PORTAL, frozenset()) == ["arturo@argia.com.mx", "tomasz@argia.com.mx"]
        assert N.status_watchers("GTO1", self.RC, self.PORTAL, frozenset()) == ["eduardo@argia.com.mx", "tomasz@argia.com.mx"]
        assert N.status_watchers("MEX3", self.RC, self.PORTAL, frozenset({"MEX3"})) == []        # CAPEX / held plant

    def test_fail_soft(self, monkeypatch):
        from argia.alerts import subscriptions
        from argia.maintenance import notify as N
        monkeypatch.setattr(subscriptions, "load_excluded_plants", lambda: (_ for _ in ()).throw(RuntimeError("no db")))
        assert N.status_watchers("NL1") == []

    def test_send_adds_watchers_but_never_the_person_who_changed_it(self):
        from argia.maintenance import notify as N

        class Mailer:
            sent = []
            def load_smtp(self):
                return {"SMTP_USER": "svc@x"}
            def build_html_email(self, subject, text, htm, sender, to):
                return {"To": to, "Subject": subject}
            def send(self, msg, cfg):
                self.sent.append(msg)
                return True
        m = Mailer()
        email = {"tomasz": "tomasz@argia.com.mx", "arturo": "arturo@argia.com.mx"}.get
        n = N.send(ticket(), "tomasz", "Status: New → In progress", "", lambda u: email(u) or "", lambda u: u,
                   "Plastic Omnium", "Inverter 1", mailer=m,
                   watchers=["tomasz@argia.com.mx", "eduardo@argia.com.mx"])
        assert n == 2 and m.sent[0]["To"] == ["arturo@argia.com.mx", "eduardo@argia.com.mx"]

    def test_the_app_and_the_daily_job_pass_watchers_on_status_changes_only(self):
        app = (V2 / "server/bundle/maint_app.py").read_text(encoding="utf-8")
        assert "notify(t, me, 'New ticket opened', t.description, status=True)" in app
        assert "request.form.get('note') or '', status=True)" in app
        assert app.count("status=True)") == 2                                     # comments, priority, ... stay as before
        daily = (V2 / "scripts/alerts_daily.py").read_text(encoding="utf-8")
        assert "watchers=NOTIFY.status_watchers(t.plant_key)" in daily


# ------------------------------------------------------ weekly reminder
class TestWeeklyReminder:
    def _items(self):
        import ticket_weekly as W
        tks = [ticket(1, keys=["k1"]), ticket(2, prio="P1", created="2026-09-26 15:00:00+00", sn=""),
               ticket(3, status="CLOSED"), ticket(4, plant="MEX1", prio="P3", sn="SN9")]
        last = {1: (dt.datetime(2026, 9, 20, 15, tzinfo=dt.timezone.utc), "waiting for the Huawei technician"),
                2: (dt.datetime(2026, 10, 4, 15, tzinfo=dt.timezone.utc), "fans replaced")}
        alerts = {1: [("CRITICAL", "inverter_temp_high", "SN1"), ("INFO", "inverter_silent", "SN1")]}
        return W, W.items(tks, last, alerts)

    def test_open_tickets_p1_first_then_oldest(self):
        W, its = self._items()
        assert [i.ticket.number for i in its] == ["TK-NL1-0002", "TK-NL1-0001", "TK-MEX1-0004"]

    def test_the_mail(self):
        W, its = self._items()
        n = naming.Names({"NL1": "Plastic Omnium", "MEX1": "SAG"}, {("NL1", "SN1"): "Inverter 1", ("MEX1", "SN9"): "Inverter 9"})
        subj, text, html = W.render(its, n, NOW)
        assert subj == "[ARGIA] 5 Oct - 3 open maintenance tickets, 2 without update for 7+ days"
        assert "TK-NL1-0001 · Plastic Omnium · Inverter 1 (SN1) · Inverter derating" in text
        assert "In progress · P2 · assigned to arturo · open 38 d 22 h" in text
        assert "NO UPDATE FOR 14 DAYS - last update 14 d 22 h ago: waiting for the Huawei technician" in text
        assert "still open: CRITICAL inverter running hot; INFO" in text
        assert "last update 22 h 10 ago: fans replaced" in text and "NO UPDATE FOR 0" not in text
        assert "no alert of this ticket is open any more" in text
        assert "https://portal.argia.com.mx/maintenance/t/TK-NL1-0001/" in text and 'href="https://portal.argia.com.mx/maintenance/t/TK-NL1-0001/"' in html
        assert "TK-NL1-0003" not in text                                  # closed: not listed

    def test_the_head_line_names_plant_and_inverter_once(self):
        """v304: production titles already say 'Plastic Omnium · Inverter 3: ...' (dry run on pio06)."""
        W, _ = self._items()
        n = naming.Names({"NL1": "Plastic Omnium"}, {("NL1", "SN1"): "Inverter 3"})
        t = ticket(1)
        for title, head in (("Plastic Omnium · Inverter 3: cooling inspection", "TK-NL1-0001 · Plastic Omnium · Inverter 3: cooling inspection"),
                            ("Inverter 3 (SN1): cooling inspection", "TK-NL1-0001 · Plastic Omnium · Inverter 3 (SN1): cooling inspection"),
                            ("Communication", "TK-NL1-0001 · Plastic Omnium · Inverter 3 (SN1) · Communication")):
            import dataclasses
            item = W.Item(dataclasses.replace(t, title=title), None, "")
            assert W.lines_for(item, n, NOW)[0] == head

    def test_views_are_scoped_and_skip_excluded_plants(self):
        W, its = self._items()
        vs = W.views(its, [("a@x", None), ("b@x", frozenset({"MEX1"})), ("c@x", frozenset({"GTO1"}))], frozenset({"MEX1"}))
        assert [(e, [i.ticket.number for i in v]) for e, v in vs] == [(["a@x"], ["TK-NL1-0002", "TK-NL1-0001"])]

    def test_load_reads_open_tickets_last_updates_and_open_alerts(self):
        import ticket_weekly as W
        cols = ",".join(TK.TICKET_COLS) + ",followers,alert_keys"
        t_csv = cols + "\n" + ",".join(["7", "TK-NL1-0007", "NL1", "SN1", "hot", "", "inverter/fault", "P2", "NEW", "tomasz", "",
                                        "2026-10-01 10:00:00+00", "2026-10-01 10:00:00+00"] + [""] * (len(TK.TICKET_COLS) - 13) + ["", "k1"]) + "\n"
        seen = []

        def q(sql):
            seen.append(sql)
            if "ticket_event" in sql:
                return "ticket_id,ts,body\n7,2026-10-02 10:00:00+00,on it\n"
            if "alert_ledger" in sql:
                return "ticket_id,severity,metric,inverter_sn\n7,WARNING,string_fault,SN1\n"
            return t_csv
        its = W.load(q)
        assert [(i.ticket.number, i.last_body, i.alerts) for i in its] == [("TK-NL1-0007", "on it", (("WARNING", "string_fault", "SN1"),))]
        assert "status IN ('NEW','IN_PROGRESS','WAITING','VERIFICATION')" in seen[0] and "state = 'OPEN'" in seen[2]

    def test_timer_service_and_registration(self):
        timer = (V2 / "server/bundle/argia-ticket-weekly.timer").read_text(encoding="utf-8")
        assert "OnCalendar=Mon *-*-* 07:10:00 America/Mexico_City" in timer and "Persistent=true" in timer
        assert "run_job.sh ticket-weekly ticket_weekly.py" in (V2 / "server/bundle/argia-ticket-weekly.service").read_text(encoding="utf-8")
        assert "argia-ticket-weekly" in (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert "| argia-ticket-weekly | Mon 07:10 MX |" in (V2 / "docs/OPERATIONS.md").read_text(encoding="utf-8")


# ------------------------------------------------------ frozen readings
T0 = dt.datetime(2026, 10, 2, 19, 0, tzinfo=dt.timezone.utc)          # 13:00 MX


class TestFrozenReadingsInTheAlertJobs:
    def _grid(self):
        """MEX1: real until 10:30 MX, then the logger froze and the cloud
        repeated 12,301 W / 214.7 kWh every 5 min until 13:00. GTO1 live."""
        hdr = ["timestamp_utc", "plant_key", "inverter_sn", "power_w", "temperature_c", "status", "fault_code", "etoday_kwh"]
        rows = []
        for m in range(0, 181, 5):
            ts = T0 - dt.timedelta(minutes=180 - m)
            frozen = m >= 30
            rows.append([ts.isoformat(), "MEX1", "M1", 12301.0 if frozen else 10000.0 + m, 40.0, 1, "",
                         214.7 if frozen else 200.0 + m / 10])
            rows.append([ts.isoformat(), "GTO1", "G1", 20000.0 + m, 41.0, 1, "", 300.0 + m / 5])
        return [hdr] + rows

    def test_the_acute_tier_sees_a_frozen_plant_as_stale(self, monkeypatch):
        import alerts_snapshot as S
        from argia.analytics.acute import evaluate_acute
        from argia.telemetry import pg_source
        monkeypatch.setattr(pg_source, "source", lambda env=None: "pg")
        monkeypatch.setattr(pg_source, "read_grid", lambda **kw: self._grid())
        samples, span = S._read_recent_samples(None)
        assert len([s for s in samples if s[1] == "MEX1"]) == 7          # 10:00 ... 10:30 MX, the 30 repeats are gone
        assert all(len(s) == 7 for s in samples)                        # evaluate_acute's shape
        stale = [b for b in evaluate_acute(samples, ["MEX1", "GTO1"], T0, absent_gap_hours=span) if b.metric == "data_stale"]
        assert [b.plant_key for b in stale] == ["MEX1"] and "last 10:30 MX" in stale[0].message

    def test_the_daily_tier_drops_the_repeats_from_the_day(self):
        import alerts_daily as D
        from argia.kpi import DayBundle
        from argia.kpi.reader import InverterRow

        def row(ts, sn, p, e):
            return InverterRow(ts, "MEX1", sn, sn, "huawei", 1, p, e, 40.0, "", None, None, None, None)
        rows = [row(T0 + dt.timedelta(minutes=5 * k), "M1", 12301.0 if k >= 2 else 9000.0 + k, 214.7 if k >= 2 else 200.0 + k)
                for k in range(6)]
        b = D.without_frozen(DayBundle(date_iso="2026-10-02", rows=tuple(rows)))
        assert [r.timestamp_utc for r in b.rows_for_plant("MEX1")] == [T0, T0 + dt.timedelta(minutes=5), T0 + dt.timedelta(minutes=10)]
        same = DayBundle(date_iso="2026-10-02", rows=tuple(rows[:2]))
        assert D.without_frozen(same) is same

    def test_drop_repeats_keeps_order_and_judges_each_inverter_alone(self):
        from argia.telemetry.fresh import drop_repeats
        a = [("A", T0, 5000.0, 10.0), ("B", T0, 5000.0, 10.0), ("A", T0 + dt.timedelta(minutes=5), 5000.0, 10.0),
             ("B", T0 + dt.timedelta(minutes=5), 5000.0, 10.5)]
        out = drop_repeats(a, lambda r: r[0], lambda r: (r[1], r[2], r[3]))
        assert out == [a[0], a[1], a[3]]
