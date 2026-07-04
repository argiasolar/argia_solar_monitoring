import datetime as dt

import pytest

from argia.report import dashboard as D
from argia.report.dashboard import Plant, Sample


def mk(ts, pk, sn, etoday, *, status=1, fault=0, power=1000, irr_wm2=800,
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


# --- fault detection -------------------------------------------------------

def test_status_3_is_fault():
    assert D.is_real_fault(3, 0) is True


def test_nonzero_fault_code_is_fault():
    assert D.is_real_fault(1, 302) is True


def test_excluded_huawei_token_is_not_fault():
    assert D.is_real_fault(1, 40000, excluded_tokens=frozenset({40000})) is False


def test_zero_fault_code_normal_status_is_not_fault():
    assert D.is_real_fault(1, 0) is False


# --- status state machine --------------------------------------------------

def base_kw(**kw):
    d = dict(reported=True, energy_kwh=50.0, sun_up=True, status=1, fault_code=0,
             derating_mode=0, peer_median=50.0)
    d.update(kw)
    return d


def test_healthy_inverter_is_online():
    assert D.inverter_status(**base_kw())[0] == D.ONLINE


def test_regression_online_while_zero_is_offline_not_online():
    """SAG-Mexico case: vendor says status=1 but inverter produced 0 while peers
    produced. The old dashboard showed ONLINE; correct answer is OFFLINE."""
    st, reason = D.inverter_status(**base_kw(energy_kwh=0.0, status=1, peer_median=120.0))
    assert st == D.OFFLINE
    assert "peers" in reason


def test_zero_with_no_plant_production_is_idle_not_offline():
    st, _ = D.inverter_status(**base_kw(energy_kwh=0.0, peer_median=None, sun_up=True))
    assert st == D.IDLE_NIGHT  # cloudy dead-calm bucket, nobody producing


def test_underperforming_below_85pct_of_peer_median():
    st, reason = D.inverter_status(**base_kw(energy_kwh=50.0, peer_median=100.0))
    assert st == D.UNDERPERFORMING and "50%" in reason


def test_not_underperforming_at_90pct():
    st, _ = D.inverter_status(**base_kw(energy_kwh=90.0, peer_median=100.0))
    assert st == D.ONLINE


def test_fault_beats_underperformance():
    st, _ = D.inverter_status(**base_kw(energy_kwh=1.0, status=3, peer_median=100.0))
    assert st == D.FAULT


def test_no_report_in_daylight_is_offline():
    st, _ = D.inverter_status(**base_kw(reported=False, sun_up=True))
    assert st == D.OFFLINE


def test_no_report_at_night_is_not_alarmed():
    st, _ = D.inverter_status(**base_kw(reported=False, sun_up=False, peer_median=0.0))
    assert st == D.IDLE_NIGHT


def test_derated_when_flag_set():
    st, _ = D.inverter_status(**base_kw(derating_mode=1))
    assert st == D.DERATED


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
        mk(ts, "GTO1", "D", 0, status=3, fault=302), mk(prev, "GTO1", "D", 0, status=3, fault=302),  # dead
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
