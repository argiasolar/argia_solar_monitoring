"""v341: black-swan scenarios for a billing month.

Tomasz, 2 Nov 2026: "make a list of possible scenarios that can occur during
a month and test against it to make sure we have bulletproof algorithms ...
once we deliver this software to Prologis there will be no margin for
errors."

The rule every scenario is held to: the month either AUTO-CLOSES AT THE
RIGHT FIGURE (the true energy within the vendors' own rounding, and the
daily table adds up to it), or it is HELD for a person. Never an auto-closed
wrong invoice. Each scenario is simulated from known true energy through the
real engine (tests/unit/month_sim.py). The catalogue with the reasoning is
in the project doc V341_BILLING_INVARIANTS_BLACK_SWANS.

Before v341 (v340 engine), four of these auto-closed WRONG: S12 -217 kWh,
S14 -335 kWh, S21 -300 kWh, S23 a 0 kWh invoice; and five right months were
held for nothing (S06, S09, S10, S11, S15). The random months found a fifth
wrong auto-close: a vendor day glitch x3 billed 4,288 kWh the inverters
never counted (now held: references above the lifetime register).
"""
import random

import pytest

from argia.recon import engine as E
from tests.unit.month_sim import Scenario as S, run

ALL = set(range(1, 32))


def is_right(s, o):
    """Right = the true energy within money_tol. A plant WITHOUT a lifetime
    register has no meter of its own: there the vendor portal month is the
    contract (v242), and right = the portal month to its rounding."""
    b = o.close.billing_kwh
    if abs(b - o.truth_kwh) <= money_tol(s, o.truth_kwh):
        return True
    no_register = not s.lifetime or s.lifetime_start_missing or s.lifetime_reset_day
    return bool(no_register) and abs(b - o.portal_kwh) <= 0.5 * s.step * s.days + 0.01


def money_tol(s, truth_kwh):
    """What 'right' means: the vendors' own rounding (a 0.1 kWh step per
    day, a 0.1 kWh lifetime step per inverter) plus the auto-close band."""
    return 0.5 * s.step * s.days + 0.1 * s.n_inv + truth_kwh * E.CLOSE_PASS_PCT / 100.0


CATALOGUE = [
    # --- clean months: must auto-close at the portal figure
    S("S01 clean month, Growatt (no month counter)"),
    S("S02 clean month, Huawei", brand="HUAWEI"),
    S("S03 clean month, SolarEdge", brand="SOLAREDGE"),
    S("S04 February of a leap year (29 days)", year=2028, month=2),
    S("S05 30-day month", month=11),
    S("S05b December (year end)", month=12),
    # --- our own collection fails: must never cost money, never hold the month
    S("S06 one inverter not read by us for 5 days", comms_down={d: {1} for d in range(3, 8)}),
    S("S07 every inverter not read by us for 3 days", comms_down={d: {0, 1, 2, 3} for d in (10, 11, 12)}),
    S("S08 our 5-min telemetry only 60 % complete", interval_completeness=0.6),
    S("S09 server down 2 nights, Growatt", snapshot_missing={5, 6}),
    S("S10 server down 2 nights, Huawei", brand="HUAWEI", snapshot_missing={5, 6}),
    S("S26 vendor portal down all month, inverter counters complete", snapshot_missing=ALL),
    # --- vendor timing: the portal fills in after our nightly snapshot
    S("S11 Huawei late uploads on 16 days (SAG Oct 2026)", brand="HUAWEI",
      late_upload={d: 0.8 for d in range(5, 21)}),
    S("S12 Growatt late uploads, small (0.3 % of the month)", late_upload={5: 0.05, 6: 0.05},
      expect="HOLD", why="the portal month we hold is 217 kWh short of the final portal"),
    S("S13 late uploads on days we did not read either", late_upload={5: 0.5, 6: 0.5},
      comms_down={5: {0, 1, 2, 3}, 6: {0, 1, 2, 3}}, expect="HOLD", why="nobody holds the full days"),
    S("S21 Huawei month counter short at close (uploads still arriving)", brand="HUAWEI",
      month_counter_short=300.0, expect="HOLD", why="month counter 300 kWh below the lifetime register"),
    # --- vendor loses energy for good (SLP2 Sep 2026 pattern)
    S("S14 portal lost 10 % of one day, counters have it", vendor_lost={9: 0.10},
      expect="HOLD", why="portal month 335 kWh below what the inverters produced"),
    S("S15 portal lost 30 % of three days, counters have it", vendor_lost={d: 0.3 for d in (3, 4, 5)}),
    # --- counter anomalies
    S("S16 Growatt inverter swapped mid-month (lifetime restarts)", lifetime_reset_day=15,
      expect="HOLD", why="no independent cross-check left"),
    S("S16b Huawei inverter swapped mid-month", brand="HUAWEI", lifetime_reset_day=15),
    S("S17 no lifetime snapshot at the end of the previous month", lifetime_start_missing=True,
      expect="HOLD", why="single source"),
    S("S18 vendor daily glitch x10 on one day, Growatt", spike_day=(12, 10.0), expect="HOLD"),
    S("S19 vendor daily glitch x10, Huawei (month counter too)", brand="HUAWEI", spike_day=(12, 10.0),
      expect="HOLD"),
    S("S20 vendor reports a negative day", negative_day=8, expect="SAFE"),
    S("S25 Growatt plant without a lifetime register", lifetime=False, expect="HOLD", why="single source"),
    S("S28 vendor down and one inverter unread on the same day",
      snapshot_missing={14}, comms_down={14: {2}}, expect="HOLD", why="639 kWh nobody reported"),
    # --- real production events: billed as produced
    S("S22 plant really off 3 days"),
    S("S23 plant off the whole month", outage_days=ALL, expect="HOLD", why="a 0 kWh invoice needs a person"),
    S("S24 one inverter really dead 10 days", inverter_dead={d: {2} for d in range(10, 20)}),
    S("S27 no data from anybody", snapshot_missing=ALL, comms_down={d: {0, 1, 2, 3} for d in ALL},
      lifetime=False, expect="HOLD"),
]


def _days(s):
    return {d for d in range(1, s.days + 1)}


@pytest.mark.parametrize("s", CATALOGUE, ids=[s.name.split()[0] for s in CATALOGUE])
def test_scenario(s):
    for k in ("comms_down", "late_upload", "vendor_lost"):        # keep the dicts inside the month
        setattr(s, k, {d: v for d, v in getattr(s, k).items() if d <= s.days})
    s.outage_days = {d for d in s.outage_days if d <= s.days}
    s.snapshot_missing = {d for d in s.snapshot_missing if d <= s.days}
    o = run(s)
    c = o.close
    if s.expect == "HOLD":
        assert c.status != E.STATUS_PASS, (s.name, c.billing_kwh, o.truth_kwh, c.note)
        return
    if s.expect == "PASS":
        assert c.status == E.STATUS_PASS, (s.name, c.status, c.note)
    if c.status == E.STATUS_PASS:
        assert is_right(s, o), (s.name, c.billing_kwh, o.truth_kwh)
        assert abs(o.daily_table_sum - c.billing_kwh) <= E.DAILY_TABLE_TOL_KWH, (s.name, o.daily_table_sum)


def test_clean_months_bill_exactly_the_portal_month():
    for s in (S("g"), S("h", brand="HUAWEI"), S("e", brand="SOLAREDGE")):
        o = run(s)
        assert o.close.billing_kwh == pytest.approx(o.portal_kwh, abs=0.01)


def _random_scenario(rnd, i):
    brand = rnd.choice(["GROWATT", "HUAWEI", "SOLAREDGE"])
    month = rnd.choice([2, 4, 10, 12])
    s = S(f"fuzz{i}", brand=brand, month=month, seed=i, n_inv=rnd.choice([1, 2, 4, 9]))
    days = list(range(1, s.days + 1))

    def some(k):
        return rnd.sample(days, k)
    if rnd.random() < 0.3:
        s.comms_down = {d: {rnd.randrange(s.n_inv)} for d in some(rnd.randint(1, 8))}
    if rnd.random() < 0.3:
        s.late_upload = {d: rnd.choice([0.01, 0.05, 0.3, 0.9]) for d in some(rnd.randint(1, 10))}
    if rnd.random() < 0.2:
        s.vendor_lost = {d: rnd.choice([0.001, 0.02, 0.1, 0.5]) for d in some(rnd.randint(1, 4))}
    if rnd.random() < 0.2:
        s.snapshot_missing = set(some(rnd.randint(1, 5)))
    if rnd.random() < 0.15:
        s.outage_days = set(some(rnd.randint(1, 4)))
    if rnd.random() < 0.15 and s.month_counter:
        s.month_counter_short = rnd.choice([1.0, 20.0, 400.0])
    if rnd.random() < 0.1:
        s.spike_day = (rnd.choice(days), rnd.choice([0.5, 1.02, 3.0]))
    if rnd.random() < 0.1:
        s.lifetime = False
    if rnd.random() < 0.05:
        s.lifetime_reset_day = rnd.choice(days[1:])
    s.interval_completeness = rnd.choice([0.5, 0.9, 0.99, 1.0])
    return s


def test_fuzz_an_auto_closed_month_is_always_right():
    """1,500 random months mixing every failure above at random sizes. The
    property: whenever the engine auto-closes, the billed kWh is the true
    energy within money_tol AND the daily table adds up to it."""
    rnd = random.Random(20261102)
    passed = held = 0
    for i in range(1500):
        s = _random_scenario(rnd, i)
        o = run(s)
        c = o.close
        if c.status != E.STATUS_PASS:
            held += 1
            continue
        passed += 1
        assert is_right(s, o), (s, c.billing_kwh, o.truth_kwh, o.portal_kwh, c.note)
        assert abs(o.daily_table_sum - c.billing_kwh) <= E.DAILY_TABLE_TOL_KWH, (s, o.daily_table_sum, c.billing_kwh)
    assert passed > 300 and held > 100, (passed, held)      # the generator exercises both sides
