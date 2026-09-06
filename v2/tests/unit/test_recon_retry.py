"""v212 — reconciliation retries: the nightly run goes back over the
open window and re-fetches the vendor's day for plant-days that still
reconcile badly because of OUR gap. Never lowers; closed months frozen;
PASS days and vendor-side gaps are left alone."""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from types import SimpleNamespace

import pytest

from argia.recon import engine as E
from argia.recon import retry as R

V2 = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = V2 / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class TestNeedsRetry:
    def test_pass_is_done(self):
        assert not R.needs_retry("PASS", "inverter counters vs vendor plant daily +0.10%", 100.0)

    def test_fail_and_no_data_retry(self):
        assert R.needs_retry("FAIL", "inverter counters vs vendor -4.00% (> 3%)", 100.0)
        assert R.needs_retry("NO_DATA", "no inverter counters and no vendor counter", None)

    def test_missing_row_retries(self):
        assert R.needs_retry("", "", None)
        assert R.needs_retry(None, None, None)

    def test_review_our_gap_retries(self):
        assert R.needs_retry("REVIEW", "no inverter counters — collection gap, vendor plant daily only", None)
        assert R.needs_retry("REVIEW", "no vendor daily counter — inverter counters only", 100.0)
        assert R.needs_retry("REVIEW", "completeness 61.1% < 95% — undercount expected (-30.00%)", 61.1)
        assert R.needs_retry("REVIEW", "inverter counters vs vendor -2.00% (> 1%)", 80.0)   # low completeness

    def test_review_vendor_gap_is_not_ours(self):
        # SLP2 2026-09-04: the inverters hold MORE than the vendor — the
        # vendor lost uploads; our counters are complete and kept
        assert not R.needs_retry("REVIEW", "inverter counters +10.60% above the vendor plant daily — vendor upload gap; inverter counters kept", 100.0)
        # a plain 2% REVIEW with full completeness: nothing new to fetch
        assert not R.needs_retry("REVIEW", "inverter counters vs vendor -2.00% (> 1%)", 100.0)


class TestWindowAndSelection:
    def test_window_excludes_today(self):
        ds = R.window_dates(dt.date(2026, 9, 6), 3)
        assert ds == ["2026-09-03", "2026-09-04", "2026-09-05"]
        assert len(R.window_dates(dt.date(2026, 9, 6))) == R.RETRY_DAYS == 14

    def test_select_missing_rows_and_bad_days_not_closed_or_pass(self):
        dates = ["2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]
        rows = [
            ("GTO1", "2026-08-30", "REVIEW", "no inverter counters — collection gap, vendor plant daily only", ""),
            ("GTO1", "2026-08-31", "PASS", "ok", "100"),
            ("GTO1", "2026-09-01", "FAIL", "inverter counters vs vendor -5.00% (> 3%)", "100"),
            # 2026-09-02 has no row -> retry
            ("MEX1", "2026-09-01", "REVIEW", "inverter counters +8.00% above the vendor plant daily — vendor upload gap; inverter counters kept", "100"),
            ("MEX1", "2026-09-02", "PASS", "ok", "100"),
        ]
        closed = [("GTO1", "2026-08")]           # August closed for GTO1
        got = R.select_retry(["GTO1", "MEX1"], dates, rows, closed=closed)
        assert got == [("GTO1", "2026-09-01"), ("GTO1", "2026-09-02"),
                       ("MEX1", "2026-08-30"), ("MEX1", "2026-08-31")]

    def test_select_is_empty_when_everything_passed(self):
        rows = [("GTO1", "2026-09-05", "PASS", "ok", "100")]
        assert R.select_retry(["GTO1"], ["2026-09-05"], rows) == []

    def test_group_dates(self):
        g = R.group_dates([("B", "2026-09-02"), ("A", "2026-09-03"), ("B", "2026-09-01"), ("B", "2026-09-02")])
        assert g == {"B": ["2026-09-01", "2026-09-02"], "A": ["2026-09-03"]}


class TestSnapshotSql:
    def test_fill_or_raise_never_lower(self):
        sql = R.build_retry_snapshot_sql([("gto1", "growatt", "2026-09-01", 1234.5678),
                                          ("MEX1", "HUAWEI", "2026-09-01", None)])
        assert "('GTO1','GROWATT',DATE '2026-09-01',1234.568,'history-retry')" in sql
        assert "MEX1" not in sql                               # None skipped
        assert "ON CONFLICT (plant_key, snap_date) DO UPDATE SET daily_kwh = EXCLUDED.daily_kwh" in sql
        assert ("WHERE vendor_counter_snapshot.daily_kwh IS NULL"
                " OR vendor_counter_snapshot.daily_kwh < EXCLUDED.daily_kwh" in sql)
        # the nightly monthly/lifetime counters are not touched
        assert "monthly_kwh" not in sql and "lifetime_kwh" not in sql

    def test_empty_batch(self):
        assert R.build_retry_snapshot_sql([]) is None
        assert R.build_retry_snapshot_sql([("GTO1", "GROWATT", "2026-09-01", None)]) is None


class TestRetryPass:
    """recon_snapshot.retry_pass with PG and the vendors monkeypatched."""

    def _wire(self, monkeypatch, recon_rows, closed_rows, growatt_days):
        import recon_snapshot as RS
        import recon_backfill as RB
        execd, reconciled = [], []

        def psql_rows(sql):
            if "FROM reconciliation_daily" in sql:
                return recon_rows
            if "FROM reconciliation_monthly" in sql:
                return closed_rows
            return []
        monkeypatch.setattr(RS, "psql_rows", psql_rows)
        monkeypatch.setattr(RS, "psql_exec", lambda sql: execd.append(sql))
        monkeypatch.setattr(RS, "reconcile_day",
                            lambda d, b, dry, frozen=None: reconciled.append((d, frozen)) or 1)
        calls = {}

        def fetch_growatt(plants, portfolio, dates, dates_by_plant=None):
            calls["growatt"] = dates_by_plant
            return {(p.plant_key, d): growatt_days.get((p.plant_key, d))
                    for p in plants for d in dates_by_plant.get(p.plant_key, [])}
        monkeypatch.setattr(RB, "fetch_growatt", fetch_growatt)
        monkeypatch.setattr(RB, "fetch_huawei", lambda plants, d0, d1: calls.setdefault("huawei", (d0, d1)) and {})
        monkeypatch.setattr(RB, "fetch_solaredge", lambda plants, d0, d1: {})
        return RS, execd, reconciled, calls

    def test_fetches_only_the_flagged_days_and_reconciles_them(self, monkeypatch):
        recon_rows = [
            ["GTO1", "2026-09-03", "REVIEW", "completeness 40.0% < 95% — undercount expected (-50.00%)", "40"],
            ["GTO1", "2026-09-04", "PASS", "ok", "100"],
            ["GTO1", "2026-09-05", "PASS", "ok", "100"],
            ["NL1", "2026-09-03", "PASS", "ok", "100"],
            ["NL1", "2026-09-04", "PASS", "ok", "100"],
            ["NL1", "2026-09-05", "FAIL", "inverter counters vs vendor -6.00% (> 3%)", "100"],
        ]
        RS, execd, reconciled, calls = self._wire(
            monkeypatch, recon_rows, [], {("GTO1", "2026-09-03"): 2100.0})
        active = [SimpleNamespace(plant_key="GTO1", brand="GROWATT"),
                  SimpleNamespace(plant_key="NL1", brand="HUAWEI")]
        n = RS.retry_pass(active, object(), {"GTO1": "GROWATT", "NL1": "HUAWEI"},
                          dt.date(2026, 9, 6), 3, dry_run=False)
        assert calls["growatt"] == {"GTO1": ["2026-09-03"]}          # not the PASS days
        assert calls["huawei"] == (dt.date(2026, 9, 5), dt.date(2026, 9, 5))
        assert len(execd) == 1 and "'GTO1','GROWATT',DATE '2026-09-03',2100.000" in execd[0]
        assert [d for d, _ in reconciled] == ["2026-09-03", "2026-09-05"]
        assert n == 2

    def test_closed_month_is_frozen_end_to_end(self, monkeypatch):
        recon_rows = [["GTO1", "2026-08-31", "FAIL", "x", "100"],
                      ["GTO1", "2026-09-01", "FAIL", "x", "100"]]
        RS, execd, reconciled, calls = self._wire(
            monkeypatch, recon_rows, [["GTO1", "2026-08"]],
            {("GTO1", "2026-08-31"): 999.0, ("GTO1", "2026-09-01"): 1000.0})
        active = [SimpleNamespace(plant_key="GTO1", brand="GROWATT")]
        RS.retry_pass(active, object(), {"GTO1": "GROWATT"}, dt.date(2026, 9, 3), 3, dry_run=False)
        # August never fetched; Sep 2 has no recon row yet -> a retry too
        assert calls["growatt"] == {"GTO1": ["2026-09-01", "2026-09-02"]}
        assert "2026-08-31" not in execd[0]
        assert reconciled == [("2026-09-01", {("GTO1", "2026-08")}),
                              ("2026-09-02", {("GTO1", "2026-08")})]  # frozen set passed on

    def test_dry_run_writes_nothing(self, monkeypatch):
        recon_rows = [["GTO1", "2026-09-05", "NO_DATA", "", ""]]
        RS, execd, reconciled, calls = self._wire(
            monkeypatch, recon_rows, [], {("GTO1", "2026-09-05"): 500.0})
        active = [SimpleNamespace(plant_key="GTO1", brand="GROWATT")]
        n = RS.retry_pass(active, object(), {"GTO1": "GROWATT"}, dt.date(2026, 9, 6), 3, dry_run=True)
        assert n == 0 and execd == [] and reconciled == []

    def test_nothing_to_retry_makes_no_vendor_calls(self, monkeypatch):
        recon_rows = [["GTO1", d, "PASS", "ok", "100"] for d in ("2026-09-03", "2026-09-04", "2026-09-05")]
        RS, execd, reconciled, calls = self._wire(monkeypatch, recon_rows, [], {})
        active = [SimpleNamespace(plant_key="GTO1", brand="GROWATT")]
        assert RS.retry_pass(active, object(), {"GTO1": "GROWATT"}, dt.date(2026, 9, 6), 3, False) == 0
        assert calls == {} and execd == []


class TestWiring:
    def test_nightly_run_has_the_retry_and_the_frozen_guard(self):
        src = (SCRIPTS / "recon_snapshot.py").read_text(encoding="utf-8")
        assert '"--retry-days", type=int, default=R.RETRY_DAYS' in src
        assert "total += retry_pass(active, portfolio, brand_by_plant," in src
        assert "frozen = closed_plant_months(dates)" in src
        assert 'LOG.info("recon %s %s: month closed — KPI row frozen"' in src
        # the heal is skipped for frozen plant-months, before any SQL
        i = src.index("in frozen:")
        j = src.index("psql_exec(B.build_fix_sql(")
        assert i < j

    def test_reconcile_day_skips_the_heal_for_a_frozen_month(self, monkeypatch):
        import recon_snapshot as RS
        execd = []
        monkeypatch.setattr(RS, "interval_by_plant", lambda d: {"GTO1": (900.0, 193)})
        monkeypatch.setattr(RS, "stored_daily", lambda d: {"GTO1": (1000.0, 500.0)})
        monkeypatch.setattr(RS, "inverter_coverage", lambda d: {"GTO1": (4, 4)})
        monkeypatch.setattr(RS, "psql_exec", lambda sql: execd.append(sql))
        RS.reconcile_day("2026-08-30", {"GTO1": "GROWATT"}, dry_run=False,
                         frozen={("GTO1", "2026-08")})
        assert not any("INSERT INTO daily_production" in s for s in execd)
        assert any("INSERT INTO reconciliation_daily" in s for s in execd)   # the check still runs
        execd.clear()
        RS.reconcile_day("2026-08-30", {"GTO1": "GROWATT"}, dry_run=False, frozen=set())
        assert any("INSERT INTO daily_production" in s for s in execd)       # open month heals

    def test_backfill_uses_the_shared_bootstrap(self):
        src = (SCRIPTS / "recon_backfill.py").read_text(encoding="utf-8")
        assert "load_portfolio(open_sheets())" in src
        assert "GOOGLE_SHEET_ID_V2" not in src
        assert "dates_by_plant: Optional[Dict[str, list]] = None" in src
