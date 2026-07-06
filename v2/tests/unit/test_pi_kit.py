"""Tests: the Pi migration kit must match the code it invokes.

The first draft passed --apply to scripts that don't have that flag and
invented an env name — caught by verification, locked here. Templates
that drift from reality brick a cutover at 05:00."""

import pathlib
import subprocess

V2 = pathlib.Path(__file__).resolve().parents[2]


def _read(rel):
    return (V2 / rel).read_text()


class TestPiKit:
    def test_shell_scripts_exist_and_parse(self):
        for rel in ("pi/deploy.sh", "pi/run_job.sh"):
            path = V2 / rel
            assert path.exists(), rel
            subprocess.run(["bash", "-n", str(path)], check=True)

    def test_crontab_flags_match_real_script_interfaces(self):
        cron = _read("pi/crontab.example")
        # apply-by-default scripts must get NO flag
        for line_frag in ("telemetry telemetry_5m.py",
                          "alerts-snap alerts_snapshot.py",
                          "alerts-daily alerts_daily.py"):
            line = next(l for l in cron.splitlines() if line_frag in l)
            assert "--apply" not in line and "--dry-run" not in line
        # opt-in scripts must get their real flags
        assert "dashboard_update.py --apply" in cron
        assert "dashboard_html_publish.py --apply" in cron
        assert "kpi_eod.py --dense-irradiance" in cron
        assert "report_daily.py --when yesterday" in cron
        assert "report_daily.py --when today" in cron

    def test_crontab_never_suggests_table_replacement(self):
        cron = _read("pi/crontab.example")
        assert "crontab -e" in cron
        assert "NEVER run `crontab <file>`" in cron   # v1 lives there too

    def test_env_example_uses_real_variable_names(self):
        env = _read("pi/env.example")
        for name in ("GOOGLE_SHEET_ID_V2", "GOOGLE_CREDENTIALS_FILE",
                     "GROWATT_USERNAME", "GCS_DASHBOARD_BUCKET",
                     "GOOGLE_ARCHIVE_FOLDER_ID"):
            assert name + "=" in env
        assert "GDRIVE_REPORTS_FOLDER_ID" not in env   # the invented name

    def test_runbook_keeps_watchdog_external(self):
        doc = _read("docs/PI_MIGRATION.md")
        assert "ONE JOB AT A TIME" in doc
        assert "v2-watchdog" in doc          # stays on Actions
        assert "playwright install chromium" in doc
        assert "ROLLBACK" in doc


class TestPhase1Discovery20260706:
    """Live discovery during Phase 1: v1 RUNS from ~/argia_solar_monitoring
    (a dirty clone with unpushed production edits). The kit must keep v2
    in its own home and must be structurally unable to erase local work."""

    def test_v2_lives_in_its_own_clone(self):
        assert "$HOME/argia_v2" in _read("pi/deploy.sh")
        assert "$HOME/argia_v2" in _read("pi/run_job.sh")
        cron = _read("pi/crontab.example")
        assert "/argia_v2/v2/pi/" in cron
        assert "argia_solar_monitoring/v2/pi" not in cron

    def test_deploy_refuses_dirty_tree(self):
        s = _read("pi/deploy.sh")
        assert "git status --porcelain" in s
        assert "deploy REFUSED" in s

    def test_env_is_append_not_overwrite(self):
        assert "APPEND" in _read("pi/env.example")
        doc = _read("docs/PI_MIGRATION.md")
        assert "v1_local_backup" in doc
        assert "clone git@github.com:argiasolar/argia_solar_monitoring.git argia_v2" in doc
