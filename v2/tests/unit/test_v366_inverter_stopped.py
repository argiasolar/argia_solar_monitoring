"""v366 - an inverter stopped while its siblings produce, and the vendor's own alarms.

Vitalmex, 7 Nov 2026: at 07:42 MX two of the three inverters stopped
(FusionSolar: "Device Fault", Major, alarm 2064) and kept REPORTING 0 W
every 5 minutes while the third made 110 kW. ARGIA showed the plant green
for a day: the live rules only knew a whole plant at 0 W or a unit that
stops answering, the FusionSolar alarm reader of v257 was never called, and
the tile/map turned amber only for silence. Pinned here:

* acute: 0 W for >= 30 min (>= 3 readings) with the day counter flat next to
  a sibling at >= 5 kW opens the daily tier's ``inverter_relative`` key -
  replayed on the real readings of that morning (fixture, identifiers
  replaced): first alert at the 08:30 tick, none at 08:00;
* a growing counter (vendor zero, MEX2 6 Oct) or a weak sibling is not it;
* vendor alarms: state file from the telemetry session, one alert per
  device with the vendor's words, datalogger capped at WARNING, a failed or
  old fetch opens and closes nothing;
* wiring: the telemetry job refreshes the alarm list and the acute job reads
  it (v257 was built and never called - this is the test that was missing);
* portal: the tile, the map and the inverter row are not green for it.

60-day replay before release (all plants, every 30-min tick): 8 alert-days -
Ryder Inverter 3 (FT=302, 1-3 Oct), Vitalmex 7-8 Nov, one 32-min Holiday Inn
episode (31 Oct); Teneria's export-limited units (counter growing) none.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.analytics import acute as A                       # noqa: E402
from argia.alerts import vendor_alarms as VA                  # noqa: E402
from argia.analytics.inverter_health import Severity          # noqa: E402

UTC = dt.timezone.utc
FIX = V2 / "tests" / "fixtures" / "v366_inverters_stopped_20261107.csv"
ALARMS = V2 / "tests" / "fixtures" / "huawei" / "alarm_list.json"
CONF = {"PLANT1": ["INV-A", "INV-B", "INV-C"]}


def _rows():
    out = []
    with open(FIX, newline="") as fh:
        for r in csv.DictReader(fh):
            f = lambda k: float(r[k]) if r[k] not in ("", None) else None   # noqa: E731
            out.append((dt.datetime.fromisoformat(r["ts_utc"].replace("Z", "+00:00")), r["plant_key"], r["inverter_sn"],
                        f("power_w"), f("temperature_c"), int(r["status"]), r["fault_code"], f("etoday_kwh")))
    return out


def _evaluate(tick_utc, rows=None):
    rows = [r for r in (rows or _rows()) if r[0] <= tick_utc]
    return A.evaluate_acute([r[:7] for r in rows], ["PLANT1"], tick_utc, configured_inverters=CONF,
                            counters=A.day_counters(rows))


def _mx(h, m):      # MX = UTC-6
    return dt.datetime(2026, 11, 7, h + 6, m, 6, tzinfo=UTC)


# ------------------------------------------------------------------ acute rule
class TestTheVitalmexMorning:
    def test_the_fixture_is_that_morning(self):
        rows = _rows()
        stop = [r for r in rows if r[2] in ("INV-A", "INV-C") and r[0] >= _mx(7, 45)]
        assert stop and all(r[3] == 0 for r in stop)
        assert max(r[3] for r in rows if r[2] == "INV-B" and r[0] >= _mx(8, 30)) > 40000

    def test_no_alert_at_0800_only_15_minutes_in(self):
        assert [b for b in _evaluate(_mx(8, 0)) if b.metric == "inverter_relative"] == []

    def test_both_stopped_units_alert_at_0830(self):
        br = [b for b in _evaluate(_mx(8, 30)) if b.metric == "inverter_relative"]
        assert sorted(b.inverter_sn for b in br) == ["INV-A", "INV-C"]
        assert all(b.severity == Severity.CRITICAL for b in br)
        assert "0 W for 45 min (since 07:45 MX" in br[0].message and "day counter flat" in br[0].message
        assert "[CRITICAL]" in br[0].message

    def test_a_growing_counter_is_the_vendors_zero_not_the_unit(self):
        rows = []
        for r in _rows():
            if r[2] == "INV-A" and r[0] >= _mx(7, 45):
                r = r[:7] + (r[7] + (r[0] - _mx(7, 45)).total_seconds() / 600.0,)    # +0.5 kWh per 5 min
            rows.append(r)
        br = [b for b in _evaluate(_mx(8, 30), rows) if b.metric == "inverter_relative"]
        assert [b.inverter_sn for b in br] == ["INV-C"]

    def test_a_weak_sibling_is_no_daylight_proof(self):
        rows = [r[:3] + ((min(r[3], 4000.0) if r[3] is not None else None),) + r[4:] for r in _rows()]
        assert [b for b in _evaluate(_mx(8, 30), rows) if b.metric == "inverter_relative"] == []

    def test_a_unit_not_in_service_is_ignored(self):
        rows = _rows()
        br = A.evaluate_acute([r[:7] for r in rows if r[0] <= _mx(8, 30)], ["PLANT1"], _mx(8, 30),
                              configured_inverters={"PLANT1": ["INV-B", "INV-C"]},
                              counters=A.day_counters(rows))
        assert [b.inverter_sn for b in br if b.metric == "inverter_relative"] == ["INV-C"]

    def test_the_whole_plant_at_zero_is_still_plant_offline_not_this(self):
        rows = [r[:3] + ((0.0 if r[3] is not None else None),) + r[4:] for r in _rows()]
        assert [b for b in _evaluate(_mx(8, 30), rows) if b.metric == "inverter_relative"] == []

    def test_it_uses_the_daily_tiers_key(self):
        from argia.alerts.engine import candidate_from_acute_breach, make_inverter_alert_key
        b = [b for b in _evaluate(_mx(8, 30)) if b.metric == "inverter_relative"][0]
        assert candidate_from_acute_breach(b).alert_key == make_inverter_alert_key("PLANT1", b.inverter_sn, "inverter_relative")


class TestZeroWhileProducing:
    def test_snapshot_view(self):
        assert A.zero_while_producing({"a": 0.0, "b": 112000.0, "c": 3.0}) == ["a", "c"]
        assert A.zero_while_producing({"a": 0.0, "b": 4000.0}) == []
        assert A.zero_while_producing({"a": None, "b": 50000.0}) == []
        assert A.zero_while_producing({}) == []


# ------------------------------------------------------------------ vendor alarms
RAW = json.loads(ALARMS.read_text())["data"]
STATIONS = {"NE=00000001": "PLANT1", "NE=00000002": "PLANT2"}
NOW = dt.datetime(2026, 9, 17, 15, 0, tzinfo=UTC)


class TestVendorAlarms:
    def test_due_on_the_quarter_hours_or_when_late(self):
        last = NOW - dt.timedelta(minutes=5)
        assert VA.due(dt.datetime(2026, 9, 17, 9, 0), last, NOW)
        assert not VA.due(dt.datetime(2026, 9, 17, 9, 5), last, NOW)
        assert VA.due(dt.datetime(2026, 9, 17, 9, 5), NOW - dt.timedelta(minutes=21), NOW)
        assert VA.due(dt.datetime(2026, 9, 17, 9, 5), None, NOW)

    def test_state_keeps_only_active_alarms_of_our_stations(self):
        st = VA.to_state(RAW, STATIONS, NOW)
        assert st["ok"] and [a["alarm_id"] for a in st["alarms"]] == [999999999, 2064]     # the cleared one (status 2) is out
        assert VA.to_state(RAW, {"NE=00000002": "PLANT2"}, NOW)["alarms"] == []

    def test_one_alert_per_device_in_the_vendors_words(self):
        st = VA.to_state(RAW, STATIONS, NOW)
        br = VA.breaches(st, {"PLANT1": ["XX0000000000", "XX0000000009"]}, NOW)
        dev = [b for b in br if b.inverter_sn == "XX0000000000"][0]
        assert dev.metric == "vendor_alarm" and dev.severity == Severity.CRITICAL
        assert "Device Fault (Major)" in dev.message and "internal circuit" in dev.message
        logger = [b for b in br if b.inverter_sn == ""][0]
        assert logger.severity == Severity.WARNING and "datalogger" in logger.message      # blind, not a measured loss

    def test_an_unmatched_device_is_plant_level_and_plants_we_do_not_run_are_ignored(self):
        st = VA.to_state(RAW, STATIONS, NOW)
        br = VA.breaches(st, {"PLANT1": ["OTHER"]}, NOW)
        assert {b.inverter_sn for b in br} == {""}
        assert VA.breaches(st, {"PLANT9": ["XX0000000000"]}, NOW) == []

    def test_several_alarms_on_one_device_make_one_alert(self):
        raw = RAW + [dict(RAW[1], alarmId=2065, alarmName="Other Fault", lev=3)]
        br = VA.breaches(VA.to_state(raw, STATIONS, NOW), {"PLANT1": ["XX0000000000"]}, NOW)
        dev = [b for b in br if b.inverter_sn == "XX0000000000"]
        assert len(dev) == 1 and "Device Fault" in dev[0].message and "(+1 more)" in dev[0].message

    @pytest.mark.parametrize("state", [None, {"ok": False, "fetched_utc": NOW.isoformat(), "alarms": []},
                                       {"ok": True, "fetched_utc": (NOW - dt.timedelta(minutes=41)).isoformat(), "alarms": []}])
    def test_a_failed_or_old_fetch_opens_and_closes_nothing(self, state):
        if state is not None and state["ok"]:
            state["alarms"] = VA.to_state(RAW, STATIONS, NOW)["alarms"]
        assert VA.breaches(state, {"PLANT1": ["XX0000000000"]}, NOW) == []

        class R:
            metric, alert_key = "vendor_alarm", "k1"
            def is_open(self): return True          # noqa: E704
            def is_silenced(self): return False     # noqa: E704
        assert VA.cleared_keys([R()], [], state, NOW) == []

    def test_cleared_alarms_are_resolved_only_from_a_good_fetch(self):
        class R:
            def __init__(self, k, m="vendor_alarm"):
                self.alert_key, self.metric = k, m
            def is_open(self): return True          # noqa: E704
            def is_silenced(self): return False     # noqa: E704
        st = VA.to_state([], STATIONS, NOW)
        assert VA.cleared_keys([R("a"), R("b"), R("c", "inverter_fault")], ["b"], st, NOW) == ["a"]

    def test_state_file_round_trip(self, tmp_path):
        p = tmp_path / "a.json"
        st = VA.to_state(RAW, STATIONS, NOW)
        VA.write_state(st, str(p))
        assert VA.read_state(str(p)) == st and VA.last_fetch(st) == NOW
        p.write_text("{broken")
        assert VA.read_state(str(p)) is None


# ------------------------------------------------------------------ telemetry job
class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def fetch_alarms(self, codes, b, e):
        self.calls.append((list(codes), b, e))
        if self.fail:
            raise RuntimeError("failCode=407")
        return RAW


class P:
    def __init__(self, pk, site):
        self.plant_key, self.site_id = pk, site


@pytest.fixture
def T5(monkeypatch):
    sys.path.insert(0, str(V2 / "scripts"))
    import telemetry_5m
    monkeypatch.setattr(telemetry_5m, "now_mx", lambda: dt.datetime(2026, 9, 17, 9, 0))
    monkeypatch.setattr(telemetry_5m, "now_utc", lambda: NOW)
    return telemetry_5m


class TestTelemetryRefresh:
    def test_writes_the_state_on_the_quarter_hour(self, T5, tmp_path):
        import logging
        c, f = FakeClient(), str(tmp_path / "h.json")
        st = T5._refresh_huawei_alarms(c, [P("PLANT1", "NE=00000001"), P("PLANT2", "NE=00000002")], logging.getLogger("t"), f)
        assert st["ok"] and len(st["alarms"]) == 2 and VA.read_state(f) == st
        assert c.calls[0][0] == ["NE=00000001", "NE=00000002"]
        assert c.calls[0][2] - c.calls[0][1] == VA.LOOKBACK_DAYS * 86400 * 1000

    def test_not_again_before_it_is_due(self, T5, tmp_path, monkeypatch):
        import logging
        c, f = FakeClient(), str(tmp_path / "h.json")
        T5._refresh_huawei_alarms(c, [P("PLANT1", "NE=00000001")], logging.getLogger("t"), f)
        monkeypatch.setattr(T5, "now_mx", lambda: dt.datetime(2026, 9, 17, 9, 5))
        assert T5._refresh_huawei_alarms(c, [P("PLANT1", "NE=00000001")], logging.getLogger("t"), f) is None
        assert len(c.calls) == 1

    def test_a_failure_is_written_as_a_failed_fetch_and_never_raises(self, T5, tmp_path):
        import logging
        f = str(tmp_path / "h.json")
        st = T5._refresh_huawei_alarms(FakeClient(fail=True), [P("PLANT1", "NE=00000001")], logging.getLogger("t"), f)
        assert st["ok"] is False and "407" in st["error"] and not VA.usable(VA.read_state(f), NOW)


class TestWiring:
    """v257 built the reader and no job called it. These fail if the calls are removed again."""
    SRC_T = (V2 / "scripts" / "telemetry_5m.py").read_text(encoding="utf-8")
    SRC_S = (V2 / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")

    def test_the_huawei_run_refreshes_the_alarm_list(self):
        blk = self.SRC_T.split("def _run_huawei(")[1].split("\ndef ")[0]
        assert "_refresh_huawei_alarms(huawei_client, plants, log)" in blk

    def test_the_acute_job_opens_and_resolves_vendor_alarms(self):
        assert "VA.breaches(va_state, configured, now_utc)" in self.SRC_S
        assert "VA.cleared_keys(" in self.SRC_S and "resolve_alert(r, now_utc)" in self.SRC_S

    def test_the_client_reuses_the_session(self):
        src = (V2 / "argia" / "vendors" / "huawei.py").read_text(encoding="utf-8")
        blk = src.split("def fetch_alarms(")[1].split("\n    def ")[0]
        assert "self._ensure_logged_in()" in blk


class TestRegistries:
    def test_the_new_metric_is_known_everywhere(self):
        from argia.alerts import explanations, naming, subscriptions
        from argia.maintenance import tickets
        assert "vendor_alarm" in explanations.catalog_metrics()
        assert "vendor_alarm" in naming.METRIC_PHRASE
        assert "vendor_alarm" in subscriptions._PLANT_PREFIXES        # a plant alert: scope and CAPEX filter apply
        assert tickets.CATEGORY_FOR_METRIC["vendor_alarm"] == "inverter/fault"


# ------------------------------------------------------------------ portal
GEN = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")


def _def(name, extra=""):
    m = re.search(rf"\ndef {name}\(.*?(?=\ndef |\nclass |\n# ---)", GEN, re.S)
    ns = {}
    exec(extra + m.group(0), ns)          # noqa: S102
    return ns[name]


class TestPortal:
    def test_map_partial_is_amber_never_live(self):
        ms = _def("map_status", "STALE_MIN = 30\n")
        assert ms(4.0, 500, True, 2) == ("partial", "warn")
        assert ms(4.0, 500, True) == ("live", "good")
        assert ms(4.0, 500, False, 2) == ("night", "off")
        assert ms(40.0, 500, True, 2) == ("stale", "warn")

    def _sem(self, invs, alerts=None, hour=10):
        sem = _def("semaphore")
        g = sem.__globals__
        g.update(LATEST={"D": {"P": invs}}, TODAY="D", WINDOW=(6, 20), NOW_MX=dt.datetime(2026, 11, 7, hour, 0),
                 STALE_MIN=30, CONFIG_INV={"P": [(i["sn"], i["sn"], 150.0) for i in invs]},
                 zero_while_producing=A.zero_while_producing,
                 device_alerts=lambda pk: [a for a in (alerts or []) if a["sev"] == "CRITICAL"])
        return sem("P")

    @staticmethod
    def _inv(sn, w):
        return {"sn": sn, "power_w": w, "age_min": 3.0, "status": 1}

    def test_the_vitalmex_tile_is_amber(self):
        cls, en, es = self._sem([self._inv("1", 0.0), self._inv("2", 112000.0), self._inv("3", 0.0)])
        assert cls == "warn" and en == "2/3 inverter(s) at 0 W while others produce" and "0 W" in es

    def test_all_producing_is_green_and_an_open_device_alert_is_not(self):
        invs = [self._inv("1", 100000.0), self._inv("2", 112000.0)]
        assert self._sem(invs)[0] == "good"
        cls, en, _ = self._sem(invs, [{"sev": "CRITICAL", "metric": "vendor_alarm", "sn": "1"}])
        assert cls == "warn" and "critical device alert" in en

    def test_the_inverter_row_says_it(self):
        assert "'0 W - others produce' if down" in GEN and "pill_cls = 'bad' if (stale or down)" in GEN
