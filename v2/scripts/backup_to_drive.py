"""Copy tonight's server backups to Google Drive (v365).

Tomasz, 8 Nov 2026: "I would rather keep only code on server and any backups,
documents, files, databases on google drive" - "do Backups to Drive".

Runs after the nightly backup (argia-backup-drive.timer, 04:15 server time).
Reads /root/argia_backups/backup_manifest_latest.json (written by
db_backup.sh: name, sha256 and size of every file of the night) and copies
each file into the shared drive ARGIA_MONT_ARCHIVE, folder
"ARGIA server backups/YYYY-MM", under its dated name
(argia_mont_latest.dump.age -> argia_mont_20261108.dump.age).

Rules:
* only SEALED backups leave the server: a manifest that says the night is not
  encrypted is refused (exit 2) unless ARGIA_BACKUP_DRIVE_ALLOW_PLAIN=1. The
  age keys are created by Tomasz (pi/prologis_vault/setup_keys.sh), never
  here; Drive members and Google then only ever hold unreadable files;
* the local file must still match the manifest (sha256, size) before upload;
* after upload Drive's own md5 and size must equal the local ones, else the
  copy is trashed and the run fails;
* idempotent: a file already on Drive with the same name and md5 is skipped;
* retention on Drive: every night for 30 days, then the 1st of each month
  for 12 months; older copies go to the Drive trash (30 more days there).

Status for the health checks: /root/argia_backups/drive_status.json.
The planning (names, what to keep) is pure and tested; DriveStore is the
only part that talks to Google.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

OUT = os.environ.get("ARGIA_BACKUP_DIR", "/root/argia_backups")
DRIVE_NAME = os.environ.get("ARGIA_BACKUP_DRIVE_NAME", "ARGIA_MONT_ARCHIVE")
FOLDER = os.environ.get("ARGIA_BACKUP_DRIVE_FOLDER", "ARGIA server backups")
KEEP_DAYS = 30
KEEP_MONTHS = 12
STAMP_RE = re.compile(r"_(\d{8})[._]")


# ------------------------------------------------------------------ pure
def dated_name(name: str, stamp: str) -> str:
    """argia_mont_latest.dump.age -> argia_mont_20261108.dump.age; a name without
    '_latest' gets the stamp before its first dot."""
    if "_latest" in name:
        return name.replace("_latest", "_" + stamp, 1)
    base, dot, rest = name.partition(".")
    return f"{base}_{stamp}{dot}{rest}"


def stamp_of(name: str) -> Optional[dt.date]:
    m = STAMP_RE.search(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def keep(day: dt.date, today: dt.date, days: int = KEEP_DAYS, months: int = KEEP_MONTHS) -> bool:
    """Every night for `days` days, then the 1st of the month for `months` months."""
    age = (today - day).days
    if age < days:
        return True
    return day.day == 1 and age < months * 31


def plan_uploads(manifest: dict, out: str, allow_plain: bool) -> Tuple[List[Tuple[str, str, str, int]], List[str]]:
    """[(local path, Drive name, md5, size)] for tonight, and the problems that stop the run."""
    errs: List[str] = []
    stamp = str(manifest.get("stamp") or "")
    if not re.fullmatch(r"\d{8}", stamp):
        return [], ["manifest has no stamp"]
    if not manifest.get("encrypted") and not allow_plain:
        return [], ["the backups are NOT encrypted - refused (no age recipients on the server yet; "
                    "Tomasz runs pi/prologis_vault/setup_keys.sh, or ARGIA_BACKUP_DRIVE_ALLOW_PLAIN=1)"]
    todo = []
    for key, f in sorted((manifest.get("files") or {}).items()):
        path = os.path.join(out, os.path.basename(str(f.get("name") or "")))
        if not os.path.isfile(path):
            errs.append(f"{key}: {path} missing")
            continue
        sha, md5 = hashlib.sha256(), hashlib.md5()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
                md5.update(chunk)
        size = os.path.getsize(path)
        if sha.hexdigest() != f.get("sha256") or size != f.get("bytes"):
            errs.append(f"{key}: {os.path.basename(path)} does not match the manifest (changed since the backup)")
            continue
        todo.append((path, dated_name(os.path.basename(path), stamp), md5.hexdigest(), size))
    mpath = os.path.join(out, "backup_manifest_latest.json")
    if os.path.isfile(mpath):
        with open(mpath, "rb") as fh:
            data = fh.read()
        todo.append((mpath, dated_name("backup_manifest_latest.json", stamp), hashlib.md5(data).hexdigest(), len(data)))
    return todo, errs


def plan_retention(files: Dict[str, str], today: dt.date) -> List[Tuple[str, str]]:
    """[(name, id)] of Drive copies past the retention; names without a date are never touched."""
    out = []
    for name, fid in sorted(files.items()):
        d = stamp_of(name)
        if d and not keep(d, today):
            out.append((name, fid))
    return out


# ------------------------------------------------------------------ Drive
class DriveStore:
    """The few Drive calls the job needs, on the shared drive (the robot has no quota of its own)."""

    def __init__(self, svc):
        self.svc = svc

    def drive_id(self, name: str) -> str:
        ds = self.svc.drives().list(pageSize=100, fields="drives(id,name)").execute().get("drives", [])
        hit = [d["id"] for d in ds if d["name"] == name]
        if not hit:
            raise RuntimeError(f"shared drive {name!r} not visible to the robot account")
        return hit[0]

    def folder(self, parent: str, name: str, drive: str) -> str:
        q = (f"name = '{name}' and '{parent}' in parents and trashed = false "
             "and mimeType = 'application/vnd.google-apps.folder'")
        fs = self.svc.files().list(q=q, fields="files(id)", corpora="drive", driveId=drive,
                                   supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
        if fs:
            return fs[0]["id"]
        return self.svc.files().create(body={"name": name, "parents": [parent], "mimeType": "application/vnd.google-apps.folder"},
                                       fields="id", supportsAllDrives=True).execute()["id"]

    def children(self, parent: str, drive: str, folders: bool = False) -> Dict[str, dict]:
        kind = "=" if folders else "!="
        q = f"'{parent}' in parents and trashed = false and mimeType {kind} 'application/vnd.google-apps.folder'"
        out, token = {}, None
        while True:
            r = self.svc.files().list(q=q, fields="nextPageToken,files(id,name,md5Checksum,size)", pageSize=1000,
                                      corpora="drive", driveId=drive, pageToken=token,
                                      supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
            for f in r.get("files", []):
                out[f["name"]] = f
            token = r.get("nextPageToken")
            if not token:
                return out

    def upload(self, parent: str, name: str, path: str) -> dict:
        from googleapiclient.http import MediaFileUpload
        media = MediaFileUpload(path, mimetype="application/octet-stream", resumable=True, chunksize=32 << 20)
        return self.svc.files().create(body={"name": name, "parents": [parent]}, media_body=media,
                                       fields="id,md5Checksum,size", supportsAllDrives=True).execute()

    def trash(self, fid: str) -> None:
        self.svc.files().update(fileId=fid, body={"trashed": True}, supportsAllDrives=True).execute()


def run(store, manifest: dict, out: str, allow_plain: bool, today: dt.date, dry: bool = False) -> dict:
    """Upload tonight, verify, apply retention. Returns the status written to drive_status.json."""
    status = {"stamp": manifest.get("stamp"), "encrypted": bool(manifest.get("encrypted")), "uploaded": [],
              "skipped": [], "trashed": [], "errors": [], "ok": False}
    todo, errs = plan_uploads(manifest, out, allow_plain)
    status["errors"] += errs
    if errs and not todo:
        return status
    drive = store.drive_id(DRIVE_NAME)
    root = store.folder(drive, FOLDER, drive)
    month = store.folder(root, f"{manifest['stamp'][:4]}-{manifest['stamp'][4:6]}", drive)
    have = store.children(month, drive)
    for path, name, md5, size in todo:
        h = have.get(name)
        if h and h.get("md5Checksum") == md5 and int(h.get("size") or -1) == size:
            status["skipped"].append(name)
            continue
        if dry:
            status["uploaded"].append(name + " (dry run)")
            continue
        f = store.upload(month, name, path)
        if f.get("md5Checksum") != md5 or int(f.get("size") or -1) != size:
            store.trash(f["id"])
            status["errors"].append(f"{name}: Drive copy does not match (md5/size) - trashed")
            continue
        status["uploaded"].append(name)
    for mname, mf in sorted(store.children(root, drive, folders=True).items()):
        if not re.fullmatch(r"\d{4}-\d{2}", mname):
            continue
        files = {n: f["id"] for n, f in store.children(mf["id"], drive).items()}
        for n, fid in plan_retention(files, today):
            if not dry:
                store.trash(fid)
            status["trashed"].append(n)
    status["ok"] = not status["errors"]
    return status


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="plan only: no upload, no trash")
    a = ap.parse_args(argv)
    with open(os.path.join(OUT, "backup_manifest_latest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)
    allow_plain = os.environ.get("ARGIA_BACKUP_DRIVE_ALLOW_PLAIN") == "1"
    from argia.core.drive import DriveClient
    store = DriveStore(DriveClient()._svc)
    now = dt.datetime.now()
    status = run(store, manifest, OUT, allow_plain, now.date(), dry=a.dry_run)
    status["at"] = now.isoformat(timespec="seconds")
    if not a.dry_run:
        tmp = os.path.join(OUT, ".drive_status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=1)
        os.replace(tmp, os.path.join(OUT, "drive_status.json"))
    stamp = dt.datetime.now().isoformat(timespec="seconds")
    for k in ("uploaded", "skipped", "trashed"):
        for n in status[k]:
            print(f"{stamp} {k}: {n}")
    for e in status["errors"]:
        print(f"{stamp} ERROR: {e}")
    print(f"{stamp} drive backup {'OK' if status['ok'] else 'FAILED'}: {len(status['uploaded'])} uploaded, "
          f"{len(status['skipped'])} already there, {len(status['trashed'])} past retention")
    if status["errors"] and not status["encrypted"] and not allow_plain:
        return 2
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
