"""v379 - counter-conserving gap reconstruction (argia.epi.gaps) and stale records."""
from __future__ import annotations

import math

import pytest

from argia.epi import gaps as G
from argia.epi import sky

SLOTS = list(range(100, 244))                                     # 12 h of 5-minute slots
SHAPE = {k: math.sin(math.pi * (k - 99) / 145) * 100_000 for k in SLOTS}   # a clear-day bell, W


def truth_counter():
    """Exact day counter for the bell: cumulative energy at the end of each slot."""
    c, out = 0.0, {99: 0.0}
    for k in SLOTS:
        c += SHAPE[k] * 5 / 60 / 1000
        out[k] = c
    return out


def test_complete_day_is_all_measured_and_conserved():
    c = truth_counter()
    rec, gaps = G.reconstruct_day(sorted(c.items()), SLOTS, SHAPE)
    assert all(s.flag == G.MEASURED for s in rec) and gaps == []
    assert G.day_energy_kwh(rec) == (pytest.approx(c[243]), 0)


def test_gap_spread_by_shape_conserves_the_counter():
    c = truth_counter()
    kept = [(k, v) for k, v in c.items() if not 150 <= k < 186]      # 3 h silent
    rec, gaps = G.reconstruct_day(kept, SLOTS, SHAPE)
    by = {s.bucket: s for s in rec}
    assert all(by[k].flag == G.RECONSTRUCTED for k in range(150, 187))
    assert G.day_energy_kwh(rec)[0] == pytest.approx(c[243], abs=1e-9)          # exact
    for k in range(150, 187):                                                    # right shape: exact here
        assert by[k].energy_kwh == pytest.approx(c[k] - c[k - 1], rel=1e-9)
    (g,) = gaps
    assert g.availability == "up" and g.energy_kwh == pytest.approx(c[186] - c[149])


def test_leading_gap_uses_the_counter_since_midnight():
    c = truth_counter()
    kept = [(k, v) for k, v in c.items() if k >= 130]                # logger online at slot 130
    rec, _ = G.reconstruct_day(kept, SLOTS, SHAPE)
    assert [s.flag for s in rec[:31]] == [G.RECONSTRUCTED] * 31
    assert sum(s.energy_kwh for s in rec[:31]) == pytest.approx(c[130])


def test_trailing_gap_unknown_until_the_daily_total():
    c = truth_counter()
    kept = [(k, v) for k, v in c.items() if k <= 200]
    rec, _ = G.reconstruct_day(kept, SLOTS, SHAPE)
    assert [s.flag for s in rec if s.bucket > 200] == [G.UNKNOWN] * 43               # never zero, never "after hours"
    assert G.day_energy_kwh(rec) == (pytest.approx(c[200]), 43)
    rec2, _ = G.reconstruct_day(kept, SLOTS, SHAPE, day_total_kwh=c[243])
    assert G.day_energy_kwh(rec2) == (pytest.approx(c[243]), 0)


def test_counter_proves_down_and_zero_weights_go_uniform():
    flat = [(99, 0.0), (120, 50.0), (150, 50.0), (151, 55.0)]        # no rise for 30 slots
    rec, gaps = G.reconstruct_day(flat, SLOTS, SHAPE)
    assert [g.availability for g in gaps] == ["up", "down"]
    rec, _ = G.reconstruct_day([(99, 0.0), (110, 2.0)], SLOTS[:11], {})
    assert [s.energy_kwh for s in rec] == pytest.approx([2.0 / 11] * 11)


def test_glitch_dropped_and_reset_continued():
    rows = [(99, 0.0), (100, 1.0), (101, 0.2), (102, 3.0), (103, 4.0)]          # 0.2 is a glitch
    assert G._clean(rows) == [(99, 0.0), (100, 1.0), (102, 3.0), (103, 4.0)]
    reset = [(99, 0.0), (100, 40.0), (101, 48.5), (102, 8.2), (103, 15.3)]      # reboot at slot 102 (Turistica 10 Oct)
    assert G._clean(reset)[-2:] == [(102, 56.7), (103, 63.8)]
    rec, _ = G.reconstruct_day(reset, [100, 101, 102, 103], {})
    assert G.day_energy_kwh(rec)[0] == pytest.approx(63.8)


def test_stale_records():
    rows = [(1, 50_000.0, 10.0), (2, 60_000.0, 15.0), (3, 60_000.0, 15.0),     # exact repeat
            (4, 70_000.0, 15.0),                                                # counter stuck at 70 kW
            (5, 1_000.0, 15.0), (6, 900.0, 15.0),                               # low power, 0.1 kWh resolution
            (7, None, None), (8, 0.0, 15.0), (9, 0.0, 15.0)]                    # night zeros are not stale
    assert G.stale_buckets(rows) == {3, 4}


def test_haurwitz_matches_pvlib():
    assert sky.clear_sky_ghi_haurwitz(0.0) == pytest.approx(1098 * math.exp(-0.059))
    assert sky.clear_sky_ghi_haurwitz(90.0) == 0.0 and sky.clear_sky_ghi_haurwitz(95.0) == 0.0
    pvlib = pytest.importorskip("pvlib")
    pd = pytest.importorskip("pandas")
    z = pd.Series([10.0, 45.0, 80.0])
    ref = pvlib.clearsky.haurwitz(z)["ghi"].values
    assert [sky.clear_sky_ghi_haurwitz(x) for x in z] == pytest.approx(list(ref), rel=1e-9)


def test_availability_uses_ac_power_then_counter_proof():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("t378", pathlib.Path(__file__).with_name("test_v378_epi_engine.py"))
    T = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(T)
    from argia.epi import engine as E
    pm, strs, chan, sn_of = T._plant()
    station, samples = T._day(pm, strs, 1.0)
    bs = sorted(station)
    ac, verdict = {}, {}
    for b in bs[10:20]:                       # strings missing, AC power says producing
        del samples[("SN1", b)]
        ac[("SN1", b)] = 15_000.0
    for b in bs[20:24]:                       # strings missing, AC power 0 -> down
        del samples[("SN1", b)]
        ac[("SN1", b)] = 0.0
    for b in bs[40:46]:                       # silent; the counter proves production
        del samples[("SN1", b)]
        verdict[("SN1", b)] = "up"
    for b in bs[50:53]:                       # silent; the counter proves no production
        del samples[("SN1", b)]
        verdict[("SN1", b)] = "down"
    for b in bs[60:62]:                       # silent, no proof
        del samples[("SN1", b)]
    res = E.run_plant(pm, strs, chan, sn_of, station, samples, T.MX, ac_power_w=ac, gap_verdict=verdict)
    a = res.avail["INV1"]
    assert (a.by_ac_power, a.up_by_counter, a.down_by_counter, a.missing) == (14, 6, 3, 2)
    judged = sum(1 for b in bs if station[b].ghi > 150)          # Availability-150 judges these only
    assert a.down == 4 + 3 and a.up + a.down + a.missing == judged
    assert res.epi600["plant"].epi == pytest.approx(1.0, abs=1e-9)
