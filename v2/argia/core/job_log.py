"""Job-run logging to the SyncRuns tab — one instrument for every job.

WHY (user request, 2026-07-07): telemetry has logged every run to
SyncRuns since day one, but kpi_eod / dashboard / alerts / reports never
joined — so the sheet answers "when did telemetry last run" and nothing
else. Watching the dashboard, there was no way to know when it last
refreshed. Now every scheduled job appends one row per execution,
INCLUDING failures (a FAILED row with the error text beats silence — the
sheet becomes the first place to look, before SSH).

Design notes:
- Same 8-column schema and run-id style telemetry already writes, so one
  tab stays one truth: run_id, started, finished, script, status,
  processed, rows, error. The two count columns are 0 for non-telemetry
  jobs; the ask is timestamps and status, not double accounting.
- Best-effort BY CONTRACT: a logging failure warns and never breaks the
  job, and the job's exit code / exception passes through untouched.
- Dry runs don't log (matching telemetry) — gating is per-script via
  `write_if`, because flag semantics differ (--apply opt-in vs
  --dry-run opt-out).
- The instrument builds its own SheetsClient from env at the END of the
  run. Cost: one extra append per job execution; benefit: zero coupling
  to each script's internals.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import os
import secrets
import socket
import sys
import time
from typing import Callable, List, Optional

LOG = logging.getLogger(__name__)

SYNC_TAB = "SyncRuns"

# v188: the run log lives in PostgreSQL (`sync_run`) — see
# argia/store/sync_run.py. The SyncRuns sheet tab is OFF by default; set
# ARGIA_SHEET_JOBLOG=1 to keep appending there as well (the switch exists
# so the cut is reversible, same pattern as ARGIA_SHEET_PLANT_TABS).
SHEET_JOBLOG_ENV = "ARGIA_SHEET_JOBLOG"


def sheet_joblog_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return str(env.get(SHEET_JOBLOG_ENV, "0")).strip().lower() in (
        "1", "true", "yes", "on")


def _default_write_if(argv: List[str]) -> bool:
    return "--dry-run" not in argv


def apply_flag_write_if(argv: List[str]) -> bool:
    """For scripts where writing is opt-in via --apply (dashboard pair)."""
    return "--apply" in argv


def _run_id() -> str:
    return (f"{int(dt.datetime.now(dt.timezone.utc).timestamp())}-"
            f"{secrets.token_hex(3)}-{socket.gethostname()}")


def _append_row(row: List) -> None:
    """Record one run: PostgreSQL always (where the mirror is on), the
    SyncRuns sheet only behind ARGIA_SHEET_JOBLOG."""
    from argia.store import sync_run
    wrote_pg = sync_run.record(row, LOG)
    if not sheet_joblog_enabled():
        if not wrote_pg:
            LOG.warning("job_log: no run-log sink is active (PG mirror off, "
                        "sheet log off) — run not recorded")
        return
    _append_sheet_row(row)


def _append_sheet_row(row: List) -> None:
    from argia.core.sheets import SheetsClient
    sheet_id = os.environ.get("GOOGLE_SHEET_ID_V2", "").strip()
    if not sheet_id:
        LOG.warning("job_log: GOOGLE_SHEET_ID_V2 not set — skipping "
                    "SyncRuns row")
        return
    client = SheetsClient(sheet_id=sheet_id)
    try:
        client.append_rows(SYNC_TAB, [row])
    except Exception as e:  # noqa: BLE001
        # 2026-07-08: kpi-eod's ~50 stamp writes ate the 60/min quota and
        # the SyncRuns append — the LAST write of the run — got the 429.
        # Heavy jobs were silently losing their log row. The quota is
        # per-minute: wait out the window and retry ONCE.
        if "429" not in str(e) and "RATE_LIMIT" not in str(e).upper():
            raise
        wait = int(os.environ.get("ARGIA_JOBLOG_RETRY_S", "65"))
        LOG.warning("job_log: Sheets write quota hit — retrying the "
                    "SyncRuns row in %ss", wait)
        time.sleep(wait)
        client.append_rows(SYNC_TAB, [row])


def instrument(script: str,
               write_if: Callable[[List[str]], bool] = _default_write_if):
    """Wrap a script's main(argv) so every non-dry run leaves a SyncRuns
    row. Exceptions re-raise and exit codes pass through unchanged."""

    def deco(main):
        # The wrapper runs with the DECORATED SCRIPT as its module context
        # for the import-hygiene guard (it resolves co_names against the
        # script's globals). Everything the wrapper needs is therefore
        # bound as keyword-only defaults — self-contained by construction,
        # and the guard stays strict.
        @functools.wraps(main)
        def wrapper(argv: Optional[List[str]] = None, *,
                    _dt=dt, _sys=sys, _log=LOG,
                    _rid=_run_id, _append=_append_row):
            args = list(argv) if argv is not None else _sys.argv[1:]
            started = _dt.datetime.now(_dt.timezone.utc)
            status, error, rc = "OK", "", 0
            try:
                rc = main(argv)
            except BaseException as e:  # noqa: BLE001 — log then re-raise
                status = "FAILED"
                error = f"{type(e).__name__}: {e}"
                raise
            else:
                if rc not in (0, None):
                    status, error = "FAILED", f"exit code {rc}"
                return rc
            finally:
                if write_if(args):
                    finished = _dt.datetime.now(_dt.timezone.utc)
                    try:
                        _append([_rid(), started.isoformat(),
                                 finished.isoformat(), script, status,
                                 0, 0, error])
                    except Exception as e:  # noqa: BLE001 — best effort
                        _log.warning("job_log: failed to write SyncRuns "
                                     "row for %s: %s", script, e)

        wrapper._job_log_script = script          # for tests
        wrapper._job_log_write_if = write_if      # for tests
        return wrapper

    return deco
