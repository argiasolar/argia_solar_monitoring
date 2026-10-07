"""v316: one Open-Meteo hiccup no longer fails the daily satellite check.

7 Oct 2026, mail "[ARGIA] still failing after 6 h: job failed:
argia-satcheck.service": at 06:20 MX the TLS handshake to Open-Meteo timed
out for SLP2, the job exited 1 and the daily unit stayed "failed" all day.
Every other plant had its verdict. Now each fetch is tried 3 times (10 s,
30 s apart); a plant still unreachable gets an honest NO_DATA row saying
why; the job fails only if NO plant could be reached or a store failed.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))

import satellite_check as S  # noqa: E402

DAYS = [(dt.date(2026, 10, 7) - dt.timedelta(days=k)).isoformat() for k in range(40, 0, -1)]
PAYLOAD = {"daily_units": {"shortwave_radiation_sum": "kWh/m²"},
           "daily": {"time": DAYS, "shortwave_radiation_sum": [6.0] * len(DAYS)}}


@pytest.fixture
def run(monkeypatch):
    stored, sleeps, calls = [], [], {}

    def go(behaviour, store_fails=False):
        """behaviour: {plant: list of results per try; an Exception is raised}"""
        monkeypatch.setattr(S.pg_mirror, "enabled", lambda: True)
        monkeypatch.setattr(S, "plant_coords", lambda: [(pk, 20.0 + i, -100.0) for i, pk in enumerate(behaviour)])
        monkeypatch.setattr(S, "measured_series", lambda: {pk: {d: 5.4 for d in DAYS} for pk in behaviour})
        monkeypatch.setattr(S, "build_url", lambda lat, lon: f"u{lat:.0f}")
        url_pk = {f"u{20 + i}": pk for i, pk in enumerate(behaviour)}

        def fake_fetch(url):
            pk = url_pk[url]
            n = calls.get(pk, 0)
            calls[pk] = n + 1
            r = behaviour[pk][min(n, len(behaviour[pk]) - 1)]
            if isinstance(r, Exception):
                raise r
            return r

        def fake_exec(sql):
            if store_fails and "INSERT" in sql:
                raise RuntimeError("disk full")
            stored.append(sql)
        monkeypatch.setattr(S, "fetch_json", fake_fetch)
        monkeypatch.setattr(S, "SLEEP", sleeps.append)
        monkeypatch.setattr(S, "psql_exec", fake_exec)
        return S.main([])
    go.stored, go.sleeps, go.calls = stored, sleeps, calls
    return go


TIMEOUT = TimeoutError("_ssl.c:1012: The handshake operation timed out")


def test_a_transient_timeout_is_retried_and_the_plant_gets_its_verdict(run):
    assert run({"SLP1": [PAYLOAD], "SLP2": [TIMEOUT, PAYLOAD]}) == 0
    assert run.calls == {"SLP1": 1, "SLP2": 2} and run.sleeps == [10]
    slp2 = [s for s in run.stored if "'SLP2'" in s][0]
    assert "'OK'" in slp2 and "unreachable" not in slp2


def test_the_7_october_case_no_longer_fails_the_job(run):
    """SLP2 unreachable on every try: the other plants keep their verdicts,
    SLP2 gets an honest NO_DATA row, the unit ends green."""
    assert run({"SLP1": [PAYLOAD], "SLP2": [TIMEOUT], "TAM1": [PAYLOAD]}) == 0
    assert run.calls["SLP2"] == S.FETCH_TRIES == 3 and run.sleeps == [10, 30]
    slp2 = [s for s in run.stored if "'SLP2'" in s][0]
    assert "'NO_DATA'" in slp2 and "unreachable after 3 tries" in slp2 and "handshake" in slp2
    assert sum("'TAM1'" in s for s in run.stored) == 1


def test_nobody_reachable_still_fails(run):
    assert run({"SLP1": [TIMEOUT], "SLP2": [OSError("network unreachable")]}) == 1


def test_a_store_failure_still_fails(run):
    assert run({"SLP1": [PAYLOAD]}, store_fails=True) == 1


def test_an_unusable_answer_is_not_retried_and_still_fails(run):
    """A wrong unit or shape is not a network blip: no retry, exit 1."""
    bad = {"daily_units": {"shortwave_radiation_sum": "furlongs"}, "daily": {"time": DAYS, "shortwave_radiation_sum": [1] * 40}}
    assert run({"SLP1": [bad], "SLP2": [PAYLOAD]}) == 1 and run.calls["SLP1"] == 1 and run.sleeps == []


def test_the_note_fits_the_column_and_says_why():
    dc = S.unreachable_verdict(TimeoutError("x" * 500))
    assert dc.status == "NO_DATA" and len(dc.note) < 200 and dc.n_recent == 0


# ------------------------------------------------------------------ ticket price = Losses page price
def test_a_plant_ticket_day_carries_the_losses_page_price():
    """Found while running the suite on 7 Oct: loss_daily prices the unrounded
    kWh (750.24 kWh x 2.30 = 1,725.55 -> '$1,726' on the Losses page), the
    ticket page re-priced the stored 750.2 kWh (1,725.46 -> '$1,725'). A plant
    ticket now shows loss_daily's own figure; an inverter ticket still prices
    its own shortfall."""
    from argia.maintenance import ticket_cost as TC
    day = dt.date(2026, 10, 5)
    pd = {"2026-10-05": TC.PlantDay(750.2, 2.3, 750.2, 0.0, 0.0, 1725.55)}
    (d,) = TC.ticket_days([day], pd)
    assert d.mxn == 1725.55 and f"{TC.total([d])[1]:,.0f}" == "1,726"
    (old,) = TC.ticket_days([day], {"2026-10-05": TC.PlantDay(750.2, 2.3, 750.2)})     # no lost_mxn: as before
    assert old.mxn == round(750.2 * 2.3, 2)
    inv = {"2026-10-05": (10.0, 20.0, [5.0, 5.0])}                                      # shortfall 30 kWh
    (i,) = TC.ticket_days([day], pd, inv)
    assert i.basis == "inverter" and i.mxn == round(i.lost_kwh * 2.3, 2)
