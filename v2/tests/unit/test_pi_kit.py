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

    def test_crontab_is_the_post_decommission_shape(self):
        """v217.1: since 2026-08-26 the Pi runs no collection/report job —
        pio06's systemd does. What remains must run from the repo checkout
        (deploy.sh keeps it current); no ~/report_watch copies."""
        cron = _read("pi/crontab.example")
        active = [l for l in cron.splitlines() if l and not l.startswith("#")]
        assert any("argia_v2/v2/pi/deploy.sh" in l for l in active)
        assert any("argia_v2/v2/pi/report_watch/report_watch.sh" in l for l in active)
        assert any("argia_v2/v2/pi/report_watch/ppa_watch.sh" in l for l in active)
        assert any("argia_v2/v2/pi/db_backups/pull_backup.sh" in l for l in active)
        assert not any("/home/zemel/report_watch/" in l for l in active)
        for retired in ("telemetry_5m.py", "alerts_snapshot.py", "kpi_eod.py", "report_daily.py"):
            assert retired not in cron, f"{retired} runs on pio06 now"

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
        # tracked-only (2026-07-07): reset --hard cannot destroy untracked
        # files, so untracked build artifacts must not block deploys —
        # a dashboard.html in the tree stalled three pushes for hours
        assert "git status --porcelain --untracked-files=no" in s
        assert "deploy REFUSED" in s

    def test_env_is_append_not_overwrite(self):
        assert "APPEND" in _read("pi/env.example")
        doc = _read("docs/PI_MIGRATION.md")
        assert "v1_local_backup" in doc
        assert "clone git@github.com:argiasolar/argia_solar_monitoring.git argia_v2" in doc


def test_shell_scripts_are_executable_in_git():
    """2026-07-06 live failure: the Windows zip->unzip->git path dropped
    the execute bit, so cron got 'Permission denied' (exit 126) silently
    every 10 minutes. The bit must live IN GIT (update-index --chmod=+x)
    so every clone gets it. Trivially true on Windows (no x concept);
    enforced on Linux CI and the Pi — where it matters."""
    import os
    for rel in ("pi/deploy.sh", "pi/run_job.sh"):
        assert os.access(V2 / rel, os.X_OK), f"{rel} not executable"


def test_run_job_sets_pythonpath():
    """2026-07-06 smoke-test catch: the wrapper cd'd into v2/ but never
    set PYTHONPATH, so every job died with ModuleNotFoundError. The
    scripts import argia exactly like the workflows do — with an
    explicit PYTHONPATH."""
    assert "PYTHONPATH" in _read("pi/run_job.sh")
