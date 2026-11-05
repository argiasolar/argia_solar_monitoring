"""v348: SolarEdge on the Monitoring API v2.

SolarEdge retired v1 on 3 Nov 2026 (every v1 call: HTTP 410 "API Version
Deprecated"); the telemetry job kept logging "OK, errors=0" with 0 rows for
three days. The fixtures below copy the SHAPES captured from the live v2 API
on 5 Nov 2026 (GTO2 probe); the numbers are synthetic - the repo is public.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from argia.recon import counters as C
from argia.vendors import solaredge_v2 as V2
from argia.vendors.solaredge import SolarEdgeAPIError, SolarEdgeAuthError, SolarEdgeClient
from argia.vendors.solaredge_telemetry import (fetch_inverter_telemetry, parse_telemetry_entries,
                                               rows_from_v2)

UTC = dt.timezone.utc


def energy_body(days, unit="WH", res="DAY"):
    return {"period": {"from": days[0][0] + "T00:00:00-06:00", "to": "x"}, "unit": unit, "resolution": res,
            "values": [{"timestamp": d + "T00:00:00-06:00", "value": v} for d, v in days]}


def series(start, vals, unit):
    t0 = dt.datetime.fromisoformat(start)
    return {"period": {"from": start, "to": "x"}, "unit": unit, "resolution": "QUARTER_HOUR",
            "values": [{"timestamp": (t0 + dt.timedelta(minutes=15 * i)).isoformat(), "value": v}
                       for i, v in enumerate(vals)]}


def telemetry_body(sns=("7E0514A6-3D", "7B115A29-0F")):
    start = "2026-11-04T06:00:00-06:00"
    # 06:00 null (asleep), 06:15 first light, 06:30, 06:45
    power = [None, 400.0, 8000.0, 16000.0]
    energy = [None, 50.0, 1000.0, 3000.0]                 # Wh per 15-min bucket
    inv = {"power": series(start, power, "W"), "energy": series(start, energy, "WH"),
           "voltage": series(start, [None, 230.1, 230.4, 230.2], "VOLT"),
           "current": series(start, [None, 1.0, 11.0, 23.0], "AMPERE"),
           "frequency": series(start, [None, 60.0, 60.01, 59.99], "HERTZ")}
    return {"inverters": {sn: inv for sn in sns}}


class FakeV2:
    """Records calls; answers like the live API."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        a = self.answers(path, params or {})
        if isinstance(a, Exception):
            raise a
        return a


# ------------------------------------------------------------ time handling
def test_local_midnight_and_local_times_go_out_as_utc():
    assert V2.local_midnight_z(dt.date(2026, 11, 4)) == "2026-11-04T06:00:00Z"
    assert V2.local_to_z("2026-11-05 00:00:00") == "2026-11-05T06:00:00Z"
    assert V2.local_to_z("2026-11-05 13:42:10") == "2026-11-05T19:42:10Z"


@pytest.mark.parametrize("s,want", [
    ("2026-11-04T06:15:00-06:00", dt.datetime(2026, 11, 4, 12, 15, tzinfo=UTC)),
    ("2026-11-05T15:19:39.873Z", dt.datetime(2026, 11, 5, 15, 19, 39, tzinfo=UTC)),
    ("2026-11-05T09:21:37.316204832-06:00", dt.datetime(2026, 11, 5, 15, 21, 37, tzinfo=UTC)),
])
def test_v2_timestamps(s, want):
    assert V2.parse_ts(s).astimezone(UTC).replace(microsecond=0) == want


def test_day_windows_stay_under_the_one_month_limit():
    w = V2.day_windows(dt.date(2026, 9, 6), dt.date(2026, 11, 4))
    assert w[0][0] == dt.date(2026, 9, 6) and w[-1][1] == dt.date(2026, 11, 5)
    assert all((b - a).days <= 28 for a, b in w)
    assert all(w[i][1] == w[i + 1][0] for i in range(len(w) - 1))           # no gap, no overlap


# ------------------------------------------------------------ v1 calls answered from v2
def test_daily_energy_reaches_the_v1_parsers_unchanged():
    fake = FakeV2(lambda p, q: energy_body([("2026-11-01", 1823541.0), ("2026-11-02", 1787591.0),
                                            ("2026-11-03", None), ("2026-11-04", 1860552.0)]))
    resp = V2.V1Compat(fake).get("/site/4362085/energy",
                                 {"timeUnit": "DAY", "startDate": "2026-11-01", "endDate": "2026-11-04"})
    assert fake.calls == [("sites/4362085/energy", {"from": "2026-11-01T06:00:00Z",
                                                    "to": "2026-11-05T06:00:00Z", "resolution": "DAY"})]
    assert C.solaredge_daily_series(resp) == {"2026-11-01": 1823.541, "2026-11-02": 1787.591,
                                              "2026-11-03": None, "2026-11-04": 1860.552}
    assert SolarEdgeClient._parse_day_kwh(resp, "2026-11-02") == 1787.591


def test_a_long_day_range_is_split_into_month_safe_calls():
    fake = FakeV2(lambda p, q: energy_body([(q["from"][:10], 1000.0)]))
    V2.V1Compat(fake).get("/site/1/energy", {"timeUnit": "DAY", "startDate": "2026-09-06", "endDate": "2026-11-04"})
    assert len(fake.calls) == 3 and all(c[1]["resolution"] == "DAY" for c in fake.calls)


def test_month_energy_and_lifetime_for_the_nightly_counters():
    def ans(p, q):
        if p.endswith("lifetime-energy"):
            return {"energy": 1335487900.0, "unit": "WH", "lastUpdateTime": "2026-11-05T15:21:24.456Z"}
        return energy_body([("2026-11-01", 7269593.0)], res="MONTH")
    fake = FakeV2(ans)
    compat = V2.V1Compat(fake)
    month = compat.get("/site/9/energy", {"timeUnit": "MONTH", "startDate": "2026-11-01", "endDate": "2026-11-04"})
    assert fake.calls[0][1] == {"from": "2026-11-01T06:00:00Z", "to": "2026-11-05T06:00:00Z", "resolution": "MONTH"}
    assert C.solaredge_energy_kwh(month) == 7269.593
    assert C.solaredge_lifetime_kwh(compat.get("/site/9/overview", {})) == 1335487.9


def test_inverter_list_keeps_the_active_inverters():
    body = [{"type": "INVERTER", "serialNumber": "7E051918-B4", "name": "Inverter 5", "active": True},
            {"type": "INVERTER", "serialNumber": "7E051184-18", "name": "Inverter 1", "active": False},
            {"type": "INVERTER", "serialNumber": "7B115A29-0F", "name": "Inverter 1", "active": True}]
    out = V2.V1Compat(FakeV2(lambda p, q: body)).get("/equipment/4362085/list", {})
    assert [i["serialNumber"] for i in out["reporters"]["list"]] == ["7E051918-B4", "7B115A29-0F"]


def test_inverter_data_one_call_per_site_and_eToday_through_the_v1_parser():
    fake = FakeV2(lambda p, q: telemetry_body())
    compat = V2.V1Compat(fake)
    win = {"startTime": "2026-11-04 00:00:00", "endTime": "2026-11-04 07:00:00"}
    a = compat.get("/equipment/4362085/7E0514A6-3D/data", win)
    compat.get("/equipment/4362085/7B115A29-0F/data", win)
    assert len(fake.calls) == 1                                               # two inverters, one credit
    assert fake.calls[0] == ("sites/4362085/inverters/telemetry",
                             {"from": "2026-11-04T06:00:00Z", "to": "2026-11-04T13:00:00Z"})
    rows = parse_telemetry_entries(a, "GTO2", "7E0514A6-3D")
    last = rows[-1]
    assert last.etoday_kwh == 4.05                                            # 50 + 1000 + 3000 Wh, nothing lost
    assert last.power_w == 16000.0 and last.l1.ac_frequency_hz == 59.99
    assert last.timestamp_utc == dt.datetime(2026, 11, 4, 13, 0, tzinfo=UTC)  # end of the 06:45 bucket


def test_unknown_call_is_refused_not_guessed():
    with pytest.raises(V2.SolarEdgeV2Error):
        V2.V1Compat(FakeV2(lambda p, q: {})).get("/site/1/powerDetails", {})


# ------------------------------------------------------------ live telemetry rows
class Inv(SimpleNamespace):
    pass


def test_rows_from_v2_cumulative_eToday_and_skip_sleeping_buckets():
    now = dt.datetime(2026, 11, 4, 12, 50, tzinfo=UTC)                        # inside the 06:45 MX bucket
    rows = rows_from_v2(telemetry_body(), "GTO2", [Inv(inverter_sn="7e0514a6-3d")], now=now)
    assert [r.etoday_kwh for r in rows] == [0.05, 1.05, 4.05]
    assert rows[-1].timestamp_utc == now                                      # the open bucket is capped at now
    assert rows[0].timestamp_utc == dt.datetime(2026, 11, 4, 12, 30, tzinfo=UTC)
    assert all(r.status == 1 and r.raw_mode == "V2" and r.etotal_kwh is None for r in rows)


def test_rows_from_v2_only_configured_inverters_and_the_overlap_window():
    now = dt.datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
    rows = rows_from_v2(telemetry_body(), "GTO2", [Inv(inverter_sn="7B115A29-0F")], now=now,
                        min_ts_utc=dt.datetime(2026, 11, 4, 12, 40, tzinfo=UTC))
    assert {r.inverter_sn for r in rows} == {"7B115A29-0F"} and len(rows) == 2


def _v2_client(answer):
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = answer
    session.get.return_value = resp
    budget = MagicMock()
    budget.spend.return_value = 1000
    return V2.SolarEdgeV2Client("k", budget=budget, session=session), session


def test_the_telemetry_job_spends_one_call_per_site():
    v2, session = _v2_client(telemetry_body())
    client = SolarEdgeClient(api_key="old-v1-key", v2_client=v2)
    plant = SimpleNamespace(plant_key="GTO2", site_id="4362085")
    fetch_inverter_telemetry(client, plant, [Inv(inverter_sn="7E0514A6-3D"), Inv(inverter_sn="7B115A29-0F")])
    assert session.get.call_count == 1
    url = session.get.call_args[0][0]
    assert url.endswith("/v2/sites/4362085/inverters/telemetry")
    assert session.get.call_args[1]["headers"]["X-API-Key"] == "k"


def test_every_poll_rebuilds_the_whole_local_day():
    """A missed poll or a server outage earlier in the day is healed by the
    next poll: the call starts at local midnight and every bucket is kept."""
    v2, session = _v2_client(telemetry_body())
    client = SolarEdgeClient(api_key="old-v1-key", v2_client=v2)
    plant = SimpleNamespace(plant_key="GTO2", site_id="4362085")
    rows = fetch_inverter_telemetry(client, plant, [Inv(inverter_sn="7E0514A6-3D"), Inv(inverter_sn="7B115A29-0F")])
    params = session.get.call_args[1]["params"]
    start = dt.datetime.strptime(params["from"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(V2.MX_OFFSET)
    assert (start.hour, start.minute, start.second) == (0, 0, 0)
    # the synthetic body is from 4 Nov: all its non-sleeping buckets come back, none dropped by age
    assert len(rows) == len(rows_from_v2(telemetry_body(), "GTO2", [Inv(inverter_sn="7E0514A6-3D"),
                                                                      Inv(inverter_sn="7B115A29-0F")]))
    assert len(rows) >= 5


def test_v2_errors_reach_the_job_as_the_errors_it_already_handles():
    for exc, want, text in ((V2.SolarEdgeV2AuthError("401"), SolarEdgeAuthError, "401"),
                            (V2.SolarEdgeV2BudgetExceeded("budget"), SolarEdgeAPIError, "rate-limited"),
                            (V2.SolarEdgeV2RateLimited("429"), SolarEdgeAPIError, "rate-limited"),
                            (V2.SolarEdgeV2Error("HTTP 500"), SolarEdgeAPIError, "HTTP 500")):
        v2 = MagicMock(spec=V2.SolarEdgeV2Client)
        v2.get.side_effect = exc
        client = SolarEdgeClient(api_key="x", v2_client=v2)
        with pytest.raises(want, match=text):
            client._get_json("/site/1/overview", {})


def test_without_the_v2_key_the_client_stays_on_v1(monkeypatch):
    monkeypatch.delenv(V2.ENV_API_KEY, raising=False)
    assert SolarEdgeClient(api_key="x")._v2 is None
    monkeypatch.setenv(V2.ENV_API_KEY, "v2key")
    assert isinstance(SolarEdgeClient(api_key="x")._v2, V2.SolarEdgeV2Client)


# ------------------------------------------------------------ the silent failure
def test_every_inverter_failing_is_an_error_not_an_empty_success():
    """5 Nov 2026: 'DONE ... rows_collected=0 errors=0 status=OK' for three days of HTTP 410"""
    client = MagicMock()
    client._v2 = None
    client._get_json.side_effect = SolarEdgeAPIError("HTTP 410 API Version Deprecated")
    with pytest.raises(SolarEdgeAPIError, match="every inverter call failed"):
        fetch_inverter_telemetry(client, SimpleNamespace(plant_key="QRO1", site_id="1"),
                                 [Inv(inverter_sn="A"), Inv(inverter_sn="B")])


def test_one_inverter_failing_still_returns_the_others():
    client = MagicMock()
    client._v2 = None
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-6))).strftime("%Y-%m-%d %H:%M:%S")
    good = {"data": {"telemetries": [{"date": now, "totalActivePower": 10.0, "totalEnergy": 5.0}]}}
    client._get_json.side_effect = [SolarEdgeAPIError("HTTP 500"), good]
    rows = fetch_inverter_telemetry(client, SimpleNamespace(plant_key="GTO2", site_id="1"),
                                    [Inv(inverter_sn="A"), Inv(inverter_sn="B")])
    assert [r.inverter_sn for r in rows] == ["B"]


# ------------------------------------------------------------ credits
def test_the_schedule_fits_the_free_plan():
    """hourly 06-20 (15 polls) x 2 sites + 3 nightly counter calls per site"""
    assert V2.monthly_credits(sites=2, polls_per_day=15, nightly_calls_per_site=3) == 1116
    assert V2.monthly_credits(sites=2, polls_per_day=15, nightly_calls_per_site=3) < V2.MONTHLY_CREDITS_DEFAULT - V2.SAFETY_MARGIN


def test_the_timer_is_hourly():
    import pathlib
    t = (pathlib.Path(V2.__file__).parents[2] / "server" / "bundle" / "argia-telemetry-se.timer").read_text()
    assert "OnCalendar=*-*-* 06..20:07:00 America/Mexico_City" in t


# ------------------------------------------------------------ per-minute pacing
class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_pacer_lets_nine_calls_through_then_waits_for_the_window():
    c = _Clock()
    p = V2.MinutePacer(9, clock=c, sleep=c.sleep)
    for _ in range(9):
        assert p.wait() == 0.0
        c.t += 1.0
    waited = p.wait()                       # the 10th call within the minute
    assert waited > 0 and c.slept == [waited]
    assert c.t - 1000.0 >= 60.0             # it went out after the first call left the window


def test_pacer_never_waits_at_the_hourly_telemetry_rate():
    c = _Clock()
    p = V2.MinutePacer(9, clock=c, sleep=c.sleep)
    for _ in range(30):
        p.wait()
        c.t += 7.0                          # ~8.6 calls a minute
    assert c.slept == []


def test_a_year_of_history_is_paced_not_rejected():
    """vendor_history_import asks 365 days of DAY energy: 14 one-month windows."""
    c = _Clock()
    pacer = V2.MinutePacer(9, clock=c, sleep=c.sleep)
    session = MagicMock()
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"unit": "WH", "values": []}
    session.get.return_value = resp
    budget = MagicMock()
    budget.spend.return_value = 1000
    client = V2.SolarEdgeV2Client("k", budget=budget, session=session, pacer=pacer)
    V2.V1Compat(client).get("/site/1/energy", {"timeUnit": "DAY", "startDate": "2025-11-01",
                                                "endDate": "2026-10-31"})
    assert session.get.call_count == 14 and len(c.slept) == 1


def test_the_default_client_shares_the_process_pacer():
    budget = MagicMock()
    a = V2.SolarEdgeV2Client("k", budget=budget, session=MagicMock())
    b = V2.SolarEdgeV2Client("k", budget=budget, session=MagicMock())
    assert a._pacer is b._pacer is V2.PACER
