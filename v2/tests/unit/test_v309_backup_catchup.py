"""v309: a missed nightly backup pull is retried by the Pi itself.

4 Oct: the server's last-resort mail "off-site backup not pulled - 694 h old"
was right. The 22:00 pull had died with "Permission denied" every night since
5 Sep, and nothing retried. v307 fixed the file mode, but the proof would have
waited for the next 22:00 run, and one bad night would again mean a day
without an off-site copy. Now report_watch (every 5 min) runs catchup.sh: when
the newest dump is older than 26 h it pulls (with bash, so the file mode no
longer matters), at most once an hour; pull_backup.sh holds a lock so the
catch-up and the 22:00 run never overlap.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
CATCHUP = V2 / "pi" / "db_backups" / "catchup.sh"
NOW = 1_791_000_000                      # a fixed epoch; files get mtimes relative to it

needs_bash = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("stat"),
                                reason="runs the real shell script (Linux CI)")


def run(tmp_path, dump_age_h=None, last_try_ago=None, pull_rc=0, key=True):
    daily = tmp_path / "daily"
    daily.mkdir(exist_ok=True)
    if dump_age_h is not None:
        d = daily / "argia_mont_20260905.dump"
        d.write_bytes(b"PGDMP")
        os.utime(d, (NOW - dump_age_h * 3600, NOW - dump_age_h * 3600))
    keyf = tmp_path / "key"
    if key:
        keyf.write_text("k")
    stamp = tmp_path / "state" / "backup_catchup_at"
    if last_try_ago is not None:
        stamp.parent.mkdir(exist_ok=True)
        stamp.write_text(str(NOW - last_try_ago))
    calls = tmp_path / "calls"
    pull = tmp_path / "fake_pull.sh"
    pull.write_text(f'echo pulled >> "{calls}"\nexit {pull_rc}\n')
    env = dict(os.environ, BK_DIR=str(daily), KEY=str(keyf), STAMP=str(stamp), PULL=str(pull),
               LOG=str(tmp_path / "pull.log"), NOW=str(NOW))
    r = subprocess.run(["bash", str(CATCHUP)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    n = len(calls.read_text().splitlines()) if calls.exists() else 0
    return r.stdout, n, (stamp.read_text().strip() if stamp.exists() else None)


@needs_bash
class TestCatchup:
    def test_fresh_backup_does_nothing(self, tmp_path):
        out, n, st = run(tmp_path, dump_age_h=20)
        assert (out, n, st) == ("", 0, None)

    def test_the_4_oct_case_pulls_now(self, tmp_path):
        out, n, st = run(tmp_path, dump_age_h=694)
        assert n == 1 and "694 h old - pulling now" in out and "pull OK" in out and st == str(NOW)

    def test_no_dump_at_all_is_overdue(self, tmp_path):
        out, n, _ = run(tmp_path, dump_age_h=None)
        assert n == 1 and "pull OK" in out

    def test_at_most_once_an_hour(self, tmp_path):
        assert run(tmp_path, dump_age_h=30, last_try_ago=600)[1] == 0
        assert run(tmp_path, dump_age_h=30, last_try_ago=3600)[1] == 1

    def test_a_failed_pull_is_reported_and_retried_later(self, tmp_path):
        out, n, st = run(tmp_path, dump_age_h=30, pull_rc=1)
        assert n == 1 and "pull FAILED" in out and "next try in 60 min" in out and st == str(NOW)

    def test_not_the_pi(self, tmp_path):
        assert run(tmp_path, dump_age_h=694, key=False) == ("", 0, None)

    def test_boundary_26_h(self, tmp_path):
        assert run(tmp_path, dump_age_h=26)[1] == 0          # exactly 26 h: still fine
        assert run(tmp_path, dump_age_h=27)[1] == 1


def _read(rel):
    return (V2 / rel).read_text(encoding="utf-8")


class TestWiring:
    def test_report_watch_runs_it_and_sends_the_status_after_a_pull(self):
        src = _read("pi/report_watch/report_watch.sh")
        assert 'CU_OUT=$(bash "$(dirname "$0")/../db_backups/catchup.sh" 2>&1)' in src
        assert 'case "$CU_OUT" in *"pull OK"*) rm -f "$STATE_DIR/pi_status_at" ;; esac' in src
        assert src.index("catchup.sh") < src.index('PS_STAMP="$STATE_DIR/pi_status_at"')   # before the status report

    def test_pull_backup_holds_a_lock(self):
        src = _read("pi/db_backups/pull_backup.sh")
        assert 'exec 9>"$BASE/.pull.lock"' in src and "flock -n 9" in src
        assert src.index("flock -n 9") < src.index("sftp -q")                              # before any download

    def test_catchup_runs_the_pull_with_bash(self):
        assert 'bash "$PULL"' in _read("pi/db_backups/catchup.sh")

    def test_gapfill_manifest_only_when_it_exists(self):
        src = _read("pi/cfe/cfe_daily.sh")
        assert 'push "$GAP" && push "$GAP.manifest.json"' not in src
        assert src.count('{ [ ! -f "$GAP.manifest.json" ] || push "$GAP.manifest.json"; }') == 2


def test_pi_status_reports_whether_the_pull_script_is_executable(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("pi_status_v309", V2 / "pi" / "report_watch" / "pi_status.py")
    ps = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ps)
    doc = ps.build(NOW, {"pull_exec": True}, [], "x", 1, "pi")
    assert doc["backup"]["pull_exec"] is True
    assert ps.build(NOW, {}, [], "x", 1, "pi")["backup"]["pull_exec"] is None
    assert ps.build(NOW, {"python": "3.11.2"}, [], "x", 1, "pi")["python"] == "3.11.2"      # v312
