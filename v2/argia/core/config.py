"""
Config loader.

Reads the v2 ``Plants`` and ``Inverters`` tabs into typed dataclasses.
v1 packed inverter SNs into columns of the SNAP tab (with header typos like
``IVERTER2``); v2 normalizes them into their own tab so adding inverter #5
is just another row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from argia.core.normalize import normalize_sn, normalize_text, safe_float
from argia.core.sheets import SheetsClient

LOG = logging.getLogger("argia.core.config")


@dataclass(frozen=True)
class PlantConfig:
    """One plant in the portfolio."""

    plant_key: str  # e.g. "MEX1", "SLP1"
    customer: str
    brand: str  # GROWATT | HUAWEI | SOLAREDGE | SMA
    site_id: str  # vendor-specific identifier

    # Capacity
    kwp_dc: float
    kwp_ac: float

    # Location
    lat: Optional[float]
    lon: Optional[float]

    # Performance targets
    expected_factor: float  # ratio used to compute Argia-expected production
    pr_target: float  # target Performance Ratio for alerts
    installation_date: str  # ISO date string (or "" if unknown)

    # Secret references — these are env var NAMES, not the secrets themselves.
    # E.g. secret_api_name="GROWATT_API_TOKEN" tells the loader to read
    # os.environ["GROWATT_API_TOKEN"].
    secret_api_name: str
    secret_user_name: str
    secret_pass_name: str

    # Weather source: ALL plants (even Huawei) read weather from a Growatt
    # ShineMaster. ``weather_plant_id`` is the Growatt plant hosting it.
    weather_plant_id: str
    datalogger_sn: str  # ShineMaster serial number
    datalogger_addr: int  # Modbus address on the ShineMaster

    active: bool


@dataclass(frozen=True)
class InverterConfig:
    """One inverter in a plant."""

    plant_key: str
    inverter_sn: str
    inverter_label: str  # human label, e.g. "Inverter 1"
    rated_kw: float
    active: bool


@dataclass(frozen=True)
class Portfolio:
    """All plants + their inverters, loaded once at start of run."""

    plants: Dict[str, PlantConfig] = field(default_factory=dict)
    inverters_by_plant: Dict[str, List[InverterConfig]] = field(default_factory=dict)

    def active_plants(self) -> List[PlantConfig]:
        return [p for p in self.plants.values() if p.active]

    def inverters_for(self, plant_key: str) -> List[InverterConfig]:
        return [i for i in self.inverters_by_plant.get(plant_key, []) if i.active]

    def plants_by_brand(self, brand: str) -> List[PlantConfig]:
        target = brand.upper()
        return [p for p in self.active_plants() if p.brand == target]


# ----------------- expected sheet headers -----------------

PLANTS_HEADER = [
    "plant_key", "customer", "brand", "site_id",
    "kwp_dc", "kwp_ac", "lat", "lon",
    "expected_factor", "pr_target", "installation_date",
    "secret_api_name", "secret_user_name", "secret_pass_name",
    "weather_plant_id", "datalogger_sn", "datalogger_addr",
    "active",
]

INVERTERS_HEADER = [
    "plant_key",
    "inverter_sn",
    "inverter_label",
    "rated_kw",
    "active",
]


def _truthy(value) -> bool:
    """Loose truthy parse for sheet values: TRUE/yes/1/y → True."""
    s = normalize_text(value).lower()
    return s in ("true", "yes", "y", "1", "x")


def load_portfolio(sheets: SheetsClient) -> Portfolio:
    """Read Plants + Inverters tabs and return a Portfolio object."""
    plants_raw = sheets.read_table("Plants", "A1:Z")
    inverters_raw = sheets.read_table("Inverters", "A1:Z")

    plants: Dict[str, PlantConfig] = {}
    for row in plants_raw:
        plant_key = normalize_text(row.get("plant_key"))
        if not plant_key:
            continue

        try:
            cfg = PlantConfig(
                plant_key=plant_key,
                customer=normalize_text(row.get("customer")),
                brand=normalize_text(row.get("brand")).upper(),
                site_id=normalize_text(row.get("site_id")),
                kwp_dc=safe_float(row.get("kwp_dc"), 0.0) or 0.0,
                kwp_ac=safe_float(row.get("kwp_ac"), 0.0) or 0.0,
                lat=safe_float(row.get("lat")),
                lon=safe_float(row.get("lon")),
                expected_factor=safe_float(row.get("expected_factor"), 0.0) or 0.0,
                pr_target=safe_float(row.get("pr_target"), 0.0) or 0.0,
                installation_date=normalize_text(row.get("installation_date")),
                secret_api_name=normalize_text(row.get("secret_api_name")),
                secret_user_name=normalize_text(row.get("secret_user_name")),
                secret_pass_name=normalize_text(row.get("secret_pass_name")),
                weather_plant_id=normalize_text(row.get("weather_plant_id")),
                datalogger_sn=normalize_text(row.get("datalogger_sn")),
                datalogger_addr=int(safe_float(row.get("datalogger_addr"), 0) or 0),
                active=_truthy(row.get("active")),
            )
        except (ValueError, TypeError) as e:
            LOG.warning("Skipping malformed Plants row %s: %s", plant_key, e)
            continue

        if plant_key in plants:
            LOG.warning("Duplicate plant_key '%s' in Plants tab — keeping first", plant_key)
            continue
        plants[plant_key] = cfg

    inverters_by_plant: Dict[str, List[InverterConfig]] = {}
    for row in inverters_raw:
        plant_key = normalize_text(row.get("plant_key"))
        sn = normalize_sn(row.get("inverter_sn"))
        if not plant_key or not sn:
            continue

        if plant_key not in plants:
            LOG.warning(
                "Inverters row references unknown plant_key '%s' — skipping", plant_key
            )
            continue

        inv = InverterConfig(
            plant_key=plant_key,
            inverter_sn=sn,
            inverter_label=normalize_text(row.get("inverter_label")) or sn,
            rated_kw=safe_float(row.get("rated_kw"), 0.0) or 0.0,
            active=_truthy(row.get("active")),
        )
        inverters_by_plant.setdefault(plant_key, []).append(inv)

    LOG.info(
        "Loaded portfolio: %d plants (%d active), %d inverters",
        len(plants),
        sum(1 for p in plants.values() if p.active),
        sum(len(v) for v in inverters_by_plant.values()),
    )
    return Portfolio(plants=plants, inverters_by_plant=inverters_by_plant)
