"""
HTTP client for the Growatt web UI.

Thin wrapper around ``requests.Session`` that:
  * logs in (form-encoded POST to ``/login``, expects ``assToken`` cookie)
  * exposes one method per documented read-only endpoint
  * refuses to call anything that looks like a write/mutation path
  * does NOT parse responses — that's ``growatt_web_parser``'s job

This module is paired with ``growatt_web_parser``. Together they replace
the v1 split of ``argia_growatt.py`` / ``argia_growatt_monitoring.py`` /
``argia_growatt_inverters.py`` / ``argia_growatt_health_client.py``.

Wiring into the orchestrator (the v2 ``GrowattClient`` facade in
``growatt.py``) is intentionally out of scope for Stage 1 — that lands in
Stage 2 once the parser is proven.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import requests

LOG = logging.getLogger("argia.vendors.growatt_web")

WEB_BASE = "https://server.growatt.com"
DEFAULT_TIMEOUT_SEC = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Anything matching these is a HARD REFUSE.
# v1 confirmed these patterns cover Growatt's mutation endpoints
# (commonDeviceSetC/, setMax, setTlx, delMax, etc.).
UNSAFE_PATH_PREFIXES = ("/commonDeviceSetC/",)
UNSAFE_KEYWORDS = (
    "setmax", "settlx", "setinverter",
    "delmax", "deltlx", "delinverter",
    "delete", "save",
)


class GrowattAuthError(RuntimeError):
    """Login failed (bad credentials, account locked, or server changed shape)."""


class GrowattAPIError(RuntimeError):
    """An endpoint returned a non-200 status or an unexpected response."""


class GrowattUnsafePathError(ValueError):
    """The requested path matches the write/mutation block-list."""


def _is_unsafe(path: str) -> bool:
    lower = path.lower()
    if any(lower.startswith(p.lower()) for p in UNSAFE_PATH_PREFIXES):
        return True
    return any(kw in lower for kw in UNSAFE_KEYWORDS)


class GrowattWebClient:
    """
    Authenticated HTTP client for the Growatt web UI.

    Usage:
        client = GrowattWebClient(username=..., password=...)
        client.login()
        fixture = client.get_max_history(sn="JFM7DXN00T", date_iso="2026-05-11")
        rows = parse_max_history(fixture)

    The methods return the raw response dict (the same shape the capture
    script writes to disk, minus the ``_meta`` wrapper). Pass directly to
    the parser functions.
    """

    brand = "GROWATT_WEB"

    def __init__(
        self,
        username: str,
        password: str,
        *,
        base_url: str = WEB_BASE,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not username:
            raise ValueError("username is required")
        if not password:
            raise ValueError("password is required")
        self._username = username
        self._password = password
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", DEFAULT_USER_AGENT)
        self._logged_in = False

    # ----- auth -----

    def login(self) -> None:
        """
        Idempotent login. Subsequent calls are a no-op once we have a
        valid assToken cookie.
        """
        if self._logged_in:
            return

        # Prime the session — Growatt sets some pre-auth cookies on GET /login
        self._session.get(f"{self._base}/login", timeout=self._timeout)

        resp = self._session.post(
            f"{self._base}/login",
            data={"account": self._username, "password": self._password},
            timeout=self._timeout,
            allow_redirects=False,
        )

        cookies = self._session.cookies.get_dict()
        # Growatt sets an "assToken" cookie on successful login; some
        # accounts use a JSON response body instead. Treat either as success.
        if "assToken" in cookies:
            self._logged_in = True
            LOG.info("Growatt web login OK (assToken cookie)")
            return

        if resp.status_code == 302:
            loc = resp.headers.get("Location", "")
            if "index" in loc or "panel" in loc:
                self._logged_in = True
                LOG.info("Growatt web login OK (302 → %s)", loc)
                return

        if resp.status_code == 200:
            try:
                j = resp.json()
                if str(j.get("result")) in ("1", "True", "true"):
                    self._logged_in = True
                    LOG.info("Growatt web login OK (JSON)")
                    return
            except ValueError:
                pass

        raise GrowattAuthError(
            f"Growatt web login failed: HTTP {resp.status_code}, "
            f"no assToken cookie set"
        )

    # ----- low-level GET/POST with safety guard -----

    def _post(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        if _is_unsafe(path):
            raise GrowattUnsafePathError(
                f"Refusing to call mutation-shaped path: {path}"
            )
        self.login()
        resp = self._session.post(
            f"{self._base}{path}",
            data=body or {},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self._base,
                "Referer": f"{self._base}/index",
            },
            timeout=self._timeout,
        )
        return self._build_envelope(resp, path, body)

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if _is_unsafe(path):
            raise GrowattUnsafePathError(
                f"Refusing to call mutation-shaped path: {path}"
            )
        self.login()
        resp = self._session.get(
            f"{self._base}{path}",
            params=params,
            timeout=self._timeout,
        )
        return self._build_envelope(resp, path, params)

    @staticmethod
    def _build_envelope(resp, path, request_body):
        """Wrap the response in the same {_meta, response} shape the fixture
        files use, so test fixtures and live calls are interchangeable
        from the parser's point of view."""
        if resp.status_code != 200:
            raise GrowattAPIError(
                f"Growatt {path} returned HTTP {resp.status_code}"
            )
        content_type = resp.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            try:
                response_body: Any = resp.json()
            except ValueError:
                response_body = {"_raw_text": resp.text}
        else:
            # Growatt's habit: JSON body served with text/html content-type.
            response_body = {"_raw_text": resp.text}
        return {
            "_meta": {
                "url": resp.url,
                "status": resp.status_code,
                "request_body": request_body or {},
            },
            "response": response_body,
        }

    # ----- endpoints (each returns the fixture-shaped envelope) -----

    def get_max_history(
        self,
        sn: str,
        date_iso: str,
        start: int = 0,
    ) -> Dict[str, Any]:
        """POST /device/getMAXHistory — per-inverter 5-min samples for a day."""
        return self._post(
            "/device/getMAXHistory",
            {
                "maxSn": sn,
                "startDate": date_iso,
                "endDate": date_iso,
                "start": str(start),
            },
        )

    def get_max_day_chart(
        self,
        sn: str,
        plant_id: str,
        date_iso: str,
    ) -> Dict[str, Any]:
        """POST /panel/max/getMAXDayChart — 288-slot pac series for a day."""
        return self._post(
            "/panel/max/getMAXDayChart",
            {"maxSn": sn, "plantId": plant_id, "date": date_iso},
        )

    def get_max_total_data(self, plant_id: str) -> Dict[str, Any]:
        """POST /panel/max/getMAXTotalData?plantId=… — plant aggregate."""
        return self._post(f"/panel/max/getMAXTotalData?plantId={plant_id}")

    def get_plant_data(self, plant_id: str) -> Dict[str, Any]:
        """POST /panel/getPlantData?plantId=… — plant metadata (note: POST)."""
        return self._post(f"/panel/getPlantData?plantId={plant_id}")

    def get_devices_by_plant(self, plant_id: str) -> Dict[str, Any]:
        """POST /panel/getDevicesByPlant?plantId=… — devices in this plant.

        Returns at most one SN per device-type bucket — see parser docstring.
        """
        return self._post(f"/panel/getDevicesByPlant?plantId={plant_id}")

    def get_alert_plant_event(self, plant_id: str) -> Dict[str, Any]:
        """GET /panel/alertPlantEvent?plantId=… — current alerts."""
        return self._get(f"/panel/alertPlantEvent?plantId={plant_id}")

    def seed_env_page(self, plant_id: str) -> None:
        """Growatt's env endpoints return EMPTY 200s without plant context:
        the web UI sets a selectedPlantId cookie and visits the env page
        first (v1-proven; skipping this was the 2026-07-06 all-zeros bug)."""
        self.login()
        self._session.cookies.set("selectedPlantId", str(plant_id))
        self._get("/device/getEnvPage")

    def get_env_list(self, plant_id: str,
                     curr_page: int = 1) -> Dict[str, Any]:
        """List env/weather devices for a plant. The configured datalogger
        SN is NOT guaranteed to be the env device (v1's explicit warning) —
        this is the authoritative source."""
        self.login()
        self._session.cookies.set("selectedPlantId", str(plant_id))
        return self._post("/device/getEnvList", {
            "plantId": str(plant_id),
            "currPage": str(curr_page),
            "alias": "",
        })

    def get_env_history(self, plant_id: str, datalog_sn: str, addr: int,
                        day_iso: str, start: int = 0) -> Dict[str, Any]:
        """ShineMaster stored history (dense W/m² samples) for one day.
        Same endpoint + payload + plant-context seeding as the proven v1
        scripts."""
        self.login()
        self._session.cookies.set("selectedPlantId", str(plant_id))
        return self._post("/device/getEnvHistory", {
            "datalogSn": datalog_sn,
            "addr": str(addr),
            "startDate": day_iso,
            "endDate": day_iso,
            "start": str(start),
        })

    def get_weather_by_plant_id(self, plant_id: str) -> Dict[str, Any]:
        """POST /index/getWeatherByPlantId?plantId=… — Growatt's weather feed."""
        return self._post(f"/index/getWeatherByPlantId?plantId={plant_id}")

    def list_device(self, account_name: str) -> Dict[str, Any]:
        """GET /returnDevice/listDevice?accountName=… — account-wide list."""
        return self._get(
            "/returnDevice/listDevice",
            params={"accountName": account_name},
        )
