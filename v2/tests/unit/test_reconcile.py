"""Tests for argia.kpi.reconcile and scripts/reconcile_daily.py.

The bulk exercises the PURE logic (no I/O). The final class is a read-only
smoke test of the CLI that proves it NEVER writes to either sheet.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from argia.core.sheets import SheetsClient
from argia.kpi.reconcile import (
    BUCKET_ENERGY,
    BUCKET_MISSING_V1,
    BUCKET_MISSING_V2,
    BUCKET_OK,
    BUCKET_PR,
    build_reconcile,
    classify,
    date_key,
    derive_pr,
    index_v1,
    index_v2,
    pct_diff,
    plant_key_norm,
    summarize,
)

SHEETS_EPOCH = dt.date(1899, 12, 30)


def _serial(d: dt.date) -> int:
    """Google Sheets serial for a date, computed the same way the code does."""
    return (d - SHEETS_EPOCH).days


# --------------------------------------------------------------------------
class TestDateKey:
    def test_serial_int(self):
        s = _serial(dt.date(2026, 6, 30))
        assert date_key(s) == "2026-06-30"

    def test_serial_float_with_time_fraction(self):
        # 46203.5 == noon of that day; date portion is what matters.
        s = _serial(dt.date(2026, 6, 27)) + 0.5
        assert date_key(s) == "2026-06-27"

    def test_serial_as_string(self):
        s = str(_serial(dt.date(2026, 1, 1)))
        assert date_key(s) == "2026-01-01"

    def test_datetime(self):
        assert date_key(dt.datetime(2026, 6, 30, 13, 51)) == "2026-06-30"

    def test_date(self):
        assert date_key(dt.date(2026, 6, 30)) == "2026-06-30"

    def test_iso_string(self):
        assert date_key("2026-06-30") == "2026-06-30"

    def test_iso_string_with_time(self):
        assert date_key("2026-06-30T13:51:00+00:00") == "2026-06-30"

    def test_us_slash_string(self):
        assert date_key("6/30/2026") == "2026-06-30"

    def test_us_slash_with_time(self):
        assert date_key("6/30/2026 13:51:00") == "2026-06-30"

    def test_none_and_blank(self):
        assert date_key(None) is None
        assert date_key("") is None
        assert date_key("   ") is None

    def test_garbage(self):
        assert date_key("not a date") is None

    def test_number_out_of_serial_range_is_not_a_date(self):
        # A 10-digit epoch must NOT be mistaken for a serial date.
        assert date_key(1700000000) is None
        assert date_key(5) is None


class TestPlantKeyNorm:
    def test_strips_and_uppercases(self):
        assert plant_key_norm("  gto1 ") == "GTO1"
        assert plant_key_norm("GTO1") == "GTO1"

    def test_none(self):
        assert plant_key_norm(None) == ""


class TestPctDiff:
    def test_normal(self):
        assert pct_diff(100.0, 110.0) == 10.0
        assert pct_diff(100.0, 90.0) == -10.0

    def test_both_zero_is_match(self):
        assert pct_diff(0.0, 0.0) == 0.0

    def test_v1_zero_v2_nonzero_is_undefined(self):
        assert pct_diff(0.0, 5.0) is None

    def test_missing(self):
        assert pct_diff(None, 5.0) is None
        assert pct_diff(5.0, None) is None


class TestDerivePr:
    def test_matches_v2_formula(self):
        # PR = E / (kwp * H)
        assert derive_pr(1000.0, 500.0, 4.0) == round(1000.0 / (500.0 * 4.0), 4)

    def test_zero_irradiance_returns_none(self):
        assert derive_pr(1000.0, 500.0, 0.0) is None

    def test_zero_kwp_returns_none(self):
        assert derive_pr(1000.0, 0.0, 4.0) is None

    def test_missing_input(self):
        assert derive_pr(None, 500.0, 4.0) is None
        assert derive_pr(1000.0, None, 4.0) is None
        assert derive_pr(1000.0, 500.0, None) is None


class TestClassify:
    TOL = 2.0

    def test_ok_energy_and_pr_close(self):
        b, within, _ = classify(1000, 1010, 1.0, 0.80, 0.80, 0.0, self.TOL)
        assert b == BUCKET_OK and within is True

    def test_energy_mismatch_over_tolerance(self):
        b, within, _ = classify(1000, 1050, 5.0, 0.80, 0.84, 5.0, self.TOL)
        assert b == BUCKET_ENERGY and within is False

    def test_pr_divergence_when_energy_ok(self):
        # Energy within 1%, but PR off by ~26% (the GTO1 capacity story).
        b, within, _ = classify(1000, 1005, 0.5, 0.93, 0.685, -26.3, self.TOL)
        assert b == BUCKET_PR and within is True

    def test_both_zero_is_ok(self):
        b, within, note = classify(0, 0, 0.0, None, None, None, self.TOL)
        assert b == BUCKET_OK and within is True and note == "both zero"

    def test_v1_zero_v2_nonzero_is_energy_mismatch(self):
        b, within, _ = classify(0, 5, None, None, None, None, self.TOL)
        assert b == BUCKET_ENERGY and within is False

    def test_missing_v1(self):
        b, within, _ = classify(None, 1000, None, None, 0.8, None, self.TOL)
        assert b == BUCKET_MISSING_V1 and within is False

    def test_missing_v2(self):
        b, within, _ = classify(1000, None, None, 0.8, None, None, self.TOL)
        assert b == BUCKET_MISSING_V2 and within is False

    def test_boundary_exactly_at_tolerance_is_ok(self):
        b, within, _ = classify(1000, 1020, 2.0, 0.8, 0.8, 0.0, self.TOL)
        assert b == BUCKET_OK and within is True


# --------------------------------------------------------------------------
class TestIndexers:
    def test_index_v2_dup_key_last_wins(self):
        rows = [
            {"plant_key": "SLP1", "date_iso": "2026-06-30", "energy_kwh": "100", "pr": "0.5", "irradiance_kwh_m2": "4"},
            {"plant_key": "SLP1", "date_iso": "2026-06-30", "energy_kwh": "565", "pr": "0.61", "irradiance_kwh_m2": "4.86"},
        ]
        idx = index_v2(rows)
        assert idx[("SLP1", "2026-06-30")]["energy_kwh"] == 565.0

    def test_index_v1_column_aliases_and_serial_date(self):
        s = _serial(dt.date(2026, 6, 29))
        rows = [{"Plant_Key": "GTO1", "Date": s, "Real_kWh": "3912.5",
                 "Irradiance_kWh_m2": "7.028", "Size_kWp_DC": "605.9"}]
        idx = index_v1(rows)
        e = idx[("GTO1", "2026-06-29")]
        assert e["energy_kwh"] == 3912.5
        assert e["kwp"] == 605.9

    def test_unparseable_rows_skipped(self):
        rows = [{"plant_key": "", "date_iso": "", "energy_kwh": "5"},
                {"plant_key": "SLP1", "date_iso": "garbage", "energy_kwh": "5"}]
        assert index_v2(rows) == {}


# --------------------------------------------------------------------------
class TestBuildReconcile:
    ACTIVE = {"SLP1", "SLP2", "GTO1", "MEX1", "NL1", "MEX2"}

    def _v1(self, plant, date, kwh, irr, kwp):
        return {"Plant_Key": plant, "Date": date, "Real_kWh": kwh,
                "Irradiance_kWh_m2": irr, "Size_kWp_DC": kwp}

    def _v2(self, plant, date, kwh, irr, pr):
        return {"plant_key": plant, "date_iso": date, "energy_kwh": kwh,
                "irradiance_kwh_m2": irr, "pr": pr}

    def test_basic_match(self):
        v1 = [self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2)]
        v2 = [self._v2("SLP1", "2026-06-30", 565.0, 4.86, 0.6144)]
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        assert len(rows) == 1
        r = rows[0]
        assert r.bucket == BUCKET_OK and r.within_tolerance is True
        assert r.energy_delta_pct == 0.0

    def test_gto1_capacity_divergence(self):
        # Identical energy + identical irradiance, but v1 uses kwp=605.9 and
        # v2's pr was computed on kwp=818.33 -> energy OK, PR diverges. This is
        # the real finding: NOT a collection bug, a config difference.
        energy, irr = 3000.0, 6.0
        v1_pr = round(energy / (605.9 * irr), 4)     # ~0.8253
        v2_pr = round(energy / (818.33 * irr), 4)    # ~0.6110
        v1 = [self._v1("GTO1", "2026-06-30", energy, irr, 605.9)]
        v2 = [self._v2("GTO1", "2026-06-30", energy, irr, v2_pr)]
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        r = rows[0]
        assert r.bucket == BUCKET_PR
        assert r.within_tolerance is True          # energy gate passes
        assert r.v1_pr == v1_pr and r.v2_pr == v2_pr
        assert r.pr_delta_pct < -20                 # v2 PR much lower by design

    def test_energy_mismatch_flagged(self):
        v1 = [self._v1("NL1", "2026-06-30", 1000.0, 4.0, 617.4)]
        v2 = [self._v2("NL1", "2026-06-30", 1100.0, 4.0, 0.44)]  # +10%
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        assert rows[0].bucket == BUCKET_ENERGY
        assert rows[0].within_tolerance is False

    def test_deactivated_plant_excluded(self):
        v1 = [self._v1("QRO1", "2026-06-30", 500.0, 4.0, 550.0)]
        v2 = [self._v2("QRO1", "2026-06-30", 500.0, 4.0, 0.22)]
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        assert rows == []  # QRO1 not in active set

    def test_today_excluded(self):
        v1 = [self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2)]
        v2 = [
            self._v2("SLP1", "2026-06-30", 565.0, 4.86, 0.61),
            self._v2("SLP1", "2026-07-01", 220.0, 6.81, 0.17),  # partial today
        ]
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0,
                               exclude_dates={"2026-07-01"})
        assert {r.date_iso for r in rows} == {"2026-06-30"}

    def test_include_dates_filter(self):
        v1 = [self._v1("SLP1", "2026-06-28", 500.0, 4.0, 189.2),
              self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2)]
        v2 = [self._v2("SLP1", "2026-06-28", 500.0, 4.0, 0.66),
              self._v2("SLP1", "2026-06-30", 565.0, 4.86, 0.61)]
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0,
                               include_dates={"2026-06-30"})
        assert {r.date_iso for r in rows} == {"2026-06-30"}

    def test_key_normalization_matches(self):
        v1 = [self._v1(" gto1 ", "2026-06-30", 3000.0, 6.0, 605.9)]
        v2 = [self._v2("GTO1", "2026-06-30", 3000.0, 6.0, 0.61)]
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        assert len(rows) == 1
        assert rows[0].v1_energy_kwh == 3000.0 and rows[0].v2_energy_kwh == 3000.0

    def test_missing_sides_and_sorting(self):
        v1 = [self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2)]  # v1 only
        v2 = [self._v2("NL1", "2026-06-30", 1770.0, 3.55, 0.80)]   # v2 only
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        by_plant = {r.plant_key: r.bucket for r in rows}
        assert by_plant["SLP1"] == BUCKET_MISSING_V2
        assert by_plant["NL1"] == BUCKET_MISSING_V1
        # sorted by (date, plant): NL1 before SLP1
        assert [r.plant_key for r in rows] == ["NL1", "SLP1"]

    def test_summarize_counts(self):
        v1 = [self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2),
              self._v1("NL1", "2026-06-30", 1000.0, 4.0, 617.4)]
        v2 = [self._v2("SLP1", "2026-06-30", 565.0, 4.86, 0.61),
              self._v2("NL1", "2026-06-30", 1100.0, 4.0, 0.44)]  # +10% mismatch
        rows = build_reconcile(v1, v2, self.ACTIVE, tolerance_pct=2.0)
        counts = summarize(rows)
        assert counts[BUCKET_OK] == 1
        assert counts[BUCKET_ENERGY] == 1


# --------------------------------------------------------------------------
# Read-only CLI smoke test — proves the script never writes.
_MOD_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "reconcile_daily.py"
_spec = importlib.util.spec_from_file_location("reconcile_daily", _MOD_PATH)
recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recon)

_WRITE_METHODS = [
    "append_rows", "write_row", "write_cell", "delete_row",
    "upsert_rows", "write_values", "write_header_row", "ensure_header",
    "ensure_tab",
]


class TestCliReadOnly:
    def _run(self, v1_rows, v2_rows, argv):
        client = MagicMock(spec=SheetsClient)

        def _read_table(tab, a1="A1:Z"):
            return v2_rows if tab == recon.V2_TAB else v1_rows

        client.read_table.side_effect = _read_table

        portfolio = SimpleNamespace(
            active_plants=lambda: [SimpleNamespace(plant_key=p) for p in
                                   ["SLP1", "SLP2", "GTO1", "MEX1", "NL1", "MEX2"]]
        )
        fixed_now = dt.datetime(2026, 7, 1, 10, 0, tzinfo=dt.timezone.utc)

        with patch.object(recon, "SheetsClient", return_value=client), \
             patch.object(recon, "load_portfolio", return_value=portfolio), \
             patch.object(recon, "now_mx", return_value=fixed_now), \
             patch.dict("os.environ", {"GOOGLE_SHEET_ID_V2": "v2id",
                                       "GOOGLE_CREDENTIALS": "{}"}, clear=False):
            code = recon.main(argv)
        return code, client

    def _v1(self, p, d, kwh, irr, kwp):
        return {"Plant_Key": p, "Date": d, "Real_kWh": kwh,
                "Irradiance_kWh_m2": irr, "Size_kWp_DC": kwp}

    def _v2(self, p, d, kwh, irr, pr):
        return {"plant_key": p, "date_iso": d, "energy_kwh": kwh,
                "irradiance_kwh_m2": irr, "pr": pr}

    def test_never_writes_and_exit_zero_on_match(self):
        v1 = [self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2)]
        v2 = [self._v2("SLP1", "2026-06-30", 565.0, 4.86, 0.61)]
        code, client = self._run(v1, v2, ["--start", "2026-06-30", "--end", "2026-06-30"])
        assert code == 0
        for m in _WRITE_METHODS:
            getattr(client, m).assert_not_called()

    def test_exit_one_on_energy_mismatch(self):
        v1 = [self._v1("SLP1", "2026-06-30", 1000.0, 4.86, 189.2)]
        v2 = [self._v2("SLP1", "2026-06-30", 1100.0, 4.86, 0.61)]  # +10%
        code, client = self._run(v1, v2, ["--start", "2026-06-30", "--end", "2026-06-30"])
        assert code == 1
        for m in _WRITE_METHODS:
            getattr(client, m).assert_not_called()

    def test_exit_two_when_no_overlap(self):
        v1 = [self._v1("SLP1", "2026-06-30", 565.0, 4.86, 189.2)]
        v2 = []  # v2 has nothing for the window
        code, _ = self._run(v1, v2, ["--start", "2026-06-30", "--end", "2026-06-30"])
        assert code == 2
