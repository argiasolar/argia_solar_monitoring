"""v330 - SolarEdge Monitoring API v2: transport, credit budget, probe.

v1 is retired on 1 Nov 2026. v2 bills every call in credits; ARGIA runs on
the free budget (2,000 a month, Tomasz 28 Oct 2026: hourly data is enough
for the two CAPEX SolarEdge plants). The budget guard refuses a call that
would cross it, so a bug can never burn the month in a day.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

from argia.vendors import solaredge_v2 as V2

V2DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2DIR / "scripts"))
import solaredge_v2_probe as P  # noqa: E402

OCT = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)
NOV = dt.datetime(2026, 11, 2, 12, tzinfo=dt.timezone.utc)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class _Session:
    def __init__(self, *resps):
        self.resps, self.calls = list(resps), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        return self.resps.pop(0)


class TestCreditBudget:
    def test_spends_and_counts_down(self, tmp_path):
        b = V2.CreditBudget(str(tmp_path / "c.json"), monthly=10, margin=2)
        assert b.spend(1, OCT) == 9
        assert b.spend(3, OCT) == 6
        assert b.remaining(OCT) == 6

    def test_refuses_before_the_reserve(self, tmp_path):
        b = V2.CreditBudget(str(tmp_path / "c.json"), monthly=10, margin=2)
        b.spend(8, OCT)
        with pytest.raises(V2.SolarEdgeV2BudgetExceeded):
            b.spend(1, OCT)
        assert b.remaining(OCT) == 2                    # the refused call cost nothing

    def test_new_month_starts_from_zero(self, tmp_path):
        b = V2.CreditBudget(str(tmp_path / "c.json"), monthly=10, margin=2)
        b.spend(8, OCT)
        assert b.remaining(NOV) == 10
        assert b.spend(1, NOV) == 9

    def test_a_broken_state_file_is_not_a_crash(self, tmp_path):
        p = tmp_path / "c.json"
        p.write_text("{not json")
        assert V2.CreditBudget(str(p), monthly=10, margin=0).spend(1, OCT) == 9


class TestClient:
    def _client(self, tmp_path, *resps, account=""):
        s = _Session(*resps)
        c = V2.SolarEdgeV2Client("K", account, budget=V2.CreditBudget(str(tmp_path / "c.json"), 50, 0), session=s)
        return c, s

    def test_headers_and_url(self, tmp_path):
        c, s = self._client(tmp_path, _Resp(200, {"sites": []}), account="A")
        assert c.get("/sites", {"x": 1}) == {"sites": []}
        url, params, hdr = s.calls[0]
        assert url == "https://monitoringapi.solaredge.com/v2/sites" and params == {"x": 1}
        assert hdr == {"X-API-Key": "K", "Accept": "application/json", "X-Account-Key": "A"}

    def test_no_key_in_the_url(self, tmp_path):
        c, s = self._client(tmp_path, _Resp(200, {}))
        c.get("sites/1")
        assert "K" not in s.calls[0][0] and "api_key" not in s.calls[0][1]

    @pytest.mark.parametrize("status,exc", [(401, V2.SolarEdgeV2AuthError), (403, V2.SolarEdgeV2AuthError),
                                            (429, V2.SolarEdgeV2RateLimited), (503, V2.SolarEdgeV2Error)])
    def test_errors(self, tmp_path, status, exc):
        c, _ = self._client(tmp_path, _Resp(status, {"message": "x"}))
        with pytest.raises(exc):
            c.get("sites")

    def test_invalid_json(self, tmp_path):
        c, _ = self._client(tmp_path, _Resp(200, "<html>"))
        with pytest.raises(V2.SolarEdgeV2Error):
            c.get("sites")

    def test_every_call_is_budgeted_even_a_failed_one(self, tmp_path):
        c, _ = self._client(tmp_path, _Resp(503, {}), _Resp(200, {}))
        with pytest.raises(V2.SolarEdgeV2Error):
            c.get("sites")
        c.get("sites")
        assert c._budget.remaining() == 48

    def test_from_env(self, tmp_path):
        with pytest.raises(V2.SolarEdgeV2AuthError):
            V2.SolarEdgeV2Client.from_env({})
        c = V2.SolarEdgeV2Client.from_env({V2.ENV_API_KEY: " K ", V2.ENV_MONTHLY: "20000"},
                                          budget=None)
        assert c._headers["X-API-Key"] == "K" and c._budget.monthly == 20000


class TestTheScheduleFitsTheFreePlan:
    def test_hourly_two_sites_fits_with_room_for_probes(self):
        # 2 sites, 06:03-20:03 hourly = 15 polls/day, one site-level call each,
        # + 3 nightly calls per site (counters) - a 31-day month
        need = V2.monthly_credits(sites=2, polls_per_day=15)
        assert need == 1116
        assert need <= V2.MONTHLY_CREDITS_DEFAULT - V2.SAFETY_MARGIN

    def test_todays_v1_cadence_would_not_fit(self):
        # every 20 min (45 polls), one call per inverter (5 per site)
        assert V2.monthly_credits(sites=2, polls_per_day=45, calls_per_poll=5) > V2.MONTHLY_CREDITS_DEFAULT


class TestProbe:
    def test_dry_run_spends_nothing(self, capsys, monkeypatch):
        monkeypatch.setattr(V2.SolarEdgeV2Client, "from_env", classmethod(lambda cls, *a, **k: pytest.fail("no client")))
        assert P.main(["--site", "4362085", "--day", "2026-10-27", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "GET /v2/sites/4362085/devices {'types': 'INVERTER'}" in out
        assert "8 call(s) = 8 credit(s)" in out

    def test_shape_never_shows_values(self):
        s = P.shape({"site": {"name": "Hirschmann", "peakPower": 561.6, "inv": [{"sn": "7E05"}] * 5}})
        assert "Hirschmann" not in s and "561.6" not in s and "7E05" not in s
        assert s == "{site: {name: str, peakPower: float, inv: [5 x {sn: str}]}}"

    def test_calls_cover_what_the_monitoring_needs(self):
        names = [c[0] for c in P.calls("1", dt.date(2026, 10, 27))]
        for n in ("devices_inverters", "energy_day", "inverters_telemetry", "overview"):
            assert n in names
        energy = dict((c[0], c[2]) for c in P.calls("1", dt.date(2026, 10, 27)))["energy_day"]
        assert energy == {"from": "2026-10-01T00:00:00Z", "to": "2026-10-28T00:00:00Z", "resolution": "DAY"}
