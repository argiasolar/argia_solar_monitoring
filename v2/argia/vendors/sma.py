"""
SMA Sunny Portal / ennexOS Monitoring API client.

Authenticates via SMA's custom OAuth2 backchannel flow:
  1. POST /oauth2/token  with grant_type=client_credentials → client token
  2. POST /oauth2/v2/bc-authorize {loginHint}   → initiates consent
  3a. Sandbox: PUT /oauth2/v2/bc-authorize/{loginHint}/status  body="accepted"
  3b. Production: poll GET /oauth2/v2/bc-authorize/{loginHint} until state=accepted

Then uses the same client token as Bearer for Monitoring API calls:
  GET /monitoring/v1/plants                                       → plant list
  GET /monitoring/v1/plants/{plantId}/devices                     → device list
  GET /monitoring/v1/devices/{deviceId}/measurements/sets         → available sets
  GET /monitoring/v1/devices/{deviceId}/measurements/sets/{set}   → time-series

Per SMA docs:
- Sandbox host:  sandbox.smaapis.de   (auth: sandbox-auth.smaapis.de)
- Production:    auth.smaapis.de + async-auth.smaapis.de + smaapis.de

Honest limitations:
- Sandbox only simulates ennexOS plants with 15-minute resolution.
- Some endpoints are unavailable in sandbox (per SMA support email).
  We log warnings and continue when individual endpoints 404.
- Power factor / per-phase data depends on what the sandbox returns — we
  build defensively and let DEBUG logs reveal what's actually in the response.

Modeled after Stage 5 SolarEdgeClient: same VendorClient Protocol surface,
same error class shape, same DEBUG-logs-raw-keys approach for live discovery.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from argia.core.config import InverterConfig, PlantConfig
from argia.core.normalize import normalize_sn, pick, safe_float
from argia.core.time_utils import MX_TZ, UTC, now_utc, parse_provider_datetime
from argia.vendors.base import InverterSnapshot, normalize_status

LOG = logging.getLogger("argia.vendors.sma")

DEFAULT_TIMEOUT_SEC = 30

# Endpoint sets per environment. Keep these constants close to the docs they
# come from (developer.sma.de / api-access-control) so future SMA URL changes
# are easy to track.
ENDPOINTS = {
    "sandbox": {
        "token":    "https://sandbox-auth.smaapis.de/oauth2/token",
        "bc_base":  "https://sandbox.smaapis.de/oauth2/v2",
        "api_base": "https://sandbox.smaapis.de/monitoring/v1",
    },
    "production": {
        "token":    "https://auth.smaapis.de/oauth2/token",
        "bc_base":  "https://async-auth.smaapis.de/oauth2/v2",
        "api_base": "https://async-auth.smaapis.de/monitoring/v1",
    },
}

# SMA inverter operational state strings → online (1) / offline (3).
# Source: SMA Monitoring API docs, device.status field. We start conservative
# and expand based on what the sandbox / production data actually returns.
OFFLINE_DEVICE_STATES = frozenset({
    "OFF", "OFFLINE", "ERROR", "FAULT", "WARNING", "DISCONNECTED",
    "MAINTENANCE", "DEACTIVATED", "STANDBY",
})


class SMAAuthError(RuntimeError):
    """Raised on 401/403 from token or API calls. Likely client creds bad
    or consent revoked."""


class SMAAPIError(RuntimeError):
    """Generic API failure (non-200, non-JSON response, rate-limit, etc).
    For rate-limit specifically, the message contains 'rate-limited'."""


class SMAConsentError(RuntimeError):
    """Backchannel consent was rejected, expired, or revoked. Distinct from
    auth errors so callers can decide whether to retry."""


class SMAClient:
    """
    SMA Monitoring API client.

    Auth is a 3-step flow done lazily via ``login()``. The same client token
    is reused for every API call. Tokens expire (SMA returns expires_in in
    the token response); we refresh them on demand when API calls return 401.

    Two environments: ``sandbox`` (default, what the SMA dev portal gives you)
    and ``production`` (after signing the API contract).

    Sandbox-specific behavior:
    - Step 3 PUTs the consent status directly, no email roundtrip.
    - SMA reply email warned some endpoints are unavailable in sandbox.
      We log warnings on 404, don't crash.
    """

    brand = "SMA"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        login_hint: str,
        environment: str = "sandbox",
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        session: Optional[requests.Session] = None,
        site_timezone: str = "America/Mexico_City",
    ) -> None:
        if not client_id:
            raise ValueError("client_id is required")
        if not client_secret:
            raise ValueError("client_secret is required")
        if not login_hint:
            raise ValueError("login_hint is required")

        env = environment.strip().lower()
        if env not in ENDPOINTS:
            raise ValueError(
                f"environment must be 'sandbox' or 'production', got {environment!r}"
            )

        self._client_id = client_id
        self._client_secret = client_secret
        self._login_hint = login_hint
        self._environment = env
        self._endpoints = ENDPOINTS[env]
        self._timeout = timeout_sec
        self._session = session or requests.Session()
        self._site_tz = MX_TZ if site_timezone == "America/Mexico_City" else MX_TZ

        # Filled by login()
        self._client_token: Optional[str] = None
        self._token_expires_at: Optional[float] = None  # unix seconds
        self._logged_in_at_consent: bool = False

    # ===== public VendorClient interface =====

    def login(self) -> None:
        """Three-step SMA auth. Idempotent — won't re-run if token still
        valid and consent already accepted in this client lifetime."""
        if self._token_valid() and self._logged_in_at_consent:
            return

        self._fetch_client_token()
        self._ensure_consent()
        self._logged_in_at_consent = True

    def fetch_day_kwh(self, plant: PlantConfig, date_iso: str) -> Optional[float]:
        """
        Total energy in kWh for the plant on the given LOCAL date.

        Implementation:
          GET /monitoring/v1/plants/{plantId}/measurements/sets/EnergyMix
              ?Period=Day&Date={date_iso}

        EnergyMix is the plant-level measurement set per SMA FAQ. Returns
        None when no data is available — common in sandbox (limited data).
        """
        self.login()
        try:
            response = self._get_json(
                f"/plants/{plant.site_id}/measurements/sets/EnergyMix",
                {"Period": "Day", "Date": date_iso},
            )
        except SMAAPIError as e:
            LOG.warning(
                "SMA plant %s EnergyMix failed: %s", plant.plant_key, e,
            )
            return None
        return self._parse_day_kwh(response, date_iso)

    def fetch_inverter_snapshots(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Latest pvGeneration measurement per inverter, one HTTP call each.

        SMA's deviceLevel measurement sets include ``pvGeneration`` which
        contains power + energy fields. The exact shape varies between
        ennexOS and Sunny Portal Classic — we parse defensively.
        """
        if not inverters:
            return []
        self.login()

        snapshots: List[InverterSnapshot] = []
        for inv in inverters:
            try:
                response = self._get_json(
                    f"/devices/{inv.inverter_sn}/measurements/sets/pvGeneration",
                    {"Period": "Recent"},
                )
            except SMAAPIError as e:
                LOG.warning(
                    "SMA device %s/%s pvGeneration failed: %s",
                    plant.plant_key, inv.inverter_sn, e,
                )
                continue
            snap = self._parse_inverter_data(
                response, plant.plant_key, inv.inverter_sn,
            )
            if snap is not None:
                snapshots.append(snap)
        return snapshots

    # ===== auth steps =====

    def _token_valid(self, slack_sec: int = 30) -> bool:
        """True if we have a token that won't expire in the next `slack_sec`."""
        if not self._client_token or self._token_expires_at is None:
            return False
        return time.time() < (self._token_expires_at - slack_sec)

    def _fetch_client_token(self) -> None:
        """Step 1: client_credentials grant. Stores token + expiry."""
        url = self._endpoints["token"]
        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        resp = self._session.post(url, data=data, timeout=self._timeout)
        if resp.status_code in (400, 401, 403):
            raise SMAAuthError(
                f"SMA token endpoint returned HTTP {resp.status_code}: "
                f"client credentials likely invalid. {resp.text[:200]}"
            )
        if resp.status_code != 200:
            raise SMAAPIError(
                f"SMA token endpoint returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except ValueError as e:
            raise SMAAPIError(f"SMA token endpoint returned non-JSON: {e}") from e

        token = payload.get("access_token")
        if not token:
            raise SMAAPIError(
                f"SMA token response missing access_token: {payload}"
            )
        self._client_token = token
        # SMA returns expires_in seconds; fall back to 1h if absent
        ttl = safe_float(payload.get("expires_in"), 3600.0) or 3600.0
        self._token_expires_at = time.time() + ttl
        LOG.debug("SMA client token acquired (ttl=%ds)", int(ttl))

    def _ensure_consent(self) -> None:
        """Step 2 (always) + Step 3 (only if needed).

        SMA's bc-authorize endpoint has consent lifecycle state. The flow is:
        1. POST bc-authorize → returns current state (pending/accepted/rejected/expired/revoked)
        2. If state is already 'accepted' → done, skip the status PUT
        3. If state is 'pending' → status PUT (sandbox) or poll (production)

        Stage 6.2: previously we always called the status PUT, but in
        sandbox once consent is accepted SMA returns 404 on subsequent
        PUTs because there's no pending request to transition. Now we
        check the state first and skip the PUT when it's not needed.
        """
        bc_base = self._endpoints["bc_base"]
        headers = self._auth_headers()

        # Step 2: initiate (or query existing) consent
        body = {"loginHint": self._login_hint}
        resp = self._session.post(
            f"{bc_base}/bc-authorize",
            headers={**headers, "Content-Type": "application/json"},
            json=body,
            timeout=self._timeout,
        )

        bc_state: Optional[str] = None
        if resp.status_code in (200, 201):
            try:
                data = resp.json()
                if isinstance(data, dict):
                    bc_state = str(data.get("state", "") or "").lower() or None
            except ValueError:
                bc_state = None
        elif resp.status_code in (400, 409):
            # "Consent already exists" path. We can't read state from the
            # error body, so we proceed to step 3 and let the lifecycle
            # handler figure out (sandbox PUT returns 404 → success).
            LOG.info(
                "SMA bc-authorize returned %d, assuming existing consent: %s",
                resp.status_code, resp.text[:120],
            )
        elif resp.status_code in (401, 403):
            raise SMAAuthError(
                f"SMA bc-authorize returned HTTP {resp.status_code}: "
                f"client token rejected. {resp.text[:200]}"
            )
        else:
            raise SMAAPIError(
                f"SMA bc-authorize returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        # Hard terminal states — no recovery in this run
        if bc_state in ("rejected", "expired", "revoked"):
            raise SMAConsentError(
                f"SMA consent state is '{bc_state}'. Resource owner did "
                f"not approve. Re-run bc-authorize and accept the request."
            )

        # If consent is already accepted, skip the status PUT entirely
        if bc_state == "accepted":
            LOG.info(
                "SMA consent already 'accepted' — skipping status PUT"
            )
            return

        # State is 'pending' or unknown — proceed with Step 3
        if self._environment == "sandbox":
            self._simulate_sandbox_consent()
        else:
            self._poll_production_consent()

    # Body shapes for the sandbox status PUT. SMA's docs say
    # `Body: "accepted"` but the actual API expects a JSON object that
    # deserializes to `AuthorizationFlowStatusChangeRequest`. The docs and
    # reality disagree, so we try shapes in order from most-likely to
    # least-likely and use the first one that returns 200/204.
    #
    # If you discover a different working shape, add it to the FRONT of this
    # list and submit a Stage 6.2 patch. The LOG.info line below tells you
    # which shape won.
    _SANDBOX_CONSENT_BODY_SHAPES = (
        {"status": "accepted"},      # path URL ends in /status → most natural
        {"newStatus": "accepted"},   # common .NET pattern
        {"state": "accepted"},       # matches GET response field name
        '"accepted"',                # literal docs example (likely outdated)
    )

    def _simulate_sandbox_consent(self) -> None:
        """Sandbox-only: PUT the consent status. SMA's docs lie about the
        body shape, so we try several. Raises SMAConsentError if all fail
        with a body listing every attempt so you can patch in Stage 6.2."""
        bc_base = self._endpoints["bc_base"]
        url = f"{bc_base}/bc-authorize/{quote(self._login_hint)}/status"
        headers = {**self._auth_headers(), "Content-Type": "application/json"}

        attempts = []
        for shape in self._SANDBOX_CONSENT_BODY_SHAPES:
            if isinstance(shape, dict):
                resp = self._session.put(
                    url, headers=headers, json=shape, timeout=self._timeout,
                )
                shape_desc = f"json={shape}"
            else:
                resp = self._session.put(
                    url, headers=headers, data=shape, timeout=self._timeout,
                )
                shape_desc = f"raw={shape!r}"

            if resp.status_code in (200, 204):
                LOG.info(
                    "SMA sandbox consent simulation succeeded with %s "
                    "(HTTP %d). If this is the first time you see this log, "
                    "consider promoting that shape to the front of "
                    "_SANDBOX_CONSENT_BODY_SHAPES.",
                    shape_desc, resp.status_code,
                )
                return

            if resp.status_code == 404:
                # Stage 6.2: SMA returns 404 when the consent record is no
                # longer in a 'pending' state (already accepted, or never
                # existed). Either way we have nothing to do — short-circuit.
                LOG.info(
                    "SMA sandbox status PUT returned 404 — consent is not "
                    "in 'pending' state (already accepted, or expired). "
                    "Treating as success."
                )
                return

            attempts.append(
                f"  - {shape_desc} → HTTP {resp.status_code}: "
                f"{resp.text[:140]}"
            )
            LOG.debug(
                "SMA sandbox consent shape %s failed (HTTP %d), trying next",
                shape_desc, resp.status_code,
            )

        details = "\n".join(attempts)
        raise SMAConsentError(
            f"SMA sandbox consent simulation failed; tried "
            f"{len(self._SANDBOX_CONSENT_BODY_SHAPES)} body shapes, none "
            f"accepted by SMA:\n{details}"
        )

    def _poll_production_consent(
        self,
        poll_interval_sec: int = 5,
        max_wait_sec: int = 600,
    ) -> None:
        """Production: poll until the resource owner accepts via email link.

        Default 10-minute timeout. Plant owner has to click an emailed link.
        For cron use this should usually be a no-op (consent already valid)
        and only re-trigger when a new system is added.
        """
        bc_base = self._endpoints["bc_base"]
        url = f"{bc_base}/bc-authorize/{quote(self._login_hint)}"
        deadline = time.time() + max_wait_sec
        while time.time() < deadline:
            resp = self._session.get(
                url, headers=self._auth_headers(), timeout=self._timeout,
            )
            if resp.status_code == 200:
                try:
                    state = resp.json().get("state", "").lower()
                except ValueError:
                    state = ""
                if state == "accepted":
                    return
                if state in ("rejected", "expired", "revoked"):
                    raise SMAConsentError(
                        f"SMA consent state={state}; resource owner did not approve"
                    )
            time.sleep(poll_interval_sec)
        raise SMAConsentError(
            f"SMA consent timed out after {max_wait_sec}s. "
            f"Resource owner did not click the email link in time."
        )

    def _auth_headers(self) -> Dict[str, str]:
        if not self._client_token:
            raise SMAAuthError("No client token; call login() first")
        return {"Authorization": f"Bearer {self._client_token}"}

    # ===== HTTP transport (mocked in tests) =====

    def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Single low-level GET against the Monitoring API.

        Tests mock THIS method. Refresh-on-401 not implemented yet — SMA
        tokens are typically long-lived (~1 hour) and the script runs are
        much shorter, so we'd see a 401 only if creds are revoked.
        """
        url = f"{self._endpoints['api_base']}{path}"
        resp = self._session.get(
            url, params=params, headers=self._auth_headers(), timeout=self._timeout,
        )
        if resp.status_code in (401, 403):
            raise SMAAuthError(
                f"SMA {path} returned HTTP {resp.status_code}: "
                f"client token rejected (consent revoked?). {resp.text[:200]}"
            )
        if resp.status_code == 404:
            # Sandbox documents that some endpoints are unavailable —
            # propagate as APIError so caller can warn-and-continue
            raise SMAAPIError(
                f"SMA {path} returned HTTP 404: endpoint not available "
                f"(common in sandbox)"
            )
        if resp.status_code == 429:
            raise SMAAPIError(
                f"SMA {path} rate-limited (HTTP 429)."
            )
        if resp.status_code != 200:
            raise SMAAPIError(
                f"SMA {path} returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise SMAAPIError(f"SMA {path} returned invalid JSON: {e}") from e

    # ===== parsers (pure, fully testable) =====

    @staticmethod
    def _parse_day_kwh(
        response: Dict[str, Any], date_iso: str
    ) -> Optional[float]:
        """Pure function. Extract day's kWh from a Monitoring API measurement
        set response.

        SMA's EnergyMix shape varies; we look for several common keys.
        Returns None when the response doesn't contain a usable value
        (rather than throwing — sandbox sometimes returns empty sets).
        """
        if not isinstance(response, dict):
            return None
        # ennexOS shape: { "plant": {...}, "setType": "EnergyMix", "set": {...} }
        # Classic shape: { "device": {...}, "set": {...} }
        s = response.get("set")
        if not isinstance(s, dict):
            return None

        # Try several SMA naming conventions
        candidates = [
            "totalEnergyDay", "energyDay", "totalEnergy", "energyTotal",
            "pvEnergyDay", "yieldDay",
        ]
        value = pick(s, candidates)
        if value is None:
            return None
        kwh = safe_float(value)
        if kwh is None:
            return None
        # If suspiciously large (>1e6), it's probably in Wh, convert
        if kwh > 1_000_000:
            kwh = kwh / 1000.0
        return round(kwh, 3)

    def _parse_inverter_data(
        self,
        response: Dict[str, Any],
        plant_key: str,
        sn: str,
    ) -> Optional[InverterSnapshot]:
        """Parse a pvGeneration measurement set into an InverterSnapshot.

        We don't yet know the exact field names sandbox returns. Until we
        capture a real response, this is defensive: try several keys for
        each field, log at DEBUG what keys actually showed up.
        """
        if not isinstance(response, dict):
            return None
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(
                "sma pvGeneration response top-level keys for %s: %s",
                sn, sorted(response.keys()) if isinstance(response, dict) else "n/a",
            )

        s = response.get("set")
        if not isinstance(s, dict):
            return None

        # Power: SMA usually returns W. Some endpoints return kW; convert if small.
        power_raw = safe_float(pick(s, [
            "power", "totalActivePower", "pac", "activePower", "powerW",
        ]))
        if power_raw is None:
            power_w = None
        elif abs(power_raw) <= 1000:
            # Heuristic: <=1000 looks like kW (an 800kW inverter is the upper bound
            # for the assumption; mainly catches sandbox values like 25.4)
            power_w = power_raw * 1000.0
        else:
            power_w = power_raw

        # eToday: same Wh-vs-kWh heuristic
        etoday_raw = safe_float(pick(s, [
            "energyDay", "yieldDay", "totalEnergyDay", "eToday",
        ]))
        if etoday_raw is None:
            etoday_kwh = None
        elif etoday_raw > 1_000_000:
            etoday_kwh = round(etoday_raw / 1000.0, 3)
        else:
            etoday_kwh = round(etoday_raw, 3)

        status_raw = pick(s, ["status", "deviceStatus", "operationalState"])
        if status_raw is None:
            # Sometimes status lives outside `set`
            device = response.get("device") or {}
            if isinstance(device, dict):
                status_raw = pick(device, ["status", "operationalState"])

        # SMA-specific status mapping
        if status_raw is None:
            status = 1
        else:
            status_upper = str(status_raw).strip().upper()
            status = 3 if status_upper in OFFLINE_DEVICE_STATES else 1

        ts_raw = pick(s, ["time", "timestamp", "date"])
        ts_utc = self._parse_sma_timestamp(ts_raw) or now_utc()

        return InverterSnapshot(
            plant_key=plant_key,
            inverter_sn=normalize_sn(sn),
            timestamp_utc=ts_utc,
            status=status,
            power_w=power_w,
            etoday_kwh=etoday_kwh,
            raw_status=str(status_raw) if status_raw is not None else "",
        )

    def _parse_sma_timestamp(self, value: Any) -> Optional[dt.datetime]:
        """SMA timestamps are usually ISO-8601 with timezone (e.g.
        '2020-03-23T12:40:00Z' or '2020-03-23T12:40:00+02:00'). Fall back
        to provider-datetime parser for older formats."""
        if not value:
            return None
        s = str(value).strip()
        # Try ISO 8601 with timezone
        try:
            # Python 3.11+ supports the 'Z' suffix in fromisoformat
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # Assume site-local (Mexico City) if naive
                parsed = parsed.replace(tzinfo=self._site_tz)
            return parsed.astimezone(UTC)
        except (ValueError, TypeError):
            pass
        # Fall back to argia's general provider parser
        return parse_provider_datetime(s)
