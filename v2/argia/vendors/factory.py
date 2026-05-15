"""
Vendor client factory.

Reads the secret reference columns from a PlantConfig and instantiates the
correct vendor client with credentials from environment variables.

This is the ONLY place that touches os.environ for vendor credentials.
Keeps the secrets-vs-code boundary clean: the Plants sheet says "look up
env var GROWATT_API_TOKEN", and this module does exactly that.

Stage 6: SMA branch added. SMA needs three values:
  secret_api_name  → SMA_CLIENT_ID
  secret_user_name → SMA_CLIENT_SECRET
  secret_pass_name → SMA_LOGIN_HINT
plus SMA_ENVIRONMENT (read directly, defaults to 'sandbox').

This re-uses the three secret_* columns the existing Plants schema already
has, no schema change required.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

from argia.core.config import PlantConfig
from argia.vendors.base import VendorClient
from argia.vendors.growatt import GrowattClient
from argia.vendors.huawei import HuaweiClient
from argia.vendors.sma import SMAClient
from argia.vendors.solaredge import SolarEdgeClient

LOG = logging.getLogger("argia.vendors.factory")


class VendorCredentialsMissing(RuntimeError):
    """Raised when a plant references env vars that are unset."""


def _env(name: str) -> str:
    """Read an env var; return empty string if not set."""
    if not name:
        return ""
    return os.environ.get(name, "").strip()


def build_client_for(plant: PlantConfig) -> VendorClient:
    """
    Construct the vendor client for one plant.

    Raises VendorCredentialsMissing if the env vars named in the plant's
    secret_*_name columns are not set.
    """
    brand = plant.brand.upper()

    if brand == "GROWATT":
        token = _env(plant.secret_api_name)
        if not token:
            user = os.environ.get("GROWATT_USERNAME", "").strip()
            pwd = os.environ.get("GROWATT_PASSWORD", "").strip()
            if not (user and pwd):
                raise VendorCredentialsMissing(
                    f"Growatt plant '{plant.plant_key}' needs either env var "
                    f"'{plant.secret_api_name}' (API token) or "
                    f"GROWATT_USERNAME+GROWATT_PASSWORD (web UI fallback). "
                    f"Neither is set."
                )
            return GrowattClient(api_token=None, web_username=user, web_password=pwd)
        return GrowattClient(api_token=token)

    if brand == "HUAWEI":
        user = _env(plant.secret_user_name)
        pwd = _env(plant.secret_pass_name)
        if not (user and pwd):
            raise VendorCredentialsMissing(
                f"Huawei plant '{plant.plant_key}' needs env vars "
                f"'{plant.secret_user_name}' and '{plant.secret_pass_name}'. "
                f"One or both are unset."
            )
        return HuaweiClient(username=user, password=pwd)

    if brand == "SOLAREDGE":
        key = _env(plant.secret_api_name)
        if not key:
            raise VendorCredentialsMissing(
                f"SolarEdge plant '{plant.plant_key}' needs env var "
                f"'{plant.secret_api_name}'. It is unset."
            )
        return SolarEdgeClient(api_key=key)

    if brand == "SMA":
        client_id = _env(plant.secret_api_name)
        client_secret = _env(plant.secret_user_name)
        login_hint = _env(plant.secret_pass_name)
        environment = os.environ.get("SMA_ENVIRONMENT", "sandbox").strip()
        if not (client_id and client_secret and login_hint):
            raise VendorCredentialsMissing(
                f"SMA plant '{plant.plant_key}' needs env vars "
                f"'{plant.secret_api_name}', '{plant.secret_user_name}', "
                f"'{plant.secret_pass_name}'. One or more is unset."
            )
        return SMAClient(
            client_id=client_id,
            client_secret=client_secret,
            login_hint=login_hint,
            environment=environment,
        )

    raise ValueError(f"Unknown brand '{plant.brand}' for plant '{plant.plant_key}'")


def build_clients_for_active_plants(
    plants: Dict[str, PlantConfig],
) -> Dict[str, VendorClient]:
    """
    Build clients for every active plant. Plants whose credentials are missing
    are logged as warnings and skipped — they won't be queried, but the rest
    of the run continues.

    Returns dict: plant_key → client.
    """
    out: Dict[str, VendorClient] = {}
    for plant_key, plant in plants.items():
        if not plant.active:
            continue
        try:
            out[plant_key] = build_client_for(plant)
        except VendorCredentialsMissing as e:
            LOG.warning("Cannot build client for %s: %s", plant_key, e)
        except Exception as e:  # noqa: BLE001 - we want to keep running
            LOG.error("Unexpected error building client for %s: %s", plant_key, e)
    return out
