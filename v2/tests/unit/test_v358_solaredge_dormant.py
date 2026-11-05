"""v358: a SolarEdge plant that sends no data (QRO1 Tetra Pak) costs one
credit a day, not one an hour, and comes back by itself when it reports.

Tomasz 2026-11-05: "drop the tetrapak, leave one ping per day to see if it works".
"""
from __future__ import annotations

import logging
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from argia.vendors import solaredge_dormant as SED

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))


def P(key):
    return SimpleNamespace(plant_key=key)


# ------------------------------------------------------------------ pure rules
def test_qro1_is_dormant_and_gto2_is_not():
    assert SED.is_dormant("QRO1") and SED.is_dormant("qro1")
    assert not SED.is_dormant("GTO2") and not SED.is_dormant("")


def test_a_dormant_plant_is_polled_only_at_the_ping_hour():
    hours = [h for h in range(6, 21) if SED.should_poll("QRO1", h, alive=False)]
    assert hours == [SED.PING_HOUR] == [12]


def test_an_active_plant_is_polled_every_hour():
    assert all(SED.should_poll("GTO2", h, alive=False) for h in range(6, 21))


def test_a_dormant_plant_that_reports_again_is_polled_every_hour():
    assert all(SED.should_poll("QRO1", h, alive=True) for h in range(6, 21))


def test_counter_calls_skip_a_dormant_plant_unless_it_reports():
    plants = [P("GTO2"), P("QRO1")]
    assert [p.plant_key for p in SED.counter_plants(plants, lambda k: False)] == ["GTO2"]
    assert [p.plant_key for p in SED.counter_plants(plants, lambda k: True)] == ["GTO2", "QRO1"]


def test_credits_a_month_drop_from_hourly_to_one_a_day():
    polls = sum(SED.should_poll("QRO1", h, alive=False) for h in range(6, 21))
    assert polls * 30 == 30                                     # was 15 x 30 = 450 telemetry + ~120 counters


# ------------------------------------------------------------------ the database read
def test_alive_reads_real_data_of_the_last_days():
    sql = SED.alive_sql("QRO1")
    assert "plant_key = 'QRO1'" in sql and "interval '3 days'" in sql and "power_w" in sql


def test_alive_is_true_only_with_rows():
    with patch("argia.store.pgq.psql_rows", return_value=[["4"]]):
        assert SED.alive("QRO1") is True
    with patch("argia.store.pgq.psql_rows", return_value=[["0"]]):
        assert SED.alive("QRO1") is False


def test_a_database_error_keeps_the_plant_on_the_daily_ping():
    with patch("argia.store.pgq.psql_rows", side_effect=RuntimeError("pg down")):
        assert SED.alive("QRO1") is False                       # never more credits by accident


# ------------------------------------------------------------------ the telemetry job
def _portfolio(keys):
    plants = [SimpleNamespace(plant_key=k, secret_api_name="SE_KEY", site_id="1", brand="SOLAREDGE") for k in keys]
    pf = MagicMock()
    pf.plants_by_brand.return_value = plants
    pf.inverters_for.return_value = [SimpleNamespace(inverter_sn="7E000000-01", inverter_label="Inv 1")]
    return pf


def _run(keys, hour, alive=False, rows=None):
    import telemetry_5m as T
    seen = []

    def fake_process(plant, *a, **k):
        seen.append(plant.plant_key)
        return (rows or []), 0
    with patch.object(T, "now_mx", return_value=SimpleNamespace(hour=hour)), \
            patch.object(T.SED, "alive", return_value=alive), \
            patch.object(T, "SolarEdgeClient"), \
            patch.object(T, "_fetch_weather_for_plant", return_value=None), \
            patch.object(T, "_process_solaredge_plant", side_effect=fake_process), \
            patch.dict(os.environ, {"SE_KEY": "x"}):
        out = T._run_solaredge(_portfolio(keys), None, "2026-11-05", None, None, None, False,
                               logging.getLogger("t"))
    return seen, out


def test_the_job_skips_qro1_outside_the_ping_hour():
    seen, (_c, processed, skipped, errors) = _run(["GTO2", "QRO1"], hour=9)
    assert seen == ["GTO2"] and processed == 1 and skipped == 1 and errors == 0


def test_the_job_pings_qro1_at_noon():
    seen, (_c, processed, _s, _e) = _run(["GTO2", "QRO1"], hour=12)
    assert seen == ["GTO2", "QRO1"] and processed == 2


def test_the_job_polls_qro1_hourly_once_it_reports_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        seen, _ = _run(["QRO1"], hour=9, alive=True, rows=[["row"]])
    assert seen == ["QRO1"]
    assert "DORMANT plant reported 1 telemetry row(s) - it works again" in caplog.text


# ------------------------------------------------------------------ nightly counters
def test_the_nightly_snapshot_makes_no_call_for_a_dormant_plant():
    import recon_snapshot as RS
    plants = [SimpleNamespace(plant_key="QRO1", brand="SOLAREDGE"), SimpleNamespace(plant_key="GTO2", brand="SOLAREDGE")]
    with patch.object(RS.SED, "alive", return_value=False):
        kept = RS.SED.counter_plants(plants, RS.SED.alive)
    assert [p.plant_key for p in kept] == ["GTO2"]


@pytest.mark.parametrize("path", ["scripts/telemetry_5m.py", "scripts/recon_snapshot.py"])
def test_both_jobs_use_the_dormant_rule(path):
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", path), encoding="utf-8").read()
    assert "solaredge_dormant as SED" in src
    if path.endswith("recon_snapshot.py"):
        assert src.count("SED.counter_plants(") == 2                # the snapshot AND the retry pass
