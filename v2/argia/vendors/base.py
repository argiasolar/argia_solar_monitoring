"""
Vendor base protocol.

Every inverter vendor (Growatt, Huawei, SolarEdge, SMA) implements this
contract. Keeping the surface small means the orchestrator doesn't care
which vendor it's talking to — it just iterates ``portfolio.active_plants()``
and dispatches to the right client.

There are exactly two public methods every vendor must provide:

  fetch_day_kwh(plant, date_iso) -> Optional[float]
      Total kWh produced by the plant on the given LOCAL date.

  fetch_inverter_snapshots(plant, inverters) -> list[InverterSnapshot]
      Live status + per-inverter EToday for the listed inverters.

If a vendor doesn't expose one of these (e.g. SMA sandbox lacks daily
totals while we wait for hardware), the method may raise NotImplementedError
and the orchestrator skips it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable

from argia.core.config import InverterConfig, PlantConfig


@dataclass(frozen=True)
class InverterSnapshot:
    """
    A single inverter's state at a moment in time.

    All energy values in kWh, all power values in W. Keep units consistent
    so the orchestrator and tests can be unit-naive.
    """

    plant_key: str
    inverter_sn: str
    timestamp_utc: dt.datetime
    status: int  # 1 = online, 3 = offline (matches v1 convention)
    power_w: Optional[float]
    etoday_kwh: Optional[float]
    raw_status: str = ""  # raw value from API for debugging


@runtime_checkable
class VendorClient(Protocol):
    """
    The interface every vendor implements. Use ``runtime_checkable`` so
    tests can assert ``isinstance(client, VendorClient)``.
    """

    brand: str  # "GROWATT" | "HUAWEI" | "SOLAREDGE" | "SMA"

    def login(self) -> None:
        """Authenticate. Should be idempotent and cheap to call again."""
        ...

    def fetch_day_kwh(self, plant: PlantConfig, date_iso: str) -> Optional[float]:
        """
        Total energy in kWh produced on the plant for the local date ``date_iso``.
        Returns None if not available (e.g. wrong day, API issue).
        """
        ...

    def fetch_inverter_snapshots(
        self,
        plant: PlantConfig,
        inverters: List[InverterConfig],
    ) -> List[InverterSnapshot]:
        """
        Live snapshot for the given inverters. Order of returned list does
        not need to match input. Inverters not found by the API may be
        omitted — the orchestrator handles missing rows.
        """
        ...


def normalize_status(raw_value) -> int:
    """
    Normalize vendor-specific status to 1/3.

    Online → 1, Offline → 3, Unknown → 1 (assume online; alarms will reveal
    truth elsewhere).
    """
    if raw_value is None:
        return 1
    s = str(raw_value).strip().lower()
    if s in ("3", "0", "offline", "off", "disconnect", "disconnected", "fault"):
        return 3
    return 1
