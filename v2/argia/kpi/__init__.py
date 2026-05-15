"""Argia KPI computation — Stage 7.2.

Pure math over archived telemetry. No side effects, no I/O outside the
explicit ``reader`` module. Computes:
- Daily energy per inverter, per plant
- Daily Performance Ratio (two flavors)
- Per-inverter peer ranking
- Capacity factor

What this module deliberately does NOT do:
- Decide if a value triggers an alert (Stage 7.4)
- Write to the sheet (Stage 7.3 archive workflow does)
- Compare against historical PRs (Stage 7.3 archive needed first)

Architecture: 4 files
- ``reader.py``  reads telemetry rows for a date, returns DayBundle
- ``energy.py``  end-of-day energy per inverter/plant
- ``irradiance.py``  trapezoidal integration of W/m² → kWh/m²
- ``performance.py``  PR, capacity factor, peer ranking
"""

from argia.kpi.energy import EnergyDay, compute_plant_energy
from argia.kpi.irradiance import integrate_irradiance_kwh_m2
from argia.kpi.performance import (
    InverterPeerRank,
    PlantPerformanceDay,
    compute_inverter_peer_ranking,
    compute_plant_pr,
)
from argia.kpi.reader import DayBundle, InverterRow, read_day_bundle

__all__ = [
    "DayBundle",
    "EnergyDay",
    "InverterPeerRank",
    "InverterRow",
    "PlantPerformanceDay",
    "compute_inverter_peer_ranking",
    "compute_plant_energy",
    "compute_plant_pr",
    "integrate_irradiance_kwh_m2",
    "read_day_bundle",
]
