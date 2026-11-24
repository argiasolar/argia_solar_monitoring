"""v393 - Arturo's methodology R2: a vendor's repeated last-known value is not a sample.

Huawei, Vitalmex 7 Nov 2026 (production data, 24 Nov): inverters 1 and 3 stopped at 07:50; the
FusionSolar real-time API kept serving 120 rows each with 0 W, the day counter frozen at
8.000 / 8.150 kWh and the temperature frozen at 26.0 / 26.5 C, and no string data. Those rows
counted as received telemetry (coverage 100 %). Synthetic data in the same shape here.
"""
from __future__ import annotations

import datetime as dt
import math

from argia.kpi import availability as AV
from argia.report import methodology as MC

LAT, LON = 19.43, -99.13
UTC = dt.timezone.utc
DAY = dt.date(2026, 11, 7)
RATED = [("I1", 150.0), ("I2", 150.0)]


def _rows(i1, i2_temp=True):
    """5-min rows 06:00-19:55 MX. ``i1(t, kw, counter) -> (kw, counter, temp)`` shapes inverter 1;
    inverter 2 produces normally."""
    out, c1, c2 = [], 0.0, 0.0
    t = dt.datetime.combine(DAY, dt.time(6, 0))
    while t.hour < 20:
        frac = (t.hour + t.minute / 60 - 6.5) / 11.5
        kw = max(0.0, 120.0 * math.sin(math.pi * frac)) if 0 < frac < 1 else 0.0
        c2 += kw * 5 / 60
        k1, c1, t1 = i1(t, kw, c1)
        ts = (t - AV.MX_OFFSET).replace(tzinfo=UTC)
        out.append((ts, "I1", 1, k1 * 1000, round(c1, 3), t1))
        out.append((ts, "I2", 1, kw * 1000, round(c2, 3), (30.0 + kw / 10) if i2_temp else None))
        t += dt.timedelta(minutes=5)
    return out


def _stops_then_frozen(t, kw, c):
    """Producing until 07:50, then the cloud repeats the 07:50 reading (0 W, counter, 26.0 C)."""
    if t < dt.datetime.combine(DAY, dt.time(7, 50)):
        return kw, c + kw * 5 / 60, 30.0 + kw / 10
    return 0.0, 8.0 if c > 8.0 else c, 26.0


def test_frozen_rows_are_not_samples_but_the_stop_stays_down():
    res = AV.compute(_rows(_stops_then_frozen), RATED, DAY, LAT, LON)
    # the step of the stop (07:45-08:00, it produced at 07:45) is UP; its last real reading was
    # 0 W while the peer produced, so every silent step after it stays DOWN - as in v392
    assert res.down_steps.get("I1") == res.steps - 1 and "I1" not in res.unknown_steps
    # but the inverter's telemetry coverage is no longer 100 %: one step with a real reading
    assert res.fresh_steps.get("I1") == 1 and res.fresh_steps["I2"] == res.steps
    assert res.repeats > 100
    per, plant = MC.availability_window([(sn, *v) for sn, v in MC.inverter_steps(res, ["I1", "I2"]).items()],
                                        dict(RATED))
    assert per["I1"] == (1 / res.steps, 1 / res.steps)
    assert per["I2"] == (1.0, 1.0) and plant[1] < 0.6


def test_a_frozen_producing_value_no_longer_keeps_a_dead_inverter_up():
    """The worse case R2 prevents: the cloud repeats a PRODUCING reading of an inverter that is gone."""
    def frozen_at_noon(t, kw, c):
        if t < dt.datetime.combine(DAY, dt.time(12, 0)):
            return kw, c + kw * 5 / 60, 30.0 + kw / 10
        return 118.0, 300.0, 41.8                     # one value forever
    res = AV.compute(_rows(frozen_at_noon), RATED, DAY, LAT, LON)
    afternoon = res.unknown_steps.get("I1", 0)
    assert afternoon > 10                              # was UP before v393
    assert res.availability == 1.0 and res.coverage < 1.0      # unknown, not an outage - and not "up"


def test_a_real_stopped_inverter_is_not_a_repeat():
    """0 W, flat counter, but the temperature keeps moving (cooling down): real readings."""
    def stopped(t, kw, c):
        m = t.hour * 60 + t.minute
        return 0.0, 0.0, round(35.0 - m / 100, 1)
    res = AV.compute(_rows(stopped), RATED, DAY, LAT, LON)
    # a 0.1 C step every 10 min: every other 5-min row repeats, but each 15-min step keeps a real one
    assert res.fresh_steps["I1"] == res.steps and res.down_steps["I1"] == res.steps


def test_dawn_zero_then_silence_is_a_connection_gap_not_a_stop():
    """Tomasz 24 Nov: a connectivity issue is not an outage. 0 W at 06:30 (peers at 0 W too, dawn),
    then nothing more from the inverter: unknown, never down."""
    rows = [r for r in _rows(lambda t, kw, c: (kw, c + kw * 5 / 60, 30.0 + kw / 10 + t.minute / 100))
            if not (r[1] == "I1" and r[0].replace(tzinfo=None) + AV.MX_OFFSET > dt.datetime.combine(DAY, dt.time(6, 30)))]
    res = AV.compute(rows, RATED, DAY, LAT, LON)
    assert "I1" not in res.down_steps and res.unknown_steps["I1"] == res.steps and res.availability == 1.0


def test_rows_without_temperature_are_never_repeats():
    def flat(t, kw, c):
        return 0.0, 0.0, None
    res = AV.compute(_rows(flat, i2_temp=False), RATED, DAY, LAT, LON)
    assert res.repeats == 0 and res.fresh_steps["I1"] == res.steps


def test_five_element_samples_still_work():
    rows = [r[:5] for r in _rows(lambda t, kw, c: (kw, c + kw * 5 / 60, 30.0))]
    res = AV.compute(rows, RATED, DAY, LAT, LON)
    assert res.availability == 1.0 and res.coverage == 1.0 and res.repeats == 0


def test_window_coverage_is_the_fresh_share_and_old_rows_fall_back():
    per, plant = MC.availability_window([("A", 30, 5, 0, 20), ("B", 35, 0, 0)], {"A": 100.0, "B": 100.0})
    assert per["A"] == (30 / 35, 20 / 35) and per["B"] == (1.0, 1.0)      # B: no fresh -> known share
    assert plant == (65 / 70, 55 / 70)


def test_a_stale_zero_before_a_gap_is_not_a_stop_when_the_counter_grew():
    """Teneria Panamericana-shaped (Growatt, 3 Nov): inverter 1 reads 0 W at 10:52 with its counter
    still growing, then the link drops until 14:00; it comes back with a much larger counter -
    it produced through the gap. Unknown (or proven), never DOWN."""
    gap = (dt.datetime.combine(DAY, dt.time(10, 55)), dt.datetime.combine(DAY, dt.time(14, 0)))

    def glitch(t, kw, c):
        c2 = c + kw * 5 / 60
        if t == dt.datetime.combine(DAY, dt.time(10, 50)):
            return 0.0, c2, 45.0
        return kw, c2, 30.0 + kw / 10 + t.minute / 100
    rows = [r for r in _rows(glitch)
            if not (r[1] == "I1" and gap[0] <= r[0].replace(tzinfo=None) + AV.MX_OFFSET < gap[1])]
    # an expectation the day falls short of (as on GTO3's 3 Nov): no counter proof - the gap stays unknown
    res = AV.compute(rows, RATED, DAY, LAT, LON, expected_kwh=1e6)
    assert "I1" not in res.down_steps and res.unknown_steps["I1"] > 5 and res.availability == 1.0
