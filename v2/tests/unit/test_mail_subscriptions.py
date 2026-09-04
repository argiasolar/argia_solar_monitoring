"""Tests: per-channel mail subscriptions + daily performance mail (v176).

Three separate lists (maintenance / financial / daily), portal users
only, per-plant scoping on maintenance. What must never regress:
- a plant-scoped subscriber NEVER sees another plant's alerts, and
  NEVER sees infrastructure alerts (disk, failed units, PG down);
- an all-plants subscriber sees everything, including infra;
- the setup-app copy of the table DDL stays byte-identical to the
  argia package's (the bundle cannot import argia);
- each mailer reads its OWN channel;
- the 19:00 daily mail is honest about preliminary data and keeps
  dark-text-on-light styling.
"""

import datetime as dt
import pathlib
import sqlite3

from argia.alerts import emailer, monitor, subscriptions as subs
from scripts import daily_perf_mail as dpm

V2 = pathlib.Path(__file__).resolve().parents[2]
SETUP_SRC = (V2 / "server" / "bundle" / "setup_app.py").read_text(
    encoding="utf-8")
MAILER_SRC = (V2 / "scripts" / "alert_mailer.py").read_text(
    encoding="utf-8")
FINMAIL_SRC = (V2 / "scripts" / "financial_mail.py").read_text(
    encoding="utf-8")
SCHEMA_SRC = (V2 / "server" / "bundle" / "schema.sql").read_text(
    encoding="utf-8")


def A(key, sev=monitor.SEV_CRIT):
    return monitor.Alert(key, sev, f"t:{key}", f"d:{key}")


# ------------------------------------------------------------ pure scope
class TestScope:
    def test_parse_plants_empty_means_all(self):
        assert subs.parse_plants("") is None
        assert subs.parse_plants(None) is None
        assert subs.parse_plants("  , ") is None

    def test_parse_plants_normalizes(self):
        assert subs.parse_plants("gto1, MEX1 ,slp2") == frozenset(
            {"GTO1", "MEX1", "SLP2"})

    def test_plants_field_roundtrip(self):
        assert subs.plants_field(["mex1", "GTO1"]) == "GTO1,MEX1"
        assert subs.plants_field(None) == ""
        assert subs.parse_plants(subs.plants_field(["nl1"])) == \
            frozenset({"NL1"})

    def test_alert_plant_extraction(self):
        assert subs.alert_plant("plant-dark:GTO1") == "GTO1"
        assert subs.alert_plant("plant-stale:SLP2") == "SLP2"
        assert subs.alert_plant("inverter-silent:GTO1:SN123") == "GTO1"
        assert subs.alert_plant("recon-fail:NL1:2026-09-01") == "NL1"
        assert subs.alert_plant("satellite-drift:MEX1") == "MEX1"

    def test_infra_alerts_have_no_plant(self):
        for key in ("disk-full", "postgres-down",
                    "unit-failed:argia-kpi.service", "cfe-heartbeat",
                    "cfe-coverage"):
            assert subs.alert_plant(key) is None, key

    def test_all_scope_covers_everything(self):
        assert subs.covers(None, "GTO1")
        assert subs.covers(None, None)          # infra included

    def test_limited_scope_excludes_other_plants_and_infra(self):
        scope = frozenset({"GTO1"})
        assert subs.covers(scope, "GTO1")
        assert not subs.covers(scope, "MEX1")
        assert not subs.covers(scope, None)     # NO infra noise

    def test_filter_alerts_keeps_order(self):
        alerts = [A("plant-dark:GTO1"), A("disk-full"),
                  A("plant-stale:MEX1")]
        got = subs.filter_alerts(alerts, frozenset({"MEX1", "GTO1"}))
        assert [a.key for a in got] == ["plant-dark:GTO1",
                                       "plant-stale:MEX1"]


class TestGrouping:
    def test_identical_views_share_one_mail(self):
        alerts = [A("plant-dark:GTO1")]
        rcpt = [("a@x.mx", None), ("b@x.mx", None)]
        groups = subs.group_recipients(alerts, [], rcpt)
        assert len(groups) == 1
        emails, g_alerts, _ = groups[0]
        assert emails == ["a@x.mx", "b@x.mx"]
        assert [a.key for a in g_alerts] == ["plant-dark:GTO1"]

    def test_scoped_subscriber_gets_own_plant_only(self):
        alerts = [A("plant-dark:GTO1"), A("plant-dark:MEX1"),
                  A("disk-full")]
        rcpt = [("admin@x.mx", None),
                ("client@x.mx", frozenset({"MEX1"}))]
        groups = subs.group_recipients(alerts, [], rcpt)
        assert len(groups) == 2
        views = {tuple(g[0]): [a.key for a in g[1]] for g in groups}
        assert views[("admin@x.mx",)] == [
            "plant-dark:GTO1", "plant-dark:MEX1", "disk-full"]
        assert views[("client@x.mx",)] == ["plant-dark:MEX1"]

    def test_empty_view_sends_nothing(self):
        alerts = [A("disk-full")]
        rcpt = [("client@x.mx", frozenset({"MEX1"}))]
        assert subs.group_recipients(alerts, [], rcpt) == []

    def test_recovered_keys_are_scoped_too(self):
        rcpt = [("client@x.mx", frozenset({"GTO1"})),
                ("admin@x.mx", None)]
        groups = subs.group_recipients(
            [], ["plant-dark:GTO1", "postgres-down"], rcpt)
        views = {tuple(g[0]): g[2] for g in groups}
        assert views[("client@x.mx",)] == ["plant-dark:GTO1"]
        assert views[("admin@x.mx",)] == ["plant-dark:GTO1",
                                          "postgres-down"]


class TestPortalOnly:
    def test_portal_emails_reads_enabled_accounts(self, tmp_path):
        p = tmp_path / "users.db"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE users (username TEXT, email TEXT,"
                  " disabled INTEGER)")
        c.executemany("INSERT INTO users VALUES (?,?,?)",
                      [("t", "T@Argia.MX", 0), ("gone", "x@y.mx", 1),
                       ("noaddr", "", 0)])
        c.commit()
        c.close()
        assert subs.portal_emails(str(p)) == frozenset({"t@argia.mx"})

    def test_missing_db_returns_none_and_skips_filter(self, tmp_path):
        assert subs.portal_emails(str(tmp_path / "absent.db")) is None
        rcpt = [("a@x.mx", None)]
        assert subs.only_portal(rcpt, None) == rcpt

    def test_non_portal_address_is_dropped_case_insensitively(self):
        rcpt = [("T@Argia.MX", None), ("stranger@evil.com", None)]
        got = subs.only_portal(rcpt, frozenset({"t@argia.mx"}))
        assert got == [("T@Argia.MX", None)]


# --------------------------------------------------- wiring (source-level)
class TestWiring:
    def test_setup_app_ddl_is_byte_identical_to_package(self):
        # the bundle can't import argia — the DDL is duplicated and
        # this test is what keeps the copies from drifting
        assert subs.ENSURE_SQL in SETUP_SRC
        assert "CREATE TABLE IF NOT EXISTS mail_subscription" \
            in SCHEMA_SRC

    def test_channels_agree_everywhere(self):
        # v196: + 'reports' (the daily PDF reports, ex Report_Outbox)
        assert subs.CHANNELS == ("maintenance", "financial", "daily", "reports")
        assert "MAIL_CHANNELS = ('maintenance', 'financial', 'daily', 'reports')" \
            in SETUP_SRC

    def test_each_mailer_reads_its_own_channel(self):
        assert 'recipients_for("maintenance")' in MAILER_SRC
        assert 'recipients_for("financial")' in FINMAIL_SRC
        dpm_src = (V2 / "scripts" / "daily_perf_mail.py").read_text(
            encoding="utf-8")
        assert 'recipients_for("daily")' in dpm_src

    def test_mailers_enforce_portal_only(self):
        for src in (MAILER_SRC, FINMAIL_SRC):
            assert "only_portal" in src and "portal_emails" in src

    def test_alert_mailer_groups_by_scope(self):
        assert "group_recipients" in MAILER_SRC
        assert "mail_recipient" not in MAILER_SRC.replace(
            "mail_recipients", "")  # legacy table fully gone

    def test_setup_add_offers_portal_users_only(self):
        # /mail/add resolves the address from the users DB — a raw
        # email field would reopen the free-for-all list
        assert "_mail_users()" in SETUP_SRC
        assert 'request.form.get(\'username\')' in SETUP_SRC
        add = SETUP_SRC.split("def mail_add():")[1].split("@app.post")[0]
        assert "form.get('email')" not in add

    def test_setup_scoping_is_maintenance_only(self):
        add = SETUP_SRC.split("def mail_add():")[1].split("@app.post")[0]
        assert "if chan == 'maintenance':" in add

    def test_old_recipients_card_is_gone(self):
        assert "recipients_card" not in SETUP_SRC
        assert "subscriptions_card()" in SETUP_SRC


class TestTimerUnits:
    def test_daily_perf_fires_1900_mx_persistent(self):
        timer = (V2 / "server" / "bundle" / "argia-dailyperf.timer"
                 ).read_text(encoding="utf-8")
        assert "OnCalendar=*-*-* 19:00:00 America/Mexico_City" in timer
        assert "Persistent=true" in timer

    def test_daily_perf_service_runs_the_script_via_run_job(self):
        svc = (V2 / "server" / "bundle" / "argia-dailyperf.service"
               ).read_text(encoding="utf-8")
        assert "run_job.sh dailyperf daily_perf_mail.py" in svc


# ------------------------------------------------------- daily perf mail
def _mkdata(**over):
    plants = [("GTO1", "Cliente Uno", 500.0), ("MEX1", "SAG", 597.78)]
    today_map = {"GTO1": (2100.0, 5.0, 4), "MEX1": (2400.0, 12.0, 6)}
    inv = {"GTO1": 4, "MEX1": 6}
    yday = {"GTO1": 2500.0, "MEX1": 2900.0}
    mtd = {"GTO1": (5000.0, 5000.0, 5200.0),
           "MEX1": (6000.0, 5800.0, 5700.0)}
    args = dict(plants=plants, today_map=today_map, inv_counts=inv,
                yday_map=yday, mtd_map=mtd, alerts=[], maint=[],
                today=dt.date(2026, 9, 2), now_hm="19:00")
    args.update(over)
    return dpm.summarize(args["plants"], args["today_map"],
                         args["inv_counts"], args["yday_map"],
                         args["mtd_map"], args["alerts"], args["maint"],
                         args["today"], args["now_hm"])


class TestDailySummary:
    def test_ok_plant_and_totals(self):
        d = _mkdata()
        assert d["tot_today"] == 4500.0
        assert d["rows"][0]["status"] == "OK"
        assert d["rows"][0]["cls"] == "ok"
        assert abs(d["rows"][0]["yield"] - 4.2) < 0.001
        # MTD vs expected is PAIRED (v174 lesson): 10800/10900
        assert abs(d["tot_vs_exp"] - 100 * 10800 / 10900) < 0.01

    def test_dark_plant_is_red(self):
        d = _mkdata(today_map={"MEX1": (2400.0, 12.0, 6)})
        gto = [r for r in d["rows"] if r["key"] == "GTO1"][0]
        assert gto["status"] == "dark" and gto["cls"] == "bad"
        assert d["n_bad"] == 1

    def test_stale_plant_is_amber(self):
        d = _mkdata(today_map={"GTO1": (2100.0, 90.0, 4),
                               "MEX1": (2400.0, 12.0, 6)})
        gto = [r for r in d["rows"] if r["key"] == "GTO1"][0]
        assert gto["cls"] == "warn" and "stale" in gto["status"]

    def test_maintenance_beats_dark(self):
        d = _mkdata(today_map={"MEX1": (2400.0, 12.0, 6)},
                    maint=["GTO1"])
        gto = [r for r in d["rows"] if r["key"] == "GTO1"][0]
        assert gto["status"] == "maintenance" and gto["cls"] == "warn"

    def test_critical_alert_flags_plant(self):
        d = _mkdata(alerts=[("recon-fail:GTO1:2026-09-01", "CRITICAL")])
        gto = [r for r in d["rows"] if r["key"] == "GTO1"][0]
        assert gto["status"] == "issues" and gto["cls"] == "bad"

    def test_issues_keep_ppa_and_infra_only(self):
        d = _mkdata(alerts=[("plant-dark:QRO1", "CRITICAL"),   # CAPEX
                            ("plant-stale:GTO1", "CRITICAL"),
                            ("disk-full", "WARNING")])
        texts = [i["who"] + " " + i["what"] for i in d["issues"]]
        assert any("GTO1" in t for t in texts)
        assert any("disk" in t for t in texts)
        assert not any("QRO1" in t for t in texts)

    def test_no_expected_yet_shows_dash_not_zero(self):
        d = _mkdata(mtd_map={})
        assert d["tot_vs_exp"] is None
        assert d["rows"][0]["vs_exp"] is None
        assert "—" in dpm.render_text(d)

    def test_describe_issue(self):
        assert dpm.describe_issue("plant-stale:GTO1") == \
            "GTO1: telemetry stale"
        assert dpm.describe_issue("inverter-silent:GTO1:SN9") == \
            "GTO1: inverter silent (SN9)"
        assert "disk-full" in dpm.describe_issue("disk-full")

    def test_subject_flags_attention(self):
        ok = dpm.mail_subject(_mkdata())
        assert "all OK" in ok and "2026-09-02" in ok
        bad = dpm.mail_subject(_mkdata(
            today_map={"MEX1": (2400.0, 12.0, 6)}))
        assert "need attention" in bad


class TestDailyTemplates:
    def test_text_is_honest_about_preliminary_data(self):
        txt = dpm.render_text(_mkdata())
        assert "preliminary" in txt
        assert "not yet reconciled" in txt
        assert "GTO1" in txt and "MEX1" in txt

    def test_html_invariants(self):
        h = dpm.render_html(_mkdata())
        # honest banner + both plants + the four KPI tiles
        assert "not yet\nreconciled" in h or "not yet reconciled" in h
        assert "GTO1" in h and "MEX1" in h
        for tile in ("Today (live)", "Yesterday", "Month to date",
                     "MTD vs expected"):
            assert tile in h
        # house rule: dark text on light ground, no dark-bg tooltips —
        # body text color and page background stay light-theme
        assert "color:#243041" in h
        assert "background:#f2f5f8" in h
        # email-safe: no external assets — the only image is the
        # CID-embedded ARGIA SOLAR logo (v180); Gmail strips data: URIs
        assert 'src="cid:argialogo"' in h
        assert 'src="http' not in h and "<script" not in h \
            and "<link" not in h

    def test_html_escapes_customer_names(self):
        d = _mkdata(plants=[("GTO1", "A<b>&Co", 500.0),
                            ("MEX1", "SAG", 597.78)])
        h = dpm.render_html(d)
        assert "A&lt;b&gt;&amp;Co" in h and "A<b>&Co" not in h

    def test_html_email_is_multipart_with_html_alternative(self):
        msg = emailer.build_html_email("s", "plain body", "<p>html</p>",
                                      "svc@argia.com.mx", ["t@x.mx"])
        parts = [p.get_content_type() for p in msg.walk()]
        assert "text/plain" in parts and "text/html" in parts


class TestPostRedirectGet:
    """v176.1 — Tomasz's 404: relative form actions broke after the
    first POST parked the browser on /setup/mail/add. Locked here:
    absolute actions + redirect-back-home on every mail/maint POST."""

    SRC = (V2 / "server" / "bundle" / "setup_app.py").read_text(
        encoding="utf-8")

    def test_mail_and_maint_forms_use_absolute_actions(self):
        for a in ("mail/add", "mail/toggle", "mail/delete", "maint/add",
                  "maint/close", "maint/approve", "maint/delete"):
            assert f'action="/setup/{a}"' in self.SRC, a
            assert f'action="{a}"' not in self.SRC, a

    def test_mail_and_maint_posts_redirect_home(self):
        # every mail_*/maint_* handler ends in _post_done, never in a
        # direct render() that would strand the browser on the POST URL
        for name in ("mail_add", "mail_toggle", "mail_delete",
                     "maint_add", "maint_close", "maint_approve",
                     "maint_delete"):
            body = self.SRC.split(f"def {name}():")[1].split("\n@app.")[0]
            assert "_post_done(" in body, name
            assert "return render(" not in body, name

    def test_post_done_is_a_303_to_setup_home(self):
        body = self.SRC.split("def _post_done(")[1].split("\ndef ")[0]
        assert "'/setup/?m='" in body and "code=303" in body

    def test_render_picks_up_redirect_message(self):
        body = self.SRC.split("def render(")[1].split("\ndef ")[0]
        assert "request.args.get('m')" in body


class TestV180Branding:
    """Tomasz, v180: every report mail carries the ARGIA SOLAR logo,
    leads with customer names (codes demoted to the description, which
    always includes the kWp size), and the plant table always ends in
    a TOTAL summary row — even with the KPI tiles above."""

    def test_display_name_matches_monitoring_gen_copy(self):
        # two standalone copies (the bundle can't import argia) — this
        # is what keeps them from drifting
        import re

        def code_lines(src):
            m = re.search(r"def display_name\(customer\):.*?"
                          r"(?=\n\ndef |\nclass )", src, re.S)
            out, indoc = [], False
            for ln in m.group(0).splitlines():
                st = ln.strip()
                if st.startswith('\"\"\"'):
                    if st.count('\"\"\"') == 1:
                        indoc = not indoc
                    continue
                if indoc or not st or st.startswith("#"):
                    continue
                out.append(st)
            return out

        gen = (V2 / "server" / "monitoring_gen.py").read_text(
            encoding="utf-8")
        mail = (V2 / "scripts" / "daily_perf_mail.py").read_text(
            encoding="utf-8")
        assert code_lines(gen) == code_lines(mail)

    def test_display_name_behavior(self):
        assert dpm.display_name("TAIGENE PPA roof (Leon, GTO)") == \
            "Taigene"
        assert dpm.display_name("SAG PPA roof (CDMX, MEX)") == "SAG"

    def test_logo_reads_the_official_asset(self):
        b = dpm.logo_png()
        assert b is not None and b[:8] == b"\x89PNG\r\n\x1a\n"

    def test_html_email_embeds_cid_images(self):
        msg = emailer.build_html_email(
            "s", "p", '<img src="cid:argialogo">', "svc@argia.com.mx",
            ["t@x.mx"], images={"argialogo": (b"\x89PNG_fake", "png")})
        types = [p.get_content_type() for p in msg.walk()]
        assert "image/png" in types
        img = [p for p in msg.walk()
               if p.get_content_type() == "image/png"][0]
        assert img["Content-ID"] == "<argialogo>"

    def test_rows_lead_with_name_and_describe_with_code_and_kwp(self):
        d = _mkdata()
        h = dpm.render_html(d)
        assert "<b style=\"color:#16324f;font-size:13.5px\">" in h
        gto = [r for r in d["rows"] if r["key"] == "GTO1"][0]
        assert gto["label"] == "Cliente Uno"
        assert gto["desc"] == "GTO1 · Cliente Uno · 500 kWp"
        assert "GTO1 · Cliente Uno · 500 kWp" in h

    def test_summary_row_always_present(self):
        d = _mkdata()
        h = dpm.render_html(d)
        assert ">TOTAL" in h
        assert "2 plants · 1,098 kWp" in h          # 500 + 597.78
        assert "border-top:2px solid #16324f" in h
        txt = dpm.render_text(d)
        assert "TOTAL" in txt and "1,098 kWp" in txt

    def test_totals_are_sums_not_copies(self):
        d = _mkdata()
        assert d["tot_kwp"] == 500.0 + 597.78
        assert abs(d["tot_yield"] - d["tot_today"] / d["tot_kwp"]) < 1e-9
        assert d["tot_inv"] == "10/10"


class TestIssueDetail:
    """v185 — '[CRITICAL] server: unit-failed' told Tomasz nothing on
    2026-09-03. An open issue must name the thing that broke, carry a
    troubleshooting handle, and say what it means."""

    def test_unit_alert_names_the_job_not_just_server(self):
        r = dpm.issue_record("unit-failed:argia-telemetry.service",
                             "CRITICAL")
        assert r["who"] == "Telemetry collector (Growatt + Huawei)"
        assert "argia-telemetry.service" in r["detail"]
        assert r["why"]

    def test_solaredge_unit_is_distinguishable_from_the_other_collector(self):
        a = dpm.issue_record("unit-failed:argia-telemetry.service", "C")
        b = dpm.issue_record("unit-failed:argia-telemetry-se.service", "C")
        assert a["who"] != b["who"]
        assert "SolarEdge" in b["who"]

    def test_unknown_unit_keeps_its_own_name(self):
        assert dpm.friendly_unit("argia-brand-new.service") \
            == "argia-brand-new"
        assert dpm.friendly_unit("") == "server"

    def test_unit_error_line_reaches_the_reader(self):
        r = dpm.issue_record(
            "unit-failed:argia-telemetry.service", "CRITICAL",
            extra={"error": "sheet write failed: above the limit of "
                            "10000000 cells"})
        assert "10000000 cells" in r["detail"]

    def test_plant_alert_leads_with_the_customer_name(self):
        r = dpm.issue_record("plant-stale:GTO1", "CRITICAL",
                             labels={"GTO1": "Taigene"})
        assert r["who"] == "Taigene (GTO1)"

    def test_plant_alert_without_a_label_still_names_the_plant(self):
        assert dpm.issue_record("plant-dark:NL1", "CRITICAL")["who"] == "NL1"

    def test_silent_inverter_carries_serial_and_label(self):
        r = dpm.issue_record("inverter-silent:GTO1:MWKNE9500D", "WARNING",
                             extra={"label": "Inverter 5"},
                             labels={"GTO1": "Taigene"})
        assert "MWKNE9500D" in r["detail"] and "Inverter 5" in r["detail"]
        assert r["who"] == "Taigene (GTO1)"

    def test_recon_fail_carries_the_day_in_question(self):
        r = dpm.issue_record("recon-fail:GTO2:2026-08-26", "WARNING")
        assert "2026-08-26" in r["detail"]

    def test_every_known_alert_kind_explains_itself(self):
        for kind in dpm._ISSUE_PHRASE:
            r = dpm.issue_record(f"{kind}:GTO1", "CRITICAL")
            assert r["why"], f"{kind} has no explanation"
            assert r["what"] != kind or kind not in dpm._ISSUE_PHRASE

    def test_unknown_alert_kind_degrades_without_crashing(self):
        r = dpm.issue_record("brand-new-thing", "WARNING")
        assert r["what"] == "brand-new-thing" and r["who"] == "Server"


class TestHumanizeSince:
    def test_none_is_blank(self):
        assert dpm.humanize_since(None) == ""

    def test_scales(self):
        import datetime as _dt
        now = _dt.datetime(2026, 9, 4, 12, 0, tzinfo=_dt.timezone.utc)
        mk = lambda **kw: dpm.humanize_since(now - _dt.timedelta(**kw), now)
        assert mk(minutes=25) == "25 min"
        assert mk(hours=5) == "5 h"
        assert mk(days=1) == "1 day"
        assert mk(days=3) == "3 days"

    def test_future_timestamp_is_blank_not_negative(self):
        import datetime as _dt
        now = _dt.datetime(2026, 9, 4, 12, 0, tzinfo=_dt.timezone.utc)
        assert dpm.humanize_since(now + _dt.timedelta(hours=2), now) == ""


class TestLastErrorLine:
    LOG = (
        "2026-09-04 02:40:36 INFO argia.telemetry_5m: [TAM1] 4 inverters\n"
        "2026-09-04 02:40:41 ERROR argia.telemetry_5m: [MEX1] sheet write "
        "failed: <HttpError 400 ... above the limit of 10000000 cells.>\n"
        "2026-09-04 02:40:44 INFO argia.telemetry_5m: DONE\n"
    )

    def test_picks_the_newest_error_and_drops_the_log_prefix(self):
        out = dpm.last_error_line(self.LOG)
        assert out.startswith("[MEX1] sheet write failed")
        assert "10000000 cells" in out
        assert "2026-09-04" not in out

    def test_no_error_is_blank(self):
        assert dpm.last_error_line("2026-09-04 INFO all good\n") == ""
        assert dpm.last_error_line("") == ""

    def test_long_lines_are_truncated(self):
        long = "2026-09-04 02:40:41 ERROR argia.x: " + "y" * 500
        assert len(dpm.last_error_line(long)) <= 180


class TestIssueRendering:
    def _data(self):
        return _mkdata(alerts=[
            ("unit-failed:argia-telemetry.service", "CRITICAL", None,
             {"error": "sheet write failed: above the limit of "
                       "10000000 cells"}),
            ("plant-stale:GTO1", "CRITICAL"),
        ])

    def test_html_shows_job_name_reason_and_explanation(self):
        html = dpm.render_html(self._data())
        assert "Telemetry collector (Growatt + Huawei)" in html
        assert "argia-telemetry.service" in html
        assert "10000000 cells" in html
        assert "missing or stale until it runs clean" in html
        assert "server: unit-failed" not in html

    def test_text_shows_the_same_substance(self):
        txt = dpm.render_text(self._data())
        assert "Telemetry collector (Growatt + Huawei)" in txt
        assert "argia-telemetry.service" in txt
        assert "10000000 cells" in txt

    def test_plant_issue_uses_the_customer_name(self):
        html = dpm.render_html(self._data())
        assert "Cliente Uno (GTO1)" in html


class TestMailHeader:
    def test_title_is_present_and_larger_than_the_logo(self):
        html = dpm.render_html(_mkdata())
        assert "DAILY&nbsp;PPA" in html
        assert 'font-size:17px;font-weight:600' in html

    def test_logo_is_smaller_than_it_was(self):
        html = dpm.render_html(_mkdata())
        assert 'height="19"' in html and 'height:19px' in html
        assert 'height="26"' not in html

    def test_logo_sits_on_the_right_of_the_lockup(self):
        html = dpm.render_html(_mkdata())
        title = html.index("DAILY&nbsp;PPA")
        logo = html.index("cid:argialogo")
        assert title < logo, "title must lead, logo follows on the right"

    def test_date_still_shown(self):
        html = dpm.render_html(_mkdata())
        assert "2026-09-02" in html and "MX" in html


class TestJobNameFromExecStart:
    """Caught in production on 2026-09-04: systemctl show -p ExecStart
    --value prints a STRUCTURED value, not a command line, so a naive
    '\\S+' after run_job.sh captured the ';' of the path= field and every
    unit resolved to /root/argia_logs/;.log."""

    REAL = ("{ path=/root/argia_v2/v2/pi/run_job.sh ; "
            "argv[]=/root/argia_v2/v2/pi/run_job.sh telemetry "
            "telemetry_5m.py --skip-brand SOLAREDGE ; ignore_errors=no ; "
            "start_time=[Fri 2026-09-04 03:30:00 CEST] ; pid=20613 ; "
            "code=exited ; status=0 }")

    def test_structured_systemd_value(self):
        assert dpm.job_name_from_execstart(self.REAL) == "telemetry"

    def test_never_returns_a_separator(self):
        assert ";" not in dpm.job_name_from_execstart(self.REAL)

    def test_plain_command_line_still_works(self):
        assert dpm.job_name_from_execstart(
            "/root/argia_v2/v2/pi/run_job.sh cfepush cfe_engine_push.py") \
            == "cfepush"

    def test_hyphenated_job_name(self):
        assert dpm.job_name_from_execstart(
            "{ path=/x/run_job.sh ; argv[]=/x/run_job.sh telemetry-se a.py }"
        ) == "telemetry-se"

    def test_no_match_is_blank_not_a_bogus_path(self):
        assert dpm.job_name_from_execstart("") == ""
        assert dpm.job_name_from_execstart("/usr/bin/true") == ""
