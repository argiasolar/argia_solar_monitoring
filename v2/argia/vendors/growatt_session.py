"""Shared Growatt session persistence + login-failure backoff.

WHY (incident 2026-07-07): every 5-minute telemetry run performed fresh
Growatt logins (web client + env client, per plant — ~200+/day). From
GitHub's rotating runner IPs that was invisible; from the Pi's single
residential IP it looked like credential-stuffing, and after the 08:10
power blip Growatt soft-blocked the pattern (HTTP 200, no assToken).
Worse, while blocked, every call retried the login POST — ~8 attempts
every 5 minutes hammering a refusing endpoint, teaching the block to
stay.

Two mechanisms, file-based so they persist across the 5-minute
processes:

1. SESSION FILE — cookies saved after a successful login, loaded on
   client construction, shared by BOTH Growatt clients (same site, same
   account). Result: ~1-2 logins/day instead of ~200.
2. BACKOFF MARKER — a refused login writes a timestamp; every login
   attempt (any client, any process) checks it first and raises
   immediately, no HTTP, until the cooldown passes. A soft block cools
   instead of being hammered permanent.

Both are best-effort: file I/O problems degrade to the old behaviour
(fresh login), never break a run.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

LOG = logging.getLogger(__name__)

DEFAULT_SESSION_FILE = "~/.argia_growatt_session.json"
DEFAULT_BACKOFF_FILE = "~/.argia_growatt_backoff"
DEFAULT_COOLDOWN_S = 900  # 15 min


def session_file() -> Path:
    return Path(os.environ.get("ARGIA_GROWATT_SESSION_FILE",
                               DEFAULT_SESSION_FILE)).expanduser()


def backoff_file() -> Path:
    return Path(os.environ.get("ARGIA_GROWATT_BACKOFF_FILE",
                               DEFAULT_BACKOFF_FILE)).expanduser()


def cooldown_s() -> int:
    try:
        return int(os.environ.get("ARGIA_GROWATT_LOGIN_COOLDOWN_S",
                                  DEFAULT_COOLDOWN_S))
    except ValueError:
        return DEFAULT_COOLDOWN_S


class LoginBackoff(RuntimeError):
    """Raised INSTEAD of attempting a login while the cooldown runs."""


# ---- cookie persistence -----------------------------------------------------

def load_cookies(http_session) -> bool:
    """Load persisted cookies into a requests session. True if the saved
    session carried an assToken (i.e. plausibly still authenticated)."""
    try:
        path = session_file()
        if not path.exists():
            return False
        data = json.loads(path.read_text())
        for name, value in data.get("cookies", {}).items():
            http_session.cookies.set(name, value)
        return "assToken" in data.get("cookies", {})
    except Exception as e:  # noqa: BLE001 — best effort by contract
        LOG.warning("growatt session load failed (%s) — fresh login", e)
        return False


def save_cookies(http_session) -> None:
    try:
        path = session_file()
        path.write_text(json.dumps({
            "saved_at": time.time(),
            "cookies": http_session.cookies.get_dict(),
        }))
        path.chmod(0o600)
    except Exception as e:  # noqa: BLE001
        LOG.warning("growatt session save failed (%s)", e)


def drop_session() -> None:
    """Forget the persisted session (stale/invalid)."""
    try:
        session_file().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


# ---- login backoff ------------------------------------------------------------

def backoff_remaining_s(now: Optional[float] = None) -> int:
    try:
        path = backoff_file()
        if not path.exists():
            return 0
        marked = float(path.read_text().strip() or 0)
        remaining = int(marked + cooldown_s() - (now or time.time()))
        return max(0, remaining)
    except Exception:  # noqa: BLE001
        return 0


def check_backoff() -> None:
    """Raise LoginBackoff if a recent refusal says: do not even try."""
    remaining = backoff_remaining_s()
    if remaining > 0:
        raise LoginBackoff(
            f"Growatt login in backoff for another {remaining}s after a "
            f"refused login — not attempting (prevents hammering a "
            f"soft-block permanent)")


def mark_login_failure() -> None:
    try:
        backoff_file().write_text(str(time.time()))
    except Exception as e:  # noqa: BLE001
        LOG.warning("growatt backoff mark failed (%s)", e)


def clear_backoff() -> None:
    try:
        backoff_file().unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
