"""
Tests for argia.orchestrator.

Uses fake VendorClient and fake SheetsClient so we test the orchestration
logic (per-plant isolation, dry-run, single-plant override, idempotency)
without hitting any network.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional
from unittest.mock import patch

import pytest

from argia.core.config import InverterConfig, PlantConfig, Portfolio
from argia.orchestrator import (
    TAB_DAILY,
    TAB_SNAP,
    TAB_SYNC,
    RunResult,
    new_run_id,
    run_daily,
    run_snapshot10m,
)
from argia.vendors.base import InverterSnapshot


# ============================================================
# Fakes
# ============================================================


class FakeVendorClient:
    """Stand-in for any real vendor client."""

    brand = "FAKE"

    def __init__(
        self,
        day_kwh: Optional[float] = 100.0,
        snapshots: Optional[List[InverterSnapshot]] = None,
        raise_on_fetch_day: bool = False,
    ) -> None:
        self.day_kwh = day_kwh
        self.snapshots = snapshots or []
        self.raise_on_fetch_day = raise_on_fetch_day
        self.login_calls = 0

    def login(self) -> None:
        self.login_calls += 1

    def fetch_day_kwh(self, plant: PlantConfig, date_iso: str) -> Optional[float]:
        if self.raise_on_fetch_day:
            raise RuntimeError("simulated API failure")
        return self.day_kwh

    def fetch_inverter_snapshots(
        self, plant: PlantConfig, inverters: List[InverterConfig]
    ) -> List[InverterSnapshot]:
        return self.snapshots


class FakeSheets:
    """In-memory replacement for SheetsClient."""

    def __init__(self) -> None:
        self.tabs: Dict[str, List[List]] = {
            "DailyProduction": [],
            "InverterSnapshot10m": [],
            "HealthLog": [],
            "SyncRuns": [],
        }
        self.upsert_calls: List[Dict] = []

    def append_rows(self, tab: str, rows: List[List]) -> int:
        self.tabs.setdefault(tab, []).extend(rows)
        return len(rows)

    def upsert_rows(
        self,
        tab: str,
        rows: List[List],
        natural_key_columns: List[int],
    ) -> Dict[str, int]:
        existing = self.tabs.setdefault(tab, [])
        # Build a lookup of existing natural keys
        keys_in_sheet = {
            tuple(str(r[c]) for c in natural_key_columns): i
            for i, r in enumerate(existing)
        }
        inserted = updated = 0
        for r in rows:
            key = tuple(str(r[c]) for c in natural_key_columns)
            if key in keys_in_sheet:
                existing[keys_in_sheet[key]] = r
                updated += 1
            else:
                existing.append(r)
                inserted += 1
        stats = {"inserted": inserted, "updated": updated, "unchanged": 0}
        self.upsert_calls.append({"tab": tab, "rows": list(rows), "stats": stats})
        return stats


# ============================================================
# Fixtures
# ============================================================


def _make_plant(plant_key="P1", brand="GROWATT", active=True, lat=20.0, lon=-100.0, **overrides):
    defaults = dict(
        plant_key=plant_key,
        customer="Test",
        brand=brand,
        site_id="123",
        kwp_dc=100.0,
        kwp_ac=80.0,
        lat=lat,
        lon=lon,
        expected_factor=0.75,
        pr_target=0.85,
        installation_date="2025-01-01",
        secret_api_name="GROWATT_API_TOKEN",
        secret_user_name="",
        secret_pass_name="",
        weather_plant_id="",
        datalogger_sn="",
        datalogger_addr=0,
        active=active,
    )
    defaults.update(overrides)
    return PlantConfig(**defaults)


def _make_portfolio(*plants) -> Portfolio:
    plant_dict = {p.plant_key: p for p in plants}
    return Portfolio(plants=plant_dict, inverters_by_plant={})


@pytest.fixture
def two_plant_portfolio():
    return _make_portfolio(_make_plant("P1"), _make_plant("P2"))


@pytest.fixture(autouse=True)
def _stub_external_meteo(monkeypatch):
    """Default: cloud + irradiance return None. Tests can override per-case."""
    from argia.meteo.open_meteo import CloudCoverClient

    monkeypatch.setattr(
        CloudCoverClient, "fetch_avg_cloudcover_pct",
        lambda self, lat, lon, date_iso: None,
    )


# ============================================================
# RunResult
# ============================================================


class TestRunResult:
    def test_finalize_with_no_errors_is_ok(self):
        r = RunResult(run_id="x", started_at_utc=dt.datetime.now(dt.timezone.utc))
        r.plants_processed = 2
        r.finalize()
        assert r.status == "OK"

    def test_finalize_with_some_errors_is_partial(self):
        r = RunResult(run_id="x", started_at_utc=dt.datetime.now(dt.timezone.utc))
        r.plants_processed = 1
        r.add_error("P2", RuntimeError("boom"))
        r.finalize()
        assert r.status == "PARTIAL"

    def test_finalize_with_all_errors_is_failed(self):
        r = RunResult(run_id="x", started_at_utc=dt.datetime.now(dt.timezone.utc))
        r.add_error("P1", RuntimeError("boom"))
        r.finalize()
        assert r.status == "FAILED"

    def test_to_sheet_row_format(self):
        r = RunResult(
            run_id="rid",
            started_at_utc=dt.datetime(2026, 5, 11, 12, 0, tzinfo=dt.timezone.utc),
            script="argia_mont_daily",
        )
        r.plants_processed = 3
        r.rows_written = 3
        r.finalize()
        row = r.to_sheet_row()
        assert row[0] == "rid"
        assert row[3] == "argia_mont_daily"
        assert row[4] == "OK"
        assert row[5] == 3  # plants
        assert row[6] == 3  # rows


def test_new_run_id_unique():
    a, b = new_run_id(), new_run_id()
    assert a != b
    assert len(a) > 10


# ============================================================
# run_daily
# ============================================================


class TestRunDaily:
    def test_writes_one_row_per_plant(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {p.plant_key: FakeVendorClient(day_kwh=150.0) for p in two_plant_portfolio.plants.values()}

        result = run_daily(
            sheets=sheets,
            portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: clients,
        )

        assert result.status == "OK"
        assert result.plants_processed == 2
        assert len(sheets.tabs[TAB_DAILY]) == 2
        # Date is the first column
        assert all(r[0] == "2026-05-10" for r in sheets.tabs[TAB_DAILY])

    def test_dry_run_writes_nothing(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {p.plant_key: FakeVendorClient(day_kwh=150.0) for p in two_plant_portfolio.plants.values()}

        result = run_daily(
            sheets=sheets,
            portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            dry_run=True,
            client_factory=lambda plants: clients,
        )

        assert result.plants_processed == 2
        # Nothing written, not even SyncRuns
        assert sheets.tabs[TAB_DAILY] == []
        assert sheets.tabs[TAB_SYNC] == []

    def test_only_plant_filter(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {p.plant_key: FakeVendorClient(day_kwh=150.0) for p in two_plant_portfolio.plants.values()}

        result = run_daily(
            sheets=sheets,
            portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            only_plant="P1",
            client_factory=lambda plants: {"P1": clients["P1"]},
        )

        assert result.plants_processed == 1
        assert len(sheets.tabs[TAB_DAILY]) == 1
        assert sheets.tabs[TAB_DAILY][0][1] == "P1"

    def test_failing_plant_does_not_stop_others(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {
            "P1": FakeVendorClient(raise_on_fetch_day=True),
            "P2": FakeVendorClient(day_kwh=200.0),
        }

        result = run_daily(
            sheets=sheets,
            portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: clients,
        )

        assert result.status == "PARTIAL"
        assert result.plants_processed == 1
        assert result.plants_skipped == 1
        assert len(result.errors) == 1
        assert "P1" in result.errors[0]
        # P2 still got its row
        assert len(sheets.tabs[TAB_DAILY]) == 1
        assert sheets.tabs[TAB_DAILY][0][1] == "P2"

    def test_no_active_clients_finishes_cleanly(self, two_plant_portfolio):
        sheets = FakeSheets()
        result = run_daily(
            sheets=sheets,
            portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: {},
        )
        assert result.plants_processed == 0
        assert sheets.tabs[TAB_DAILY] == []

    def test_idempotent_re_run(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {p.plant_key: FakeVendorClient(day_kwh=150.0) for p in two_plant_portfolio.plants.values()}

        # First run
        run_daily(
            sheets=sheets, portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: clients,
        )
        # Second run with different value — should UPDATE, not append
        for c in clients.values():
            c.day_kwh = 250.0
        run_daily(
            sheets=sheets, portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: clients,
        )

        # Still 2 rows total, updated values
        assert len(sheets.tabs[TAB_DAILY]) == 2
        # Look at column index 3 (real_kwh) — should be 250 not 150
        kwh_values = {r[1]: r[3] for r in sheets.tabs[TAB_DAILY]}
        assert kwh_values["P1"] == 250.0
        assert kwh_values["P2"] == 250.0

    def test_sync_runs_row_appended(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {p.plant_key: FakeVendorClient(day_kwh=150.0) for p in two_plant_portfolio.plants.values()}

        run_daily(
            sheets=sheets, portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: clients,
        )

        assert len(sheets.tabs[TAB_SYNC]) == 1
        sync_row = sheets.tabs[TAB_SYNC][0]
        assert sync_row[3] == "argia_mont_daily"
        assert sync_row[4] == "OK"

    def test_login_called_per_plant(self, two_plant_portfolio):
        sheets = FakeSheets()
        clients = {p.plant_key: FakeVendorClient(day_kwh=150.0) for p in two_plant_portfolio.plants.values()}

        run_daily(
            sheets=sheets, portfolio=two_plant_portfolio,
            date_iso="2026-05-10",
            client_factory=lambda plants: clients,
        )

        for c in clients.values():
            assert c.login_calls == 1


# ============================================================
# run_snapshot10m
# ============================================================


class TestRunSnapshot10m:
    def _portfolio_with_inverters(self):
        plant = _make_plant("P1")
        inv1 = InverterConfig(plant_key="P1", inverter_sn="SN001",
                              inverter_label="Inv 1", rated_kw=50, active=True)
        inv2 = InverterConfig(plant_key="P1", inverter_sn="SN002",
                              inverter_label="Inv 2", rated_kw=50, active=True)
        return Portfolio(
            plants={"P1": plant},
            inverters_by_plant={"P1": [inv1, inv2]},
        )

    def _snapshots(self):
        ts = dt.datetime(2026, 5, 11, 14, 0, tzinfo=dt.timezone.utc)
        return [
            InverterSnapshot(
                plant_key="P1", inverter_sn="SN001", timestamp_utc=ts,
                status=1, power_w=15000.0, etoday_kwh=80.0,
            ),
            InverterSnapshot(
                plant_key="P1", inverter_sn="SN002", timestamp_utc=ts,
                status=1, power_w=14000.0, etoday_kwh=75.0,
            ),
        ]

    def test_appends_one_row_per_inverter(self):
        sheets = FakeSheets()
        portfolio = self._portfolio_with_inverters()
        client = FakeVendorClient(snapshots=self._snapshots())

        result = run_snapshot10m(
            sheets=sheets, portfolio=portfolio,
            client_factory=lambda plants: {"P1": client},
        )

        assert result.status == "OK"
        assert len(sheets.tabs[TAB_SNAP]) == 2

    def test_dry_run_writes_nothing(self):
        sheets = FakeSheets()
        portfolio = self._portfolio_with_inverters()
        client = FakeVendorClient(snapshots=self._snapshots())

        run_snapshot10m(
            sheets=sheets, portfolio=portfolio,
            dry_run=True,
            client_factory=lambda plants: {"P1": client},
        )

        assert sheets.tabs[TAB_SNAP] == []
        assert sheets.tabs[TAB_SYNC] == []

    def test_plant_with_no_inverters_skipped(self):
        sheets = FakeSheets()
        plant = _make_plant("P1")
        portfolio = Portfolio(plants={"P1": plant}, inverters_by_plant={})  # no inverters
        client = FakeVendorClient(snapshots=[])

        result = run_snapshot10m(
            sheets=sheets, portfolio=portfolio,
            client_factory=lambda plants: {"P1": client},
        )

        # plant_processed is NOT incremented when we skip (continue without try)
        # Both 0 processed and 0 skipped, all rows empty, status OK
        assert sheets.tabs[TAB_SNAP] == []
        assert result.status == "OK"

    def test_failing_plant_isolated(self):
        sheets = FakeSheets()
        portfolio = self._portfolio_with_inverters()

        class Boom(FakeVendorClient):
            def fetch_inverter_snapshots(self, plant, inverters):
                raise RuntimeError("vendor down")

        result = run_snapshot10m(
            sheets=sheets, portfolio=portfolio,
            client_factory=lambda plants: {"P1": Boom()},
        )

        assert result.status == "FAILED"
        assert len(result.errors) == 1
