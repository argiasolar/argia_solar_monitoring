"""Shared physical / reporting constants — single source of truth.

Constants that must be identical across every Argia surface (daily
report, invoicing annex, dashboard, audit text) live here so they can
never drift between outputs. Import from here; never redefine a local
copy.
"""

from __future__ import annotations

# Grid emission factor for "avoided CO2" claims on grid-displacing solar,
# in kg CO2e per kWh delivered. Tomasz standardised on 0.438 kg/kWh
# (SEMARNAT/CRE Mexican grid factor, decision 2026-09-01) — the report
# site already used it, and the invoice annex disagreed at 0.444 until
# the August close caught the mismatch. Change the number here and it
# changes everywhere the constant is imported.
CO2_KG_PER_KWH = 0.438
