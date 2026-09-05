"""Vendor fault/state code catalog — human explanations (v3 item 15).

Honest coverage policy (AGS-901: never silently invent): only mappings
backed by vendor documentation are included. SolarEdge inverter modes
and Huawei FusionSolar inverter_state/run_state values are documented;
Growatt numeric fault codes vary by model family, so unknown codes get
an explicit "not in catalog" answer instead of a guess. PURE.
"""

from __future__ import annotations

import re
from typing import Optional

# Huawei FusionSolar getDevRealKpi inverter_state (documented values)
HUAWEI_INVERTER_STATE = {
    0: "standby: initializing",
    1: "standby: insulation-resistance check",
    2: "standby: irradiance too low",
    3: "standby: grid voltage/frequency check",
    256: "starting",
    512: "on-grid (normal operation)",
    513: "on-grid: power limited by grid/dispatch",
    514: "on-grid: self-derating (temperature/derating)",
    768: "SHUTDOWN: fault",
    769: "SHUTDOWN: by command",
    770: "SHUTDOWN: OVGR (grid protection)",
    771: "SHUTDOWN: communication interrupted",
    772: "SHUTDOWN: power limited to zero",
    773: "SHUTDOWN: manual startup required",
    774: "SHUTDOWN: DC switch off",
    1025: "grid dispatch: cos(phi)-P curve",
    1026: "grid dispatch: Q-U curve",
    1536: "spot check / inspection",
    1792: "AFCI self-check",
    2048: "I-V curve scanning",
    2304: "DC input detection",
}
HUAWEI_RUN_STATE = {0: "disconnected", 1: "grid-connected"}

# SolarEdge inverterMode (monitoring API, documented)
SOLAREDGE_MODE = {
    "OFF": "off",
    "SLEEPING": "night sleep (normal after dusk)",
    "STARTING": "waking up / starting",
    "MPPT": "producing (normal operation)",
    "THROTTLED": "producing but throttled (power limit)",
    "SHUTTING_DOWN": "shutting down (normal at dusk; investigate midday)",
    "FAULT": "FAULT — inverter reports an error",
    "STANDBY": "standby (commanded)",
    "LOCKED_STDBY": "locked standby",
    "PENDING": "pending activation",
    "IDLE": "idle",
}

# Growatt three-phase (MAX / MID / MOD series) error codes — the
# grid-side and thermal ones we have actually seen in the fleet, from the
# published Growatt error table (deenergy.com.au/growatt-fault-code,
# mirrors the MAX manual). Stored as "FT=<code>" (faultType) in our
# fault_code summary; FC1=/FC2= are the bit-coded faultCode1/2 and stay
# uncatalogued. SLP1 2026-08-02..04 (300, both units, grid outage) and
# SLP2 2026-09-03 (302, 50 min) are the real cases behind this list.
GROWATT_FAULT_TYPE = {
    300: "grid voltage out of range (AC V outrange) — utility side, not the PV array",
    301: "grid frequency out of range (AC F outrange) — utility side",
    302: "no AC connection — the inverter lost the grid (breaker open, "
         "utility outage, or the AC cable); it stops until the grid is back",
    303: "neutral-to-PE voltage above 30 V — AC wiring / grounding issue",
    304: "grid frequency out of permissible range — utility side",
    402: "output DC injection too high (High DCI) — inverter side, restart; repeat = service",
    404: "bus sample fault — inverter internal, service if it repeats",
    405: "relay fault — inverter internal, service",
    407: "auto-test failed — commissioning / grid-code setting",
    408: "over temperature — the inverter shut down on heat: check cooling, fans, heatsink, shade",
    409: "bus over-voltage — DC side / inverter internal, service if it repeats",
    420: "GFCI / residual current fault — insulation or leakage on the DC side, site check",
}

_GW_FT_RE = re.compile(r"FT=(\d+)")

_HU_RE = re.compile(r"^IS=(\d+),RS=(\d+)$")
_SE_RE = re.compile(r"^MODE=([A-Z_]+)$")


def explain_fault(vendor: str, raw: str) -> Optional[str]:
    """Human explanation for a stored fault/state string.

    Returns None for empty/'0' (nothing to explain), a documented
    explanation when the catalog knows the code, and an explicit
    "not in catalog" pointer otherwise — never a guess.
    """
    s = (raw or "").strip()
    if s in ("", "0"):
        return None
    v = (vendor or "").upper()

    m = _HU_RE.match(s)
    if m and v in ("", "HUAWEI"):
        state = HUAWEI_INVERTER_STATE.get(int(m.group(1)))
        run = HUAWEI_RUN_STATE.get(int(m.group(2)))
        if state:
            return state + (f", {run}" if run else "")
        return (f"Huawei state {m.group(1)} — not in catalog, "
                "check FusionSolar")

    m = _SE_RE.match(s)
    if m and v in ("", "SOLAREDGE"):
        mode = SOLAREDGE_MODE.get(m.group(1))
        return mode or (f"SolarEdge mode {m.group(1)} — not in catalog, "
                        "check the SolarEdge portal")

    if v == "GROWATT":
        m = _GW_FT_RE.search(s)
        if m and int(m.group(1)) in GROWATT_FAULT_TYPE:
            return f"Growatt error {m.group(1)}: {GROWATT_FAULT_TYPE[int(m.group(1))]}"
        return (f"Growatt code {s} — model-specific, check the Growatt "
                "OSS portal (codes vary by series)")
    return f"vendor code {s} — not in catalog, check the vendor portal"


def is_normal_state(vendor: str, raw: str) -> bool:
    """True when the raw state string describes NORMAL operation (used
    to keep informational states out of warning displays)."""
    s = (raw or "").strip()
    if s in ("", "0"):
        return True
    m = _HU_RE.match(s)
    if m:
        return int(m.group(1)) in (512, 1025, 1026)
    m = _SE_RE.match(s)
    if m:
        return m.group(1) in ("MPPT", "SLEEPING", "STARTING")
    return False
