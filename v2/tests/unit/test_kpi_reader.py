"""Tests for argia.kpi.reader."""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock

import pytest

from argia.kpi.reader import (
    ARGIA_TAB_NAME,
    DayBundle,
    InverterRow,
    _date_window_utc,
    _parse_timestamp,
    filter_to_date,
    parse_rows,
    read_day_bundle,
)
from argia.core.time_utils import MX_TZ, UTC


# ============================================================
# _parse_timestamp
# ============================================================


class TestParseTimestamp:
    def test_iso_z_format(self):
        ts = _parse_timestamp("2026-05-14T18:00:00Z")
        assert ts is not None
        assert ts.tzinfo is not None
        assert ts.year == 2026 and ts.hour == 18

    def test_iso_with_offset(self):
        ts = _parse_timestamp("2026-05-14T12:00:00-06:00")
        assert ts is not None
        # 12:00 -06:00 = 18:00 UTC
        assert ts.hour == 18

    def test_iso_no_tz_assumed_utc(self):
        ts = _parse_timestamp("2026-05-14T18:00:00")
        assert ts is not None
        # Naive ISO is assumed UTC
        assert ts.hour == 18
        assert ts.tzinfo is not None

    def test_none_returns_none(self):
        assert _parse_timestamp(None) is None

    def test_empty_returns_none(self):
        assert _parse_timestamp("") is None

    def test_garbage_returns_none(self):
        assert _parse_timestamp("not a date") is None
        assert _parse_timestamp("2026-99-99") is None


# ============================================================
# Row parsing
# ============================================================


def _make_cells(
    timestamp="2026-05-14T18:00:00Z",
    timestamp_mx="2026-05-14 12:00:00",
    vendor="GROWATT",
    plant_key="QRO1",
    inverter_sn="SN1",
    inverter_label="Inverter 1",
    status="1",
    power_w="25000",
    etoday_kwh="120.5",
    temperature_c="42",
    fault_code="0",
    irradiance_wm2="850",
    irradiance_kwh_m2_5m="0.07",
    cloud_cover_pct="15",
    ambient_temp_c="",
):
    """Build a 15-cell raw sheet row in Telemetry_Argia order."""
    return [
        timestamp, timestamp_mx, vendor, plant_key, inverter_sn, inverter_label,
        status, power_w, etoday_kwh, temperature_c, fault_code,
        irradiance_wm2, irradiance_kwh_m2_5m, cloud_cover_pct, ambient_temp_c,
    ]


class TestParseRows:
    def test_parse_single_row(self):
        rows = parse_rows([_make_cells()])
        assert len(rows) == 1
        r = rows[0]
        assert r.plant_key == "QRO1"
        assert r.inverter_sn == "SN1"
        assert r.power_w == 25000
        assert r.etoday_kwh == 120.5
        assert r.irradiance_wm2 == 850
        assert r.status == 1

    def test_skips_header_row(self):
        header = ["timestamp_utc", "timestamp_mx", "vendor", "plant_key",
                  "inverter_sn", "inverter_label", "status", "power_w",
                  "etoday_kwh", "temperature_c", "fault_code",
                  "irradiance_wm2", "irradiance_kwh_m2_5m",
                  "cloud_cover_pct", "ambient_temp_c"]
        rows = parse_rows([header, _make_cells()])
        assert len(rows) == 1

    def test_skips_row_missing_timestamp(self):
        rows = parse_rows([_make_cells(timestamp="")])
        assert len(rows) == 0

    def test_skips_row_missing_plant_key(self):
        rows = parse_rows([_make_cells(plant_key="")])
        assert len(rows) == 0

    def test_skips_row_missing_sn(self):
        rows = parse_rows([_make_cells(inverter_sn="")])
        assert len(rows) == 0

    def test_handles_short_row(self):
        """Trailing-empty cells often get dropped by sheets — must tolerate."""
        cells = _make_cells()
        short = cells[:11]  # cut off irradiance + cloud + ambient
        rows = parse_rows([short])
        assert len(rows) == 1
        assert rows[0].irradiance_wm2 is None
        assert rows[0].cloud_cover_pct is None

    def test_status_default_when_missing(self):
        """Garbage status defaults to 1 (online), not 3."""
        rows = parse_rows([_make_cells(status="garbage")])
        assert rows[0].status == 1

    def test_offline_status_parsed(self):
        rows = parse_rows([_make_cells(status="3")])
        assert rows[0].status == 3

    def test_sn_normalized(self):
        """SN whitespace stripped, uppercased."""
        rows = parse_rows([_make_cells(inverter_sn="  abc-123  ")])
        assert rows[0].inverter_sn == "ABC-123"

    def test_garbage_numerics_become_none(self):
        rows = parse_rows([_make_cells(
            power_w="garbage", etoday_kwh="x", irradiance_wm2="",
        )])
        assert rows[0].power_w is None
        assert rows[0].etoday_kwh is None
        assert rows[0].irradiance_wm2 is None


# ============================================================
# Date window
# ============================================================


class TestDateWindow:
    def test_window_is_24h(self):
        start, end = _date_window_utc("2026-05-14")
        assert (end - start) == dt.timedelta(days=1)

    def test_mx_midnight_is_06_utc(self):
        """MX is UTC-6 in non-DST. 00:00 MX = 06:00 UTC."""
        start, end = _date_window_utc("2026-05-14", site_tz=MX_TZ)
        assert start.tzinfo is not None
        # Mexico City switched to no-DST in 2022, so always UTC-6
        assert start.hour == 6
        assert end.hour == 6

    def test_invalid_date_raises(self):
        with pytest.raises(ValueError):
            _date_window_utc("not-a-date")


class TestFilterToDate:
    def _r(self, ts):
        return InverterRow(
            timestamp_utc=ts, plant_key="P", inverter_sn="S",
            inverter_label="", vendor="", status=1,
            power_w=None, etoday_kwh=None, temperature_c=None,
            fault_code="", irradiance_wm2=None, irradiance_kwh_m2_5m=None,
            cloud_cover_pct=None, ambient_temp_c=None,
        )

    def test_keeps_midday_row(self):
        # 18:00 UTC on 2026-05-14 = 12:00 MX (mid-day)
        row = self._r(dt.datetime(2026, 5, 14, 18, 0, tzinfo=UTC))
        result = filter_to_date([row], "2026-05-14")
        assert len(result) == 1

    def test_excludes_previous_day(self):
        # 05:59 UTC = 23:59 MX previous day
        row = self._r(dt.datetime(2026, 5, 14, 5, 59, tzinfo=UTC))
        result = filter_to_date([row], "2026-05-14")
        assert len(result) == 0

    def test_excludes_next_day(self):
        # 06:00 UTC next day = 00:00 MX next day — NOT in 2026-05-14
        row = self._r(dt.datetime(2026, 5, 15, 6, 0, tzinfo=UTC))
        result = filter_to_date([row], "2026-05-14")
        assert len(result) == 0

    def test_includes_morning_local(self):
        # 06:01 UTC = 00:01 MX same day
        row = self._r(dt.datetime(2026, 5, 14, 6, 1, tzinfo=UTC))
        result = filter_to_date([row], "2026-05-14")
        assert len(result) == 1


# ============================================================
# DayBundle indexing
# ============================================================


class TestDayBundle:
    def _r(self, plant_key, sn, hour):
        return InverterRow(
            timestamp_utc=dt.datetime(2026, 5, 14, hour, 0, tzinfo=UTC),
            plant_key=plant_key, inverter_sn=sn, inverter_label="",
            vendor="", status=1,
            power_w=None, etoday_kwh=None, temperature_c=None,
            fault_code="", irradiance_wm2=None, irradiance_kwh_m2_5m=None,
            cloud_cover_pct=None, ambient_temp_c=None,
        )

    def test_empty_bundle(self):
        b = DayBundle(date_iso="2026-05-14")
        assert b.plant_keys() == []
        assert b.rows_for_plant("X") == []
        assert b.inverter_sns_for_plant("X") == []

    def test_partition_by_plant(self):
        rows = (
            self._r("A", "S1", 10),
            self._r("A", "S2", 10),
            self._r("B", "S3", 10),
        )
        b = DayBundle(date_iso="2026-05-14", rows=rows)
        assert b.plant_keys() == ["A", "B"]
        assert len(b.rows_for_plant("A")) == 2
        assert len(b.rows_for_plant("B")) == 1

    def test_rows_sorted_by_timestamp(self):
        rows = (
            self._r("A", "S1", 14),
            self._r("A", "S1", 10),
            self._r("A", "S1", 12),
        )
        b = DayBundle(date_iso="2026-05-14", rows=rows)
        ts_list = [r.timestamp_utc for r in b.rows_for_plant("A")]
        assert ts_list == sorted(ts_list)

    def test_unique_inverter_sns(self):
        rows = (
            self._r("A", "S1", 10),
            self._r("A", "S1", 11),
            self._r("A", "S2", 10),
        )
        b = DayBundle(date_iso="2026-05-14", rows=rows)
        assert sorted(b.inverter_sns_for_plant("A")) == ["S1", "S2"]

    def test_rows_for_inverter(self):
        rows = (
            self._r("A", "S1", 10),
            self._r("A", "S2", 10),
            self._r("A", "S1", 11),
        )
        b = DayBundle(date_iso="2026-05-14", rows=rows)
        s1_rows = b.rows_for_inverter("A", "S1")
        assert len(s1_rows) == 2
        assert all(r.inverter_sn == "S1" for r in s1_rows)


# ============================================================
# read_day_bundle (integration with mock sheets)
# ============================================================


class TestReadDayBundle:
    def _sheets_with(self, raw_rows):
        sheets = MagicMock()
        sheets.read_range.return_value = raw_rows
        return sheets

    def test_returns_empty_when_sheet_empty(self):
        b = read_day_bundle(self._sheets_with([]), "2026-05-14")
        assert len(b.rows) == 0

    def test_returns_empty_when_sheet_error(self):
        sheets = MagicMock()
        sheets.read_range.side_effect = Exception("tab missing")
        b = read_day_bundle(sheets, "2026-05-14")
        assert len(b.rows) == 0

    def test_filters_to_requested_date(self):
        # Two rows on different days
        in_day = _make_cells(timestamp="2026-05-14T18:00:00Z")
        out_of_day = _make_cells(timestamp="2026-05-13T18:00:00Z",
                                 inverter_sn="SN2")
        b = read_day_bundle(self._sheets_with([in_day, out_of_day]), "2026-05-14")
        assert len(b.rows) == 1
        assert b.rows[0].inverter_sn == "SN1"

    def test_reads_correct_tab(self):
        sheets = self._sheets_with([])
        read_day_bundle(sheets, "2026-05-14")
        sheets.read_range.assert_called_once_with(ARGIA_TAB_NAME, "A1:O")
