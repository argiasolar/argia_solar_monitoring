"""SolarEdge Monitoring API v2 - transport and credit budget (v330).

SolarEdge retires the v1 Monitoring API (``api_key`` query parameter,
``/site/...`` paths) on 1 Nov 2026; on 27 Oct 2026 v1 already answered
HTTP 503 "Scheduled V1 deprecation window" at times. v2 lives on the same
host under ``/v2`` (``GET https://monitoringapi.solaredge.com/v2/sites``
answered HTTP 401 to the v1 key on 28 Oct 2026, so the host is live and
needs the new credentials):

* **Fleet Access** (installer / O&M accounts): an application created on
  the SolarEdge Developer Platform, sent as the ``X-API-Key`` header (some
  accounts are also given an account key, ``X-Account-Key``).
* **Credits**: every call costs one credit; the plan sets a monthly budget
  and a per-minute limit. ARGIA runs on the free budget (2,000 credits a
  month, Tomasz 28 Oct 2026: hourly data is enough for the two CAPEX
  SolarEdge plants), so this module refuses a call that would cross the
  monthly budget instead of letting SolarEdge answer 429 for the rest of
  the month.

What is NOT here yet: the response parsers. The v2 response shapes are
taken from a live probe (scripts/solaredge_v2_probe.py) once the
application exists, never from guesses - then the parsers are written
against those captured fixtures.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

LOG = logging.getLogger("argia.vendors.solaredge_v2")

BASE_URL = "https://monitoringapi.solaredge.com/v2"
DEFAULT_TIMEOUT_SEC = 30
MONTHLY_CREDITS_DEFAULT = 2000
SAFETY_MARGIN = 100          # never spend the last 100 credits (probe, manual checks)
STATE_DEFAULT = "/root/argia_logs/solaredge_v2_credits.json"

ENV_API_KEY = "SOLAREDGE_V2_API_KEY"
ENV_ACCOUNT_KEY = "SOLAREDGE_V2_ACCOUNT_KEY"
ENV_MONTHLY = "SOLAREDGE_V2_MONTHLY_CREDITS"


class SolarEdgeV2Error(RuntimeError):
    pass


class SolarEdgeV2AuthError(SolarEdgeV2Error):
    pass


class SolarEdgeV2BudgetExceeded(SolarEdgeV2Error):
    """Our own guard: the call would spend credits the month does not have."""


class SolarEdgeV2RateLimited(SolarEdgeV2Error):
    pass


# ------------------------------------------------------------ credit budget

@dataclass
class CreditBudget:
    """Monthly credit counter in a small JSON file. One credit per call.

    ``spend()`` raises SolarEdgeV2BudgetExceeded BEFORE the call when the
    month's allowance minus the safety margin is used up. The counter
    resets when the calendar month (UTC) changes. Pure apart from the file;
    the clock and the path are injectable for tests."""

    path: str = STATE_DEFAULT
    monthly: int = MONTHLY_CREDITS_DEFAULT
    margin: int = SAFETY_MARGIN

    def _load(self, month: str) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            st = {}
        if st.get("month") != month:
            st = {"month": month, "used": 0}
        return st

    def _save(self, st: Dict[str, Any]) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, self.path)

    def remaining(self, now: Optional[dt.datetime] = None) -> int:
        month = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")
        return self.monthly - int(self._load(month).get("used", 0))

    def spend(self, n: int = 1, now: Optional[dt.datetime] = None) -> int:
        month = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")
        st = self._load(month)
        used = int(st.get("used", 0))
        if used + n > self.monthly - self.margin:
            raise SolarEdgeV2BudgetExceeded(
                f"SolarEdge v2 credit budget for {month}: {used} of {self.monthly} used,"
                f" {self.margin} kept in reserve - call refused")
        st["used"] = used + n
        self._save(st)
        return self.monthly - st["used"]


# ------------------------------------------------------------------ client

def headers(api_key: str, account_key: str = "") -> Dict[str, str]:
    """Request headers. PURE."""
    if not api_key:
        raise ValueError("SolarEdge v2 needs an API key")
    h = {"X-API-Key": api_key, "Accept": "application/json"}
    if account_key:
        h["X-Account-Key"] = account_key
    return h


class SolarEdgeV2Client:
    """GET-only v2 client. Every call passes the credit budget first."""

    def __init__(self, api_key: str, account_key: str = "",
                 budget: Optional[CreditBudget] = None,
                 base_url: str = BASE_URL, timeout_sec: int = DEFAULT_TIMEOUT_SEC,
                 session: Optional[requests.Session] = None) -> None:
        self._headers = headers(api_key, account_key)
        self._budget = budget or CreditBudget()
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._session = session or requests.Session()

    @classmethod
    def from_env(cls, env=None, budget: Optional[CreditBudget] = None) -> "SolarEdgeV2Client":
        env = os.environ if env is None else env
        key = (env.get(ENV_API_KEY) or "").strip()
        if not key:
            raise SolarEdgeV2AuthError(f"{ENV_API_KEY} is not set")
        monthly = int((env.get(ENV_MONTHLY) or MONTHLY_CREDITS_DEFAULT))
        return cls(key, (env.get(ENV_ACCOUNT_KEY) or "").strip(),
                   budget=budget or CreditBudget(monthly=monthly))

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """One GET, one credit. Raises the module's errors; never retries
        (a retry is another credit - the caller decides)."""
        self._budget.spend(1)
        url = f"{self._base}/{path.lstrip('/')}"
        r = self._session.get(url, params=params or {}, headers=self._headers,
                              timeout=self._timeout)
        if r.status_code in (401, 403):
            raise SolarEdgeV2AuthError(f"SolarEdge v2 {path}: HTTP {r.status_code} - key rejected")
        if r.status_code == 429:
            raise SolarEdgeV2RateLimited(
                f"SolarEdge v2 {path}: HTTP 429 (per-minute limit or monthly credits): {r.text[:200]}")
        if r.status_code != 200:
            raise SolarEdgeV2Error(f"SolarEdge v2 {path}: HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise SolarEdgeV2Error(f"SolarEdge v2 {path}: invalid JSON: {e}") from e


# --------------------------------------------------------------- planning

def monthly_credits(sites: int, polls_per_day: int, calls_per_poll: int = 1,
                    nightly_calls_per_site: int = 3, days: int = 31) -> int:
    """Credits a schedule spends in a month. PURE - used to size the
    cadence against the plan (tests pin the ARGIA schedule)."""
    return sites * days * (polls_per_day * calls_per_poll + nightly_calls_per_site)
