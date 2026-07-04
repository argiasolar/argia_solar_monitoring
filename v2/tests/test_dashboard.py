import datetime as dt

import pytest

from argia.report import dashboard as D
from argia.report.dashboard import Plant, Sample


def mk(ts, pk, sn, etoday, *, status=1, fault="0", power=1000, irr_wm2=800,
       irr5m=0.066, cloud=10, mtemp=45, atemp=30, label=None, derate=None):
    return Sample(
        ts=ts, plant_key=pk, inverter_sn=sn, inverter_label=label or sn,
        status=status, fault_code=fault, power_w=power, etoday_kwh=etoday,
        temperature_c=mtemp, irradiance_wm2=irr_wm2, irradiance_kwh_m2_5m=irr5m,
        cloud_cover_pct=cloud, ambient_temp_c=atemp, module_temp_c=mtemp,
        derating_mode=derate,
    )


DAY = dt.date(2026, 7, 2)


# --- bucketing helpers -----------------------------------------------------

def test_bucket_start_floors_to_hour():
    assert D.bucket_start(dt.datetime(2026, 7, 2, 10, 47, 33)) == dt.datetime(2026, 7, 2, 10, 0)


def test_daylight_buckets_count_and_bounds():
    b = D.daylight_buckets(DAY)
    assert b[0] == dt.datetime(2026, 7, 2, 6, 0)
    assert b[-1] == dt.datetime(2026, 7, 2, 19, 0)
    assert len(b) == D.DAY_END_H - D.DAY_START_H  # 14 hourly buckets


def test_30min_buckets_extend_without_code_change():
    b = D.daylight_buckets(DAY, minutes=30)
    assert len(b) == (D.DAY_END_H - D.DAY_START_H) * 2
    assert D.bucket_start(dt.datetime(2026, 7, 2, 10, 47), minutes=30) == dt.datetime(2026, 7, 2, 10, 30)


# --- cumulative-counter bucket energy --------------------------------------

def test_bucket_energy_is_cumulative_diff():
    day0 = dt.datetime.combine(DAY, dt.time())
    s = [mk(dt.datetime(2026, 7, 2, 9, 5), "P", "A", 100),
         mk(dt.datetime(2026, 7, 2, 10, 5), "P", "A", 160)]
    e, rep = D.bucket_energy(s, dt.datetime(2026, 7, 2, 10, 0),
                             dt.datetime(2026, 7, 2, 11, 0), day0)
    assert rep and e == pytest.approx(60.0)


def test_bucket_energy_clamps_negative_counter_reset():
    day0 = dt.datetime.combine(DAY, dt.time())
    s = [mk(dt.datetime(2026, 7, 2, 9, 5), "P", "A", 500),
         mk(dt.datetime(2026, 7, 2, 10, 5), "P", "A", 3)]  # counter reset artifact
    e, rep = D.bucket_energy(s, dt.datetime(2026, 7, 2, 10, 0),
                             dt.datetime(2026, 7, 2, 11, 0), day0)
    assert e == 0.0


def test_bucket_energy_gap_rolls_into_next_bucket():
    """A bucket with no poll yields 0; the missed energy shows in the next."""
    day0 = dt.datetime.combine(DAY, dt.time())
    s = [mk(dt.datetime(2026, 7, 2, 9, 5), "P", "A", 100),
         mk(dt.datetime(2026, 7, 2, 11, 5), "P", "A", 260)]  # no 10:00 sample
    e10, rep10 = D.bucket_energy(s, dt.datetime(2026, 7, 2, 10, 0),
                                 dt.datetime(2026, 7, 2, 11, 0), day0)
    e11, rep11 = D.bucket_energy(s, dt.datetime(2026, 7, 2, 11, 0),
                                 dt.datetime(2026, 7, 2, 12, 0), day0)
    assert (e10, rep10) == (0.0, False)
    assert rep11 and e11 == pytest.approx(160.0)


# --- status is DELEGATED to the shared classifier ---------------------------
# Unit tests for the state machine itself live in tests/unit/test_status.py.
# Here we assert the delegation contract: the dashboard consumes THE shared
# module, not a private copy that could drift (the 2026-07-03 lesson).

def test_status_vocabulary_is_the_shared_modules():
    from argia.analytics import status as shared
    assert D.ONLINE is shared.ONLINE
    assert D.FAULT is shared.FAULT
    assert D.classify_plant_bucket is shared.classify_plant_bucket
    assert D.InverterBucket is shared.InverterBucket


def test_build_uses_string_fault_codes_from_unified_feed():
    """Regression: Telemetry_Argia.fault_code is a STRING ("FT=302"); the old
    numeric coercion silently disabled vendor-fault detection."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    prev = dt.datetime(2026, 7, 2, 9, 5)
    samples = [
        mk(ts, "GTO1", "A", 140), mk(prev, "GTO1", "A", 40),
        mk(ts, "GTO1", "B", 90, fault="FT=302"), mk(prev, "GTO1", "B", 35, fault="FT=302"),
    ]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"A", "B"}})
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    rows = {r["inverter_sn"]: r for r in res.inverter_rows if r["bucket_ts"] == b10}
    assert rows["B"]["status"] == D.FAULT
    assert "FT=302" in rows["B"]["status_reason"]
    assert rows["A"]["status"] == D.ONLINE


def test_build_huawei_state_tokens_do_not_fault():
    ts = dt.datetime(2026, 7, 2, 10, 5)
    prev = dt.datetime(2026, 7, 2, 9, 5)
    samples = [
        mk(ts, "GTO1", "A", 140, fault="IS=512,RS=1"),
        mk(prev, "GTO1", "A", 40, fault="IS=512,RS=1"),
        mk(ts, "GTO1", "B", 130, fault="IS=512,RS=1"),
        mk(prev, "GTO1", "B", 35, fault="IS=512,RS=1"),
    ]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"A", "B"}})
    assert all(r["status"] != D.FAULT for r in res.inverter_rows)


def test_build_per_kw_ratings_prevent_small_inverter_false_positive():
    """GTO1 MWKNE9500D regression: 60 kW unit among 124 kW peers must not be
    flagged when its per-kW output matches the plant."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    prev = dt.datetime(2026, 7, 2, 9, 5)
    samples = [
        mk(ts, "GTO1", "BIG1", 124), mk(prev, "GTO1", "BIG1", 0),
        mk(ts, "GTO1", "BIG2", 124), mk(prev, "GTO1", "BIG2", 0),
        mk(ts, "GTO1", "SMALL", 60), mk(prev, "GTO1", "SMALL", 0),
    ]
    ratings = {("GTO1", "BIG1"): 124.0, ("GTO1", "BIG2"): 124.0,
               ("GTO1", "SMALL"): 60.0}
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"BIG1", "BIG2", "SMALL"}},
                  inverter_ratings=ratings)
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    rows = {r["inverter_sn"]: r for r in res.inverter_rows if r["bucket_ts"] == b10}
    assert rows["SMALL"]["status"] == D.ONLINE
    # without ratings the same data DOES flag it — proving ratings matter
    res2 = D.build(DAY, plant_map(), samples,
                   active_inverters={"GTO1": {"BIG1", "BIG2", "SMALL"}})
    rows2 = {r["inverter_sn"]: r for r in res2.inverter_rows if r["bucket_ts"] == b10}
    assert rows2["SMALL"]["status"] == D.UNDERPERFORMING


# --- theoretical ties to KPI_Daily -----------------------------------------

def test_theoretical_matches_kpi_formula():
    p = Plant("SLP1", "Quimica", kwp_dc=189.2, expected_factor=0.75)
    assert p.theoretical(3.4917) == pytest.approx(495.47, abs=0.01)  # KPI_Daily cell


def test_bucketed_theoretical_sums_to_daily_expected():
    """Per-bucket theoretical must sum to the KPI_Daily daily expected_kwh."""
    p = Plant("GTO1", "Taigene", kwp_dc=818.33, expected_factor=0.75)
    daily_irr = 3.611
    per_bucket = [daily_irr / 10] * 10
    assert sum(p.theoretical(x) for x in per_bucket) == pytest.approx(p.theoretical(daily_irr))


# --- end-to-end build ------------------------------------------------------

def plant_map():
    return {"GTO1": Plant("GTO1", "Taigene", 818.33, 0.75)}


def test_build_produces_both_grains_and_no_double_count():
    """Plant theoretical lives only on plant rows; summing inverter energy in a
    bucket equals that bucket's plant total (no per-inverter inflation)."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    samples = [
        mk(ts, "GTO1", "A", 100), mk(ts, "GTO1", "B", 90),
        mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "A", 40),
        mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "B", 35),
    ]
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"A", "B"}})
    assert "theoretical_kwh" not in D.INVERTER_COLUMNS  # never on inverter grain
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    inv10 = [r for r in res.inverter_rows if r["bucket_ts"] == b10]
    plant10 = [r for r in res.plant_rows if r["bucket_ts"] == b10][0]
    assert sum(r["energy_kwh"] for r in inv10) == pytest.approx(plant10["total_kwh"])
    assert plant10["total_kwh"] == pytest.approx(115.0)  # (100-40)+(90-35)


def test_build_handles_inverter_growth_without_schema_change():
    """4 -> 6 inverters: extra inverters just add rows, same columns."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    sns = [f"INV{i}" for i in range(6)]
    samples = [mk(ts, "GTO1", sn, 100) for sn in sns] + \
              [mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", sn, 40) for sn in sns]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": set(sns)})
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    inv10 = [r for r in res.inverter_rows if r["bucket_ts"] == b10]
    assert len(inv10) == 6
    assert set(D.INVERTER_COLUMNS) == set(k for k in res.inverter_rows[0] if k in D.INVERTER_COLUMNS)


def test_build_unions_unconfigured_inverter_from_telemetry():
    """A brand-new inverter present in telemetry but not yet in config appears."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    samples = [mk(ts, "GTO1", "KNOWN", 100), mk(ts, "GTO1", "NEW", 80),
               mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "KNOWN", 40),
               mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "NEW", 30)]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"KNOWN"}})
    sns = {r["inverter_sn"] for r in res.inverter_rows}
    assert "NEW" in sns


def test_peer_median_not_mean_avoids_dead_peer_masking():
    """One dead peer (0) must not drag the reference down via a mean."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    prev = dt.datetime(2026, 7, 2, 9, 5)
    # three peers: 100, 100, and a dead one at 0; a fourth at 55.
    samples = [
        mk(ts, "GTO1", "A", 140), mk(prev, "GTO1", "A", 40),   # +100
        mk(ts, "GTO1", "B", 140), mk(prev, "GTO1", "B", 40),   # +100
        mk(ts, "GTO1", "D", 0, status=3, fault="FT=302"), mk(prev, "GTO1", "D", 0, status=3, fault="FT=302"),  # dead
        mk(ts, "GTO1", "C", 95),  mk(prev, "GTO1", "C", 40),   # +55
    ]
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"A", "B", "C", "D"}})
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    rows = {r["inverter_sn"]: r for r in res.inverter_rows if r["bucket_ts"] == b10}
    # producing peers are {100,100,55}; median 100. 55 < 0.85*100 => underperforming.
    assert rows["C"]["status"] == D.UNDERPERFORMING
    assert rows["D"]["status"] == D.FAULT
    assert rows["A"]["status"] == D.ONLINE


def test_anchored_theoretical_sums_exactly_to_kpi_daily():
    """When a KPI daily expected is supplied, per-bucket theoretical must sum to
    it exactly, regardless of how sparse/wrong the raw intraday irradiance is."""
    ts1 = dt.datetime(2026, 7, 2, 10, 5)
    ts2 = dt.datetime(2026, 7, 2, 13, 5)
    samples = [
        mk(ts1, "GTO1", "A", 100, irr5m=0.05), mk(ts2, "GTO1", "A", 300, irr5m=0.09),
        mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "A", 40, irr5m=0.02),
    ]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"A"}},
                  daily_expected={"GTO1": 4978.0})
    total = sum(r["theoretical_kwh"] for r in res.plant_rows if r["plant_key"] == "GTO1")
    assert total == pytest.approx(4978.0, abs=0.05)


# --- regression: 2026-07-03 midnight carryover (shared rule with kpi_eod) ---

def test_carryover_row_does_not_zero_the_inverters_day():
    """Real incident 2026-07-03: SLP1 JNM7DY306G polled at 00:04 still carried
    Jul 2's etoday (502.4). The cumulative-diff baseline then exceeded every
    later sample, zeroing the inverter's whole day in the dashboard. With the
    shared carryover strip, the day totals its true production."""
    def s(hh, mm, e):
        return mk(dt.datetime(2026, 7, 2, hh, mm), "GTO1", "X", e)
    samples = [s(0, 4, 502.4), s(6, 48, 0.1), s(8, 58, 11.8), s(10, 36, 59.9),
               s(11, 56, 146.0), s(13, 40, 278.2), s(17, 41, 422.5),
               s(19, 16, 434.9)]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"X"}})
    day_total = sum(r["total_kwh"] for r in res.plant_rows)
    assert day_total == pytest.approx(434.9)   # was ~0 before the fix


def test_carryover_strip_shares_rule_with_kpi_energy():
    """Dashboard must use the SAME carryover function as kpi_eod — no second
    implementation allowed to drift."""
    from argia.kpi.energy import find_carryover_cut as kpi_cut
    assert D.find_carryover_cut is kpi_cut
