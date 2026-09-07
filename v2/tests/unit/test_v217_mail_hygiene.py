"""v217 — mail / push hygiene (Tomasz, 2026-09-06, after two mails):

1. no alerts about CAPEX plants — ever (the two Budenheim string_fault
   mails went out while the portfolio filter failed OPEN on a PG timeout);
2. no plant codes in any communication: names first, code as detail;
3. infrastructure / monitoring-internal messages only to the admin;
4. the Pi's ntfy push names portal.argia.com.mx and carries the HTTP code.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from argia.alerts import ledger_mail as LM
from argia.alerts import monitor as M
from argia.alerts import naming as N
from argia.alerts import subscriptions as S
from argia.core.alerts_state import AlertRecord, AlertState

V2 = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class A:
    key: str
    severity: str = "CRITICAL"


NAMES = N.Names(
    {"NL2": "Budenheim", "NL1": "Plastic Omnium", "GTO1": "Taigene", "MEX1": "SAG"},
    {("NL2", "JJM4D4P017"): "Inverter 1", ("GTO1", "SN9"): "Inverter 3"})


def rec(plant="NL2", sn="JJM4D4P017", metric="string_fault", sev="WARNING",
        msg=None, aid="ALT-1"):
    return AlertRecord(
        alert_id=aid, alert_key=f"{metric}:{plant}:{sn}", plant_key=plant,
        inverter_sn=sn, metric=metric, severity=sev, state=AlertState.OPEN,
        opened_utc="2026-09-06T12:30:09+00:00", last_seen_utc="", resolved_utc="",
        value=3.0, threshold=0.0,
        message=msg if msg is not None else
        f"{plant} {sn}: NEW string-diagnostic bit(s) [break:0] not seen in prior 14 days [WARNING]",
        channels_sent="", explanation="Worth watching; act if it persists.")


# ------------------------------------------------------------ 1. CAPEX
class TestCapexNeverMailed:
    def test_capex_is_excluded_even_when_the_env_allows_it(self):
        allowed = S.mail_portfolios({"ARGIA_MAIL_PORTFOLIOS": "PPA,CAPEX"})
        assert allowed == frozenset({"PPA"})
        ex = S.excluded_plants({"GTO1": "PPA", "NL2": "CAPEX", "TAM1": "capex"},
                               frozenset({"PPA", "CAPEX"}))
        assert ex == frozenset({"NL2", "TAM1"})

    def test_default_env_still_ppa_only(self):
        assert S.mail_portfolios({}) == frozenset({"PPA"})
        assert S.excluded_plants({"GTO1": "PPA", "NL2": "CAPEX", "X": ""}, S.mail_portfolios({})) \
            == frozenset({"NL2", "X"})

    def test_filter_unavailable_holds_every_plant_alert_but_not_infra(self, monkeypatch):
        """2026-09-06 22:02 CEST: psql timed out during a lock incident and
        the old fail-OPEN mailed Budenheim to everyone. Now: fail CLOSED."""
        import argia.store.pgq as pgq

        def boom(sql):
            raise RuntimeError("psql timed out after 120 seconds")
        monkeypatch.setattr(pgq, "psql_rows", boom)
        ex = S.load_excluded_plants()
        assert ex == S.HOLD_ALL
        assert not S.is_mailable("NL2", ex)
        assert not S.is_mailable("GTO1", ex)          # PPA waits one run too
        assert S.is_mailable(None, ex)                # infrastructure passes

    def test_empty_plant_table_is_treated_as_unavailable(self, monkeypatch):
        import argia.store.pgq as pgq
        monkeypatch.setattr(pgq, "psql_rows", lambda sql: [])
        assert S.load_excluded_plants() == S.HOLD_ALL

    def test_ledger_mail_holds_capex_and_retries_later(self, monkeypatch):
        """A held alert keeps channels_sent empty -> it is a candidate
        again next run (when the filter is back it is dropped, not mailed)."""
        monkeypatch.setattr(S, "load_excluded_plants", lambda: S.HOLD_ALL)
        monkeypatch.setattr(LM, "recipients", lambda: [("a@x.mx", None)])
        sent = []
        monkeypatch.setattr(LM, "plant_labels", lambda: NAMES)
        from argia.alerts import emailer
        monkeypatch.setattr(emailer, "load_smtp", lambda: {"SMTP_USER": "s@x"})
        monkeypatch.setattr(emailer, "send", lambda m, c: sent.append(m) or True)
        out = LM.mail_new_alerts([rec()])
        assert sent == [] and out[0].channels_sent == ""

    def test_alert_mailer_filters_what_is_sent_not_what_is_tracked(self):
        src = (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        i_plan = src.index("monitor.plan_sends(")
        i_drop = src.index("dropped = [a for a in to_send")
        assert i_plan < i_drop, "the portfolio filter must sit on to_send"
        assert "to_send = [a for a in to_send if a not in dropped]" in src
        assert "if subscriptions.is_mailable(subscriptions.alert_plant(k), excluded)]" in src
        assert "active = [a for a in active if a not in dropped]" not in src


# ------------------------------------------------------------ 2. names
class TestNames:
    def test_short_customer(self):
        assert N.short_customer("PLASTIC OMNIUM PPA land (Monterrey, NL)") == "Plastic Omnium"
        assert N.short_customer("HOLIDAY INN EXPRESS, Turistica Arizona PPA roof (SLP, SLP)") == "Holiday Inn Express"
        assert N.short_customer("SAG PPA roof (CDMX, MEX)") == "SAG"
        assert N.short_customer("SMS (CDMX,MEX)") == "SMS"
        assert N.short_customer("HIRSCHMANN-MEXICO (San Miguel, GTO)") == "Hirschmann-Mexico"
        assert N.short_customer("BUDENHEIM (Monterrey, NL)") == "Budenheim"
        assert N.short_customer("") == ""

    def test_plant_and_inverter_rendering(self):
        assert NAMES.plant("NL2") == "Budenheim"
        assert NAMES.plant_full("NL2") == "Budenheim (NL2)"
        assert NAMES.plant_full("QRO1") == "QRO1"                 # unknown stays visible
        assert NAMES.plant_full("PORTFOLIO") == "Portfolio"
        assert NAMES.inverter("NL2", "JJM4D4P017") == "Inverter 1"
        assert NAMES.inverter_full("NL2", "JJM4D4P017") == "Inverter 1 · SN JJM4D4P017"
        assert NAMES.inverter_full("NL2", "JJM4D4P01C") == "inverter JJM4D4P01C"
        assert NAMES.inverter_full("NL2", "") == ""

    def test_text_drops_the_prefix_and_names_other_mentions(self):
        t = NAMES.text("NL2 JJM4D4P017: NEW string-diagnostic bit(s) not seen before",
                       "NL2", "JJM4D4P017")
        assert t == "NEW string-diagnostic bit(s) not seen before"
        t = NAMES.text("NL1: below its twin GTO1 by 30% (SN9 idle)", "NL1")
        assert t == "below its twin Taigene by 30% (SN9 idle)"
        # no prefix stripping when asked (titles)
        assert NAMES.text("GTO1: no telemetry today", strip_prefix=False) == "Taigene: no telemetry today"
        # a serial inside another word is not a plant code
        assert NAMES.text("serial XNL2Y stays", strip_prefix=False) == "serial XNL2Y stays"

    def test_names_from_rows_ignores_labels_equal_to_the_serial(self):
        n = N.names_from_rows([("NL2", "BUDENHEIM (Monterrey, NL)")],
                              [("NL2", "S1", "S1"), ("NL2", "S2", "Inverter 2")])
        assert n.inverter_full("NL2", "S1") == "inverter S1"
        assert n.inverter_full("NL2", "S2") == "Inverter 2 · SN S2"

    def test_phrase_table_is_shared_with_the_daily_mail(self):
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        import daily_perf_mail as dpm
        assert dpm._ISSUE_PHRASE == N.METRIC_PHRASE
        assert N.phrase("string_fault") == "new string diagnostic flag"
        assert N.phrase("something_new") == "something new"

    def test_load_names_degrades_to_codes(self, monkeypatch):
        import argia.store.pgq as pgq

        def boom(sql):
            raise RuntimeError("down")
        monkeypatch.setattr(pgq, "psql_rows", boom)
        assert N.load_names().plant_full("NL2") == "NL2"


class TestLedgerMailNames:
    def test_single_alert_names_first_codes_as_detail(self):
        subj, body, html = LM.render_mail([rec()], NAMES, when_mx="2026-09-06 06:30")
        assert subj == "[ARGIA] 6 Sep — 1 warning (Budenheim)"
        assert "  Budenheim (NL2)\n    new string diagnostic flag — Inverter 1\n      Inverter 1: NEW string-diagnostic bit(s)" in body
        assert "NL2 JJM4D4P017:" not in body and "NL2 JJM4D4P017:" not in html
        assert "Inverter 1" in html and "new string diagnostic flag" in html

    def test_digest_subject_names_plants(self):
        subj, body, _ = LM.render_mail([rec(aid="ALT-1"), rec(sn="JJM4D4P01C", aid="ALT-2"),
                                        rec(plant="GTO1", sn="SN9", metric="inverter_temp_high", aid="ALT-3")],
                                       NAMES, when_mx="2026-09-06 06:30")
        assert subj == "[ARGIA] 6 Sep — 3 warnings (Budenheim, Taigene)"
        assert "  Budenheim (NL2)\n    new string diagnostic flag — Inverter 1, inverter JJM4D4P01C" in body
        assert "  Taigene (GTO1)\n    inverter running hot — Inverter 3" in body
        assert re.search(r"^NL2\b", body, re.M) is None

    def test_old_label_dict_still_accepted(self):
        subj, _, _ = LM.render_mail([rec(aid="1"), rec(aid="2", sn="X")], {"NL2": "Budenheim"})
        assert "(Budenheim)" in subj

    def test_mailer_loads_the_naming_layer(self):
        src = (V2 / "argia/alerts/ledger_mail.py").read_text(encoding="utf-8")
        assert "return naming.load_names()" in src
        assert "labels = plant_labels()" in src


class TestMonitorNames:
    def test_render_body_names_first_code_as_detail(self):
        alerts = [M.Alert("plant-stale:GTO1", M.SEV_CRIT, "GTO1: telemetry stale 50 min",
                          "Last usable sample from GTO1 is 50 minutes old (threshold 45)."),
                  M.Alert("unit-failed:argia-telemetry.service", M.SEV_CRIT,
                          "job failed: argia-telemetry.service", "systemd reports it failed")]
        body = M.render_body(alerts, ["plant-dark:NL1", "inverter-silent:GTO1:SN9",
                                      "unit-failed:argia-kpi.service", "recon-fail:MEX1:2026-09-05"],
                             "2026-09-06 14:00", names=NAMES)
        assert "• Taigene (GTO1): telemetry stale 50 min" in body
        assert "Last usable sample from Taigene is 50 minutes old" in body
        assert "• job failed: argia-telemetry.service" in body
        assert "• Plastic Omnium (NL1): no telemetry today" in body
        assert "• Taigene (GTO1): inverter silent (Inverter 3 · SN SN9)" in body
        assert "• scheduled job failed: argia-kpi.service" in body
        assert "• SAG (MEX1): reconciliation FAIL 2026-09-05" in body
        assert "plant-dark:" not in body and "recon-fail:" not in body

    def test_without_names_the_body_still_renders_codes(self):
        body = M.render_body([M.Alert("plant-dark:GTO1", M.SEV_CRIT, "GTO1: no telemetry today", "d")],
                             ["disk-full"], "now")
        assert "• GTO1: no telemetry today" in body and "• server disk nearly full" in body

    def test_alert_mailer_passes_names(self):
        src = (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert "names = naming.load_names()" in src
        assert 'now_mx.strftime("%Y-%m-%d %H:%M"), names=names)' in src


# ------------------------------------------------------------ 3. admin-only
class TestInternalToAdminOnly:
    ADMIN = frozenset({"tomasz.zemelka@argia.com.mx"})

    def test_default_admin(self):
        assert S.admin_emails({}) == self.ADMIN
        assert S.admin_emails({"ARGIA_MAIL_ADMIN": "A@x.mx, b@x.mx"}) == frozenset({"a@x.mx", "b@x.mx"})

    def test_internal_classification(self):
        for k in ("unit-failed:x.service", "disk-full", "postgres-down", "cfe-coverage",
                  "recon-fail:SLP2:2026-09-05", "satellite-drift:NL1"):
            assert S.is_internal(k), k
        for k in ("plant-dark:GTO1", "plant-stale:GTO1", "inverter-silent:GTO1:SN",
                  "string_fault:NL2:SN", "inverter_temp_high:MEX1:SN"):
            assert not S.is_internal(k), k

    def test_all_plants_subscriber_no_longer_gets_infra(self):
        alerts = [A("plant-dark:GTO1"), A("unit-failed:argia-kpi.service"),
                  A("recon-fail:SLP2:2026-09-05"), A("satellite-drift:NL1")]
        rcpt = [("arturo@x.mx", None), ("eduardo@x.mx", None),
                ("tomasz.zemelka@argia.com.mx", None)]
        groups = S.group_recipients(alerts, ["unit-failed:argia-telemetry.service", "plant-stale:GTO1"],
                                    rcpt, admins=self.ADMIN)
        views = {tuple(g[0]): ([a.key for a in g[1]], g[2]) for g in groups}
        assert views[("arturo@x.mx", "eduardo@x.mx")] == (["plant-dark:GTO1"], ["plant-stale:GTO1"])
        assert views[("tomasz.zemelka@argia.com.mx",)] == (
            [a.key for a in alerts], ["unit-failed:argia-telemetry.service", "plant-stale:GTO1"])

    def test_admin_gets_infra_even_without_a_subscription(self):
        groups = S.group_recipients([A("disk-full"), A("plant-dark:GTO1")], [],
                                    [("arturo@x.mx", None)], admins=self.ADMIN)
        views = {tuple(g[0]): [a.key for a in g[1]] for g in groups}
        assert views == {("arturo@x.mx",): ["plant-dark:GTO1"],
                         ("tomasz.zemelka@argia.com.mx",): ["disk-full"]}

    def test_infra_only_run_mails_the_admin_only(self):
        groups = S.group_recipients([A("unit-failed:argia-dash-update.service")],
                                    ["unit-failed:argia-telemetry.service"],
                                    [("a@x.mx", None), ("b@x.mx", None), ("tomasz.zemelka@argia.com.mx", None)],
                                    admins=self.ADMIN)
        assert [g[0] for g in groups] == [["tomasz.zemelka@argia.com.mx"]]

    def test_default_admins_come_from_env(self, monkeypatch):
        monkeypatch.delenv("ARGIA_MAIL_ADMIN", raising=False)
        groups = S.group_recipients([A("disk-full")], [], [("a@x.mx", None)])
        assert [g[0] for g in groups] == [["tomasz.zemelka@argia.com.mx"]]

    def test_daily_mail_drops_internal_issues(self):
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        import daily_perf_mail as dpm
        src = (V2 / "scripts/daily_perf_mail.py").read_text(encoding="utf-8")
        assert "and not subscriptions.is_internal(a[0])]" in src
        assert "or subscriptions.alert_plant(a[0]) is None]" not in src
        import datetime as dt
        d = dpm.summarize([("GTO1", "TAIGENE PPA roof (Leon, GTO)", 500.0)],
                          {"GTO1": (100.0, 5.0, 2)}, {"GTO1": 2}, {}, {},
                          [("unit-failed:argia-kpi.service", "CRITICAL"),
                           ("recon-fail:GTO1:2026-09-05", "WARNING"),
                           ("plant-stale:GTO1", "CRITICAL"),
                           ("plant-dark:NL2", "CRITICAL")],
                          set(), dt.date(2026, 9, 6), "19:00")
        assert [i["key"] for i in d["issues"]] == ["plant-stale:GTO1"]

    def test_daily_mail_ledger_detail_has_no_code_prefix(self):
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        import daily_perf_mail as dpm
        r = dpm.issue_record("string_fault:NL2:JJM4D4P017", "WARNING",
                             extra={"message": "NL2 JJM4D4P017: NEW string-diagnostic bit(s)",
                                    "label": "Inverter 1"})
        assert r["detail"] == "Inverter 1 · SN JJM4D4P017 · NEW string-diagnostic bit(s)"


# ------------------------------------------------------------ 4. ntfy
class TestPiPush:
    def test_watchdog_names_the_portal_and_carries_the_http_code(self):
        rw = (V2 / "pi/report_watch/report_watch.sh").read_text(encoding="utf-8")
        assert 'alert "portal.argia.com.mx is DOWN"' in rw
        assert 'alert "portal.argia.com.mx is BACK UP"' in rw
        assert "ARGIA report site" not in rw
        assert "-w '\\n__HTTP__%{http_code}'" in rw
        assert 'HTTP ${http:-none}' in rw and 'HTTP ${http:-?}' in rw
        assert '[ "$http" = 000 ] && http="no response"' in rw
        assert 'URL="https://portal.argia.com.mx/login"' in rw and 'MARKER="ARGIA"' in rw

    def test_watchdog_probe_parses_the_status_line(self, tmp_path):
        """The sed pair: the last __HTTP__ line becomes $http and leaves the body."""
        import subprocess
        script = r'''body=$(printf 'x ARGIA y\n__HTTP__401'); http=$(printf '%s' "$body" | sed -n 's/^__HTTP__//p' | tail -1); body=$(printf '%s' "$body" | sed '/^__HTTP__/d'); echo "$http|$body"'''
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.strip()
        assert out == "401|x ARGIA y"

    def test_outage_watch_names_plants(self):
        pw = (V2 / "pi/report_watch/ppa_watch.py").read_text(encoding="utf-8")
        assert "from argia.alerts.naming import short_customer" in pw
        assert 'push("%s NOT producing" % name.get(pk, pk)' in pw
        assert 'push("%s producing again" % name.get(pk, pk)' in pw
        assert '"PPA plant %s NOT producing" % pk' not in pw


class TestSourceInvariants:
    def test_no_mailer_renders_a_bare_plant_key_header(self):
        lm = (V2 / "argia/alerts/ledger_mail.py").read_text(encoding="utf-8")
        assert 'f"{plant_key} · {name}" if name else plant_key' not in lm
        assert "n.plant_full(pk)" in lm


class TestV217_1PiFollowUp:
    """What the Pi check-up found (2026-09-06): the Pi does NOT self-update
    (deploy cron off since the decommission) and its watchdog ran from a
    copy still probing report.argia.com.mx (301 -> 47 false FAILs, hourly
    "DOWN" pushes); portfolio_latest.json never existed because
    argia-dbdump had no HOME for run_job.sh."""

    def test_every_run_job_unit_sets_home(self):
        import re
        bundle = V2 / "server/bundle"
        for f in sorted(bundle.glob("*.service")):
            src = f.read_text(encoding="utf-8")
            if re.search(r"ExecStart=.*(run_job\.sh|db_backup\.sh)", src):
                assert "Environment=HOME=/root" in src, f"{f.name}: run_job.sh needs HOME under systemd"

    def test_pi_cron_example_runs_the_watchdog_from_the_repo(self):
        """The Pi's crontab must call the repo checkout, never a copy in
        ~/report_watch — the copy is how v214/v217 never reached it."""
        ex = (V2 / "pi/crontab.example").read_text(encoding="utf-8")
        assert "argia_v2/v2/pi/report_watch/report_watch.sh" in ex
        assert "argia_v2/v2/pi/report_watch/ppa_watch.sh" in ex
        assert "argia_v2/v2/pi/deploy.sh" in ex
