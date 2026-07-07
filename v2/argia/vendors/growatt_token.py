"""Growatt OpenAPI token fallback — degraded-mode plant energy.

WHY (incident 2026-07-07): Growatt soft-blocked our web-session logins
(the path that carries per-inverter data, temps, faults, ShineMaster
irradiance). v1 ran for months on a DIFFERENT door: the OpenAPI token
(`GROWATT_API_TOKEN`), plant-level only. This module mirrors v1's exact,
production-proven call so that while the web session is blocked the
business numbers keep flowing:

    GET {base}/v1/plant/data  headers={token}  params={plant_id}
    -> data.today_energy  (kWh, resets at midnight)

Degraded-mode design, honestly scoped:
- PLANT-LEVEL ENERGY ONLY. No per-inverter rows are fabricated — the
  dashboard shows real gaps (post-v44, unknown != downtime), and only
  the daily energy/production numbers are preserved.
- The midnight problem: kpi-eod runs at 06:00 for YESTERDAY, when
  today_energy has already reset. So telemetry CACHES the value intraday
  (last write ~21:55, after sunset — the day is complete) and kpi-eod
  reads the cache next morning.
- Fallback triggers ONLY when the web path produced zero rows for a
  plant (the block signature). Healthy days never touch the token API —
  it stays cold, rate-limit budget intact, for emergencies.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import requests

LOG = logging.getLogger(__name__)

OPENAPI_BASE = "https://openapi.growatt.com"
DEFAULT_CACHE_FILE = "~/.argia_growatt_token_energy.json"
INTER_PLANT_SLEEP_S = 2.0   # v1's politeness pause, kept verbatim


def cache_file() -> Path:
    return Path(os.environ.get("ARGIA_GROWATT_TOKEN_CACHE",
                               DEFAULT_CACHE_FILE)).expanduser()


class GrowattTokenClient:
    """Plant-level energy via the OpenAPI token (v1's proven route)."""

    def __init__(self, token: str, *, base_url: str = OPENAPI_BASE,
                 timeout_sec: int = 25, session=None) -> None:
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._http = session or requests.Session()

    @classmethod
    def from_env(cls) -> Optional["GrowattTokenClient"]:
        token = os.environ.get("GROWATT_API_TOKEN", "").strip()
        return cls(token) if token else None

    def plant_today_energy(self, plant_id: str) -> Optional[float]:
        """today_energy in kWh, or None on any failure (logged, never
        raised — the fallback must not add failure modes to telemetry)."""
        if not plant_id:
            return None
        try:
            time.sleep(INTER_PLANT_SLEEP_S)
            r = self._http.get(
                f"{self._base}/v1/plant/data",
                headers={"token": self._token, "Accept": "application/json"},
                params={"plant_id": str(plant_id)},
                timeout=self._timeout,
            )
            if r.status_code != 200:
                LOG.warning("growatt token API HTTP %s for plant %s",
                            r.status_code, plant_id)
                return None
            js = r.json()
            data = js.get("data") if isinstance(js, dict) else None
            if not data:
                LOG.warning("growatt token API empty payload for plant %s "
                            "(error_msg=%s)", plant_id,
                            js.get("error_msg") if isinstance(js, dict)
                            else "?")
                return None
            val = data.get("today_energy")
            return float(val) if val not in (None, "") else None
        except Exception as e:  # noqa: BLE001 — fallback never raises
            LOG.warning("growatt token API failed for plant %s: %s",
                        plant_id, e)
            return None


# ---- intraday energy cache (telemetry writes, kpi-eod reads) ----------------

def cache_energy(date_iso: str, plant_key: str, kwh: float) -> None:
    """Persist today's plant energy. Keeps ONLY the current date — the
    file self-prunes so stale values can never leak into a later day."""
    try:
        path = cache_file()
        data: Dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except ValueError:
                data = {}
        day = data.get(date_iso, {}) if date_iso in data else {}
        day[plant_key] = kwh
        path.write_text(json.dumps({date_iso: day}))
        path.chmod(0o600)
    except Exception as e:  # noqa: BLE001
        LOG.warning("growatt token cache write failed (%s)", e)


def cached_energy(date_iso: str, plant_key: str) -> Optional[float]:
    try:
        path = cache_file()
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        val = data.get(date_iso, {}).get(plant_key)
        return float(val) if val is not None else None
    except Exception:  # noqa: BLE001
        return None


def apply_energy_fallback(
    energy_by_inv: Dict[str, Optional[float]],
    date_iso: str,
    plant_key: str,
) -> Tuple[Dict[str, Optional[float]], bool]:
    """Pure decision for kpi-eod: if the telemetry-derived energies are
    absent/zero and the token cache holds a value for this plant+date,
    substitute a single synthetic entry. Returns (energies, used)."""
    have_real = any(v for v in energy_by_inv.values() if v)
    if have_real:
        return energy_by_inv, False
    kwh = cached_energy(date_iso, plant_key)
    if kwh is None or kwh <= 0:
        return energy_by_inv, False
    return {"_token_fallback": kwh}, True
