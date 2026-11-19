"""EPI-600 model: spec v0.7 steps 2-5, with the audit's changes.

Audit changes (EPI600_PARAMETER_AUDIT_2026-11-19.md, Tomasz 19 Nov 2026):
- inverter limit: Cap_DC = AC limit / 0.985, the AC limit is a measured or
  datasheet value per inverter (never 1.1 x nominal applied twice);
- monthly bias B_m is computed here from the design year
  (``bias_from_design``), so every plant scores 100 % on its own design
  weather; Annex B's typed values are only compared, never used.

Every function is pure: plain floats and dates in, floats out.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

INVERTER_EFFICIENCY = 0.985      # datasheet European efficiency 98.4-98.5 %
T_CELL_OFFSET_K = 3.0            # spec step 2 (back sensor -> cell at 1000 W/m2)
EPI_600 = 600.0
EPI_150 = 150.0
PRODUCING_SHARE = 0.05           # spec 3.2: inverter DC >= 5 % of its expected
MIN_VALID_SAMPLES = 200          # spec: fewer -> "insufficient data"
GREEN, AMBER, SUSPECT = 0.97, 0.93, 1.03

# Module degradation, % (year-1 loss, each later year). Spec v0.7 table, plus
# RS5J-655NBG-E1 from the ReneSola datasheet RS5J-001-B-EN (99 % at year 1,
# 0.40 %/yr, 87.4 % at year 30: 99 - 0.4 x 29 = 87.4).
DEGRADATION: Dict[str, Tuple[float, float]] = {
    "CS7N-660MS": (2.0, 0.55),
    "TWMNF-66HS715": (1.0, 0.40),
    "CS6W-555MS": (2.0, 0.55),
    "LR5-72HTH-590M": (1.5, 0.40),
    "LR7-72HVH-650M": (1.0, 0.35),
    "CS7N-700TB-AG": (2.0, 0.55),
    "LR7-72HTH-615M": (1.0, 0.40),
    "JAM72S40605": (1.0, 0.40),
    "RS5J-655NBG-E1": (1.0, 0.40),
}


@dataclass(frozen=True)
class StringDef:
    uid: str                 # "INV1:PV3"
    inverter: str            # "INV1"
    kwp: float
    tilt: float
    azimuth: float           # 180 = south
    module: str
    cod: dt.date             # energization of this string


@dataclass(frozen=True)
class PlantModel:
    plant: str
    lat: float
    lon: float
    b: Tuple[float, float, float, float]          # PR0 coefficients
    soiling_pct: Tuple[float, ...]                # 12 values, Jan..Dec
    bias: Tuple[float, ...] = (1.0,) * 12          # 12 values, Jan..Dec
    ac_limit_kw: Mapping[str, float] = field(default_factory=dict)   # inverter -> kW AC


# --- step 2 -----------------------------------------------------------------
def t_cell(t_panel: float, g_poa: float) -> float:
    return t_panel + T_CELL_OFFSET_K * min(g_poa / 1000.0, 1.2)


# --- step 3 -----------------------------------------------------------------
def pr0(b: Sequence[float], tc: float, g_poa: float) -> float:
    g = g_poa / 1000.0
    return max(0.0, b[0] + b[1] * tc + b[2] * g + b[3] * g * g)


def degradation_factor(module: str, cod: dt.date, day: dt.date) -> Optional[float]:
    """D_s. None before the COD (the string does not exist yet: excluded)."""
    if day < cod:
        return None
    y1, y2 = DEGRADATION[module]
    years = (day - cod).days / 365.25
    loss = y1 * years if years <= 1 else y1 + y2 * (years - 1)
    return 1.0 - loss / 100.0


def expected_string_w(kwp: float, d: float, g_poa: float, pr: float,
                      soiling_pct: float, bias: float) -> float:
    """E_s = kWp x 1000 x D x G[kW/m2] x PR0 x (1 - S) x B  [W]."""
    return kwp * 1000.0 * d * (g_poa / 1000.0) * pr * (1 - soiling_pct / 100.0) * bias


# --- step 4 -----------------------------------------------------------------
def cap_dc_w(ac_limit_kw: float) -> float:
    return ac_limit_kw * 1000.0 / INVERTER_EFFICIENCY


def cap_scale(expected_w: Iterable[float], ac_limit_kw: float) -> float:
    tot = sum(expected_w)
    return 1.0 if tot <= 0 else min(1.0, cap_dc_w(ac_limit_kw) / tot)


# --- step 5 -----------------------------------------------------------------
@dataclass
class Tally:
    """Sum of real and expected over the same valid samples (never a mean of ratios)."""
    real_wh: float = 0.0
    expected_wh: float = 0.0
    n: int = 0

    def add(self, real_w: float, expected_w: float, hours: float) -> None:
        self.real_wh += real_w * hours
        self.expected_wh += expected_w * hours
        self.n += 1

    def merge(self, other: "Tally") -> None:
        self.real_wh += other.real_wh
        self.expected_wh += other.expected_wh
        self.n += other.n

    @property
    def epi(self) -> Optional[float]:
        return None if self.expected_wh <= 0 else self.real_wh / self.expected_wh


def band(epi: Optional[float], n: int) -> str:
    if epi is None or n < MIN_VALID_SAMPLES:
        return "insufficient"
    if epi > SUSPECT:
        return "suspect"
    if epi >= GREEN:
        return "green"
    return "amber" if epi >= AMBER else "red"


# --- monthly bias from the design year (audit section 2b) --------------------
def unbiased_plant_expected_w(pm: PlantModel, strings: Sequence[StringDef], g_poa_by_uid: Mapping[str, float],
                              tc: float, month: int) -> float:
    """Design-year model output for one hour WITHOUT B_m and WITHOUT degradation
    or inverter limit: what B_m must be calibrated against (the design's DC
    before clipping, HelioScope ``optimal_dc_power``)."""
    s = pm.soiling_pct[month - 1]
    tot = 0.0
    for st in strings:
        g = g_poa_by_uid[st.uid]
        tot += expected_string_w(st.kwp, 1.0, g, pr0(pm.b, tc, g), s, 1.0)
    return tot


def bias_from_design(model_w_by_month: Mapping[int, float], design_w_by_month: Mapping[int, float]) -> Tuple[float, ...]:
    """B_m = sum(design DC before clipping) / sum(model without bias), per month.
    Reproduces Annex B within 0.0007-0.0027 at 4 plants (audit 2b)."""
    out = []
    for m in range(1, 13):
        mw = model_w_by_month.get(m, 0.0)
        if mw <= 0:
            raise ValueError(f"month {m}: no modelled energy, cannot calibrate")
        out.append(design_w_by_month.get(m, 0.0) / mw)
    return tuple(out)
