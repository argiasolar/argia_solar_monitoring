"""Shared physical / reporting constants — single source of truth.

Constants that must be identical across every Argia surface (daily
report, invoicing annex, dashboard, audit text) live here so they can
never drift between outputs. Import from here; never redefine a local
copy.
"""

from __future__ import annotations

# Grid emission factor for "avoided CO2" claims on grid-displacing solar,
# in kg CO2e per kWh delivered.
#
# This is now a REGISTER, not one number: argia.core.co2 holds the
# SEMARNAT/CRE factor per year plus per-plant contracted overrides (SAG
# uses 0.202 across their whole history). Prefer co2.factor(year,
# plant_key) — it is exact for the year and plant being reported.
#
# CO2_KG_PER_KWH stays as the plain national factor currently in force
# (2024 onward: 0.444) for the few places that legitimately have no year
# or plant in hand. Tomasz set the register on 2026-09-04; before that
# the surfaces had drifted to three different numbers (0.435 on the map,
# 0.438 here and in the report, 0.444 in the annex).
from argia.core.co2 import CURRENT as CO2_KG_PER_KWH  # noqa: F401
