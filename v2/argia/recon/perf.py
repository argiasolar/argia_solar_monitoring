"""Weather-normalized performance — PR_STC (AGS-701 R2). PURE.

The Golden Standard's rule: performance is judged on temperature-
corrected PR_STC, never raw kWh and never raw PR — standard PR
penalizes hot Mexican rooftops for physics, not faults.

    PR      = E / (kWp_DC * H)                    (already in the KPI)
    PR_STC  = PR / (1 + gamma * (T_cell_eff - 25))

with gamma = the module's power temperature coefficient (negative,
e.g. -0.0034 /°C, from plant config) and T_cell_eff the
irradiance-WEIGHTED module temperature of the day — hot noon hours
carry the energy, so they carry the correction too (IEC 61724-1
weighting). No measured module temperature or no gamma -> None:
a correction is computed from measurements or not at all (AGS-901).
"""

from __future__ import annotations

from typing import Optional

STC_TEMP_C = 25.0
# sanity rails: outside these the inputs are broken, not the plant
_T_MIN, _T_MAX = -10.0, 90.0
_GAMMA_MIN, _GAMMA_MAX = -0.01, 0.0


def pr_stc(pr: Optional[float], t_cell_eff_c: Optional[float],
           gamma_pmax: Optional[float]) -> Optional[float]:
    """Temperature-corrected PR, rounded to 4 dp. None whenever any
    input is missing or implausible — never a silent guess."""
    if pr is None or t_cell_eff_c is None or gamma_pmax is None:
        return None
    if not (_T_MIN <= t_cell_eff_c <= _T_MAX):
        return None
    if not (_GAMMA_MIN <= gamma_pmax < _GAMMA_MAX):
        return None
    denom = 1.0 + gamma_pmax * (t_cell_eff_c - STC_TEMP_C)
    if denom <= 0.5:            # >50% derating implies broken inputs
        return None
    return round(pr / denom, 4)
