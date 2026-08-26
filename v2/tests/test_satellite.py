"""Satellite irradiance cross-check — pure-function tests.

Fixture is a REAL Open-Meteo response captured from pio06 on 2026-08-26
(SLP1 coordinates) — the parser is tested against what the API actually
returns, not what its docs promise.
"""

from __future__ import annotations

import json
from pathlib import Path

from argia.kpi.satellite import (
    DRIFT_REVIEW_PCT,
    build_url,
    drift_check,
    parse_daily_ghi,
    ratio_series,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "openmeteo"
           / "forecast_slp1.json")


def _payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["response"]


# ------------------------------------------------------------ build_url
def test_build_url_contains_coords_and_daily_param():
    url = build_url(22.121844, -100.918215)
    assert url.startswith("https://api.open-meteo.com/v1/forecast?")
    assert "latitude=22.121844" in url
    assert "longitude=-100.918215" in url
    assert "shortwave_radiation_sum" in url
    assert "past_days=40" in url
    assert "America%2FMexico_City" in url


# ------------------------------------------------------- parse_daily_ghi
def test_parse_real_fixture_mj_converted_to_kwh():
    ghi = parse_daily_ghi(_payload())
    assert len(ghi) == 41
    # 21.33 MJ/m2 / 3.6 = 5.925 kWh/m2
    assert ghi["2026-07-17"] == 5.925
    assert ghi["2026-08-26"] == round(27.93 / 3.6, 3)
    # plausible Mexican-highlands daily GHI
    assert all(2.0 < v < 9.0 for v in ghi.values())


def test_parse_skips_null_days():
    p = {"daily_units": {"shortwave_radiation_sum": "MJ/m²"},
         "daily": {"time": ["2026-08-01", "2026-08-02"],
                   "shortwave_radiation_sum": [18.0, None]}}
    assert parse_daily_ghi(p) == {"2026-08-01": 5.0}


def test_parse_wh_unit():
    p = {"daily_units": {"shortwave_radiation_sum": "Wh/m²"},
         "daily": {"time": ["2026-08-01"],
                   "shortwave_radiation_sum": [5500]}}
    assert parse_daily_ghi(p) == {"2026-08-01": 5.5}


def test_parse_unknown_unit_returns_empty():
    p = {"daily_units": {"shortwave_radiation_sum": "furlongs"},
         "daily": {"time": ["2026-08-01"],
                   "shortwave_radiation_sum": [5.0]}}
    assert parse_daily_ghi(p) == {}


def test_parse_malformed_payloads_return_empty():
    assert parse_daily_ghi({}) == {}
    assert parse_daily_ghi({"daily": {}}) == {}
    assert parse_daily_ghi({"daily_units": None, "daily": None}) == {}


# --------------------------------------------------------- ratio_series
def test_ratio_series_matches_days_and_filters_low_light():
    measured = {"2026-08-01": 6.0, "2026-08-02": 0.2,   # 0.2: dark day
                "2026-08-03": 5.0, "2026-08-04": None,
                "2026-08-05": 4.0}                       # no satellite
    sat = {"2026-08-01": 5.0, "2026-08-02": 5.0,
           "2026-08-03": 0.3}                            # sat dark day
    r = ratio_series(measured, sat)
    assert r == {"2026-08-01": 1.2}


# ---------------------------------------------------------- drift_check
def _ratios(n_baseline, n_recent, base_val, recent_val):
    out = {}
    day = 1
    for _ in range(n_baseline):
        out[f"2026-07-{day:02d}"] = base_val
        day += 1
    day = 1
    for _ in range(n_recent):
        out[f"2026-08-{day:02d}"] = recent_val
        day += 1
    return out


def test_drift_ok_when_stable():
    dc = drift_check(_ratios(20, 7, 1.10, 1.12))
    assert dc.status == "OK"
    assert abs(dc.drift_pct) < DRIFT_REVIEW_PCT
    assert dc.n_recent == 7 and dc.n_baseline == 20


def test_drift_review_on_step_change():
    dc = drift_check(_ratios(20, 7, 1.10, 0.90))   # -18% step
    assert dc.status == "REVIEW"
    assert dc.drift_pct < -DRIFT_REVIEW_PCT
    assert "sensor" in dc.note


def test_drift_median_shrugs_off_one_broken_day():
    ratios = _ratios(20, 7, 1.10, 1.10)
    ratios["2026-08-03"] = 0.10          # one absurd recent day
    dc = drift_check(ratios)
    assert dc.status == "OK"


def test_drift_no_data_when_recent_too_thin():
    dc = drift_check(_ratios(20, 3, 1.1, 1.1))
    assert dc.status == "NO_DATA"
    assert dc.drift_pct is None
    assert "not enough" in dc.note


def test_drift_no_data_when_baseline_too_thin():
    dc = drift_check(_ratios(5, 7, 1.1, 1.1))
    assert dc.status == "NO_DATA"


def test_drift_no_data_on_empty_series():
    dc = drift_check({})
    assert dc.status == "NO_DATA"
    assert dc.n_recent == 0 and dc.n_baseline == 0


# ------------------------------------------------------ alert plumbing
def test_satellite_alerts_only_review_rows():
    from argia.alerts.monitor import satellite_alerts
    rows = [("SLP1", "OK", "1.2", "stable"),
            ("GTO1", "REVIEW", "-14.3", "check the sensor"),
            ("QRO1", "NO_DATA", "?", "not enough days"),
            None, ("BAD",)]
    alerts = satellite_alerts(rows)
    assert [a.key for a in alerts] == ["satellite-drift:GTO1"]
    assert alerts[0].severity == "WARNING"
    assert "-14.3%" in alerts[0].detail
