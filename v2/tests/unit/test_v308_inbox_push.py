"""v308: the Pi's pushes land again.

Found 2026-10-04 by reproducing the Pi's push on the server against the same
forced command (``rrsync -wo``): since the Debian security update of rsync to
3.5.0 (29 Sep 2026 04:04) the write-only jail accepts a NEW file but refuses
to replace an existing one:

    rsync: [generator] delete_file: unlink(4) failed: Operation not permitted (1)
    could not make way for new regular file: 4      (exit 23)

So the server's CFE heartbeat froze on 28 Sep and the Pi's hourly status
landed once (15:45 UTC, 4 Oct) and never again. The daily CSVs kept landing
because they carry a new name each day and the ingest moves them away.

Fix: every fixed-name push gets a UTC-stamped name; the server reads the
newest and removes the older ones (``newest_push``, one copy in the argia
package for alert_mailer and one in the bundle for cfe_ingest - this file
runs both); manifests are filed to processed/ so a retried month is not
blocked; the month's MARK no longer depends on the manifest push.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import pathlib
import re
import sys

import pytest

from argia.alerts import monitor as M

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, V2 / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ING = _load("cfe_ingest_v308", "server/bundle/cfe_ingest.py")
PS = _load("pi_status_v308", "pi/report_watch/pi_status.py")
NOW = dt.datetime(2026, 10, 4, 19, 0, tzinfo=dt.timezone.utc).timestamp()
IMPLS = [pytest.param(M.newest_push, id="monitor"), pytest.param(ING.newest_push, id="cfe_ingest")]


def put(d, name, body="{}", mtime=None):
    p = d / name
    p.write_text(body, encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


class FakeJail:
    """rsync 3.5.0 + rrsync -wo as observed on the server: a new name lands,
    an existing name is refused (exit 23) and the old file stays."""

    def __init__(self, d):
        self.d = d

    def push(self, name, body):
        if (self.d / name).exists():
            return 23
        (self.d / name).write_text(body, encoding="utf-8")
        return 0


# ------------------------------------------------------------ newest_push
@pytest.mark.parametrize("newest_push", IMPLS)
class TestNewestPush:
    def test_picks_the_newest_stamp_and_removes_the_older(self, tmp_path, newest_push):
        for s in ("20261004T170000Z", "20261004T190000Z", "20261004T180000Z"):
            put(tmp_path, f"pi_status_{s}.json")
        got = newest_push(str(tmp_path), "pi_status")
        assert os.path.basename(got) == "pi_status_20261004T190000Z.json"
        assert sorted(os.listdir(tmp_path)) == ["pi_status_20261004T190000Z.json"]

    def test_legacy_fixed_name_ranks_by_its_mtime(self, tmp_path, newest_push):
        put(tmp_path, "heartbeat.json", mtime=dt.datetime(2026, 9, 28, 14, 43, tzinfo=dt.timezone.utc).timestamp())
        assert os.path.basename(newest_push(str(tmp_path), "heartbeat")) == "heartbeat.json"   # alone: still read
        put(tmp_path, "heartbeat_20261005T141000Z.json")
        assert os.path.basename(newest_push(str(tmp_path), "heartbeat")) == "heartbeat_20261005T141000Z.json"
        assert not (tmp_path / "heartbeat.json").exists()                                     # the frozen one is gone

    def test_a_legacy_file_newer_than_the_stamps_wins(self, tmp_path, newest_push):
        put(tmp_path, "pi_status_20261004T150000Z.json")
        put(tmp_path, "pi_status.json", mtime=dt.datetime(2026, 10, 4, 16, 0, tzinfo=dt.timezone.utc).timestamp())
        assert os.path.basename(newest_push(str(tmp_path), "pi_status")) == "pi_status.json"

    def test_other_files_are_never_touched(self, tmp_path, newest_push):
        keep = ["heartbeat_20261004T140000Z.json", "cfe_gapfill_20261004.csv", "cfe_2026-10_full.csv.manifest.json",
                ".pi_status_20261004T200000Z.json.Ab12Cd",          # rsync's temp file while a push is in flight
                "pi_status_latest.json", "xpi_status_20261004T200000Z.json"]
        for n in keep:
            put(tmp_path, n)
        put(tmp_path, "pi_status_20261004T190000Z.json")
        assert os.path.basename(newest_push(str(tmp_path), "pi_status")) == "pi_status_20261004T190000Z.json"
        assert set(keep) <= set(os.listdir(tmp_path))

    def test_nothing_there(self, tmp_path, newest_push):
        assert newest_push(str(tmp_path), "pi_status") is None
        assert newest_push(str(tmp_path / "missing"), "pi_status") is None

    def test_prune_off_keeps_everything(self, tmp_path, newest_push):
        put(tmp_path, "pi_status_20261004T170000Z.json")
        put(tmp_path, "pi_status_20261004T180000Z.json")
        assert os.path.basename(newest_push(str(tmp_path), "pi_status", prune=False)) == "pi_status_20261004T180000Z.json"
        assert len(os.listdir(tmp_path)) == 2


def test_both_copies_are_the_same_code():
    """The bundle runs without the argia package, so cfe_ingest keeps its own
    copy; the two must not drift."""
    import inspect
    body = lambda f: inspect.getsource(f).split('"""')[-1]          # the code after the docstring
    assert body(M.newest_push).replace(": str", "").replace(": bool", "").replace(" -> Optional[str]", "") \
        == body(ING.newest_push)
    assert M._PUSHED.pattern == ING._PUSHED.pattern


# ------------------------------------------------------------ the Pi side
class TestThePiPushesNewNames:
    def test_push_name_is_utc_stamped_and_read_back_by_the_server(self):
        n = PS.push_name(NOW)
        assert n == "pi_status_20261004T190000Z.json"
        m = M._PUSHED.match(n)
        assert m and m.group("stem") == "pi_status"

    def test_main_pushes_under_the_stamped_name(self, tmp_path, monkeypatch):
        (tmp_path / ".ssh").mkdir()
        (tmp_path / ".ssh" / "config").write_text("Host argia-cfe\n")
        monkeypatch.setattr(PS, "HOME", str(tmp_path))
        monkeypatch.setattr(PS, "gather", lambda now: {"ts": "x"})
        calls = []

        class R:
            returncode, stderr = 0, ""

        monkeypatch.setattr(PS.subprocess, "run", lambda cmd, **k: calls.append(cmd) or R())
        assert PS.main() == 0
        (cmd,) = calls
        assert cmd[0] == "rsync" and re.fullmatch(r"argia-cfe:pi_status_\d{8}T\d{6}Z\.json", cmd[-1])

    def test_cfe_daily_heartbeat_and_month_mark(self):
        src = (V2 / "pi" / "cfe" / "cfe_daily.sh").read_text(encoding="utf-8")
        assert 'push "$HB" "heartbeat_$(date -u +%Y%m%dT%H%M%SZ).json"' in src
        assert 'local dst="${2:-$(basename "$1")}"' in src and 'rsync -t --timeout=60 "$1" "$INBOX:$dst"' in src
        # the month is marked sent once the CSV landed - a refused manifest must not keep it open
        assert 'push "$FULL.manifest.json" && touch "$MARK"' not in src
        assert src.count('push "$FULL" && { push "$FULL.manifest.json"; touch "$MARK"; }') == 2
        assert not re.search(r'^push "\$HB"\s*$', src, re.M)


# ------------------------------------------------------------ the server side
class TestTheServerReadsTheNewest:
    def test_regression_hourly_reports_through_the_jail(self, tmp_path, monkeypatch):
        """The 4 Oct failure, replayed: with the fixed name the second report
        is refused and the server keeps reading the first; with stamped names
        every report lands and the server reads the latest."""
        jail = FakeJail(tmp_path)
        assert jail.push("pi_status.json", json.dumps({"ts": "2026-10-04T15:45:08Z"})) == 0
        first = dt.datetime(2026, 10, 4, 15, 45, 8, tzinfo=dt.timezone.utc).timestamp()
        os.utime(tmp_path / "pi_status.json", (first, first))                 # as on the server
        assert jail.push("pi_status.json", json.dumps({"ts": "2026-10-04T16:45:09Z"})) == 23     # the bug
        import alert_mailer as AM
        monkeypatch.setattr(AM.monitor, "PI_INBOX", str(tmp_path))
        assert AM.gather_pi_status()["ts"] == "2026-10-04T15:45:08Z"           # what Tomasz saw
        for h in (16, 17, 18):
            t = dt.datetime(2026, 10, 4, h, 45, tzinfo=dt.timezone.utc).timestamp()
            assert jail.push(PS.push_name(t), json.dumps({"ts": f"2026-10-04T{h}:45:00Z"})) == 0
        assert AM.gather_pi_status()["ts"] == "2026-10-04T18:45:00Z"
        assert sorted(os.listdir(tmp_path)) == ["pi_status_20261004T184500Z.json"]
        assert AM.gather_pi_status()["ts"] == "2026-10-04T18:45:00Z"           # read again: still there

    def test_gather_pi_status_without_a_report(self, tmp_path, monkeypatch):
        import alert_mailer as AM
        monkeypatch.setattr(AM.monitor, "PI_INBOX", str(tmp_path))
        assert AM.gather_pi_status() is None
        put(tmp_path, "pi_status_20261004T184500Z.json", body="{not json")
        assert AM.gather_pi_status() is None

    def test_cfe_ingest_reads_the_newest_heartbeat(self):
        src = (V2 / "server" / "bundle" / "cfe_ingest.py").read_text(encoding="utf-8")
        assert 'hb_path = newest_push(INBOX, "heartbeat")' in src and "moved = file_manifests(INBOX)" in src

    def test_manifests_are_filed_so_a_retry_can_land(self, tmp_path):
        put(tmp_path, "cfe_2026-10_full.csv.manifest.json")
        put(tmp_path, "cfe_gapfill_20261004.csv")
        put(tmp_path, "heartbeat_20261004T141000Z.json")
        assert ING.file_manifests(str(tmp_path)) == ["cfe_2026-10_full.csv.manifest.json"]
        assert (tmp_path / "processed" / "cfe_2026-10_full.csv.manifest.json").exists()
        assert sorted(os.listdir(tmp_path)) == ["cfe_gapfill_20261004.csv", "heartbeat_20261004T141000Z.json", "processed"]
        put(tmp_path, "cfe_2026-10_full.csv.manifest.json", body='{"v": 2}')            # filed again: replaces
        ING.file_manifests(str(tmp_path))
        assert (tmp_path / "processed" / "cfe_2026-10_full.csv.manifest.json").read_text() == '{"v": 2}'
