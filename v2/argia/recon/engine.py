"""Reconciliation engine — PURE functions, no I/O.

Four checks at month close (external reconciliation advice, 2026-08):
    CHECK 1  Σ interval (our 5-min telemetry)  vs  Σ vendor daily counters
    CHECK 2  Σ vendor daily counters           vs  vendor monthly counter
    CHECK 3  lifetime counter delta (our own nightly snapshots)
    CHECK 4  lifetime delta                    vs  vendor monthly counter

Billing control priority (most tamper-proof first): lifetime delta >
vendor monthly > Σ vendor daily > Σ interval. Interval data is analytics
and verification — a comms gap between 13:00 and 17:00 must never shrink
an invoice, because the cumulative counter still captured it.

Statuses follow the AGS PASS / REVIEW / FAIL convention (AGS-701 §7);
missing data is flagged, never silently used (AGS-901 R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

# Thresholds, percent. Daily counters are coarse (Growatt reports 0.1 kWh
# steps), so daily tolerance is looser than the monthly close.
DAILY_PASS_PCT = 1.0
DAILY_REVIEW_PCT = 3.0
MONTHLY_PASS_PCT = 0.5
MONTHLY_REVIEW_PCT = 1.5
# Below this interval completeness the interval sum is expected to
# undercount — CHECK 1 then informs, it does not fail the close.
COMPLETENESS_MIN_PCT = 95.0

STATUS_PASS = "PASS"
STATUS_REVIEW = "REVIEW"
STATUS_FAIL = "FAIL"
STATUS_NO_DATA = "NO_DATA"

BASIS_DAILY_REF = "inverter_counter_daily_sum"
"""v206: Σ over the month of the daily reference — the inverters' own
eToday counters (the vendor plant-daily only where it is higher)."""
BASIS_LIFETIME = "lifetime_delta"
BASIS_MONTHLY = "vendor_monthly"
BASIS_DAILY_SUM = "vendor_daily_sum"
BASIS_INTERVAL = "interval_sum"
BASIS_NONE = "none"


def variance_pct(measured: Optional[float],
                 reference: Optional[float]) -> Optional[float]:
    """(measured - reference) / reference * 100.

    Both zero -> 0.0 (a no-production day matching is a match). Reference
    zero but measured non-zero, or either side missing -> None (undefined
    — the caller flags it rather than dividing by zero).
    """
    if measured is None or reference is None:
        return None
    if reference == 0:
        return 0.0 if measured == 0 else None
    return (measured - reference) / reference * 100.0


# ---------------------------------------------------------------------------
# Daily reconciliation.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DailyRecon:
    interval_kwh: Optional[float]      # Σ inverter eToday counters (our samples)
    vendor_daily_kwh: Optional[float]  # the vendor's PLANT daily figure
    kpi_kwh: Optional[float]
    completeness_pct: Optional[float]
    variance_pct: Optional[float]      # inverter counters vs vendor plant daily
    status: str
    note: str
    reference_kwh: Optional[float] = None   # v206: the day's billing reference
    reference_basis: str = BASIS_NONE


BASIS_INV = "inverter_counters"
BASIS_VENDOR_DAILY = "vendor_plant_daily"


def reference_kwh(inverter_counter_kwh: Optional[float],
                  vendor_daily_kwh: Optional[float]
                  ) -> tuple[Optional[float], str]:
    """The day's energy reference — v206, Tomasz 2026-09-05: "always
    prefer the inverter counter, it is the only way we can prove it to
    the customer". Σ of the inverters' own eToday registers wins; the
    vendor's plant-daily figure (server-side, built from uploaded 5-min
    data — SLP2 2026-09-04 lost the 145 kWh of a link gap that way) is
    used only when it is HIGHER by more than DAILY_PASS_PCT, i.e. when
    our own sampling missed part of the day. Never lowers. Pure."""
    if inverter_counter_kwh is None and vendor_daily_kwh is None:
        return None, BASIS_NONE
    if inverter_counter_kwh is None:
        return vendor_daily_kwh, BASIS_VENDOR_DAILY
    if vendor_daily_kwh is None:
        return inverter_counter_kwh, BASIS_INV
    if vendor_daily_kwh > inverter_counter_kwh * (1 + DAILY_PASS_PCT / 100.0):
        return vendor_daily_kwh, BASIS_VENDOR_DAILY
    return inverter_counter_kwh, BASIS_INV


def daily_recon(interval_kwh: Optional[float],
                vendor_daily_kwh: Optional[float],
                kpi_kwh: Optional[float],
                completeness_pct: Optional[float]) -> DailyRecon:
    """Judge one plant-day. ``interval_kwh`` is Σ of the inverters' own
    eToday counters as we sampled them; the vendor plant-daily figure is
    the cross-check. v206: the inverter counters are the reference — a
    vendor figure BELOW them is the vendor's upload gap (REVIEW, never
    FAIL); a vendor figure ABOVE them is our collection gap (the
    reference rises to it, never lowers)."""
    notes: List[str] = []
    var = variance_pct(interval_kwh, vendor_daily_kwh)
    ref, basis = reference_kwh(interval_kwh, vendor_daily_kwh)

    if interval_kwh is None and vendor_daily_kwh is None:
        status = STATUS_NO_DATA
        notes.append("no inverter counters and no vendor counter")
    elif vendor_daily_kwh is None:
        status = STATUS_REVIEW
        notes.append("no vendor daily counter — inverter counters only")
    elif interval_kwh is None:
        status = STATUS_REVIEW
        notes.append("no inverter counters — collection gap, vendor plant daily only")
    elif var is None:
        status = STATUS_REVIEW
        notes.append("vendor counter 0 but inverter counters non-zero — counter anomaly")
    elif (completeness_pct is not None
          and completeness_pct < COMPLETENESS_MIN_PCT):
        # Our undercount is EXPECTED here; the vendor counter still
        # captured the day. Flag, never FAIL on our own gap (AGS-901 R6).
        status = STATUS_REVIEW
        notes.append(f"completeness {completeness_pct:.1f}% < "
                     f"{COMPLETENESS_MIN_PCT:g}% — undercount expected "
                     f"({var:+.2f}%)")
    elif abs(var) <= DAILY_PASS_PCT:
        status = STATUS_PASS
        notes.append(f"inverter counters vs vendor plant daily {var:+.2f}%")
    elif var > 0:
        # the inverters' own registers hold MORE than the vendor's plant
        # figure: the vendor lost uploads (SLP2 2026-09-04 +10.6%); the
        # counters are the reference and the proof
        status = STATUS_REVIEW
        notes.append(f"inverter counters {var:+.2f}% above the vendor plant "
                     "daily — vendor upload gap; inverter counters kept")
    elif abs(var) <= DAILY_REVIEW_PCT:
        status = STATUS_REVIEW
        notes.append(f"inverter counters vs vendor {var:+.2f}% (> {DAILY_PASS_PCT:g}%)")
    else:
        status = STATUS_FAIL
        notes.append(f"inverter counters vs vendor {var:+.2f}% (> {DAILY_REVIEW_PCT:g}%)")

    kvar = variance_pct(kpi_kwh, ref)
    if kvar is not None and abs(kvar) > DAILY_PASS_PCT:
        notes.append(f"KPI row vs reference {kvar:+.2f}%")

    note = "; ".join(notes)
    return DailyRecon(interval_kwh, vendor_daily_kwh, kpi_kwh,
                      completeness_pct, var, status, note, ref, basis)


# ---------------------------------------------------------------------------
# Monthly close — the four checks + billing-basis selection.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MonthlyClose:
    interval_sum_kwh: Optional[float]
    vendor_daily_sum_kwh: Optional[float]
    vendor_monthly_kwh: Optional[float]
    lifetime_delta_kwh: Optional[float]
    completeness_pct: Optional[float]
    check1_pct: Optional[float]   # interval sum   vs Σ vendor daily
    check2_pct: Optional[float]   # Σ vendor daily vs vendor monthly
    check4_pct: Optional[float]   # vendor monthly vs lifetime delta
    billing_kwh: Optional[float]
    billing_basis: str
    status: str
    note: str


def lifetime_delta(lifetime_start_kwh: Optional[float],
                   lifetime_end_kwh: Optional[float]) -> Optional[float]:
    """Month energy from two lifetime-counter snapshots (end of previous
    month, end of this month). A negative delta means a counter reset or
    inverter swap — returned as None so it can never become an invoice."""
    if lifetime_start_kwh is None or lifetime_end_kwh is None:
        return None
    d = round(lifetime_end_kwh - lifetime_start_kwh, 3)
    return d if d >= 0 else None


def select_billing(lifetime_delta_kwh: Optional[float],
                   vendor_monthly_kwh: Optional[float],
                   vendor_daily_sum_kwh: Optional[float],
                   interval_sum_kwh: Optional[float],
                   daily_days: int, days_in_month: int,
                   daily_ref_sum_kwh: Optional[float] = None,
                   daily_ref_days: int = 0,
                   ) -> tuple[Optional[float], str]:
    """Billing control value + basis.

    v206: the Σ of the daily references (inverter counters, vendor plant
    daily only where higher) is FIRST when every day of the month has
    one — the customer can verify it register by register. The lifetime
    delta and the vendor monthly counter follow (plant-level vendor
    figures), then Σ vendor daily, then the bare interval sum. Any Σ
    only qualifies with EVERY day covered; a partial sum silently
    under-bills. The caller downgrades the status on the last resort.
    """
    if daily_ref_sum_kwh is not None and daily_ref_days >= days_in_month:
        return daily_ref_sum_kwh, BASIS_DAILY_REF
    if lifetime_delta_kwh is not None:
        return lifetime_delta_kwh, BASIS_LIFETIME
    if vendor_monthly_kwh is not None:
        return vendor_monthly_kwh, BASIS_MONTHLY
    if vendor_daily_sum_kwh is not None and daily_days >= days_in_month:
        return vendor_daily_sum_kwh, BASIS_DAILY_SUM
    if interval_sum_kwh is not None:
        return interval_sum_kwh, BASIS_INTERVAL
    return None, BASIS_NONE


def monthly_close(interval_sum_kwh: Optional[float],
                  vendor_daily_sum_kwh: Optional[float],
                  vendor_monthly_kwh: Optional[float],
                  lifetime_start_kwh: Optional[float],
                  lifetime_end_kwh: Optional[float],
                  completeness_pct: Optional[float],
                  daily_days: int,
                  days_in_month: int,
                  daily_ref_sum_kwh: Optional[float] = None,
                  daily_ref_days: int = 0) -> MonthlyClose:
    """Run the four checks for one plant-month and pick the billing value.

    Status: PASS when every computable counter-vs-counter check is inside
    MONTHLY_PASS_PCT and a counter-based billing value exists; FAIL when a
    check exceeds MONTHLY_REVIEW_PCT; REVIEW in between, for coverage
    gaps, or when billing had to fall back below the counter sources.
    CHECK 1 (interval vs counters) informs — with low completeness an
    interval undercount is expected and must not fail the close.
    """
    notes: List[str] = []
    ld = lifetime_delta(lifetime_start_kwh, lifetime_end_kwh)
    if (lifetime_start_kwh is not None and lifetime_end_kwh is not None
            and ld is None):
        notes.append("lifetime counter went BACKWARDS — reset/swap? "
                     "excluded from billing")

    c1 = variance_pct(interval_sum_kwh, vendor_daily_sum_kwh)
    c2 = variance_pct(vendor_daily_sum_kwh, vendor_monthly_kwh)
    c4 = variance_pct(vendor_monthly_kwh, ld)

    billing, basis = select_billing(ld, vendor_monthly_kwh,
                                    vendor_daily_sum_kwh, interval_sum_kwh,
                                    daily_days, days_in_month,
                                    daily_ref_sum_kwh, daily_ref_days)
    # v206 cross-check: the plant-level counters against the Σ of daily
    # inverter-counter references. A lifetime delta ABOVE the sum means
    # days where we had no inverter data at all; BELOW means the vendor
    # lost uploads (the SLP2 pattern) — informational either way.
    c3 = variance_pct(daily_ref_sum_kwh, ld) if basis == BASIS_DAILY_REF else None

    hard_checks = [c for c in (c2, c4) if c is not None]
    low_completeness = (completeness_pct is not None
                        and completeness_pct < COMPLETENESS_MIN_PCT)

    if billing is None:
        status = STATUS_NO_DATA
        notes.append("no billing-grade value from any source")
    elif any(abs(c) > MONTHLY_REVIEW_PCT for c in hard_checks):
        status = STATUS_FAIL
        notes.append("counter sources disagree beyond "
                     f"{MONTHLY_REVIEW_PCT:g}% — investigate before billing")
    elif any(abs(c) > MONTHLY_PASS_PCT for c in hard_checks):
        status = STATUS_REVIEW
        notes.append("counter sources agree only within "
                     f"{MONTHLY_REVIEW_PCT:g}%")
    elif basis == BASIS_INTERVAL:
        status = STATUS_REVIEW
        notes.append("billing fell back to the INTERVAL sum — no vendor "
                     "counter available; verify manually")
    elif not hard_checks and basis == BASIS_DAILY_SUM:
        status = STATUS_REVIEW
        notes.append("single counter source (Σ daily) — no independent "
                     "cross-check possible")
    else:
        status = STATUS_PASS

    if daily_days < days_in_month:
        notes.append(f"vendor daily snapshots cover {daily_days}/"
                     f"{days_in_month} days")
        if status == STATUS_PASS and basis == BASIS_DAILY_SUM:
            status = STATUS_REVIEW
    if c3 is not None:
        if abs(c3) > MONTHLY_REVIEW_PCT:
            notes.append(f"CHECK3 Σ inverter-counter references vs lifetime "
                         f"delta {c3:+.2f}% — "
                         + ("days without inverter data? verify before billing"
                            if c3 < 0 else "vendor upload gaps; counters kept"))
            if status == STATUS_PASS and c3 < 0:
                status = STATUS_REVIEW
        else:
            notes.append(f"CHECK3 Σ inverter-counter references vs lifetime "
                         f"delta {c3:+.2f}%")
    if c1 is not None:
        if low_completeness:
            notes.append(f"CHECK1 interval vs Σdaily {c1:+.2f}% "
                         f"(completeness {completeness_pct:.1f}% — "
                         "undercount expected)")
        elif abs(c1) > MONTHLY_REVIEW_PCT:
            notes.append(f"CHECK1 interval vs Σdaily {c1:+.2f}% — telemetry "
                         "pipeline losing data despite good completeness")
            if status == STATUS_PASS:
                status = STATUS_REVIEW
        else:
            notes.append(f"CHECK1 interval vs Σdaily {c1:+.2f}%")

    return MonthlyClose(interval_sum_kwh, vendor_daily_sum_kwh,
                        vendor_monthly_kwh, ld, completeness_pct,
                        c1, c2, c4, billing, basis, status,
                        "; ".join(notes))


def effective_completeness(tick_pct: Optional[float],
                           reporting: Optional[int],
                           configured: Optional[int]) -> Optional[float]:
    """Tick completeness scaled by the share of CONFIGURED inverters
    that actually reported that day.

    The GTO2 lesson, round two (2026-08-27): with Inverter 2's
    monitoring comms dead, tick completeness read 100% while the
    interval sum was guaranteed ~25% under the vendor counter — recon
    FAILed every single day for a cause that was already known, alerted
    and tracked. Scaling by reporting/configured turns that into an
    honest sub-95% REVIEW ("undercount expected") without hiding
    anything: the silent inverter still has its own alert.
    Pure; None in -> None out; factor clamped to [0, 1].
    """
    if tick_pct is None:
        return None
    if not configured or configured <= 0 or reporting is None:
        return round(tick_pct, 2)
    factor = max(0.0, min(1.0, float(reporting) / float(configured)))
    return round(tick_pct * factor, 2)
