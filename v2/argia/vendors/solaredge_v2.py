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

v348 (5 Nov 2026, v1 answers HTTP 410 "retired on November 3, 2026"):
ARGIA runs on v2. ``V1Compat`` answers the few v1 calls the jobs make from
v2 calls, in the v1 shapes the existing parsers read (shapes from the live
probe of 5 Nov 2026, never guessed); live telemetry is one
``inverters/telemetry`` call per site (argia.vendors.solaredge_telemetry).
``MinutePacer`` keeps every process under the per-minute limit.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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


# ------------------------------------------------------------------ pacing

CALLS_PER_MINUTE = 9
"""v348: SolarEdge v2 allows 10 calls a minute (x-ratelimit-limit-minute,
probe 5 Nov 2026); one call of margin. Above it SolarEdge answers 429 and
the job loses the call (and maybe the credit)."""


class MinutePacer:
    """Sliding 60-second window shared by every client in the process: a
    history import of a year (14 one-month windows) or the nightly snapshot
    of several sites waits instead of hitting the per-minute limit."""

    def __init__(self, per_minute: int = CALLS_PER_MINUTE, clock=None, sleep=None) -> None:
        import time
        self.per_minute = per_minute
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._calls: List[float] = []

    def wait(self) -> float:
        """Block until one more call fits in the window; returns seconds slept."""
        now = self._clock()
        self._calls = [t for t in self._calls if now - t < 60.0]
        slept = 0.0
        if len(self._calls) >= self.per_minute:
            slept = 60.0 - (now - self._calls[0]) + 0.5
            LOG.info("SolarEdge v2: %d calls in the last minute - waiting %.0f s", len(self._calls), slept)
            self._sleep(slept)
            now = self._clock()
            self._calls = [t for t in self._calls if now - t < 60.0]
        self._calls.append(now)
        return slept


PACER = MinutePacer()


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
                 session: Optional[requests.Session] = None,
                 pacer: Optional[MinutePacer] = None) -> None:
        self._headers = headers(api_key, account_key)
        self._pacer = pacer or PACER
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
        self._pacer.wait()
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


# ===================================================================== v348
# The switch (SolarEdge retired v1 on 3 Nov 2026: every v1 call answers HTTP
# 410 "API Version Deprecated"). Shapes below were captured from the live v2
# API on 5 Nov 2026 (probe on GTO2 Hirschmann 4362085):
#
#   GET sites/{id}/energy?from&to&resolution=DAY|MONTH|YEAR
#       {"period": {"from","to"}, "unit": "WH", "resolution": "DAY",
#        "values": [{"timestamp": "2026-11-01T00:00:00-06:00", "value": 1823541.0}]}
#       DAY allows a window of at most one month (HTTP 400 otherwise).
#       from/to sent as UTC 'Z'; buckets are the SITE's local days.
#   GET sites/{id}/lifetime-energy        {"energy": Wh, "unit": "WH", "lastUpdateTime"}
#   GET sites/{id}/devices?types=INVERTER [{"serialNumber","name","active",...}]
#   GET sites/{id}/inverters/telemetry?from&to
#       {"inverters": {"7E0514A6-3D": {"power": {"unit": "W", "resolution":
#        "QUARTER_HOUR", "values": [{"timestamp", "value"}]}, "energy": {...WH per
#        15-min bucket...}, "voltage", "current", "frequency", ...}}}
#       values are null while the inverter sleeps. ONE call covers every
#       inverter of the site (v1 needed one call per inverter).
#
# GTO2 1 and 2 Nov from v2 (1,823,541 / 1,787,591 Wh) equal what v1 stored
# (1,823.541 / 1,787.591 kWh) - same counters, new transport.

MX_OFFSET = dt.timezone(dt.timedelta(hours=-6))          # Mexico has no DST since 2022
DAY_WINDOW_DAYS = 28                                     # < the API's one-month limit


def local_midnight_z(d: dt.date, tz=MX_OFFSET) -> str:
    """Site-local midnight of ``d`` as a UTC 'Z' string. PURE."""
    return dt.datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_to_z(s: str, tz=MX_OFFSET) -> str:
    """'YYYY-MM-DD HH:MM:SS' site-local (the v1 parameter format) -> UTC 'Z'. PURE."""
    t = dt.datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> Optional[dt.datetime]:
    """v2 timestamp ('2026-11-04T06:15:00-06:00', '...Z', fractional seconds) -> aware datetime. PURE."""
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    if "." in t:                                         # trim nanoseconds python cannot parse
        head, rest = t.split(".", 1)
        tail = ""
        for i, ch in enumerate(rest):
            if not ch.isdigit():
                tail = rest[i:]
                break
        t = head + tail
    try:
        v = dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    return v if v.tzinfo else v.replace(tzinfo=MX_OFFSET)


def day_windows(d0: dt.date, d1: dt.date, step: int = DAY_WINDOW_DAYS):
    """[(from_date, to_date_exclusive)] covering d0..d1 inclusive. PURE."""
    out = []
    a = d0
    while a <= d1:
        b = min(a + dt.timedelta(days=step), d1 + dt.timedelta(days=1))
        out.append((a, b))
        a = b
    return out


def energy_v1_shape(bodies, time_unit: str) -> Dict[str, Any]:
    """v2 energy answers -> the v1 /site/{id}/energy shape the parsers read
    ({"energy": {"timeUnit", "unit", "values": [{"date": "YYYY-MM-DD 00:00:00", "value"}]}}).
    Bucket dates are the site-local dates the v2 timestamps carry. PURE."""
    values = []
    unit = "Wh"
    for b in bodies:
        u = str((b or {}).get("unit") or "WH")
        unit = {"WH": "Wh", "KWH": "kWh", "MWH": "MWh"}.get(u.upper(), u)
        for v in (b or {}).get("values") or []:
            if isinstance(v, dict) and v.get("timestamp"):
                values.append({"date": str(v["timestamp"])[:10] + " 00:00:00", "value": v.get("value")})
    values.sort(key=lambda x: x["date"])
    return {"energy": {"timeUnit": time_unit, "unit": unit, "values": values}}


def lifetime_v1_shape(body) -> Dict[str, Any]:
    """v2 lifetime-energy -> the v1 /site/{id}/overview fields the counters read. PURE."""
    e = (body or {}).get("energy")
    u = str((body or {}).get("unit") or "WH").upper()
    if e is not None and u == "KWH":
        e = float(e) * 1000.0
    return {"overview": {"lifeTimeData": {"energy": e}, "lastUpdateTime": (body or {}).get("lastUpdateTime")}}


def devices_v1_shape(body) -> Dict[str, Any]:
    """v2 devices -> the v1 /equipment/{id}/list shape (active inverters only). PURE."""
    items = body if isinstance(body, list) else ((body or {}).get("devices") or [])
    lst = [{"serialNumber": d.get("serialNumber"), "name": d.get("name"), "model": d.get("partNumber")}
           for d in items if isinstance(d, dict) and d.get("active", True)
           and str(d.get("type", "INVERTER")).upper() == "INVERTER"]
    return {"reporters": {"count": len(lst), "list": lst}}


def inverter_series(body, now: Optional[dt.datetime] = None) -> Dict[str, List[Dict[str, Any]]]:
    """v2 inverters/telemetry -> {serial: [{"ts_utc", "power_w", "etoday_wh",
    "voltage", "current", "frequency"}]} in time order. One entry per 15-min
    bucket that carries data; ``ts_utc`` is the END of the bucket (capped at
    ``now``) and ``etoday_wh`` the energy since the window start up to it -
    the cumulative EToday every vendor feed delivers. PURE."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for sn, m in ((body or {}).get("inverters") or {}).items():
        if not isinstance(m, dict):
            continue

        def series(name):
            vals = ((m.get(name) or {}).get("values")) or []
            return {str(v.get("timestamp")): v.get("value") for v in vals if isinstance(v, dict)}
        power, energy = series("power"), series("energy")
        volt, curr, freq = series("voltage"), series("current"), series("frequency")
        e_unit = str(((m.get("energy") or {}).get("unit")) or "WH").upper()
        scale = 1000.0 if e_unit == "KWH" else 1.0
        rows, acc = [], 0.0
        for ts in sorted(set(power) | set(energy), key=lambda x: parse_ts(x) or now):
            start = parse_ts(ts)
            if start is None:
                continue
            e, p = energy.get(ts), power.get(ts)
            if e is not None:
                acc += float(e) * scale
            if e is None and p is None:
                continue                                  # inverter asleep / no data
            end = min(start + dt.timedelta(minutes=15), now).astimezone(dt.timezone.utc)
            rows.append({"ts_utc": end, "power_w": None if p is None else float(p), "etoday_wh": round(acc, 1),
                         "voltage": volt.get(ts), "current": curr.get(ts), "frequency": freq.get(ts)})
        out[str(sn)] = rows
    return out


class V1Compat:
    """Answers the v1 calls the monitoring makes, from v2 (one place to
    translate; every caller and parser keeps working). Telemetry of a site
    is fetched once per window and served to each inverter from memory."""

    def __init__(self, client: "SolarEdgeV2Client") -> None:
        self._c = client
        self._telemetry_cache: Dict[tuple, Any] = {}

    def get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) == 3 and parts[0] == "site" and parts[2] == "energy":
            unit = str(params.get("timeUnit") or "DAY").upper()
            d0 = dt.date.fromisoformat(str(params["startDate"])[:10])
            d1 = dt.date.fromisoformat(str(params["endDate"])[:10])
            if unit == "DAY":
                bodies = [self._c.get(f"sites/{parts[1]}/energy",
                                      {"from": local_midnight_z(a), "to": local_midnight_z(b), "resolution": "DAY"})
                          for a, b in day_windows(d0, d1)]
            else:
                bodies = [self._c.get(f"sites/{parts[1]}/energy",
                                      {"from": local_midnight_z(d0), "to": local_midnight_z(d1 + dt.timedelta(days=1)),
                                       "resolution": unit})]
            return energy_v1_shape(bodies, unit)
        if len(parts) == 3 and parts[0] == "site" and parts[2] == "overview":
            return lifetime_v1_shape(self._c.get(f"sites/{parts[1]}/lifetime-energy", {}))
        if len(parts) == 3 and parts[0] == "equipment" and parts[2] == "list":
            return devices_v1_shape(self._c.get(f"sites/{parts[1]}/devices", {"types": "INVERTER"}))
        if len(parts) == 4 and parts[0] == "equipment" and parts[3] == "data":
            site, sn = parts[1], parts[2]
            key = (site, params.get("startTime"), params.get("endTime"))
            if key not in self._telemetry_cache:
                self._telemetry_cache[key] = self._c.get(
                    f"sites/{site}/inverters/telemetry",
                    {"from": local_to_z(params["startTime"]), "to": local_to_z(params["endTime"])})
            rows = inverter_series(self._telemetry_cache[key]).get(sn, [])
            tel = [{"date": r["ts_utc"].astimezone(MX_OFFSET).strftime("%Y-%m-%d %H:%M:%S"),
                    "totalActivePower": r["power_w"], "totalEnergy": r["etoday_wh"],
                    "L1Data": {"acVoltage": r["voltage"], "acCurrent": r["current"], "acFrequency": r["frequency"]}}
                   for r in rows]
            if tel:                                       # v1 eToday = entry - FIRST entry: anchor 0 at the window start
                tel = [{"date": str(params["startTime"]).strip(), "totalActivePower": None, "totalEnergy": 0.0}] + tel
            return {"data": {"count": len(tel), "telemetries": tel}}
        raise SolarEdgeV2Error(f"no v2 translation for v1 call {path}")
