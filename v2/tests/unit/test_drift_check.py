"""v218 status-quo harness: scripts/drift_check.py (pure parts) and the
admin-only alerts the mailer builds from its report."""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))
import drift_check as DC  # noqa: E402
from argia.alerts import monitor as M  # noqa: E402
from argia.alerts import subscriptions as S  # noqa: E402

BUNDLE = V2 / "server" / "bundle"
BUNDLE_FILES = sorted(p.name for p in BUNDLE.iterdir())


class TestMapping:
    def test_every_real_bundle_file_has_a_deploy_rule_or_is_declared(self):
        assert DC.unmapped(BUNDLE_FILES) == [], "add a rule in drift_check.py or list it in NOT_DEPLOYED"

    def test_rules_cover_units_bundle_and_nginx(self):
        # normalise separators: the laptop suite runs on Windows
        prs = {s.replace("\\", "/"): d.replace("\\", "/") for s, d in DC.pairs("/r", BUNDLE_FILES)}
        assert prs["/r/v2/server/bundle/argia-mailer.timer"] == "/etc/systemd/system/argia-mailer.timer"
        assert prs["/r/v2/server/bundle/setup_app.py"] == "/opt/argia/bundle/setup_app.py"
        assert prs["/r/v2/server/bundle/db_backup.sh"] == "/opt/argia/bundle/db_backup.sh"
        assert prs["/r/v2/server/monitoring_gen.py"] == "/opt/argia/bundle/monitoring_gen.py"
        assert prs["/r/v2/server/bundle/nginx-argia_session.conf"] == "/etc/nginx/snippets/argia_auth.conf"
        assert prs["/r/v2/server/bundle/portal.argia.com.mx.conf"] == "/etc/nginx/sites-enabled/portal.argia.com.mx.conf"
        assert prs["/r/v2/server/bundle/nginx-monitoring.argia.com.mx.conf"] == "/etc/nginx/sites-enabled/monitoring.argia.com.mx.conf"
        # declared not deployed -> no pair
        assert not any(s.endswith("nginx-argia_auth.conf") or s.endswith(".http.conf") or s.endswith("README.md") for s in prs)

    def test_the_duplicate_basic_auth_snippet_is_gone(self):
        assert not (BUNDLE / "nginx_argia_auth.conf").exists()
        assert (BUNDLE / "nginx-argia_auth.conf").exists()          # documented rollback stays

    def test_drift_units_exist_and_run_via_run_job(self):
        svc = (BUNDLE / "argia-drift.service").read_text(encoding="utf-8")
        tmr = (BUNDLE / "argia-drift.timer").read_text(encoding="utf-8")
        assert "run_job.sh drift drift_check.py" in svc and "Environment=HOME=/root" in svc
        assert "06:30:00 America/Mexico_City" in tmr and "Persistent=true" in tmr
        assert '"argia-drift"' in (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")


class TestCompare:
    def test_statuses(self):
        files = {"/r/a": b"x", "/d/a": b"x", "/r/b": b"y", "/d/b": b"z", "/r/c": b"q"}
        out = DC.compare([("/r/a", "/d/a"), ("/r/b", "/d/b"), ("/r/c", "/d/c"), ("/r/n", "/d/n")], files.get)
        assert [o["status"] for o in out] == ["same", "diff", "missing", "no-source"]

    def test_extras_ignore_pycache(self):
        assert DC.extras(["a.py", "old.tgz", "__pycache__", "loans.csv"], ["a.py"]) == ["loans.csv", "old.tgz"]

    def test_http_and_age_judgements(self):
        assert DC.judge_http("portal-login", 401)["ok"] and not DC.judge_http("portal-login", 200)["ok"]
        assert not DC.judge_http("portal-login", None)["ok"]
        assert DC.judge_http("old-report", 301)["ok"]
        assert [n for n, _, _ in DC.SMOKE_HTTP if n.startswith("old-")] == ["old-report", "old-monitoring"]
        assert DC.judge_age("backup-dump", 3.2)["ok"] and not DC.judge_age("backup-dump", 30.0)["ok"]
        assert not DC.judge_age("backup-dump", None)["ok"]

    def test_findings_lines(self):
        rep = {"git": {"head": "abc1234def", "origin": "fff0000aaa", "dirty": ["v2/x.py"]},
               "files": [{"src": "s", "dst": "/opt/argia/bundle/a.py", "status": "diff"},
                         {"src": "s", "dst": "/etc/systemd/system/b.timer", "status": "same"}],
               "extras": ["loans.csv"], "unmapped": [],
               "smoke": [DC.judge_http("portal-login", 502), DC.judge_age("backup-dump", 40.0),
                         {"check": "timers-active", "ok": False, "detail": "inactive: argia-kpi.timer"},
                         DC.judge_http("old-report", 301)]}
        f = DC.findings(rep)
        assert f == ["checkout abc1234 is not origin/main fff0000",
                     "hand-edited in checkout: v2/x.py",
                     "diff: /opt/argia/bundle/a.py",
                     "extra (not in git): loans.csv",
                     "portal-login: HTTP 502 (expected 401)",
                     "backup-dump: 40.0 h old (max 26.0)",
                     "timers-active: inactive: argia-kpi.timer"]

    def test_clean_report_has_no_findings(self):
        rep = {"git": {"head": "a", "origin": "a", "dirty": []}, "files": [], "extras": [], "unmapped": [],
               "smoke": [DC.judge_http("portal-login", 401)]}
        assert DC.findings(rep) == []


class TestAlerts:
    NOW = dt.datetime(2026, 9, 7, 14, 0, tzinfo=dt.timezone.utc)

    def test_missing_report_alerts(self):
        a = M.drift_alerts(None, now=self.NOW)
        assert [x.key for x in a] == ["drift-stale"] and a[0].severity == M.SEV_WARN

    def test_split_config_vs_smoke(self):
        rep = {"generated_utc": "2026-09-07T12:30:00Z",
               "findings": ["diff: /opt/argia/bundle/a.py", "extra (not in git): loans.csv",
                            "portal-login: HTTP 502 (expected 401)", "backup-dump: 40.0 h old (max 26.0)"]}
        a = {x.key: x for x in M.drift_alerts(rep, now=self.NOW)}
        assert set(a) == {"config-drift", "smoke-fail"}
        assert "loans.csv" in a["config-drift"].detail and "HTTP 502" in a["smoke-fail"].detail
        assert all(x.severity == M.SEV_WARN for x in a.values())

    def test_stale_report_alerts_even_when_clean(self):
        rep = {"generated_utc": "2026-09-05T12:30:00Z", "findings": []}
        assert [x.key for x in M.drift_alerts(rep, now=self.NOW)] == ["drift-stale"]

    def test_clean_fresh_report_is_silent(self):
        assert M.drift_alerts({"generated_utc": "2026-09-07T12:30:00Z", "findings": []}, now=self.NOW) == []

    def test_these_are_internal_admin_only(self):
        for k in ("config-drift", "smoke-fail", "drift-stale"):
            assert S.alert_plant(k) is None and S.is_internal(k)


class TestSecurityHeaders:
    def test_portal_vhost_sends_the_headers_always(self):
        conf = (BUNDLE / "portal.argia.com.mx.conf").read_text(encoding="utf-8")
        for h in ("X-Content-Type-Options nosniff always", "X-Frame-Options SAMEORIGIN always",
                  "Referrer-Policy same-origin always", 'Strict-Transport-Security "max-age=31536000" always'):
            assert f"add_header {h};" in conf, h
        # add_header is per level: no location in the vhost may define its own
        # (it would drop the server-level set)
        body = conf[conf.index("listen 443"):]
        assert "add_header" not in body[body.index("include /etc/nginx/snippets"):]


class TestRunbookCoversEveryJob:
    """docs/OPERATIONS.md must name every timer that exists — a job nobody
    can find in the runbook is a job nobody will fix at 3 a.m."""

    def test_every_timer_is_in_the_runbook(self):
        ops = (V2 / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        missing = [p.stem for p in sorted(BUNDLE.glob("argia-*.timer")) if p.stem not in ops]
        assert missing == [], missing

    def test_checklist_and_readme_point_at_the_runbook(self):
        assert "docs/OPERATIONS.md" in (V2 / "README.md").read_text(encoding="utf-8")
        assert "OPERATIONS.md" in (V2 / "docs/GO_LIVE_CHECKLIST.md").read_text(encoding="utf-8")
