import datetime as dt
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from argia.core.sheets import SheetsClient
from argia.report import dashboard as D

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
R = importlib.import_module("dashboard_update")


# --- coercion ---------------------------------------------------------------

def test_coerce_ts_passthrough_datetime():
    x = dt.datetime(2026, 7, 2, 10, 5, 2)
    assert R.coerce_ts(x) is x


def test_coerce_ts_iso_string():
    assert R.coerce_ts("2026-07-02 10:05:02") == dt.datetime(2026, 7, 2, 10, 5, 2)


def test_coerce_ts_t_separator_and_offset():
    assert R.coerce_ts("2026-07-02T10:05:02+00:00") == dt.datetime(2026, 7, 2, 10, 5, 2)


def test_coerce_ts_google_serial():
    serial = (dt.datetime(2026, 7, 2, 12, 0) - R.GOOGLE_EPOCH).total_seconds() / 86400
    assert R.coerce_ts(serial) == dt.datetime(2026, 7, 2, 12, 0)


def test_coerce_ts_garbage_is_none():
    assert R.coerce_ts("not a date") is None
    assert R.coerce_ts(None) is None


def test_coerce_date_from_iso_and_datetime():
    assert R.coerce_date("2026-07-02") == dt.date(2026, 7, 2)
    assert R.coerce_date(dt.datetime(2026, 7, 2, 9, 0)) == dt.date(2026, 7, 2)


# --- pure helpers -------------------------------------------------------------

def test_window_days_ordered_oldest_first():
    days = R.window_days(dt.date(2026, 7, 4), 3)
    assert days == [dt.date(2026, 7, 2), dt.date(2026, 7, 3), dt.date(2026, 7, 4)]


def test_kpi_expected_map():
    rows = [
        {"date_iso": "2026-07-02", "plant_key": "GTO1", "expected_kwh": 4978.0},
        {"date_iso": "2026-07-02", "plant_key": "SLP1", "expected_kwh": "919.4"},
        {"date_iso": "bad", "plant_key": "X", "expected_kwh": 1.0},
        {"date_iso": "2026-07-03", "plant_key": "GTO1", "expected_kwh": None},
    ]
    m = R.kpi_expected_map(rows)
    assert m[dt.date(2026, 7, 2)]["GTO1"] == 4978.0
    assert m[dt.date(2026, 7, 2)]["SLP1"] == pytest.approx(919.4)
    assert dt.date(2026, 7, 3) not in m


def test_col_letter():
    assert R._col_letter(1) == "A"
    assert R._col_letter(26) == "Z"
    assert R._col_letter(27) == "AA"


def test_matrix_serializes_datetime_and_none():
    rows = [{"a": dt.datetime(2026, 7, 2, 10, 0), "b": None, "c": 1.5}]
    m = R.to_matrix(["a", "b", "c"], rows)
    assert m[1] == ["2026-07-02 10:00:00", "", 1.5]


# --- rewrite_tab (mocked client, spec-locked) ---------------------------------

def _matrix():
    return [["h1", "h2"], [1, 2], [3, 4]]


def test_rewrite_tab_dry_run_touches_nothing():
    client = MagicMock(spec=SheetsClient)
    R.rewrite_tab(client, "Dashboard_Plant", _matrix(), apply=False)
    client.write_values.assert_not_called()
    client.delete_row_range.assert_not_called()
    client.ensure_tab.assert_not_called()


def test_rewrite_tab_apply_writes_and_trims():
    client = MagicMock(spec=SheetsClient)
    client.read_range.return_value = [["x"]] * 10
    R.rewrite_tab(client, "Dashboard_Plant", _matrix(), apply=True)
    client.write_values.assert_called_once_with(
        "Dashboard_Plant", "A1:B3", _matrix())
    client.delete_row_range.assert_called_once_with("Dashboard_Plant", 4, 10)


def test_rewrite_tab_apply_no_trim_when_growing():
    client = MagicMock(spec=SheetsClient)
    client.read_range.return_value = [["x"]]
    R.rewrite_tab(client, "Dashboard_Plant", _matrix(), apply=True)
    client.delete_row_range.assert_not_called()


# --- end-to-end run with a fully mocked sheet ----------------------------------

def _tables():
    day = "2026-07-02"
    plants = [{"plant_key": "GTO1", "customer": "Taigene",
               "kwp_dc": 818.33, "expected_factor": 0.75}]
    inverters = [
        {"plant_key": "GTO1", "inverter_sn": "A", "active": "TRUE",
         "in_service_today": "TRUE"},
        {"plant_key": "GTO1", "inverter_sn": "B", "active": "TRUE",
         "in_service_today": "TRUE"},
    ]
    kpi = [{"date_iso": day, "plant_key": "GTO1", "expected_kwh": 4978.0}]
    tele = []
    for sn, e9, e10 in (("A", 40, 140), ("B", 35, 130)):
        for hh, e in ((9, e9), (10, e10)):
            tele.append({
                "timestamp_mx": f"2026-07-02 {hh:02d}:05:00",
                "plant_key": "GTO1", "inverter_sn": sn, "inverter_label": sn,
                "status": 1, "fault_code": 0, "power_w": 50000,
                "etoday_kwh": e, "temperature_c": 45,
                "irradiance_wm2": 800, "irradiance_kwh_m2_5m": 0.066,
                "cloud_cover_pct": 10, "ambient_temp_c": 30,
                "module_temp_c": 50,
            })
    return {"Plants": plants, "Inverters": inverters,
            "KPI_Daily": kpi, "Telemetry_Argia": tele}


def _client(tables):
    client = MagicMock(spec=SheetsClient)
    client.read_table.side_effect = lambda tab, rng="A1:Z": tables[tab]
    client.read_range.return_value = [["x"]]
    return client


def test_run_dry_run_reads_but_never_writes():
    client = _client(_tables())
    rc = R.run(client, window=2, apply=False, today=dt.date(2026, 7, 2))
    assert rc == 0
    client.write_values.assert_not_called()
    client.delete_row_range.assert_not_called()


def test_run_apply_writes_both_tabs_with_correct_headers_and_energy():
    client = _client(_tables())
    rc = R.run(client, window=2, apply=True, today=dt.date(2026, 7, 2))
    assert rc == 0
    tabs = {c.args[0]: c.args[2] for c in client.write_values.call_args_list}
    assert set(tabs) == {"Dashboard_Inverter", "Dashboard_Plant"}
    assert tabs["Dashboard_Inverter"][0] == D.INVERTER_COLUMNS
    assert tabs["Dashboard_Plant"][0] == D.PLANT_COLUMNS
    prows = tabs["Dashboard_Plant"][1:]
    i_total = D.PLANT_COLUMNS.index("total_kwh")
    i_date = D.PLANT_COLUMNS.index("date_mx")
    day_total = sum(r[i_total] for r in prows if r[i_date] == "2026-07-02")
    assert day_total == pytest.approx(270.0)  # cumulative: final etoday 140+130


class TestAnchorHardening20260705:
    """Regression: NL1 read 32% because a same-day partial KPI row anchored
    the live day, cramming the full-day expected into elapsed buckets."""

    def test_partial_rows_never_anchor(self):
        rows = [{"date_iso": "2026-07-04", "plant_key": "NL1",
                 "expected_kwh": 3982.4, "data_class": "partial"}]
        m = R.kpi_expected_map(rows, today=dt.date(2026, 7, 5))
        assert m == {}

    def test_current_day_never_anchors_even_if_full(self):
        rows = [{"date_iso": "2026-07-05", "plant_key": "NL1",
                 "expected_kwh": 2247.6, "data_class": "full"}]
        m = R.kpi_expected_map(rows, today=dt.date(2026, 7, 5))
        assert m == {}

    def test_completed_full_day_still_anchors(self):
        rows = [{"date_iso": "2026-07-04", "plant_key": "NL1",
                 "expected_kwh": 3982.4, "data_class": "full"}]
        m = R.kpi_expected_map(rows, today=dt.date(2026, 7, 5))
        assert m[dt.date(2026, 7, 4)]["NL1"] == pytest.approx(3982.4)

    def test_missing_data_class_treated_as_anchorable_when_past(self):
        rows = [{"date_iso": "2026-07-03", "plant_key": "GTO1",
                 "expected_kwh": 3038.7}]
        m = R.kpi_expected_map(rows, today=dt.date(2026, 7, 5))
        assert m[dt.date(2026, 7, 3)]["GTO1"] == pytest.approx(3038.7)


def test_dashpub_default_output_is_outside_the_repo(monkeypatch):
    """2026-07-07: the default --out wrote dashboard.html into the repo
    working tree; on the Pi the untracked artifact tripped the deploy
    guard and three pushes sat undelivered. The default must live in
    ARGIA_LOG_DIR (or tmp), never the working tree."""
    import importlib
    monkeypatch.setenv("ARGIA_LOG_DIR", "/some/log/dir")
    import scripts.dashboard_html_publish as dp
    importlib.reload(dp)
    ap = dp.build_parser() if hasattr(dp, "build_parser") else None
    if ap is None:
        # parser built inside main; assert on source contract instead
        src = open(dp.__file__).read()
        assert 'default="dashboard.html"' not in src
        assert "ARGIA_LOG_DIR" in src and "tempfile.gettempdir()" in src
    else:
        assert ap.get_default("out") == "/some/log/dir/dashboard.html"


def test_update_reads_full_width():
    """v78 read-range regression: see test_dashboard_html counterpart."""
    import inspect
    import scripts.dashboard_update as U
    src = inspect.getsource(U)
    assert 'read_table("Plants", "A1:ZZ")' in src
    assert 'read_table("Inverters", "A1:Z")' in src
