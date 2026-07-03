#!/usr/bin/env python3
"""Argia_Mont — archive preflight: prove Drive access BEFORE building on it.

Checks, in order, each with a concrete remedy on failure:
  1. GOOGLE_ARCHIVE_FOLDER_ID is set
  2. Drive API reachable with the drive scope (API enabled in GCP project)
  3. The archive folder is visible to the service account (shared with it)
  4. The SA can create a spreadsheet in the folder (then trashes the test file)

Safe to run repeatedly: the only side effect is one trashed test file.

USAGE
    PYTHONPATH=. python scripts/archive_preflight.py

EXIT CODES
    0  all checks passed — archive can be built/run
    1  a check failed (remedy printed)
"""

from __future__ import annotations

import logging
import os
import sys

from argia.core.drive import DriveClient

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("argia.archive_preflight")


def main() -> int:
    folder_id = os.environ.get("GOOGLE_ARCHIVE_FOLDER_ID", "").strip()
    if not folder_id:
        log.error(
            "FAIL [1/4] GOOGLE_ARCHIVE_FOLDER_ID not set.\n"
            "  Remedy: in Google Drive create a folder (e.g. "
            "'Argia_Mont_Archives'),\n"
            "  share it with the service account as Editor, and add the "
            "folder's ID\n"
            "  (the part after /folders/ in its URL) as a GitHub Actions "
            "secret named\n"
            "  GOOGLE_ARCHIVE_FOLDER_ID.")
        return 1
    log.info("PASS [1/4] GOOGLE_ARCHIVE_FOLDER_ID is set")

    try:
        drive = DriveClient()
        who = drive.whoami()
        log.info("PASS [2/4] Drive API reachable as %s", who)
    except Exception as e:  # noqa: BLE001
        log.error(
            "FAIL [2/4] Drive API not reachable: %s\n"
            "  Remedy: in Google Cloud Console for the service account's "
            "project,\n"
            "  enable the 'Google Drive API' "
            "(APIs & Services -> Enable APIs).", e)
        return 1

    try:
        name = drive.folder_name(folder_id)
        log.info("PASS [3/4] archive folder visible: '%s'", name)
    except Exception as e:  # noqa: BLE001
        log.error(
            "FAIL [3/4] folder %s not visible to the service account: %s\n"
            "  Remedy: share the folder with the service account e-mail "
            "as Editor.", folder_id, e)
        return 1

    try:
        test_id = drive.create_spreadsheet(folder_id,
                                           "_argia_preflight_delete_me")
        drive.trash_file(test_id)
        log.info("PASS [4/4] create+trash in folder works (test file "
                 "trashed)")
    except Exception as e:  # noqa: BLE001
        log.error(
            "FAIL [4/4] could not create a spreadsheet in the folder: %s\n"
            "  Remedy: the share must be EDITOR (not Viewer).", e)
        return 1

    log.info("\nAll preflight checks passed — the monthly archive can run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
