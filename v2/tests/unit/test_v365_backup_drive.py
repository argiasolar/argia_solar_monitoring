"""v365 - the Prologis backup fix and backups to Google Drive.

8 Nov 2026: the disk reached 93 %. Since 6 Nov the nightly Prologis archive
held 4.5 GB of LED documents, tar answered "file changed as we read it"
(exit 1), the archive was never renamed and its 3.9 GB .tmp stayed every
night (the rotation never sees .tmp files). Tomasz: "do Backups to Drive and
fix Prologis backup now". Pinned here:

* db_backup.sh: the Prologis archive leaves out lighting/docs, accepts tar
  exit 1, and no .tmp survives a failed night (own leftovers on exit, older
  leftovers at the start);
* backup_to_drive.py: dated names, retention (30 nights + 12 monthly),
  only sealed backups leave the server, files must match the manifest,
  Drive's md5 must match, re-runs upload nothing twice;
* drift_check: backup-drive and the Prologis archive freshness.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import json
import os
import pathlib
import shutil
import sys
import tarfile

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "scripts"))
from tests.unit import test_v319_encryption_at_rest as T319     # noqa: E402

B = importlib.import_module("backup_to_drive")
D = importlib.import_module("drift_check")
linux = T319.linux


# ------------------------------------------------------------------ db_backup.sh
def _prologis_store(env):
    pl = pathlib.Path(env["ARGIA_PL_DIR"])
    docs = pl / "lighting" / "docs" / "1_DELIVERED"
    docs.mkdir(parents=True)
    (docs / "big.pdf").write_bytes(os.urandom(200_000))
    (pl / "lighting" / "lighting.json").write_text("{}")
    return pl


@linux
class TestPrologisArchive:
    def test_the_documents_stay_out_and_the_rest_is_in(self, tmp_path):
        env, out, _ = T319.backup_env(tmp_path)
        _prologis_store(env)
        r = T319.run_backup(env)
        assert r.returncode == 0, r.stdout + r.stderr
        names = tarfile.open(out / "prologis_latest.tar.gz").getnames()
        assert "./lighting/lighting.json" in names and "./registry.json" in names and "prologis.db" in names
        assert not [n for n in names if "lighting/docs" in n]

    def test_file_changed_as_we_read_it_is_not_a_failure(self, tmp_path):
        env, out, _ = T319.backup_env(tmp_path)
        shims = pathlib.Path(env["PATH"].split(":")[0])
        real = shutil.which("tar")
        (shims / "tar").write_text(f'#!/bin/bash\n"{real}" "$@" || exit $?\necho "tar: .: file changed as we read it" >&2\nexit 1\n')
        (shims / "tar").chmod(0o755)
        r = T319.run_backup(env)
        assert r.returncode == 0 and "prologis archive FAILED" not in r.stdout
        assert (out / "prologis_latest.tar.gz").exists() and not list(out.glob(".*.tmp"))

    def test_a_real_tar_failure_leaves_no_tmp_and_keeps_the_main_backup(self, tmp_path):
        env, out, _ = T319.backup_env(tmp_path)
        shims = pathlib.Path(env["PATH"].split(":")[0])
        real = shutil.which("tar")
        (shims / "tar").write_text(f'#!/bin/bash\n"{real}" "$@"\nexit 2\n')
        (shims / "tar").chmod(0o755)
        r = T319.run_backup(env)
        assert r.returncode == 0 and "prologis archive FAILED (tar exit 2" in r.stdout
        assert (out / "argia_mont_latest.dump").exists()
        assert not list(out.glob(".*.tmp")) and not list(out.glob("prologis_*"))

    def test_a_failure_of_the_whole_run_leaves_no_tmp(self, tmp_path):
        env, out, fake = T319.backup_env(tmp_path)
        fake.write_bytes(b"PGDMP tiny")                     # the stub dump fails the run
        assert T319.run_backup(env).returncode != 0
        assert not list(out.glob(".*.tmp"))

    def test_leftovers_of_earlier_nights_are_removed_fresh_ones_kept(self, tmp_path):
        env, out, _ = T319.backup_env(tmp_path)
        out.mkdir()
        old = out / ".prologis_20261106.tmp"
        old.write_bytes(b"x" * 1000)
        os.utime(old, (old.stat().st_mtime - 7200, old.stat().st_mtime - 7200))
        fresh = out / ".something_else.tmp"
        fresh.write_bytes(b"y")
        r = T319.run_backup(env)
        assert r.returncode == 0 and "removed leftover" in r.stdout
        assert not old.exists() and fresh.exists()


# ------------------------------------------------------------------ backup_to_drive.py
class TestPlanning:
    def test_dated_names(self):
        assert B.dated_name("argia_mont_latest.dump.age", "20261108") == "argia_mont_20261108.dump.age"
        assert B.dated_name("prologis_latest.tar.gz", "20261108") == "prologis_20261108.tar.gz"
        assert B.dated_name("backup_manifest_latest.json", "20261108") == "backup_manifest_20261108.json"
        assert B.dated_name("odd.bin", "20261108") == "odd_20261108.bin"

    def test_stamp_of(self):
        assert B.stamp_of("users_20261108.db.age") == dt.date(2026, 11, 8)
        assert B.stamp_of("readme.txt") is None and B.stamp_of("x_20261399.db") is None

    @pytest.mark.parametrize("day, kept", [((2026, 11, 8), True), ((2026, 10, 10), True), ((2026, 10, 9), False),
                                           ((2026, 10, 1), True), ((2025, 12, 1), True), ((2025, 11, 1), False),
                                           ((2025, 12, 2), False)])
    def test_retention_30_nights_then_monthly_for_a_year(self, day, kept):
        assert B.keep(dt.date(*day), dt.date(2026, 11, 8)) is kept

    def test_retention_never_touches_names_without_a_date(self):
        files = {"argia_mont_20250101.dump.age": "a", "notes.txt": "b", "users_20261107.db.age": "c"}
        assert B.plan_retention(files, dt.date(2026, 11, 8)) == [("argia_mont_20250101.dump.age", "a")]


def _night(tmp_path, encrypted=True):
    out = tmp_path / "bk"
    out.mkdir()
    files = {}
    for key, name in (("dump", "argia_mont_latest.dump.age"), ("users", "users_latest.db.age"),
                      ("prologis", "prologis_latest.tar.gz.age")):
        data = os.urandom(5000)
        (out / name).write_bytes(data)
        files[key] = {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    m = {"stamp": "20261108", "encrypted": encrypted, "files": files}
    (out / "backup_manifest_latest.json").write_text(json.dumps(m))
    return out, m


class FakeStore:
    """In-memory shared drive: {folder id: {name: file}}."""

    def __init__(self, corrupt=False):
        self.items = {"DRIVE": {}}
        self.n = 0
        self.corrupt = corrupt
        self.uploads = []

    def drive_id(self, name):
        assert name == "ARGIA_MONT_ARCHIVE"
        return "DRIVE"

    def _new(self):
        self.n += 1
        return f"id{self.n}"

    def folder(self, parent, name, drive):
        for f in self.items[parent].values():
            if f["name"] == name and f.get("folder"):
                return f["id"]
        fid = self._new()
        self.items[parent][name] = {"id": fid, "name": name, "folder": True}
        self.items[fid] = {}
        return fid

    def children(self, parent, drive, folders=False):
        return {n: f for n, f in self.items[parent].items() if bool(f.get("folder")) == folders and not f.get("trashed")}

    def upload(self, parent, name, path):
        data = pathlib.Path(path).read_bytes()
        md5 = hashlib.md5(data + (b"x" if self.corrupt else b"")).hexdigest()
        f = {"id": self._new(), "name": name, "md5Checksum": md5, "size": str(len(data))}
        self.items[parent][name] = f
        self.uploads.append(name)
        return f

    def trash(self, fid):
        for files in self.items.values():
            for f in files.values():
                if f["id"] == fid:
                    f["trashed"] = True


class TestRun:
    TODAY = dt.date(2026, 11, 8)

    def test_a_sealed_night_goes_up_dated_and_checked(self, tmp_path):
        out, m = _night(tmp_path)
        st = FakeStore()
        s = B.run(st, m, str(out), False, self.TODAY)
        assert s["ok"] and not s["errors"]
        assert sorted(s["uploaded"]) == ["argia_mont_20261108.dump.age", "backup_manifest_20261108.json",
                                         "prologis_20261108.tar.gz.age", "users_20261108.db.age"]
        root = st.folder("DRIVE", "ARGIA server backups", "DRIVE")
        assert "2026-11" in st.children(root, "DRIVE", folders=True)

    def test_a_second_run_uploads_nothing(self, tmp_path):
        out, m = _night(tmp_path)
        st = FakeStore()
        B.run(st, m, str(out), False, self.TODAY)
        s = B.run(st, m, str(out), False, self.TODAY)
        assert s["ok"] and s["uploaded"] == [] and len(s["skipped"]) == 4 and len(st.uploads) == 4

    def test_plain_backups_are_refused_unless_allowed(self, tmp_path):
        out, m = _night(tmp_path, encrypted=False)
        st = FakeStore()
        s = B.run(st, m, str(out), False, self.TODAY)
        assert not s["ok"] and "NOT encrypted" in s["errors"][0] and st.uploads == []
        assert B.run(st, m, str(out), True, self.TODAY)["ok"]

    def test_a_file_changed_since_the_backup_is_not_uploaded(self, tmp_path):
        out, m = _night(tmp_path)
        (out / "users_latest.db.age").write_bytes(b"changed")
        st = FakeStore()
        s = B.run(st, m, str(out), False, self.TODAY)
        assert not s["ok"] and "users" in s["errors"][0] and "users_20261108.db.age" not in st.uploads
        assert "argia_mont_20261108.dump.age" in st.uploads               # the good files still go

    def test_a_drive_copy_that_does_not_match_is_trashed_and_fails(self, tmp_path):
        out, m = _night(tmp_path)
        st = FakeStore(corrupt=True)
        s = B.run(st, m, str(out), False, self.TODAY)
        assert not s["ok"] and all("does not match" in e for e in s["errors"]) and s["uploaded"] == []
        month = st.folder(st.folder("DRIVE", "ARGIA server backups", "DRIVE"), "2026-11", "DRIVE")
        assert st.children(month, "DRIVE") == {}

    def test_retention_trashes_only_old_dated_copies(self, tmp_path):
        out, m = _night(tmp_path)
        st = FakeStore()
        root = st.folder("DRIVE", "ARGIA server backups", "DRIVE")
        sep = st.folder(root, "2026-09", "DRIVE")
        for n in ("argia_mont_20260901.dump.age", "argia_mont_20260915.dump.age", "keep_me.txt"):
            st.items[sep][n] = {"id": n, "name": n, "md5Checksum": "x", "size": "1"}
        s = B.run(st, m, str(out), False, self.TODAY)
        assert s["trashed"] == ["argia_mont_20260915.dump.age"]
        assert sorted(st.children(sep, "DRIVE")) == ["argia_mont_20260901.dump.age", "keep_me.txt"]

    def test_dry_run_changes_nothing(self, tmp_path):
        out, m = _night(tmp_path)
        st = FakeStore()
        s = B.run(st, m, str(out), False, self.TODAY, dry=True)
        assert st.uploads == [] and len(s["uploaded"]) == 4


# ------------------------------------------------------------------ drift check
class TestDriftCheck:
    def test_drive_check(self):
        assert D.drive_check(None, None)["ok"] is False
        assert "h ago" in D.drive_check({"ok": True}, 31.0)["detail"]
        bad = D.drive_check({"ok": False, "errors": ["the backups are NOT encrypted - refused"]}, 2.0)
        assert bad["ok"] is False and "NOT encrypted" in bad["detail"]
        good = D.drive_check({"ok": True, "stamp": "20261108", "uploaded": ["a"], "skipped": ["b"]}, 2.0)
        assert good == {"check": "backup-drive", "ok": True, "detail": "2 files of 20261108 on Drive"}

    def test_the_prologis_archive_is_watched(self):
        assert any(n == "backup-prologis" for n, _ in D.FRESH_FILES)


def test_the_timer_runs_after_the_backup():
    t = (V2 / "server" / "bundle" / "argia-backup-drive.timer").read_text()
    s = (V2 / "server" / "bundle" / "argia-backup-drive.service").read_text()
    assert "OnCalendar=*-*-* 04:15:00" in t and "run_job.sh backup-drive backup_to_drive.py" in s
    assert "03:30:00" in (V2 / "server" / "bundle" / "argia-dbdump.timer").read_text()
