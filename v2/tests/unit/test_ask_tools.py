"""Unit tests — argia.ask.tools against a fake database.

The fake answers by the ``/*tag:...*/`` comment each query carries, so
these tests pin the numbers the assistant is handed, not SQL text.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from argia.ask import tools as T

TAG = re.compile(r"/\*tag:(\w+)\*/")


class FakeDB:
    """rows(sql) keyed on the query tag; records every call."""

    def __init__(self, data):
        self.data, self.calls, self.sql = data, [], []

    def __call__(self, sql):
        m = TAG.search(sql)
        assert m, f"query without tag: {sql[:80]}"
        self.calls.append(m.group(1))
        self.sql.append(sql)
        v = self.data.get(m.group(1), [])
        return v(sql) if callable(v) else v


PLANTS = [
    # key, customer, brand, kwp, portfolio, active, tariff, pr_baseline
    ["GTO1", "Taigene", "GROWATT", "500.0", "PPA", "t", "2.1", "0.80"],
    ["MEX2", "Vitalmex", "HUAWEI", "300.0", "PPA", "t", "2.3", "0.80"],
    ["GTO2", "Some Owner", "SOLAREDGE", "200.0", "CAPEX", "t", "0", "0"],
    ["OLD1", "Gone Co", "GROWATT", "100.0", "PPA", "f", "1.0", "0"],
]


def perf_rows(sql):
    """Honour the plant filter like real SQL would."""
    all_rows = [
        # plant, prod, exp(on days w/ exp), prod on those days, pr, avail, days, days_pr, irr
        ["GTO1", "1843", "2110", "1843", "0.71", "0.894", "1", "1", "5.2"],
        ["MEX2", "1200", "1200", "1200", "0.80", "1.0", "1", "1", "5.0"],
        ["GTO2", "500", "", "", "0.75", "", "1", "1", ""],
    ]
    m = re.search(r"plant_key = '(\w+)'", sql)
    return [r for r in all_rows if not m or r[0] == m.group(1)]


BASE = {
    "plants": PLANTS,
    "freshness": [["2026-09-04 16:05:00+00", "2026-09-03"]],
    "perf": perf_rows,
}


@pytest.fixture
def db():
    return FakeDB(dict(BASE))


# -------------------------------------------------------------- resolve
def test_resolve_plant_by_key_customer_and_fragment(db):
    assert T.resolve_plant(db, "gto1") == "GTO1"
    assert T.resolve_plant(db, "Taigene") == "GTO1"
    assert T.resolve_plant(db, "vital") == "MEX2"


def test_resolve_plant_unknown_lists_vocabulary(db):
    with pytest.raises(T.ToolError) as e:
        T.resolve_plant(db, "Prologis")
    assert "GTO1=Taigene" in str(e.value) and "MEX2=Vitalmex" in str(e.value)


def test_resolve_plant_ambiguous(db):
    with pytest.raises(T.ToolError, match="ambiguous"):
        T.resolve_plant(db, "o")          # Taigene? no — 'Some Owner', 'Gone Co'


def test_resolve_plant_never_interpolates_raw_input(db):
    with pytest.raises(T.ToolError):
        T.resolve_plant(db, "x' OR 1=1 --")
    assert all("OR 1=1" not in s for s in db.sql)


# ---------------------------------------------------------------- dates
def test_date_validation():
    assert T._date("2026-08-31", "d") == "2026-08-31"
    assert T._date("today", "d") == T.today_mx().isoformat()
    assert T._date("yesterday", "d") == (T.today_mx() - dt.timedelta(days=1)).isoformat()
    for bad in ("31/08/2026", "2026-13-01", "2026-01-01' OR 1=1", "", None):
        with pytest.raises(T.ToolError):
            T._date(bad, "d")


def test_range_swaps_and_caps():
    assert T._range("2026-08-31", "2026-08-01") == ("2026-08-01", "2026-08-31")
    with pytest.raises(T.ToolError, match="too long"):
        T._range("2024-01-01", "2026-01-01")


# ------------------------------------------------------------ generation
def test_get_generation_totals_only_over_days_with_expectation(db):
    db.data["daily_range"] = [
        ["2026-09-01", "1000", "1100", "5.1", "0.75", "0.98", "A", ""],
        ["2026-09-02", "900", "", "", "", "", "B", "vendor backfill"],
        ["2026-09-03", "843", "1010", "4.9", "0.68", "0.80", "A", ""],
    ]
    db.data["perf"] = [["GTO1", "2743", "2110", "1843", "0.715", "0.89", "3", "2", "10.0"]]
    out = T.get_generation(db, "Taigene", "2026-09-01", "2026-09-03")
    assert out["plant_key"] == "GTO1"
    assert [d["vs_expected_pct"] for d in out["days"]] == [90.9, None, 83.5]
    assert out["days"][1]["note"] == "vendor backfill"
    assert out["totals"]["vs_expected_pct"] == 87.3     # 1843/2110, not 2743/2110
    assert out["source"]["daily_kpi_latest_date"] == "2026-09-03"


def test_get_performance_ranks_worst_first_and_adds_yield(db):
    out = T.get_performance(db, "2026-08-01", "2026-08-31")
    keys = [p["plant_key"] for p in out["plants"]]
    assert keys == ["GTO1", "MEX2", "GTO2"]          # 87.4, 100, None last
    assert out["plants"][0]["vs_expected_pct"] == 87.3
    assert out["plants"][0]["specific_yield_kwh_per_kwp"] == 3.7   # 1843/500
    assert out["plants"][1]["availability_pct"] == 100.0


def test_get_performance_single_plant_filters(db):
    out = T.get_performance(db, "2026-08-01", "2026-08-31", plant="MEX2")
    assert [p["plant_key"] for p in out["plants"]] == ["MEX2"]


# ------------------------------------------------------------------ lost
def test_get_lost_generation_values_at_tariff(db):
    db.data["lost"] = [["267", "1", "1", "1", "2110", "1843"]]
    db.data["lost_days"] = [["2026-09-03", "1843", "2110", ""]]
    db.data["maintenance"] = []
    out = T.get_lost_generation(db, "GTO1", "2026-09-03", "2026-09-03")
    assert out["lost_kwh"] == 267 and out["tariff_mxn_per_kwh"] == 2.1
    assert out["lost_mxn"] == 561                     # 267 * 2.1 = 560.7
    assert out["worst_days"][0]["shortfall_kwh"] == 267


def test_get_lost_generation_capex_has_no_money(db):
    db.data["lost"] = [["50", "1", "1", "1", "550", "500"]]
    out = T.get_lost_generation(db, "GTO2", "2026-09-03", "2026-09-03")
    assert out["lost_kwh"] == 50 and out["lost_mxn"] is None
    assert out["tariff_mxn_per_kwh"] is None


# ------------------------------------------------------------- inverters
def test_get_inverter_performance_flags(db):
    db.data["inverters"] = [
        ["SN1", "INV-01", "50"], ["SN2", "INV-02", "50"],
        ["SN3", "INV-03", "50"], ["SN4", "INV-04", "50"],
    ]
    db.data["inverter_day"] = [
        # sn, e(max etoday), status, fault, power_w, last, samples
        ["SN1", "250", "1", "0", "40000", "16:00", "120"],
        ["SN2", "245", "1", "", "39000", "16:00", "120"],
        ["SN3", "120", "3", "117", "0", "14:00", "80"],
        # SN4 silent: configured, no rows
        ["SN9", "10", "1", "", "100", "16:00", "5"],         # unconfigured
    ]
    out = T.get_inverter_performance(db, "gto1", "2026-09-03")
    assert out["median_specific_yield_kwh_per_kw"] == 4.9      # of [2.4, 4.9, 5.0]
    assert out["flags"]["silent"] == ["INV-04"]
    assert out["flags"]["underperforming"] == ["INV-03"]
    assert out["flags"]["faulted"] == ["INV-03"]
    inv3 = next(i for i in out["inverters"] if i["label"] == "INV-03")
    assert "Growatt code 117" in inv3["fault"]
    assert any(i.get("unconfigured") for i in out["inverters"])


def test_get_inverter_performance_normal_state_is_not_a_fault(db):
    db.data["plants"] = [["MEX2", "Vitalmex", "HUAWEI", "300", "PPA", "t", "2.3", "0.8"]]
    db.data["inverters"] = [["H1", "INV-1", "100"]]
    db.data["inverter_day"] = [["H1", "500", "1", "IS=512,RS=1", "80000", "16:00", "100"]]
    out = T.get_inverter_performance(db, "MEX2", "today")
    assert out["flags"]["faulted"] == [] and out["inverters"][0]["fault"] is None


# ---------------------------------------------------------------- alarms
def test_get_active_alarms_filters_by_plant(db):
    db.data["alarms_active"] = lambda sql: (
        [["inverter-silent:GTO1:SN4", "WARNING", "2026-09-03 17:00+00", "2026-09-04 16:00+00"]]
        if "GTO1" in sql else
        [["inverter-silent:GTO1:SN4", "WARNING", "a", "b"],
         ["plant-stale:MEX2", "CRITICAL", "a", "b"]])
    db.data["maintenance"] = []
    assert len(T.get_active_alarms(db)["alarms"]) == 2
    out = T.get_active_alarms(db, "Taigene")
    assert [a["key"] for a in out["alarms"]] == ["inverter-silent:GTO1:SN4"]


def test_get_alarm_history_explains_fault_codes(db):
    db.data["alarms_range"] = [["plant-stale:GTO1", "CRITICAL", "a", "b", "f"]]
    db.data["faults_range"] = [
        ["2026-09-03", "GTO1", "INV-03", "117", "80"],
        ["2026-09-03", "MEX2", "INV-1", "IS=512,RS=1", "100"],     # normal -> dropped
    ]
    db.data["maintenance"] = [["7", "GTO1", "2026-09-02 10:00+00", "", "customer", "cleaning", "tz"]]
    out = T.get_alarm_history(db, "2026-09-01", "2026-09-03")
    assert out["alarms"][0]["active"] is False
    assert [f["inverter"] for f in out["inverter_faults"]] == ["INV-03"]
    assert out["maintenance"][0]["approved"] is True and out["maintenance"][0]["end"] is None


# ------------------------------------------------------------- overview
def test_get_portfolio_overview_skips_inactive_and_sums(db):
    db.data["today_energy"] = [["GTO1", "1843", "3", "4"], ["MEX2", "1200", "2", "3"]]
    db.data["now_kw"] = [["GTO1", "310.5"], ["MEX2", "200"]]
    db.data["alarms_active"] = [["inverter-silent:GTO1:SN4", "WARNING", "a", "b"]]
    out = T.get_portfolio_overview(db)
    keys = [p["plant_key"] for p in out["plants"]]
    assert "OLD1" not in keys and keys == ["GTO1", "MEX2", "GTO2"]
    assert out["totals"]["today_kwh"] == 3043 and out["totals"]["current_kw"] == 510.5
    gto1 = out["plants"][0]
    assert gto1["active_alarms"] == ["inverter-silent:GTO1:SN4"]
    assert gto1["mtd_vs_expected_pct"] == 87.3 and gto1["pr_30d"] == 0.71
    gto2 = out["plants"][2]
    assert gto2["today_kwh"] is None and gto2["active_alarms"] == []


def test_get_plant_overview(db):
    db.data["inverter_count"] = [["4", "5"]]
    db.data["today_energy"] = [["GTO1", "1843", "3", "4"]]
    db.data["now_kw"] = [["GTO1", "310.5"]]
    db.data["alarms_active"] = []
    db.data["maintenance"] = []
    out = T.get_plant_overview(db, "GTO1")
    assert out["plant"]["customer"] == "Taigene"
    assert out["inverters_configured_active"] == 4
    assert out["today"]["current_kw"] == 310.5
    assert out["last_30_days"]["pr"] == 0.71


# ---------------------------------------------------------------- dispatch
def test_run_tool_errors_are_returned_not_raised(db):
    assert "unknown tool" in T.run_tool(db, "drop_table", {})["error"]
    assert "required" in T.run_tool(db, "get_generation", {"plant": "GTO1"})["error"]
    assert "unknown plant" in T.run_tool(db, "get_plant_overview", {"plant": "Nope"})["error"]
    assert "YYYY-MM-DD" in T.run_tool(
        db, "get_performance", {"date_from": "ayer", "date_to": "hoy"})["error"]


def test_run_tool_drops_unknown_params(db):
    db.data["inverter_count"] = [["1", "1"]]
    db.data["maintenance"] = []
    out = T.run_tool(db, "get_plant_overview", {"plant": "GTO1", "sql": "DROP"})
    assert "error" not in out
    assert all("DROP" not in s for s in db.sql)


def test_registry_and_dispatch_agree():
    assert {t["name"] for t in T.TOOLS} == set(T.DISPATCH)
    for t in T.TOOLS:
        assert t["input_schema"]["type"] == "object"
        for req in t["input_schema"].get("required", []):
            assert req in t["input_schema"]["properties"]
