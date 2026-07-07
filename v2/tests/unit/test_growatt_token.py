"""Tests: Growatt token-API fallback (degraded-mode plant energy).

Incident 2026-07-07: the web-session block (the only carrier of
per-inverter data) left four Growatt plants dark. The fallback mirrors
v1's months-proven OpenAPI call — plant-level today_energy — captured
intraday by telemetry into a day-cache and consumed next morning by
kpi-eod (today_energy resets at midnight, kpi runs at 06:00).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
import responses

from argia.vendors import growatt_token as gt


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_GROWATT_TOKEN_CACHE",
                       str(tmp_path / "token_energy.json"))
    # no politeness pause in tests
    monkeypatch.setattr(gt, "INTER_PLANT_SLEEP_S", 0)


class TestTokenClient:
    def _client(self):
        return gt.GrowattTokenClient("TOK123")

    @responses.activate
    def test_happy_path_mirrors_v1(self):
        responses.add(
            responses.GET, f"{gt.OPENAPI_BASE}/v1/plant/data",
            json={"data": {"today_energy": "1234.5", "plant_id": "99"}},
            status=200,
            match=[responses.matchers.query_param_matcher(
                {"plant_id": "99"})],
        )
        assert self._client().plant_today_energy("99") == 1234.5
        assert responses.calls[0].request.headers["token"] == "TOK123"

    @responses.activate
    def test_http_error_returns_none_never_raises(self):
        responses.add(responses.GET, f"{gt.OPENAPI_BASE}/v1/plant/data",
                      status=500)
        assert self._client().plant_today_energy("99") is None

    @responses.activate
    def test_empty_payload_returns_none(self):
        responses.add(responses.GET, f"{gt.OPENAPI_BASE}/v1/plant/data",
                      json={"error_msg": "permission denied", "data": None},
                      status=200)
        assert self._client().plant_today_energy("99") is None

    @responses.activate
    def test_connection_error_returns_none(self):
        responses.add(responses.GET, f"{gt.OPENAPI_BASE}/v1/plant/data",
                      body=ConnectionError("boom"))
        assert self._client().plant_today_energy("99") is None

    def test_missing_plant_id_short_circuits(self):
        assert self._client().plant_today_energy("") is None

    def test_from_env(self, monkeypatch):
        monkeypatch.delenv("GROWATT_API_TOKEN", raising=False)
        assert gt.GrowattTokenClient.from_env() is None
        monkeypatch.setenv("GROWATT_API_TOKEN", "T")
        assert gt.GrowattTokenClient.from_env() is not None


class TestEnergyCache:
    def test_roundtrip_and_accumulation(self):
        gt.cache_energy("2026-07-07", "SLP1", 1000.0)
        gt.cache_energy("2026-07-07", "GTO1", 2500.0)
        gt.cache_energy("2026-07-07", "SLP1", 1100.0)   # later run wins
        assert gt.cached_energy("2026-07-07", "SLP1") == 1100.0
        assert gt.cached_energy("2026-07-07", "GTO1") == 2500.0
        assert gt.cached_energy("2026-07-07", "NL1") is None

    def test_new_day_prunes_old_values(self):
        """Stale energy leaking into a later day would be silent data
        corruption — the cache keeps ONLY the current date."""
        gt.cache_energy("2026-07-07", "SLP1", 1000.0)
        gt.cache_energy("2026-07-08", "SLP1", 50.0)
        assert gt.cached_energy("2026-07-07", "SLP1") is None
        assert gt.cached_energy("2026-07-08", "SLP1") == 50.0
        raw = json.loads(gt.cache_file().read_text())
        assert list(raw.keys()) == ["2026-07-08"]

    def test_corrupt_cache_degrades_to_none(self):
        gt.cache_file().write_text("{broken")
        assert gt.cached_energy("2026-07-07", "SLP1") is None
        gt.cache_energy("2026-07-07", "SLP1", 9.0)      # and self-heals
        assert gt.cached_energy("2026-07-07", "SLP1") == 9.0


class TestKpiFallbackGate:
    def test_real_energy_wins_token_never_consulted(self):
        gt.cache_energy("2026-07-07", "SLP1", 9999.0)
        energies = {"INV1": 500.0, "INV2": 506.0}
        out, used = gt.apply_energy_fallback(energies, "2026-07-07", "SLP1")
        assert out is energies and used is False

    def test_all_none_uses_cache(self):
        gt.cache_energy("2026-07-07", "SLP1", 1006.0)
        out, used = gt.apply_energy_fallback(
            {"INV1": None, "INV2": None}, "2026-07-07", "SLP1")
        assert used is True and out == {"_token_fallback": 1006.0}

    def test_empty_dict_uses_cache(self):
        gt.cache_energy("2026-07-07", "SLP1", 1006.0)
        out, used = gt.apply_energy_fallback({}, "2026-07-07", "SLP1")
        assert used is True and out["_token_fallback"] == 1006.0

    def test_no_cache_no_substitution(self):
        out, used = gt.apply_energy_fallback({}, "2026-07-07", "SLP1")
        assert used is False and out == {}

    def test_zero_cached_value_rejected(self):
        gt.cache_energy("2026-07-07", "SLP1", 0.0)
        out, used = gt.apply_energy_fallback({}, "2026-07-07", "SLP1")
        assert used is False


class TestTelemetryTrigger:
    """The fallback fires ONLY on the block signature: zero rows AND
    errors. Partial success (some inverters answered) must not fire —
    real data is present and the token would add nothing."""

    def _call(self, *, plant_rows, errors, token_client):
        # replicate the guard exactly as wired in telemetry_5m
        fired = []
        plant = MagicMock(weather_plant_id="99", plant_key="SLP1")
        if not plant_rows and errors and token_client is not None:
            kwh = token_client.plant_today_energy(plant.weather_plant_id)
            if kwh is not None and kwh > 0:
                fired.append(kwh)
        return fired

    def test_fires_on_block_signature(self):
        tc = MagicMock()
        tc.plant_today_energy.return_value = 1234.0
        assert self._call(plant_rows=[], errors=2, token_client=tc) == [1234.0]

    def test_silent_when_rows_exist(self):
        tc = MagicMock()
        self._call(plant_rows=[["row"]], errors=1, token_client=tc)
        tc.plant_today_energy.assert_not_called()

    def test_silent_when_no_errors(self):
        tc = MagicMock()
        self._call(plant_rows=[], errors=0, token_client=tc)
        tc.plant_today_energy.assert_not_called()

    def test_silent_without_token(self):
        assert self._call(plant_rows=[], errors=2, token_client=None) == []


class TestWiring:
    def test_telemetry_and_kpi_are_wired(self):
        # anchored to this file, not cwd (2026-07-07: passed in the
        # sandbox, failed on the laptop — cwd-relative paths in tests
        # are traps)
        from pathlib import Path
        v2 = Path(__file__).resolve().parents[2]
        tel = (v2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
        assert "GrowattTokenClient.from_env()" in tel
        assert "if not plant_rows and errors and token_client is not None:" in tel
        assert "cache_energy(date_iso, plant.plant_key, kwh)" in tel
        kpi = (v2 / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        assert "apply_energy_fallback" in kpi
        assert "energy via Growatt token API" in kpi
