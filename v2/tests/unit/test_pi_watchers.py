"""v275 - the office Pi's watchers must not cry wolf.

Tomasz, 2026-09-29: "why am I receiving messages in ntfy that portal is down?"
The ntfy topic showed 11 'portal.argia.com.mx is DOWN' pushes in one afternoon,
every one 'curl rc=6 Could not resolve host' - the Pi's own DNS failing while
public DNS answered and the portal served every page. Each false DOWN also
switched on the outage-mode plant watch, which pushed six plants as 'NOT
producing ... frozen for 32910 min' (23 days: a baseline left over from an old
outage, compared with today's counter).
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
RW = V2 / "pi" / "report_watch"

spec = importlib.util.spec_from_file_location("ppa_watch", RW / "ppa_watch.py")
PW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PW)

DAY, NOW = "2026-09-29", 1_790_000_000.0


class TestPpaWatchStep:
    def test_the_incident_old_baseline_never_makes_today_frozen(self):
        # left over from an outage 23 days ago, counter higher than today's
        old = {"etoday": 2500.0, "ts": NOW - 32910 * 60, "last_alert": 0, "day": "2026-09-06"}
        rec, action, stalled = PW.step(old, 2031.2, NOW, DAY)
        assert action is None and stalled == 0
        assert rec["day"] == DAY and rec["etoday"] == 2031.2 and rec["ts"] == NOW

    def test_a_record_without_a_day_is_also_reset(self):          # state files written before v275
        rec, action, _ = PW.step({"etoday": 2500.0, "ts": NOW - 10 ** 6}, 2031.2, NOW, DAY)
        assert action is None and rec["etoday"] == 2031.2

    def test_frozen_counter_today_alerts_after_55_min_then_waits_2_h(self):
        rec, _, _ = PW.step({}, 800.0, NOW, DAY)
        rec, action, _ = PW.step(rec, 800.0, NOW + 30 * 60, DAY)
        assert action is None                                      # 30 min: not yet
        rec, action, stalled = PW.step(rec, 800.0, NOW + 60 * 60, DAY)
        assert action == "frozen" and stalled == pytest.approx(3600)
        rec, action, _ = PW.step(rec, 800.0, NOW + 90 * 60, DAY)
        assert action is None                                      # re-alert only after 2 h
        rec, action, _ = PW.step(rec, 812.0, NOW + 120 * 60, DAY)
        assert action == "recovered" and rec["alerted"] is False

    def test_no_answer_from_the_vendor_is_unknown_not_frozen(self):
        rec, _, _ = PW.step({}, 800.0, NOW, DAY)
        rec, action, _ = PW.step(rec, None, NOW + 2 * 3600, DAY)
        assert action == "unknown"
        rec, action, _ = PW.step(rec, None, NOW + 2.5 * 3600, DAY)
        assert action is None                                      # once per 2 h
        assert rec["etoday"] == 800.0                              # the baseline survives


FAKE_CURL = r"""#!/bin/bash
# test double for curl: MODE_NAME=ok|dnsfail|down  MODE_IP=ok|down ; ntfy posts are recorded
args="$*"
if echo "$args" | grep -q "ntfy.sh"; then
  title=""; prio=""
  while [ $# -gt 0 ]; do
    case "$1" in -H) case "$2" in Title:*) title="${2#Title: }";; Priority:*) prio="${2#Priority: }";; esac; shift 2;; *) shift;; esac
  done
  echo "$prio|$title" >> "$CALLS"; exit 0
fi
if echo "$args" | grep -q -- "--resolve"; then
  [ "$MODE_IP" = ok ] && { echo "<title>Sign in - ARGIA</title>"; exit 0; }
  echo "curl: (7) Failed to connect" >&2; exit 7
fi
case "$MODE_NAME" in
  ok) printf '<title>Sign in - ARGIA</title>\n__HTTP__401'; exit 0;;
  dnsfail) echo "curl: (6) Could not resolve host: portal.argia.com.mx" >&2; exit 6;;
  *) echo "curl: (7) Failed to connect" >&2; exit 7;;
esac
"""


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="bash harness (CI / Linux)")
class TestReportWatch:
    @pytest.fixture
    def run(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "curl").write_text(FAKE_CURL)
        (bindir / "curl").chmod(0o755)
        calls = tmp_path / "calls.txt"
        calls.write_text("")

        def go(mode_name, mode_ip="ok", times=1, push="on"):
            env = dict(os.environ, HOME=str(tmp_path), PATH=f"{bindir}:{os.environ['PATH']}",
                       CALLS=str(calls), MODE_NAME=mode_name, MODE_IP=mode_ip,
                       ARGIA_PUSH=push)                     # v305.2: pushes are off in push.conf
            out = ""
            for _ in range(times):
                r = subprocess.run(["bash", str(RW / "report_watch.sh")], env=env, capture_output=True, text=True, timeout=30)
                assert r.returncode == 0, r.stderr
                out += r.stdout
            state = (tmp_path / "report_watch" / "state").read_text()
            return out, [ln for ln in calls.read_text().splitlines() if ln], state
        return go

    def test_the_pis_dns_failing_is_not_a_portal_outage(self, run):
        out, pushes, state = run("dnsfail", "ok", times=4)          # 20 min of rc=6, like 29 Sep
        assert not any("is DOWN" in p for p in pushes), pushes
        assert pushes == ["low|Office Pi: DNS lookups failing (portal is UP)"]   # one quiet note, not 4
        assert "status=OK" in state and "fails=0" in state
        assert "OK via 37.235.105.173" in out

    def test_a_real_outage_still_pages(self, run):
        _, pushes, state = run("dnsfail", "down", times=2)          # name AND address dead
        assert pushes == ["high|portal.argia.com.mx is DOWN"]
        assert "status=DOWN" in state
        _, pushes, state = run("ok")
        assert pushes[-1] == "high|portal.argia.com.mx is BACK UP" and "status=OK" in state

    def test_server_refusing_connections_pages_as_before(self, run):
        _, pushes, _ = run("down", "down", times=2)
        assert pushes == ["high|portal.argia.com.mx is DOWN"]

    def test_healthy_portal_is_silent(self, run):
        _, pushes, state = run("ok", times=3)
        assert pushes == [] and "status=OK" in state


@pytest.mark.skipif(os.name == "nt" or shutil.which("bash") is None, reason="bash harness (CI / Linux)")
class TestPushSwitch:
    """v305.2 (Tomasz, 2026-10-04: "I do not need any push notifications to
    my phone at this moment"): push.conf says off - the watchdog still
    watches and logs, nothing reaches the phone."""

    def test_the_repo_switch_is_off(self):
        assert "\nPUSH=off\n" in (RW / "push.conf").read_text(encoding="utf-8")

    def test_a_real_outage_is_logged_not_pushed(self, tmp_path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "curl").write_text(FAKE_CURL)
        (bindir / "curl").chmod(0o755)
        calls = tmp_path / "calls.txt"
        calls.write_text("")
        env = {k: v for k, v in os.environ.items() if k != "ARGIA_PUSH"}
        env.update(HOME=str(tmp_path), PATH=f"{bindir}:{os.environ['PATH']}", CALLS=str(calls),
                   MODE_NAME="down", MODE_IP="down")
        out = ""
        for _ in range(2):
            r = subprocess.run(["bash", str(RW / "report_watch.sh")], env=env, capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr
            out += r.stdout
        assert "alert (push off): portal.argia.com.mx is DOWN" in out
        assert [ln for ln in calls.read_text().splitlines() if "ntfy" in ln or "|" in ln] == []
        assert "status=DOWN" in (tmp_path / "report_watch" / "state").read_text()

    def test_python_watchers_follow_the_switch(self, tmp_path, monkeypatch):
        import importlib.util as U
        for name in ("ppa_watch", "health_watch"):
            spec = U.spec_from_file_location(name + "_sw", RW / f"{name}.py")
            m = U.module_from_spec(spec)
            spec.loader.exec_module(m)
            monkeypatch.delenv("ARGIA_PUSH", raising=False)
            assert m.push_enabled() is False                                   # the repo's push.conf
            conf = tmp_path / "p.conf"
            conf.write_text("PUSH=on\n")
            assert m.push_enabled(str(conf)) is True
            monkeypatch.setenv("ARGIA_PUSH", "on")
            assert m.push_enabled() is True
            monkeypatch.delenv("ARGIA_PUSH")
        spec = U.spec_from_file_location("ppa_sw2", RW / "ppa_watch.py")
        m = U.module_from_spec(spec)
        spec.loader.exec_module(m)
        monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pushed")))
        m.push("SAG NOT producing", "x")                                       # logged only

    def test_the_backup_pull_follows_it_too(self):
        src = (V2 / "pi" / "db_backups" / "pull_backup.sh").read_text(encoding="utf-8")
        assert '. "$(dirname "$0")/../report_watch/push.conf"' in src and 'if [ "$PUSH" = on ]; then' in src
