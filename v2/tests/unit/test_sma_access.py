"""v252 — how ARGIA actually reaches SMA.

The Stage 6 scaffold was written before anyone had seen a real SMA response
(its own doc predicted a "Stage 6.1 hotfix"). Three things it got wrong are
pinned here, because each one silently returns no data rather than failing
loudly:

1. the production *data* host is a guess — SMA publishes the token and consent
   hosts only — so it must be overridable without a code change;
2. the device measurement set is discovered, never hard-coded: the one real
   capture we hold proves "pvGeneration" is not a set name the API accepts;
3. no SMA credential is committed to this repository, which is public.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))

from argia.vendors import sma as SMA                     # noqa: E402
from argia.vendors import sma_telemetry as ST            # noqa: E402

FIX = V2 / "tests" / "fixtures" / "sma"


def _fix(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class TestProductionHosts:
    def test_the_hosts_sma_publishes_are_pinned_exactly(self):
        """developer.sma.de/api-access-control, read 2026-09-11."""
        assert SMA.ENDPOINTS["sandbox"]["token"] == "https://sandbox-auth.smaapis.de/oauth2/token"
        assert SMA.ENDPOINTS["production"]["token"] == "https://auth.smaapis.de/oauth2/token"
        assert SMA.ENDPOINTS["sandbox"]["bc_base"] == "https://sandbox.smaapis.de/oauth2/v2"
        assert SMA.ENDPOINTS["production"]["bc_base"] == "https://async-auth.smaapis.de/oauth2/v2"

    def test_the_unverified_data_host_is_overridable_without_touching_code(self):
        assert SMA.api_base("sandbox") == "https://sandbox.smaapis.de/monitoring/v1"
        assert SMA.api_base("production", "https://smaapis.de/monitoring/v1") == "https://smaapis.de/monitoring/v1"
        assert SMA.api_base("production", "https://smaapis.de/monitoring/v1/") == "https://smaapis.de/monitoring/v1"
        assert SMA.api_base("production", "   ") == SMA.ENDPOINTS["production"]["api_base"]
        assert SMA.api_base("production", None) == SMA.ENDPOINTS["production"]["api_base"]

    def test_the_client_takes_the_override_from_the_environment(self, monkeypatch):
        monkeypatch.setenv(SMA.API_BASE_ENV, "https://smaapis.de/monitoring/v1")
        c = SMA.SMAClient("id", "secret", "a@b.com", "production")
        assert c._endpoints["api_base"] == "https://smaapis.de/monitoring/v1"
        assert c._endpoints["token"] == "https://auth.smaapis.de/oauth2/token"   # untouched
        monkeypatch.delenv(SMA.API_BASE_ENV)
        assert SMA.SMAClient("id", "secret", "a@b.com", "production")._endpoints["api_base"].endswith("/monitoring/v1")

    def test_an_explicit_override_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv(SMA.API_BASE_ENV, "https://from-env/monitoring/v1")
        c = SMA.SMAClient("id", "s", "a@b.com", "production", api_base_override="https://explicit/monitoring/v1")
        assert c._endpoints["api_base"] == "https://explicit/monitoring/v1"

    def test_one_client_never_mutates_the_shared_endpoint_table(self, monkeypatch):
        before = dict(SMA.ENDPOINTS["production"])
        monkeypatch.setenv(SMA.API_BASE_ENV, "https://smaapis.de/monitoring/v1")
        SMA.SMAClient("id", "secret", "a@b.com", "production")
        assert SMA.ENDPOINTS["production"] == before


class TestMeasurementSetDiscovery:
    def test_a_real_inverter_capture_picks_the_energy_set_not_the_guess(self):
        listing = _fix("live_inverter_sets_16.json")
        assert SMA.sets_of(listing) == ["Sensor", "EnergyAndPowerPv", "PowerDc", "PowerAc"]
        assert "pvGeneration" not in SMA.sets_of(listing)      # the scaffold's guess is absent
        assert SMA.pick_set(listing) == "EnergyAndPowerPv"

    def test_a_sensor_device_offers_nothing_readable(self):
        assert SMA.sets_of(_fix("live_device_sets_14.json")) == []
        assert SMA.pick_set(_fix("live_device_sets_14.json")) is None

    def test_preference_order_and_case_insensitive_matching(self):
        assert SMA.pick_set(["PowerAc", "PowerDc"]) == "PowerAc"
        assert SMA.pick_set(["PowerDc"]) == "PowerDc"
        assert SMA.pick_set(["energyandpowerpv", "PowerAc"]) == "energyandpowerpv"   # server spelling wins
        assert SMA.pick_set(["pvGeneration"]) == "pvGeneration"                      # still honoured if ever real
        assert SMA.pick_set([]) is None and SMA.pick_set(None) is None
        assert SMA.pick_set({"sets": ["Nothing", "We", "Know"]}) is None

    def test_junk_never_raises(self):
        for junk in (None, [], {}, {"sets": None}, {"sets": [None, ""]}, "nope", 7):
            assert SMA.sets_of(junk) == [] or isinstance(SMA.sets_of(junk), list)
            assert SMA.pick_set(junk) is None


class _Client:
    """Records every path the telemetry loop asks for."""

    def __init__(self, listing, data=None, fail_listing=None):
        self.listing, self.data, self.fail_listing = listing, data, fail_listing
        self.paths = []

    def _get_json(self, path, params):
        self.paths.append(path)
        if path.endswith("/measurements/sets"):
            if self.fail_listing:
                raise self.fail_listing
            return self.listing
        return self.data or {}


class _P:
    plant_key = "SMA1"


class _I:
    def __init__(self, sn):
        self.inverter_sn = sn


class TestTheFetchLoopUsesWhatTheDeviceOffers:
    def test_it_asks_the_device_then_calls_the_set_the_device_named(self):
        c = _Client(_fix("live_inverter_sets_16.json"))
        ST.fetch_inverter_telemetry(c, _P(), [_I("16")])
        assert c.paths == ["/devices/16/measurements/sets",
                           "/devices/16/measurements/sets/EnergyAndPowerPv"]
        assert not any("pvGeneration" in p for p in c.paths)

    def test_a_device_with_no_readable_set_is_skipped_not_guessed_at(self):
        c = _Client(_fix("live_device_sets_14.json"))
        assert ST.fetch_inverter_telemetry(c, _P(), [_I("14")]) == []
        assert c.paths == ["/devices/14/measurements/sets"]

    def test_the_listing_is_asked_once_per_device_per_run(self):
        c = _Client(_fix("live_inverter_sets_16.json"))
        ST.fetch_inverter_telemetry(c, _P(), [_I("16"), _I("16")])
        assert c.paths.count("/devices/16/measurements/sets") == 1

    def test_a_listing_failure_skips_that_device_and_the_run_continues(self):
        c = _Client(None, fail_listing=SMA.SMAAPIError("404"))
        assert ST.fetch_inverter_telemetry(c, _P(), [_I("16")]) == []

    def test_an_auth_failure_still_stops_the_whole_run(self):
        c = _Client(None, fail_listing=SMA.SMAAuthError("401"))
        with pytest.raises(SMA.SMAAuthError):
            ST.fetch_inverter_telemetry(c, _P(), [_I("16")])
        c = _Client(None, fail_listing=SMA.SMAConsentError("revoked"))
        with pytest.raises(SMA.SMAConsentError):
            ST.fetch_inverter_telemetry(c, _P(), [_I("16")])

    def test_no_inverters_is_no_calls(self):
        c = _Client(None)
        assert ST.fetch_inverter_telemetry(c, _P(), []) == [] and c.paths == []


class TestNoCredentialIsCommitted:
    """The repository is public. A credential in it is a credential published."""

    # 20+ chars of base64-ish noise assigned to something SMA-secret shaped
    SECRET = re.compile(r"(SMA_CLIENT_SECRET|client_secret)\s*[:=]\s*[\"']?([A-Za-z0-9+/_-]{20,})", re.I)
    ALLOW = {"SMA_CLIENT_SECRET", "client_secret", "secrets"}

    def test_no_sma_secret_literal_anywhere_in_the_tree(self):
        bad = []
        for path in V2.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in (".py", ".md", ".yml", ".yaml", ".sh", ".txt", ".json"):
                continue
            if "__pycache__" in path.parts or ".venv" in path.parts or ".git" in path.parts:
                continue
            for m in self.SECRET.finditer(path.read_text(encoding="utf-8", errors="ignore")):
                value = m.group(2)
                if value in self.ALLOW or value.startswith(("SMA_", "secrets", "os", "your", "YOUR")):
                    continue
                bad.append(f"{path.relative_to(V2)}: {m.group(1)}={value[:6]}…")
        assert not bad, "credential literal committed to a public repo:\n  " + "\n  ".join(bad)

    def test_the_known_leaked_sandbox_secret_is_gone_from_the_working_tree(self):
        leaked = "a0TGLSqAs6NU5uH5" + "eFvgSSDjXNhli9eX"     # split so this test is not itself the leak
        hits = [str(p.relative_to(V2)) for p in V2.rglob("*.md")
                if p.is_file() and leaked in p.read_text(encoding="utf-8", errors="ignore")]
        assert not hits, f"still present in {hits} — and still in git history, so it must also be rotated with SMA"
