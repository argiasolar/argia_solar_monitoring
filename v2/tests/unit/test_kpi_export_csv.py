"""Unit tests for scripts/kpi_export_csv.py (pure logic, no Google imports)."""
import datetime as dt

from scripts.kpi_export_csv import normalize_date_iso, window_rows

TODAY = dt.date(2026, 8, 25)
HDR = ["date_iso", "plant_key", "energy_kwh"]


def test_normalize_iso_passthrough():
    assert normalize_date_iso("2026-08-24") == "2026-08-24"


def test_normalize_us_formatted():
    # Sheets reads date cells back FORMATTED (v95 lesson)
    assert normalize_date_iso("8/24/2026") == "2026-08-24"


def test_normalize_garbage_and_blank():
    assert normalize_date_iso("") is None
    assert normalize_date_iso("not-a-date") is None
    assert normalize_date_iso(None) is None


def test_window_filters_and_normalizes():
    values = [
        HDR,
        ["2026-08-24", "GTO1", "3100.5"],          # in window
        ["8/23/2026", "SLP1", "800.0"],            # formatted date, in window
        ["2026-07-01", "GTO1", "2900.0"],          # too old (20d window)
        ["", "GTO1", "1.0"],                       # blank date -> dropped
        ["2026-08-24", "", "1.0"],                 # blank plant -> dropped
        ["garbage", "GTO1", "1.0"],                # bad date -> dropped
    ]
    header, rows = window_rows(values, days=20, today=TODAY)
    assert header == HDR
    assert [(r[0], r[1]) for r in rows] == [
        ("2026-08-24", "GTO1"), ("2026-08-23", "SLP1"),
    ]


def test_window_empty_input():
    assert window_rows([], 20, TODAY) == ([], [])


def test_window_keeps_extra_columns():
    values = [HDR + ["data_class"], ["2026-08-25", "NL1", "5", "full"]]
    _, rows = window_rows(values, 20, TODAY)
    assert rows[0][3] == "full"


def test_normalize_more_locale_formats():
    # es-MX / cs-CZ locales and datetime-carrying cells (live failure 2026-08-25:
    # the first Actions run matched zero rows — every date cell was formatted)
    assert normalize_date_iso("24/08/2026") == "2026-08-24"
    assert normalize_date_iso("24.08.2026") == "2026-08-24"
    assert normalize_date_iso("2026/08/24") == "2026-08-24"
    assert normalize_date_iso("2026-08-24 0:00:00") == "2026-08-24"
    assert normalize_date_iso("2026-08-24T05:00:00Z") == "2026-08-24"
