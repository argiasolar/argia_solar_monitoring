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


# --- regression: live-day theoretical fallback (trapezoid irradiance) --------

def test_trapezoid_constant_irradiance_integrates_exactly():
    """Two samples 1h apart at a constant 1000 W/m2 = 1.0 kWh/m2."""
    s = [mk(dt.datetime(2026, 7, 2, 10, 0), "GTO1", "A", 10, irr_wm2=1000),
         mk(dt.datetime(2026, 7, 2, 11, 0), "GTO1", "A", 20, irr_wm2=1000)]
    b = D.daylight_buckets(DAY)
    out = D.irradiance_by_bucket(s, b, dt.timedelta(minutes=60))
    assert sum(out.values()) == pytest.approx(1.0)
    assert out[dt.datetime(2026, 7, 2, 10, 0)] == pytest.approx(1.0)


def test_trapezoid_splits_across_bucket_edge():
    """A segment spanning 09:30-10:30 lands half in each bucket."""
    s = [mk(dt.datetime(2026, 7, 2, 9, 30), "GTO1", "A", 10, irr_wm2=1000),
         mk(dt.datetime(2026, 7, 2, 10, 30), "GTO1", "A", 20, irr_wm2=1000)]
    b = D.daylight_buckets(DAY)
    out = D.irradiance_by_bucket(s, b, dt.timedelta(minutes=60))
    assert out[dt.datetime(2026, 7, 2, 9, 0)] == pytest.approx(0.5)
    assert out[dt.datetime(2026, 7, 2, 10, 0)] == pytest.approx(0.5)


def test_trapezoid_caps_long_gaps():
    """A 6h gap contributes only its first IRR_MAX_GAP_H hours — the sky
    state across a long silence is unknown, not free energy."""
    s = [mk(dt.datetime(2026, 7, 2, 8, 0), "GTO1", "A", 10, irr_wm2=1000),
         mk(dt.datetime(2026, 7, 2, 14, 0), "GTO1", "A", 20, irr_wm2=1000)]
    b = D.daylight_buckets(DAY)
    out = D.irradiance_by_bucket(s, b, dt.timedelta(minutes=60))
    assert sum(out.values()) == pytest.approx(D.IRR_MAX_GAP_H * 1.0)


def test_regression_sparse_polls_do_not_collapse_fallback_theoretical():
    """The Jul 4 incident: ~90-min polling made the old fallback (sum of
    5-min deltas) report theoretical ~3x BELOW actual production. With the
    trapezoid the same sparse day integrates the full sun window."""
    times = [dt.datetime(2026, 7, 2, 8, 0), dt.datetime(2026, 7, 2, 9, 30),
             dt.datetime(2026, 7, 2, 11, 0), dt.datetime(2026, 7, 2, 12, 30),
             dt.datetime(2026, 7, 2, 14, 0)]
    samples = [mk(t, "GTO1", "A", 50 * i, irr_wm2=800, irr5m=0.066)
               for i, t in enumerate(times)]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"A"}})
    theo = sum(r["theoretical_kwh"] for r in res.plant_rows)
    # old behavior: 818.33 * (5 * 0.066) * 0.75 = 202 kWh
    # trapezoid: 818.33 * (6h * 0.8) * 0.75 = 2946 kWh
    assert theo == pytest.approx(818.33 * 4.8 * 0.75, rel=0.01)


def test_anchored_day_still_sums_exactly_to_kpi_with_trapezoid_shape():
    ts1 = dt.datetime(2026, 7, 2, 10, 5)
    ts2 = dt.datetime(2026, 7, 2, 13, 5)
    samples = [
        mk(ts1, "GTO1", "A", 100, irr_wm2=700), mk(ts2, "GTO1", "A", 300, irr_wm2=900),
        mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "A", 40, irr_wm2=400),
    ]
    res = D.build(DAY, plant_map(), samples, active_inverters={"GTO1": {"A"}},
                  daily_expected={"GTO1": 4978.0})
    total = sum(r["theoretical_kwh"] for r in res.plant_rows)
    assert total == pytest.approx(4978.0, abs=0.05)


# --- availability-loss estimate (est_loss_kwh) -------------------------------

def _loss_setup(ratings=True):
    ts = dt.datetime(2026, 7, 2, 10, 5)
    prev = dt.datetime(2026, 7, 2, 9, 5)
    samples = [
        mk(ts, "GTO1", "A", 140), mk(prev, "GTO1", "A", 40),      # +100
        mk(ts, "GTO1", "B", 130), mk(prev, "GTO1", "B", 35),      # +95
        mk(ts, "GTO1", "D", 0, status=3, fault="FT=302"),
        mk(prev, "GTO1", "D", 0, status=3, fault="FT=302"),       # dead
    ]
    r = {("GTO1", "A"): 100.0, ("GTO1", "B"): 100.0,
         ("GTO1", "D"): 50.0} if ratings else None
    return samples, r


def test_faulted_inverter_gets_perkw_scaled_loss():
    """Dead 50 kW unit among 100 kW peers producing ~1 kWh/kW: the loss is
    per-kW scaled (~48.8 kWh), NOT the raw peer median (~97.5)."""
    samples, ratings = _loss_setup()
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"A", "B", "D"}},
                  inverter_ratings=ratings)
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    rows = {r["inverter_sn"]: r for r in res.inverter_rows
            if r["bucket_ts"] == b10}
    assert rows["D"]["est_loss_kwh"] == pytest.approx(48.75, abs=0.1)
    assert rows["A"]["est_loss_kwh"] == 0.0
    assert rows["B"]["est_loss_kwh"] == 0.0


def test_loss_falls_back_to_raw_median_without_ratings():
    samples, _ = _loss_setup(ratings=False)
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"A", "B", "D"}})
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    rows = {r["inverter_sn"]: r for r in res.inverter_rows
            if r["bucket_ts"] == b10}
    assert rows["D"]["est_loss_kwh"] == pytest.approx(97.5, abs=0.1)


def test_whole_plant_dark_estimates_zero_loss():
    """No producing peers -> no basis; the case screams via production %."""
    ts = dt.datetime(2026, 7, 2, 10, 5)
    samples = [mk(ts, "GTO1", "A", 0, status=3, fault="FT=302"),
               mk(ts, "GTO1", "B", 0, status=3, fault="FT=302")]
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"A", "B"}})
    assert all(r["est_loss_kwh"] == 0.0 for r in res.inverter_rows)


def test_underperforming_is_not_counted_as_availability_loss():
    ts = dt.datetime(2026, 7, 2, 10, 5)
    prev = dt.datetime(2026, 7, 2, 9, 5)
    samples = [
        mk(ts, "GTO1", "A", 140), mk(prev, "GTO1", "A", 40),
        mk(ts, "GTO1", "B", 130), mk(prev, "GTO1", "B", 35),
        mk(ts, "GTO1", "C", 90), mk(prev, "GTO1", "C", 40),   # +50 laggard
    ]
    res = D.build(DAY, plant_map(), samples,
                  active_inverters={"GTO1": {"A", "B", "C"}})
    b10 = dt.datetime(2026, 7, 2, 10, 0)
    rows = {r["inverter_sn"]: r for r in res.inverter_rows
            if r["bucket_ts"] == b10}
    assert rows["C"]["status"] == D.UNDERPERFORMING
    assert rows["C"]["est_loss_kwh"] == 0.0


def test_plant_rows_carry_tariff_and_parse_reads_it():
    plants = D.parse_plants([{"plant_key": "SLP1", "customer": "Quimica",
                              "kwp_dc": 189.2, "expected_factor": 0.75,
                              "tariff_mxn_per_kwh": "2.596"}])
    assert plants["SLP1"].tariff_mxn_per_kwh == pytest.approx(2.596)
    ts = dt.datetime(2026, 7, 2, 10, 5)
    res = D.build(DAY, plants, [mk(ts, "SLP1", "A", 10)],
                  active_inverters={"SLP1": {"A"}})
    assert res.plant_rows[0]["tariff_mxn_per_kwh"] == pytest.approx(2.596)
    assert "tariff_mxn_per_kwh" in D.PLANT_COLUMNS
    assert "est_loss_kwh" in D.INVERTER_COLUMNS


def test_parse_plants_skips_inactive_and_defaults_to_active():
    """Regression: 4 inactive plants (MEX3/NL2/QRO1/GTO2) generated pure
    NO_DATA padding rows every run — filter at the source. Rows without an
    `active` column stay included (backward compatible)."""
    rows = [
        {"plant_key": "GTO1", "kwp_dc": 818.33, "expected_factor": 0.75,
         "active": "TRUE"},
        {"plant_key": "QRO1", "kwp_dc": 100.0, "expected_factor": 0.75,
         "active": "FALSE"},
        {"plant_key": "NL2", "kwp_dc": 100.0, "expected_factor": 0.75,
         "active": False},
        {"plant_key": "SLP1", "kwp_dc": 189.2, "expected_factor": 0.75},
    ]
    plants = D.parse_plants(rows)
    assert set(plants) == {"GTO1", "SLP1"}


def test_plant_rows_stamp_data_start():
    """Incident 2026-07-06: overnight collector outage -> first sample 08:19,
    early energy rolled into the 08:00 bucket, live % read 269% with no
    warning. The build stamps each plant-day's first-sample time so the
    page can flag the asymmetry."""
    late = [mk(dt.datetime(2026, 7, 2, 8, 19), "GTO1", "A", 150),
            mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "A", 260)]
    res = D.build(DAY, plant_map(), late, active_inverters={"GTO1": {"A"}})
    assert res.plant_rows[0]["data_start"] == "08:19"
    assert "data_start" in D.PLANT_COLUMNS

    normal = [mk(dt.datetime(2026, 7, 2, 6, 4), "GTO1", "A", 0),
              mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "A", 260)]
    res2 = D.build(DAY, plant_map(), normal, active_inverters={"GTO1": {"A"}})
    assert res2.plant_rows[0]["data_start"] == "06:04"

    # the hole the real data exposed: a delayed MIDNIGHT run's 00:5x rows
    # must not mask a late daylight start
    night_then_late = [mk(dt.datetime(2026, 7, 2, 0, 51), "GTO1", "A", 480),
                       mk(dt.datetime(2026, 7, 2, 8, 19), "GTO1", "A", 30),
                       mk(dt.datetime(2026, 7, 2, 9, 5), "GTO1", "A", 120)]
    res3 = D.build(DAY, plant_map(), night_then_late,
                   active_inverters={"GTO1": {"A"}})
    assert res3.plant_rows[0]["data_start"] == "08:19"


class TestCoverageGuards20260706:
    """Replay of the GTO1 morning: degraded polling hit 1 of 6 inverters
    in the 09h bucket -> 5 phantom OFFLINEs (+$804 phantom loss), and the
    lone reporter read "43% of peers CRITICAL" in the 10h bucket because
    its energy window differed from the peers'."""

    def _gto_like(self):
        t = lambda h, m: dt.datetime(2026, 7, 2, h, m)
        S = []
        # 08h: all six report (post-gap rollover bucket)
        for i, sn in enumerate("ABCDEF"):
            S += [mk(t(8, 18), "GTO1", sn, 50 + i)]
        # 09h: ONE stray poll reaches only F
        S += [mk(t(9, 59), "GTO1", "F", 150)]
        # 10h: the other five return; F polls again at 10:49
        for i, sn in enumerate("ABCDE"):
            S += [mk(t(10, 48), "GTO1", sn, 230 + i)]
        S += [mk(t(10, 49), "GTO1", "F", 228)]
        return S

    def _build(self):
        return D.build(DAY, plant_map(), self._gto_like(),
                       active_inverters={"GTO1": set("ABCDEF")},
                       inverter_ratings={("GTO1", c): 124.0
                                         for c in "ABCDEF"})

    def test_partial_poll_bucket_yields_no_phantom_offline_or_loss(self):
        rows = {(r["hour_label"], r["inverter_sn"]): r
                for r in self._build().inverter_rows}
        for sn in "ABCDE":
            r = rows[("09:00", sn)]
            assert r["status"] == D.NO_DATA, r
            assert r["est_loss_kwh"] == 0.0
        assert rows[("09:00", "F")]["status"] == D.ONLINE

    def test_no_phantom_underperformance_after_broken_bucket(self):
        rows = {(r["hour_label"], r["inverter_sn"]): r
                for r in self._build().inverter_rows}
        # F's 10h window (09:59->10:49) vs peers' (08:18->10:48): without
        # the guard this read ~43% of peers CRITICAL
        assert rows[("10:00", "F")]["status"] == D.ONLINE

    def test_true_single_inverter_outage_still_flags(self):
        """The guard must NOT hide a real outage: full plant coverage,
        one inverter silent -> OFFLINE with loss, as before."""
        t = lambda h, m: dt.datetime(2026, 7, 2, h, m)
        S = []
        for h in (9, 10):
            for i, sn in enumerate("ABCDE"):   # F silent all day
                S += [mk(t(h, 5), "GTO1", sn, (h - 8) * 100 + i)]
        res = D.build(DAY, plant_map(), S,
                      active_inverters={"GTO1": set("ABCDEF")},
                      inverter_ratings={("GTO1", c): 124.0
                                        for c in "ABCDEF"})
        rows = {(r["hour_label"], r["inverter_sn"]): r
                for r in res.inverter_rows}
        r = rows[("10:00", "F")]
        assert r["status"] == D.OFFLINE
        assert r["est_loss_kwh"] > 0
