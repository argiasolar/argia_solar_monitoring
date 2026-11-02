"""Unit tests - argia.recon.engine (pure reconciliation logic)."""
import pytest

from argia.recon.engine import (
    BASIS_DAILY_SUM,
    BASIS_INTERVAL,
    BASIS_LIFETIME,
    BASIS_MONTHLY,
    BASIS_NONE,
    STATUS_FAIL,
    STATUS_NO_DATA,
    STATUS_PASS,
    STATUS_REVIEW,
    daily_recon,
    lifetime_delta,
    monthly_close,
    select_billing,
    variance_pct,
)


# ---------------------------------------------------------------- variance
def test_variance_basic():
    assert variance_pct(101.0, 100.0) == 1.0
    assert variance_pct(99.0, 100.0) == -1.0


def test_variance_both_zero_is_match():
    assert variance_pct(0.0, 0.0) == 0.0


def test_variance_zero_reference_nonzero_measured_undefined():
    assert variance_pct(5.0, 0.0) is None


def test_variance_missing_sides():
    assert variance_pct(None, 100.0) is None
    assert variance_pct(100.0, None) is None


# ------------------------------------------------------------ daily recon
def test_daily_pass_within_1pct():
    r = daily_recon(1000.0, 1005.0, 1004.0, 100.0)
    assert r.status == STATUS_PASS
    assert abs(r.variance_pct - (-0.4975)) < 0.001


def test_daily_review_between_1_and_3pct():
    r = daily_recon(980.0, 1000.0, None, 100.0)
    assert r.status == STATUS_REVIEW


def test_daily_fail_beyond_3pct():
    r = daily_recon(900.0, 1000.0, None, 100.0)
    assert r.status == STATUS_FAIL


def test_daily_low_completeness_never_fails():
    # 4-hour comms gap: interval undercounts 20% but completeness says why.
    r = daily_recon(800.0, 1000.0, None, 80.0)
    assert r.status == STATUS_REVIEW
    assert "undercount expected" in r.note


def test_daily_no_data():
    r = daily_recon(None, None, None, None)
    assert r.status == STATUS_NO_DATA


def test_daily_missing_vendor_counter_is_review():
    r = daily_recon(500.0, None, 500.0, 100.0)
    assert r.status == STATUS_REVIEW
    assert "no vendor daily counter" in r.note


def test_daily_missing_interval_is_review_not_fail():
    # The invoice must not depend on our collection (advice, core rule).
    r = daily_recon(None, 1000.0, 1000.0, 0.0)
    assert r.status == STATUS_REVIEW
    assert "collection gap" in r.note


def test_daily_kpi_divergence_lands_in_note():
    r = daily_recon(1000.0, 1000.0, 950.0, 100.0)
    assert r.status == STATUS_PASS
    assert "KPI row vs reference" in r.note


def test_daily_zero_zero_day_passes():
    r = daily_recon(0.0, 0.0, 0.0, 100.0)
    assert r.status == STATUS_PASS


# --------------------------------------------------------- lifetime delta
def test_lifetime_delta_normal():
    assert lifetime_delta(4739812.2, 4838354.4) == 98542.2


def test_lifetime_delta_backwards_counter_is_none():
    assert lifetime_delta(5000.0, 4000.0) is None


def test_lifetime_delta_missing_endpoint():
    assert lifetime_delta(None, 100.0) is None
    assert lifetime_delta(100.0, None) is None


# --------------------------------------------------------- billing basis
def test_billing_prefers_the_portal_month_counter():
    # v339: the vendor month counter (what the portal shows) comes before the lifetime delta
    v, basis = select_billing(98542.2, 98540.0, 98500.0, 98000.0, 31, 31)
    assert (v, basis) == (98540.0, BASIS_MONTHLY)
    # without a month counter (Growatt): the full-month sum of the vendor dailies
    assert select_billing(98542.2, None, 98500.0, 98000.0, 31, 31) == (98500.0, BASIS_DAILY_SUM)
    # without any portal figure: the lifetime delta
    assert select_billing(98542.2, None, 98500.0, 98000.0, 30, 31) == (98542.2, BASIS_LIFETIME)


def test_billing_falls_back_to_monthly():
    v, basis = select_billing(None, 98540.0, 98500.0, 98000.0, 31, 31)
    assert (v, basis) == (98540.0, BASIS_MONTHLY)


def test_billing_daily_sum_requires_full_coverage():
    v, basis = select_billing(None, None, 98500.0, 98000.0, 30, 31)
    assert basis == BASIS_INTERVAL  # 30/31 days -> partial sum refused
    v, basis = select_billing(None, None, 98500.0, 98000.0, 31, 31)
    assert (v, basis) == (98500.0, BASIS_DAILY_SUM)


def test_billing_none_when_nothing():
    v, basis = select_billing(None, None, None, None, 0, 31)
    assert (v, basis) == (None, BASIS_NONE)


# ----------------------------------------------------------- monthly close
def test_close_pass_all_counters_agree():
    # The advice's own worked example: MEX1 / SolarEdge / Aug 2026.
    mc = monthly_close(93827.5, 93833.8, 93834.1,
                       1000000.0, 1093834.0, 99.96, 31, 31)
    assert mc.status == STATUS_PASS
    assert mc.billing_basis == BASIS_MONTHLY                 # v339: the portal month
    assert abs(mc.billing_kwh - 93834.1) < 0.01
    assert abs(mc.check1_pct) < 0.05


def test_close_fail_when_counters_disagree():
    # Vendor monthly and lifetime delta 5% apart: something is wrong.
    mc = monthly_close(95000.0, 95000.0, 100000.0,
                       0.0, 95000.0, 100.0, 31, 31)
    assert mc.status == STATUS_FAIL


def test_close_interval_gap_does_not_fail_close():
    # 20% interval undercount at 80% completeness: counters rule, PASS.
    mc = monthly_close(80000.0, 100000.0, 100010.0,
                       0.0, 100005.0, 80.0, 31, 31)
    assert mc.status == STATUS_PASS
    assert "undercount expected" in mc.note


def test_close_interval_loss_with_good_completeness_is_flagged():
    # Interval lost 5% while completeness says 100% -> pipeline bug.
    # v341: flagged in the note; it holds the month only when no lifetime
    # register confirms the billed month (our telemetry is not billing).
    mc = monthly_close(95000.0, 100000.0, 100010.0,
                       0.0, 100005.0, 100.0, 31, 31)
    assert mc.status == STATUS_PASS
    assert "losing data" in mc.note
    mc = monthly_close(95000.0, 100000.0, 100010.0,
                       None, None, 100.0, 31, 31)
    assert mc.status == STATUS_REVIEW
    assert "losing data" in mc.note


def test_close_backwards_lifetime_never_bills():
    mc = monthly_close(95000.0, 95001.0, None, 500000.0, 400000.0,
                       100.0, 31, 31)
    assert mc.billing_basis != BASIS_LIFETIME
    assert "BACKWARDS" in mc.note


def test_close_interval_fallback_is_review():
    mc = monthly_close(95000.0, None, None, None, None, 100.0, 0, 31)
    assert mc.status == STATUS_REVIEW
    assert mc.billing_basis == BASIS_INTERVAL


def test_close_no_data():
    mc = monthly_close(None, None, None, None, None, None, 0, 31)
    assert mc.status == STATUS_NO_DATA
    assert mc.billing_kwh is None



# ----------------------------------------------------------- v206: inverter counters first
from argia.recon.engine import (BASIS_DAILY_REF, BASIS_INV, BASIS_VENDOR_DAILY,
                                BASIS_NONE, monthly_close, reference_kwh)


def test_reference_prefers_the_inverter_counters():
    # SLP2 2026-09-04: inverters 1513.4, vendor plant daily 1368.6 (upload gap)
    assert reference_kwh(1513.4, 1368.6) == (1513.4, BASIS_INV)
    # v339: a vendor plant daily HIGHER by any amount wins (the inverters' 0.1 kWh steps)
    assert reference_kwh(1000.0, 1005.0) == (1005.0, BASIS_VENDOR_DAILY)
    assert reference_kwh(3667.0, 3667.2) == (3667.2, BASIS_VENDOR_DAILY)   # Taigene 2026-10-01
    assert reference_kwh(1000.0, 1000.0) == (1000.0, BASIS_INV)
    # our own sampling missed part of the day: the vendor saw more -> never lower
    assert reference_kwh(900.0, 1000.0) == (1000.0, BASIS_VENDOR_DAILY)
    assert reference_kwh(None, 1000.0) == (1000.0, BASIS_VENDOR_DAILY)
    assert reference_kwh(1000.0, None) == (1000.0, BASIS_INV)
    assert reference_kwh(None, None) == (None, BASIS_NONE)


def test_daily_vendor_below_inverters_is_review_not_fail():
    r = daily_recon(1513.4, 1368.6, 1368.6, 100.0)
    assert r.status == STATUS_REVIEW and r.reference_kwh == 1513.4 and r.reference_basis == BASIS_INV
    assert "vendor upload gap; inverter counters kept" in r.note
    assert "KPI row vs reference -9.57%" in r.note          # the stored row is short - the fix raises it
    # the other direction (we lost data) still fails beyond 3 %
    r = daily_recon(900.0, 1000.0, None, 100.0)
    assert r.status == STATUS_FAIL and r.reference_kwh == 1000.0 and r.reference_basis == BASIS_VENDOR_DAILY


def test_billing_takes_the_portal_month_unless_the_counters_prove_more():
    # portal month within 0.5 % of the inverter-counter sum: the portal month (v339)
    v, basis = select_billing(98542.2, 98540.0, 98500.0, 98000.0, 31, 31,
                              daily_ref_sum_kwh=98600.0, daily_ref_days=31)
    assert (v, basis) == (98540.0, BASIS_MONTHLY)
    # portal month more than 0.5 % below the counters (late uploads): the counters - never shrink an invoice
    v, basis = select_billing(98542.2, 97000.0, 98500.0, 98000.0, 31, 31,
                              daily_ref_sum_kwh=98600.0, daily_ref_days=31)
    assert (v, basis) == (98600.0, BASIS_DAILY_REF)
    # no portal figure at all: the counters' sum
    assert select_billing(98542.2, None, None, None, 0, 31, daily_ref_sum_kwh=98600.0, daily_ref_days=31) == (98600.0, BASIS_DAILY_REF)
    # a partial month of references cannot overrule the portal month
    v, basis = select_billing(98542.2, 98540.0, 98500.0, 98000.0, 31, 31,
                              daily_ref_sum_kwh=99900.0, daily_ref_days=28)
    assert (v, basis) == (98540.0, BASIS_MONTHLY)
    # legacy call shape unchanged
    assert select_billing(98542.2, None, None, None, 0, 31) == (98542.2, BASIS_LIFETIME)


def test_close_check3_flags_a_lifetime_delta_above_the_references():
    # no portal figure: the references are billed and CHECK3 still compares them with the lifetime delta
    mc = monthly_close(98000.0, None, None, 100000.0, 198542.2, 99.0, 0, 31,
                       daily_ref_sum_kwh=96000.0, daily_ref_days=31)
    assert mc.billing_basis == BASIS_DAILY_REF and mc.billing_kwh == 96000.0
    assert mc.status == STATUS_REVIEW and "CHECK3" in mc.note and "days without inverter data" in mc.note
    # v341: references ABOVE the lifetime delta are no longer waved through -
    # a vendor day glitch raises a reference; the month waits for a person
    mc = monthly_close(98000.0, None, None, 100000.0, 198542.2, 99.0, 0, 31,
                       daily_ref_sum_kwh=100500.0, daily_ref_days=31)
    assert mc.status == STATUS_REVIEW and "vendor upload gaps; counters kept" in mc.note
    assert "above what the inverters counted" in mc.note
    # within the auto-close band it still closes
    mc = monthly_close(98000.0, None, None, 100000.0, 198542.2, 99.0, 0, 31,
                       daily_ref_sum_kwh=98560.0, daily_ref_days=31)
    assert mc.status == STATUS_PASS


# ----------------------------------------------------------- v339: October 2026, the real figures
# Tomasz 2026-11-02 checked the vendor portals: Taigene 102,141.5, Plastic Omnium 61,035.0,
# Quimica 17,314.8, Holiday Inn 27,364.8, Vitalmex 62,505.42, SAG 55,139.65 kWh. The close
# billed the inverter-counter sums (0.4-7.8 kWh lower) and FAILED SAG. Inputs below are the
# real reconciliation_monthly values of 2026-10 (interval, Σ vendor daily, month counter,
# lifetime delta, completeness, Σ inverter references).
OCT_2026 = {
    "GTO1": (102133.7, 102141.5, None, 102142.0, 100.0, 102133.7, 102141.5),
    "NL1": (61030.2, 61035.0, None, 61034.3, 97.2, 61030.2, 61035.0),
    "SLP1": (17311.8, 17314.8, None, 17313.6, 100.0, 17311.8, 17314.8),
    "SLP2": (27263.8, 27364.8, None, 27362.1, 99.8, 27361.5, 27364.8),
    "MEX2": (62505.0, 62504.97, 62505.42, 62505.42, 100.0, 62505.0, 62505.42),
    "MEX1": (48119.4, 48119.37, 55139.65, 55139.65, 96.8, 48119.4, 55139.65),
}


@pytest.mark.parametrize("pk", sorted(OCT_2026))
def test_october_2026_closes_at_the_portal_figures(pk):
    interval, vdaily, vmonth, life, compl, refsum, portal = OCT_2026[pk]
    mc = monthly_close(interval, vdaily, vmonth, 0.0, life, compl, 31, 31,
                       daily_ref_sum_kwh=refsum, daily_ref_days=31)
    assert mc.billing_kwh == portal, (pk, mc.billing_kwh, mc.billing_basis)
    assert mc.status == STATUS_PASS, (pk, mc.status, mc.note)


def test_sag_october_explains_the_late_snapshots():
    interval, vdaily, vmonth, life, compl, refsum, _ = OCT_2026["MEX1"]
    mc = monthly_close(interval, vdaily, vmonth, 0.0, life, compl, 31, 31, daily_ref_sum_kwh=refsum, daily_ref_days=31)
    assert mc.billing_basis == BASIS_MONTHLY and "late vendor uploads" in mc.note


def test_without_a_lifetime_register_the_daily_snapshots_still_guard_the_month():
    # CHECK2 is the only cross-check left: a 12.7 % gap must still FAIL
    mc = monthly_close(48119.4, 48119.37, 55139.65, None, None, 96.8, 31, 31,
                       daily_ref_sum_kwh=48119.4, daily_ref_days=31)
    assert mc.status == STATUS_FAIL
