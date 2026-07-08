"""Shared physical / reporting constants — single source of truth.

Constants that must be identical across every Argia surface (daily
report, invoicing annex, dashboard, audit text) live here so they can
never drift between outputs. Import from here; never redefine a local
copy.
"""

from __future__ import annotations

# Grid emission factor for "avoided CO2" claims on grid-displacing solar,
# in kg CO2e per kWh delivered. Argia standardised on 0.444 kg/kWh across
# all reporting and customer-facing documents (2026-07); this supersedes
# the earlier ~0.435 figure. Change the number here and it changes
# everywhere the constant is imported.
CO2_KG_PER_KWH = 0.444
