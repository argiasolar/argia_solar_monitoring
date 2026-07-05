"""Tests: watchdog dead-man's switch.

Contract: fail loudly when data did NOT arrive (KPI missing, telemetry
stale, Pi feed stale), stay silent when it did, never spam at night, and
never write anything in dry-run or on success.
"""

import datetime as dt
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

from argia.core.sheets import SheetsClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
W = importlib.import_module("watchdog")

NOW_MX = dt.datetime(2026, 7, 6, 7, 15)          # 07:15 MX, in window
NIGHT_MX = dt.datetime(2026, 7, 6, 23, 30)       # outside window


def kpi_rows(date_iso, n):
    return [{"date_iso": date_iso, "plant_key": f"P{i}"} for i in range(n)]


class TestKpiCheck:
    def test_all_plants_stamped_is_ok(self):
        assert W.check_kpi_yesterday(
            kpi_rows("2026-07-05", 6), NOW_MX, min_plants=4) is None

    def test_missing_yesterday_fails(self):
        f = W.check_kpi_yesterday(
            kpi_rows("2026-07-04", 6), NOW_MX, min_plants=4)
        assert f and f["severity"] == "CRITICAL"
        assert "2026-07-05" in f["detail"]

    def test_partial_stamping_below_threshold_fails(self):
        f = W.check_kpi_yesterday(
            kpi_rows("2026-07-05", 2), NOW_MX, min_plants=4)
        assert f and "2 plant rows" in f["detail"]

    def test_datetime_cells_from_sheets_accepted(self):
        rows = [{"date_iso": dt.datetime(2026, 7, 5, 0, 0),
                 "plant_key": f"P{i}"} for i in range(4)]
        assert W.check_kpi_yesterday(rows, NOW_MX, min_plants=4) is None


class TestFreshness:
    def test_fresh_data_is_ok(self):
        newest = NOW_MX - dt.timedelta(minutes=20)
        assert W.check_freshness("t", newest, NOW_MX, 90, "x") is None

    def test_stale_data_fails_with_age_in_detail(self):
        newest = NOW_MX - dt.timedelta(minutes=200)
        f = W.check_freshness("t", newest, NOW_MX, 90, "v2 telemetry")
        assert f and "200 min old" in f["detail"]

    def test_no_timestamps_at_all_fails(self):
        f = W.check_freshness("t", None, NOW_MX, 90, "x")
        assert f and "no parseable timestamps" in f["detail"]

    def test_newest_ts_parses_strings_and_datetimes(self):
        rows = [{"ts": "2026-07-06 07:00:00"},
                {"ts": dt.datetime(2026, 7, 6, 7, 10)},
                {"ts": "garbage"}, {"ts": None}]
        assert W.newest_ts(rows, "ts") == dt.datetime(2026, 7, 6, 7, 10)

    def test_collection_window(self):
        assert W.in_collection_window(NOW_MX) is True
        assert W.in_collection_window(NIGHT_MX) is False


def _v2(kpi, tele_newest):
    c = MagicMock(spec=SheetsClient)
    tables = {"KPI_Daily": kpi,
              "Telemetry_Argia": [{"timestamp_mx": tele_newest}]}
    c.read_table.side_effect = lambda tab, rng="A1:Z": tables[tab]
    return c


class TestRun:
    def test_all_ok_writes_nothing_exit_0(self):
        v2 = _v2(kpi_rows("2026-07-05", 6),
                 NOW_MX - dt.timedelta(minutes=10))
        rc = W.run(v2, None, apply=True, max_age_min=90,
                   min_kpi_plants=4, now_mx=NOW_MX)
        assert rc == 0
        v2.append_rows.assert_not_called()
        v2.ensure_tab.assert_not_called()

    def test_failure_writes_outbox_row_and_exits_1(self):
        v2 = _v2(kpi_rows("2026-07-04", 6),          # yesterday missing
                 NOW_MX - dt.timedelta(minutes=10))
        rc = W.run(v2, None, apply=True, max_age_min=90,
                   min_kpi_plants=4, now_mx=NOW_MX)
        assert rc == 1
        (tab, rows), _ = v2.append_rows.call_args
        assert tab == W.WATCHDOG_TAB
        assert rows[0][1] == "kpi_yesterday"
        assert rows[0][-1] == ""                      # notifier claim column

    def test_dry_run_never_writes_even_on_failure(self):
        v2 = _v2(kpi_rows("2026-07-04", 6),
                 NOW_MX - dt.timedelta(minutes=500))
        rc = W.run(v2, None, apply=False, max_age_min=90,
                   min_kpi_plants=4, now_mx=NOW_MX)
        assert rc == 1
        v2.append_rows.assert_not_called()

    def test_night_run_skips_freshness_but_keeps_kpi(self):
        v2 = _v2(kpi_rows("2026-07-05", 6),
                 NIGHT_MX - dt.timedelta(minutes=500))   # stale but night
        rc = W.run(v2, None, apply=True, max_age_min=90,
                   min_kpi_plants=4, now_mx=NIGHT_MX)
        assert rc == 0                                   # silence is normal

    def test_pi_check_runs_when_v1_client_given(self):
        v2 = _v2(kpi_rows("2026-07-05", 6),
                 NOW_MX - dt.timedelta(minutes=10))
        v1 = MagicMock(spec=SheetsClient)
        stale = (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                 - dt.timedelta(minutes=300))
        v1.read_table.return_value = [
            {"ExtractedAtUTC": stale.strftime("%Y-%m-%d %H:%M:%S")}]
        rc = W.run(v2, v1, apply=True, max_age_min=90,
                   min_kpi_plants=4, now_mx=NOW_MX)
        assert rc == 1
        (tab, rows), _ = v2.append_rows.call_args
        assert rows[0][1] == "pi_v1_feed"
        assert "Pi / v1 collector" in rows[0][3]


class TestSerialFormatRegression20260705:
    """The live API false-alarm: UNFORMATTED_VALUE returns datetimes as
    SERIAL floats; the watchdog reported 'no parseable timestamps at all'
    and '0 KPI rows' on a perfectly healthy sheet. The watchdog must read
    exactly what the API sends."""

    def _serial(self, d):
        from argia.core.cells import GOOGLE_EPOCH
        return (d - GOOGLE_EPOCH) / dt.timedelta(days=1)

    def test_kpi_check_accepts_serial_dates(self):
        serial = self._serial(dt.datetime(2026, 7, 5))
        rows = [{"date_iso": serial, "plant_key": f"P{i}"} for i in range(6)]
        assert W.check_kpi_yesterday(rows, NOW_MX, min_plants=4) is None

    def test_newest_ts_accepts_serial_timestamps(self):
        rows = [{"timestamp_mx": self._serial(dt.datetime(2026, 7, 6, 7, 0))},
                {"timestamp_mx": self._serial(dt.datetime(2026, 7, 6, 7, 10))}]
        assert W.newest_ts(rows, "timestamp_mx") == \
            dt.datetime(2026, 7, 6, 7, 10)

    def test_healthy_serial_sheet_end_to_end_is_all_ok(self):
        serial_kpi = self._serial(dt.datetime(2026, 7, 5))
        v2 = MagicMock(spec=SheetsClient)
        tables = {
            "KPI_Daily": [{"date_iso": serial_kpi, "plant_key": f"P{i}"}
                          for i in range(6)],
            "Telemetry_Argia": [{"timestamp_mx":
                self._serial(NOW_MX - dt.timedelta(minutes=10))}],
        }
        v2.read_table.side_effect = lambda tab, rng="A1:Z": tables[tab]
        rc = W.run(v2, None, apply=True, max_age_min=90,
                   min_kpi_plants=4, now_mx=NOW_MX)
        assert rc == 0
        v2.append_rows.assert_not_called()
