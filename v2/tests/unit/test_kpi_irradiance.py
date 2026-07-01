"""Tests for argia.kpi.irradiance."""

from __future__ import annotations

import datetime as dt

import pytest

from argia.kpi.irradiance import (
    MAX_PLAUSIBLE_WM2,
    MIN_SHINEMASTER_SAMPLES,
    IrradianceDay,
    IrradianceSource,
    _avg_daytime_cloud,
    _clear_sky_kwh_m2_simple,
    _dedupe_by_timestamp,
    _trapezoidal_integrate_wm2_to_kwh_m2,
    daily_irradiance_for_plant,
    estimate_irradiance_from_clouds,
    integrate_irradiance_kwh_m2,
)
from argia.kpi.reader import InverterRow
from argia.core.time_utils import UTC


def _row(hour, irradiance_wm2=None, cloud_cover_pct=None, power_w=None,
         sn="SN1", minute=0):
    return InverterRow(
        timestamp_utc=dt.datetime(2026, 5, 14, hour, minute, tzinfo=UTC),
        plant_key="P", inverter_sn=sn, inverter_label="", vendor="",
        status=1, power_w=power_w, etoday_kwh=None, temperature_c=None,
        fault_code="", irradiance_wm2=irradiance_wm2,
        irradiance_kwh_m2_5m=None, cloud_cover_pct=cloud_cover_pct,
        ambient_temp_c=None,
    )


# ============================================================
# Dedupe
# ============================================================


class TestDedupe:
    def test_drops_none_readings(self):
        rows = [_row(10, None), _row(11, 500.0)]
        result = _dedupe_by_timestamp(rows)
        assert len(result) == 1
        assert result[0][1] == 500.0

    def test_clamps_implausible_high(self):
        """Sensor spikes (e.g. 1420 W/m²) are CLAMPED to MAX_PLAUSIBLE_WM2,
        not dropped — dropping would remove the time point and blow a hole
        in the daily integral."""
        rows = [_row(10, MAX_PLAUSIBLE_WM2 + 220.0), _row(11, 500.0)]
        result = _dedupe_by_timestamp(rows)
        assert len(result) == 2  # spike kept, not dropped
        assert result[0][1] == MAX_PLAUSIBLE_WM2  # clamped
        assert result[1][1] == 500.0

    def test_drops_negative(self):
        rows = [_row(10, -50.0), _row(11, 500.0)]
        result = _dedupe_by_timestamp(rows)
        assert len(result) == 1

    def test_dedupes_per_timestamp(self):
        """Multiple inverters at same timestamp → one irradiance value."""
        rows = [_row(10, 500.0, sn="A"), _row(10, 500.0, sn="B"),
                _row(11, 600.0, sn="A")]
        result = _dedupe_by_timestamp(rows)
        assert len(result) == 2

    def test_sorts_by_timestamp(self):
        rows = [_row(15, 600.0), _row(10, 400.0), _row(12, 500.0)]
        result = _dedupe_by_timestamp(rows)
        timestamps = [t for t, _ in result]
        assert timestamps == sorted(timestamps)


# ============================================================
# Trapezoidal integration
# ============================================================


class TestTrapezoidal:
    def test_two_points_one_hour_apart(self):
        """Two points at 500 W/m² one hour apart → 0.5 kWh/m²."""
        points = [
            (dt.datetime(2026, 5, 14, 10, 0, tzinfo=UTC), 500.0),
            (dt.datetime(2026, 5, 14, 11, 0, tzinfo=UTC), 500.0),
        ]
        result = _trapezoidal_integrate_wm2_to_kwh_m2(points)
        assert result == 0.5

    def test_triangle_shape(self):
        """Linear ramp 0 → 1000 → 0 over 2 hours. Integral = (1000 × 2)/2 / 1000 = 1.0"""
        points = [
            (dt.datetime(2026, 5, 14, 10, 0, tzinfo=UTC), 0.0),
            (dt.datetime(2026, 5, 14, 11, 0, tzinfo=UTC), 1000.0),
            (dt.datetime(2026, 5, 14, 12, 0, tzinfo=UTC), 0.0),
        ]
        result = _trapezoidal_integrate_wm2_to_kwh_m2(points)
        assert result == 1.0

    def test_short_series_returns_none(self):
        assert _trapezoidal_integrate_wm2_to_kwh_m2([]) is None
        assert _trapezoidal_integrate_wm2_to_kwh_m2(
            [(dt.datetime(2026, 5, 14, 10, 0, tzinfo=UTC), 500.0)],
        ) is None

    def test_skips_large_gap(self):
        """Gap > max_gap_sec must NOT be interpolated through."""
        points = [
            (dt.datetime(2026, 5, 14, 10, 0, tzinfo=UTC), 500.0),
            # 2-hour gap — exceeds explicit 30-min max
            (dt.datetime(2026, 5, 14, 12, 0, tzinfo=UTC), 500.0),
        ]
        result = _trapezoidal_integrate_wm2_to_kwh_m2(points, max_gap_sec=1800)
        # The gap is skipped entirely; 0 contribution
        assert result == 0.0

    def test_two_hour_gap_bridged_by_default(self):
        """The ShineMaster reports ~every 2h; the default cap must bridge it."""
        points = [
            (dt.datetime(2026, 5, 14, 10, 0, tzinfo=UTC), 400.0),
            (dt.datetime(2026, 5, 14, 12, 0, tzinfo=UTC), 600.0),
        ]
        # (400+600)/2 * 2h / 1000 = 1.0
        assert _trapezoidal_integrate_wm2_to_kwh_m2(points) == pytest.approx(1.0)

    def test_gap_beyond_three_hours_skipped(self):
        """A >3h gap is a real outage — not interpolated."""
        points = [
            (dt.datetime(2026, 5, 14, 10, 0, tzinfo=UTC), 400.0),
            (dt.datetime(2026, 5, 14, 14, 0, tzinfo=UTC), 600.0),
        ]
        assert _trapezoidal_integrate_wm2_to_kwh_m2(points) == 0.0


# ============================================================
# integrate_irradiance_kwh_m2 (ShineMaster path)
# ============================================================


class TestShineMasterPath:
    def _sunny_day_rows(self, n=20):
        """Generate n rows with a smooth bell-curve irradiance."""
        import math
        out = []
        # Sample every 30 min from 06:00 to 18:00
        for i in range(n):
            h_frac = 6 + (i / (n - 1)) * 12  # 6.0 to 18.0
            hour = int(h_frac)
            minute = int((h_frac - hour) * 60)
            # Bell curve peaking at 12:00 (noon)
            x = (h_frac - 12) / 4.0
            irr = max(0, 950 * math.exp(-x * x))
            out.append(_row(hour, irr, minute=minute))
        return out

    def test_sunny_day_kwh_m2_realistic(self):
        rows = self._sunny_day_rows(n=24)
        result = integrate_irradiance_kwh_m2(rows)
        assert result.source == IrradianceSource.SHINEMASTER
        # Mexican sunny day = 5-7 kWh/m² typical
        assert 4 < result.kwh_m2 < 8

    def test_below_threshold_returns_none(self):
        """Fewer than MIN_SHINEMASTER_SAMPLES samples → NONE source."""
        rows = [_row(10, 500.0), _row(11, 600.0)]
        result = integrate_irradiance_kwh_m2(rows)
        assert result.source == IrradianceSource.NONE
        assert result.kwh_m2 is None
        assert result.samples_used == 2

    def test_zero_integral_returns_none(self):
        """Pathological case — all samples are 0. Don't report kwh_m2=0
        because that would zero out PR. Treat as no data."""
        # Build N samples at 5-min spacing starting 10:00, all 0 W/m²
        rows = []
        for i in range(MIN_SHINEMASTER_SAMPLES + 5):
            hour = 10 + (i * 5) // 60
            minute = (i * 5) % 60
            rows.append(_row(hour, 0.0, minute=minute))
        result = integrate_irradiance_kwh_m2(rows)
        # We expect this to return source=NONE because 0 integral is
        # treated as unusable
        assert result.kwh_m2 is None

    def _bursty_day_rows(self):
        """Mimic the real ShineMaster shape: distinct readings ~2h apart, each
        repeated a few times a minute apart. This is the pattern that collapsed
        the old 1h-gap integrator to ~0."""
        out = []
        arch = [(6, 30), (8, 300), (10, 650), (12, 950),
                (14, 780), (16, 380), (18, 40)]
        for hour, wm2 in arch:
            for m in range(4):  # 4 readings a minute apart -> distinct timestamps
                out.append(_row(hour, float(wm2), minute=m))
        return out

    def test_bursty_shinemaster_day_integrates_realistically(self):
        """A sunny bursty day must land in the realistic 4-8 kWh/m² band —
        not collapse to ~0 the way the 1h gap cap did."""
        result = integrate_irradiance_kwh_m2(self._bursty_day_rows())
        assert result.source == IrradianceSource.SHINEMASTER
        assert 4 < result.kwh_m2 < 8

    def test_old_1h_cap_would_collapse_bursty_day(self):
        """Regression guard: the SAME bursty day under a 1h cap integrates to
        near-zero (every ~2h interval skipped) — proving the widened bridge is
        what fixes it."""
        points = _dedupe_by_timestamp(self._bursty_day_rows())
        collapsed = _trapezoidal_integrate_wm2_to_kwh_m2(points, max_gap_sec=3600)
        assert collapsed < 0.5  # vs ~6 kWh/m² with the correct bridge


# ============================================================
# Clear-sky model
# ============================================================


class TestClearSkyModel:
    def test_summer_solstice_mexico(self):
        """Mexico City (lat 19.4) at June 21. Expect ~7-9 kWh/m² clear sky."""
        result = _clear_sky_kwh_m2_simple(
            lat=19.4, date_iso="2026-06-21", cloud_fraction=0.0,
        )
        assert result is not None
        # Heuristic model: declination at solstice ≈ 23.45, lat 19.4
        # → noon elevation factor ≈ 0.998 → ~9 kWh/m²
        assert 8.0 < result < 10.0

    def test_full_overcast_low(self):
        """100% overcast → derate × 0.3 = 30% of clear."""
        clear = _clear_sky_kwh_m2_simple(
            lat=19.4, date_iso="2026-06-21", cloud_fraction=0.0,
        )
        overcast = _clear_sky_kwh_m2_simple(
            lat=19.4, date_iso="2026-06-21", cloud_fraction=1.0,
        )
        assert overcast == pytest.approx(clear * 0.3, rel=0.01)

    def test_winter_lower_than_summer(self):
        """December < June for Mexican latitude."""
        june = _clear_sky_kwh_m2_simple(19.4, "2026-06-21", 0.0)
        dec = _clear_sky_kwh_m2_simple(19.4, "2026-12-21", 0.0)
        assert dec < june

    def test_polar_night_returns_zero(self):
        """Sun below horizon all day at high northern latitude in December."""
        result = _clear_sky_kwh_m2_simple(
            lat=80.0, date_iso="2026-12-21", cloud_fraction=0.0,
        )
        assert result == 0.0

    def test_invalid_lat_returns_none(self):
        assert _clear_sky_kwh_m2_simple(91, "2026-06-21", 0.0) is None
        assert _clear_sky_kwh_m2_simple(-91, "2026-06-21", 0.0) is None

    def test_cloud_clipped_to_range(self):
        """Bad cloud values get clipped, not crashed."""
        result = _clear_sky_kwh_m2_simple(19.4, "2026-06-21", 1.5)
        # Should treat as 1.0
        assert result == _clear_sky_kwh_m2_simple(19.4, "2026-06-21", 1.0)


# ============================================================
# Cloud cover averaging
# ============================================================


class TestAvgDaytimeCloud:
    def test_filters_to_daylight_when_possible(self):
        """If some rows have daylight signals (irradiance>50 or power>0),
        use those rows' cloud values; ignore nighttime."""
        rows = [
            _row(3, irradiance_wm2=0, cloud_cover_pct=80, power_w=0),  # night
            _row(12, irradiance_wm2=900, cloud_cover_pct=20, power_w=25000),
            _row(13, irradiance_wm2=850, cloud_cover_pct=30, power_w=24000),
        ]
        result = _avg_daytime_cloud(rows)
        # (20 + 30) / 2 / 100 = 0.25
        assert result == pytest.approx(0.25)

    def test_fallback_when_no_daylight_signal(self):
        """No row has irradiance or power → use all cloud values."""
        rows = [
            _row(8, irradiance_wm2=0, cloud_cover_pct=50, power_w=0),
            _row(14, irradiance_wm2=None, cloud_cover_pct=70, power_w=None),
        ]
        result = _avg_daytime_cloud(rows)
        # (50 + 70)/2/100 = 0.60
        assert result == pytest.approx(0.60)

    def test_no_cloud_data_returns_none(self):
        rows = [_row(12, irradiance_wm2=900, cloud_cover_pct=None)]
        assert _avg_daytime_cloud(rows) is None

    def test_empty_returns_none(self):
        assert _avg_daytime_cloud([]) is None


# ============================================================
# Cloud-based fallback
# ============================================================


class TestEstimateFromClouds:
    def test_uses_clouds_when_no_lat_fails(self):
        rows = [_row(12, cloud_cover_pct=30)]
        result = estimate_irradiance_from_clouds(
            rows, lat=None, date_iso="2026-05-14",
        )
        assert result.source == IrradianceSource.NONE

    def test_uses_clouds_with_lat(self):
        rows = [_row(12, cloud_cover_pct=30, irradiance_wm2=900),
                _row(13, cloud_cover_pct=20, irradiance_wm2=850)]
        result = estimate_irradiance_from_clouds(
            rows, lat=19.4, date_iso="2026-05-14",
        )
        assert result.source == IrradianceSource.CLOUD_COVER_MODEL
        assert result.kwh_m2 is not None
        assert result.kwh_m2 > 0


# ============================================================
# Hybrid entry point
# ============================================================


class TestHybridChoice:
    def test_prefers_shinemaster_when_enough_samples(self):
        """With enough samples, ShineMaster path always wins."""
        import math
        rows = []
        for i in range(MIN_SHINEMASTER_SAMPLES + 5):
            x = (i - 10) / 4.0
            irr = max(0, 950 * math.exp(-x * x))
            rows.append(_row(8 + i // 2, irr, minute=(i % 2) * 30,
                             cloud_cover_pct=30))
        result = daily_irradiance_for_plant(rows, lat=19.4, date_iso="2026-05-14")
        assert result.source == IrradianceSource.SHINEMASTER

    def test_falls_back_when_shinemaster_sparse(self):
        """Few irradiance samples but plant has cloud_cover + lat → use fallback."""
        rows = [_row(12, irradiance_wm2=900, cloud_cover_pct=30)]  # 1 sample
        result = daily_irradiance_for_plant(rows, lat=19.4, date_iso="2026-05-14")
        assert result.source == IrradianceSource.CLOUD_COVER_MODEL

    def test_returns_none_when_all_missing(self):
        rows = [_row(12)]  # no irradiance, no clouds
        result = daily_irradiance_for_plant(rows, lat=19.4, date_iso="2026-05-14")
        assert result.source == IrradianceSource.NONE
        assert result.kwh_m2 is None
