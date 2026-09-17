"""v257 — what an outage COSTS, in pesos.

Tomasz, 2026-09-16, watching SAG sit at 0 W all afternoon: "show the
bleeding in MXN, so we can see how much money we are losing".

Two questions, two honest methods:

* **During** the outage (the acute alert): we cannot know what the plant
  *would* have made, so we model it — nameplate x measured irradiance x
  the plant's baseline performance ratio — and say plainly that it is an
  estimate. Irradiance is on every telemetry row (verified: 612/612
  samples carried it on 2026-09-16), so this works for every plant, with
  no dependence on a healthy twin.
* **After** the day closes (the daily report): no model is needed.
  ``daily_production.expected_kwh`` is already the irradiance-based
  expectation, stamped nightly for all ten plants. Lost = expected
  - actual, and that is the number the report should price.

The tariff is the PPA price for the month in force
(``contract_monthly.tariff_mxn``), falling back to the plant's standing
``tariff_mxn_per_kwh``. The five CAPEX plants have no per-kWh tariff at
all — they are net-metering sites ARGIA does not bill per kWh — so for
them the answer is kWh and an explicit "no tariff", never a fabricated
peso figure.

Pure: no I/O, no database, no clock.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

# A sample below this irradiance earns nothing anyway: counting it as
# "lost" would inflate every dawn and dusk outage.
MIN_IRRADIANCE_WM2 = 20.0
STC_WM2 = 1000.0
DEFAULT_PR = 0.80


def expected_kw(kwp_dc: Optional[float], irradiance_wm2: Optional[float],
                pr: Optional[float] = None) -> Optional[float]:
    """What this plant should be making right now, in kW.

    nameplate x (irradiance / 1000) x performance ratio — the standard
    first-order expectation, and the same shape the nightly
    ``expected_kwh`` uses. Returns None when an input is missing, so a
    gap never silently becomes a zero (which would read as "lost")."""
    if not kwp_dc or irradiance_wm2 is None:
        return None
    if irradiance_wm2 < MIN_IRRADIANCE_WM2:
        return 0.0
    ratio = pr if pr and pr > 0 else DEFAULT_PR
    return max(0.0, float(kwp_dc) * (float(irradiance_wm2) / STC_WM2) * float(ratio))


def lost_kwh_intraday(samples: Sequence[Tuple[Optional[float], Optional[float]]],
                      kwp_dc: Optional[float], pr: Optional[float] = None,
                      interval_min: float = 5.0) -> float:
    """Energy missed so far, from ``(irradiance_wm2, actual_kw)`` samples.

    ``actual_kw`` None means the vendor sent no reading — during an
    outage that is exactly the case that matters (SAG's inverters went
    from 0 W to no value at all once the datalogger dropped), so a
    missing reading counts as zero production, not as a missing sample.
    Only the shortfall counts; a plant beating the model never earns a
    negative loss."""
    if not samples or not kwp_dc:
        return 0.0
    hours = max(0.0, float(interval_min)) / 60.0
    total = 0.0
    for irr, actual in samples:
        exp = expected_kw(kwp_dc, irr, pr)
        if exp is None or exp <= 0:
            continue
        total += max(0.0, exp - (actual or 0.0)) * hours
    return round(total, 1)


def lost_kwh_day(expected_kwh: Optional[float], actual_kwh: Optional[float]) -> Optional[float]:
    """The closed day's shortfall. None when there is no expectation to
    compare against — an unknown is not a zero."""
    if expected_kwh is None:
        return None
    return round(max(0.0, float(expected_kwh) - float(actual_kwh or 0.0)), 1)


def tariff_mxn(contract_tariff: Optional[float] = None,
               plant_tariff: Optional[float] = None) -> Optional[float]:
    """The PPA price per kWh: the month's contract tariff wins, the
    plant's standing tariff is the fallback, and None means this site is
    not billed per kWh (every CAPEX plant)."""
    for t in (contract_tariff, plant_tariff):
        if t is not None:
            try:
                v = float(t)
            except (TypeError, ValueError):
                continue
            if v > 0:
                return v
    return None


def cost_mxn(lost_kwh: Optional[float], tariff: Optional[float]) -> Optional[float]:
    """Pesos not earned. None when either half is unknown — the caller
    then shows kWh and says why there is no peso figure."""
    if lost_kwh is None or tariff is None:
        return None
    return round(max(0.0, float(lost_kwh)) * float(tariff), 2)


def fmt_mxn(v: Optional[float]) -> str:
    """'$12,345 MXN' — whole pesos; centavos are noise on a loss estimate."""
    if v is None:
        return "—"
    return f"${v:,.0f} MXN"


def fmt_kwh(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:,.0f} kWh"


def loss_phrase(lost_kwh: Optional[float], tariff: Optional[float],
                estimated: bool = True) -> str:
    """The clause that goes on the end of an alert or a report line.

    Never claims a peso figure it cannot support: no tariff, no pesos.
    """
    if lost_kwh is None:
        return ""
    money = cost_mxn(lost_kwh, tariff)
    approx = "≈ " if estimated else ""
    if money is None:
        return f"{approx}{fmt_kwh(lost_kwh)} lost (no per-kWh tariff for this site)"
    return f"{approx}{fmt_kwh(lost_kwh)} lost — {fmt_mxn(money)}"


def total_cost(rows: Iterable[Tuple[Optional[float], Optional[float]]]) -> Tuple[float, Optional[float]]:
    """Roll (lost_kwh, tariff) pairs into (total kWh, total MXN).

    The peso total covers only the rows that HAVE a tariff, and is None
    when none of them do — so a fleet total never quietly prices the
    CAPEX plants at zero and calls it complete."""
    kwh = 0.0
    money: Optional[float] = None
    for lost, tar in rows:
        if lost is None:
            continue
        kwh += float(lost)
        c = cost_mxn(lost, tar)
        if c is not None:
            money = (money or 0.0) + c
    return round(kwh, 1), (round(money, 2) if money is not None else None)
