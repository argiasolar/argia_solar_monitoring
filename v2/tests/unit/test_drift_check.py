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
        from argia.alerts.monitor import MONITORING_UNITS          # v305: the watch list lives here
        assert "argia-drift" in MONITORING_UNITS


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
                         DC.judge_http("old-report", 301)],
               "registry": ["NL1 JGMAE65009: table says 'Inverter 1', the vendor's name is 'Inversor 3' -> 'Inverter 3'"]}
        f = DC.findings(rep)
        assert f == ["checkout abc1234 is not origin/main fff0000",
                     "hand-edited in checkout: v2/x.py",
                     "diff: /opt/argia/bundle/a.py",
                     "extra (not in git): loans.csv",
                     "inverter registry: NL1 JGMAE65009: table says 'Inverter 1', the vendor's name is 'Inversor 3' -> 'Inverter 3'",
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
    """docs/OPERATIONS.md must name every timer that exists - a job nobody
    can find in the runbook is a job nobody will fix at 3 a.m."""

    def test_every_timer_is_in_the_runbook(self):
        ops = (V2 / "docs/OPERATIONS.md").read_text(encoding="utf-8")
        missing = [p.stem for p in sorted(BUNDLE.glob("argia-*.timer")) if p.stem not in ops]
        assert missing == [], missing

    def test_checklist_and_readme_point_at_the_runbook(self):
        assert "docs/OPERATIONS.md" in (V2 / "README.md").read_text(encoding="utf-8")
        assert "OPERATIONS.md" in (V2 / "docs/GO_LIVE_CHECKLIST.md").read_text(encoding="utf-8")


class TestTestSchemaMatchesProduction:
    """v263: the end-to-end tests build their database from a copy of the
    production DDL; the morning check says when that copy is stale."""

    def _fixture(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[2] / "tests/fixtures/pg/schema.sql").read_text(encoding="utf-8")

    def test_identical_schema_is_silent(self):
        import drift_check as dc
        repo = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        live = "-- dumped\nSET statement_timeout = 0;\n\\restrict abc\n" + self._fixture()
        assert dc.schema_findings(repo, live=live) == []

    def test_a_new_production_column_is_reported(self):
        import drift_check as dc
        repo = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        live = self._fixture().replace("    plant_key text NOT NULL,", "    plant_key text NOT NULL,\n    new_col text,", 1)
        out = dc.schema_findings(repo, live=live)
        assert len(out) == 1 and "test schema is out of date: 1 DDL line(s) only in production" in out[0]

    def test_empty_dump_is_a_finding_not_a_pass(self):
        import drift_check as dc
        repo = str(__import__("pathlib").Path(__file__).resolve().parents[3])
        assert "could not compare" in dc.schema_findings(repo, live="")[0]

    def test_schema_findings_reach_the_report(self):
        import drift_check as dc
        rep = {"git": {"head": "a", "origin": "a", "dirty": []}, "files": [], "extras": [], "unmapped": [],
               "smoke": [], "registry": [], "schema": ["test schema is out of date: x"]}
        assert "test schema is out of date: x" in dc.findings(rep)


class TestExecuteBit:
    """v264: the backup script lost its execute bit and the backup stopped."""

    def test_direct_exec_paths_are_found_and_interpreted_ones_ignored(self):
        units = ["[Service]\nExecStart=/opt/argia/bundle/db_backup.sh\n",
                 "ExecStart=/root/argia_v2/v2/pi/run_job.sh kpi kpi_eod.py\n",
                 "ExecStart=/usr/bin/python3 /opt/argia/bundle/portal_gen.py /www\n",
                 "ExecStart=/bin/bash -c 'x.sh'\n"]
        assert DC.direct_exec_paths(units) == ["/opt/argia/bundle/db_backup.sh", "/root/argia_v2/v2/pi/run_job.sh"]

    def test_a_script_without_the_bit_is_a_finding(self):
        repo = str(V2.parent)
        out = DC.exec_findings(repo, is_exec=lambda p: not p.endswith("db_backup.sh"))
        assert out == ["not executable (a unit runs it directly): /opt/argia/bundle/db_backup.sh"]
        assert DC.exec_findings(repo, is_exec=lambda p: True) == []

    def test_exec_findings_reach_the_report(self):
        rep = {"git": {"head": "a", "origin": "a", "dirty": []}, "files": [], "extras": [], "unmapped": [],
               "smoke": [], "registry": [], "exec": ["not executable (a unit runs it directly): /x.sh"]}
        assert "not executable (a unit runs it directly): /x.sh" in DC.findings(rep)


def test_scripts_units_run_directly_are_executable_in_git():
    """The deploy copies from the git checkout, so the mode must live in git."""
    import shutil
    import subprocess
    if not shutil.which("git"):
        import pytest
        pytest.skip("git not available")
    units = [p.read_text(encoding="utf-8") for p in (V2 / "server/bundle").glob("*.service")]
    for path in DC.direct_exec_paths(units):
        rel = {"/opt/argia/bundle/": "v2/server/bundle/", "/root/argia_v2/v2/": "v2/"}
        repo_rel = next((path.replace(a, b) for a, b in rel.items() if path.startswith(a)), None)
        if repo_rel is None:
            continue
        out = subprocess.run(["git", "ls-files", "-s", repo_rel], cwd=str(V2.parent), capture_output=True, text=True).stdout
        if not out:
            continue                                   # not a git checkout (a bundle): nothing to check
        assert out.startswith("100755"), f"{repo_rel} is {out.split()[0]} in git - run: git update-index --chmod=+x {repo_rel}"
