"""v392 - Arturo's "Reporting methodology corrections" v0.1 (23 Nov 2026) as regression tests.

His worked examples are customer data (Huawei exports, contract volumes), so the repo (public)
carries synthetic cases built in the same shape; the real figures are checked on the server
by scripts/methodology_acceptance.py against a server-only expectations file.

R1  availability: inverter value from the plant's own evidence rule; plant = capacity weighted
R1+ Tomasz 24 Nov: a connectivity gap is NOT an outage (our rule is kept)
R4  specific yield on DC kWp from the string register
R7  contract comparison over the same period; partial month out of the total
R8  thermal: peer deviation only at similar, steady load; no cooling grade shown
"""
from __future__ import annotations

import datetime as dt
import math
import pathlib

import pytest

from argia.analytics import thermal as TH
from argia.kpi import availability as AV
from argia.report import methodology as MC

V2 = pathlib.Path(__file__).resolve().parents[2]
LAT, LON = 19.43, -99.13            # any central-Mexico site
UTC = dt.timezone.utc
RATED = {"I1": 150.0, "I2": 150.0, "I3": 150.0}


def _day_samples(day, stops=()):
    """5-minute samples 06:00-19:55 MX for I1..I3, sine power profile, day counters.
    ``stops``: [(sn, mx_from, mx_to)] - online at 0 W in that span, counter flat."""
    out, counter = [], {sn: 0.0 for sn in RATED}
    t = dt.datetime.combine(day, dt.time(6, 0))
    while t.hour < 20:
        frac = (t.hour + t.minute / 60 - 6.5) / 11.5
        p_kw = max(0.0, 120.0 * math.sin(math.pi * frac)) if 0 < frac < 1 else 0.0
        for sn in RATED:
            stopped = any(s == sn and a <= t < b for s, a, b in stops)
            kw = 0.0 if stopped else p_kw
            counter[sn] += kw * 5 / 60
            out.append(((t - AV.MX_OFFSET).replace(tzinfo=UTC), sn, 1, kw * 1000, round(counter[sn], 2)))
        t += dt.timedelta(minutes=5)
    return out


def _vitalmex_shaped_window():
    """22 days; I1 and I3 online at 0 W from day 7 07:50 until day 8 10:55 (I1) / 12:05 (I3) -
    the shape of Arturo's R1 example."""
    rows, plant_days = [], []
    for n in range(1, 23):
        day = dt.date(2026, 11, n)
        stops = []
        if n == 7:
            stops = [(sn, dt.datetime(2026, 11, 7, 7, 50), dt.datetime(2026, 11, 8)) for sn in ("I1", "I3")]
        if n == 8:
            stops = [("I1", dt.datetime(2026, 11, 8), dt.datetime(2026, 11, 8, 10, 55)),
                     ("I3", dt.datetime(2026, 11, 8), dt.datetime(2026, 11, 8, 12, 5))]
        res = AV.compute(_day_samples(day, stops), list(RATED.items()), day, LAT, LON)
        plant_days.append((res.availability, res.coverage))
        rows += [(sn, *v) for sn, v in MC.inverter_steps(res, list(RATED)).items()]
    return rows, plant_days


class TestR1Availability:
    def test_online_at_zero_watts_is_down_like_arturos_example(self):
        rows, _ = _vitalmex_shaped_window()
        per, plant = MC.availability_window(rows, RATED)
        # Arturo: "about 94% for INV1 and INV3, 100% for INV2, about 96% for the plant"
        assert 93.0 <= 100 * per["I1"][0] <= 95.0 and 93.0 <= 100 * per["I3"][0] <= 95.0
        assert per["I3"][0] < per["I1"][0]                 # I3 came back later
        assert per["I2"] == (1.0, 1.0)
        assert 95.0 <= 100 * plant[0] <= 97.0 and plant[1] == 1.0

    def test_plant_equals_capacity_weighted_inverters(self):
        rows, plant_days = _vitalmex_shaped_window()
        per, plant = MC.availability_window(rows, RATED)
        assert plant[0] == pytest.approx(sum(per[s][0] for s in RATED) / 3, abs=1e-9)    # equal ratings, full coverage
        # ...and the same number as the plant's own daily values over the window (the header tile)
        header, _cov = AV.window(plant_days, owed_days=22)
        assert 100 * plant[0] == pytest.approx(100 * header, abs=0.05)

    def test_capacity_weighting_uses_rated_kw(self):
        rows = [("big", 90, 10, 0), ("small", 100, 0, 0)]
        _per, plant = MC.availability_window(rows, {"big": 300.0, "small": 100.0})
        assert plant[0] == pytest.approx((300 * 90 + 100 * 100) / (300 * 100 + 100 * 100))

    def test_nothing_known_is_none_never_100(self):
        per, plant = MC.availability_window([("A", 0, 0, 35)], {"A": 100.0})
        assert per["A"] == (None, 0.0) and plant == (None, 0.0)
        assert MC.availability_window([], {}) == ({}, (None, None))


class TestR1ConnectivityIsNotAnOutage:
    """Tomasz 24 Nov: always distinguish a connectivity issue from missing data - keep our rule."""

    @staticmethod
    def _gap(samples, sns, a, b):
        """Drop the samples of ``sns`` between MX times a and b (nothing received)."""
        return [s for s in samples if not (s[1] in sns and a <= s[0].replace(tzinfo=None) + AV.MX_OFFSET < b)]

    def test_plant_wide_silence_is_unknown_not_down(self):
        # the line drops at 13:00 and nothing arrives for the rest of the day: the energy is short of
        # normal, so nothing proves production - the silent time is coverage, never downtime
        day = dt.date(2026, 11, 12)
        full = _day_samples(day)
        normal = sum(max(e for _t, sn, _st, _p, e in full if sn == x) for x in RATED)
        s = self._gap(full, set(RATED), dt.datetime(2026, 11, 12, 13), dt.datetime(2026, 11, 13))
        res = AV.compute(s, list(RATED.items()), day, LAT, LON, expected_kwh=normal)
        assert res.availability == 1.0 and res.coverage < 1.0 and not res.down_steps
        per, _ = MC.availability_window([(sn, *v) for sn, v in MC.inverter_steps(res, list(RATED)).items()], RATED)
        assert all(a == 1.0 and c < 1.0 for a, c in per.values())

    def test_silence_the_counter_later_proves_is_production(self):
        # internet back at 13:00, the day counters show the whole day's energy: the gap was production
        day = dt.date(2026, 11, 12)
        full = _day_samples(day)
        normal = sum(max(e for _t, sn, _st, _p, e in full if sn == x) for x in RATED)
        s = self._gap(full, set(RATED), dt.datetime(2026, 11, 12, 11), dt.datetime(2026, 11, 12, 13))
        res = AV.compute(s, list(RATED.items()), day, LAT, LON, expected_kwh=normal, plant_energy_kwh=normal)
        assert res.availability == 1.0 and res.coverage == 1.0 and not res.down_steps

    def test_one_inverter_silent_while_peers_report_is_not_counted_down(self):
        day = dt.date(2026, 11, 12)
        s = [x for x in _day_samples(day) if not (x[1] == "I2" and 11 <= (x[0].replace(tzinfo=None) + AV.MX_OFFSET).hour < 12)]
        res = AV.compute(s, list(RATED.items()), day, LAT, LON)
        assert "I2" not in res.down_steps                               # its own counter proves it produced
        assert res.availability == 1.0

    def test_zero_watts_while_peers_produce_is_down(self):
        day = dt.date(2026, 11, 12)
        s = _day_samples(day, [("I2", dt.datetime(2026, 11, 12, 11), dt.datetime(2026, 11, 12, 12, 5))])
        res = AV.compute(s, list(RATED.items()), day, LAT, LON)
        assert res.down_steps == {"I2": 4} and res.availability < 1.0


class TestR4DcBasis:
    class _S:
        def __init__(self, inv, kwp):
            self.inverter, self.kwp = inv, kwp

    class _C:
        def __init__(self, sn_of, strings):
            self.sn_of, self.strings = sn_of, strings

    def test_dc_kwp_sums_the_register_strings_per_serial(self):
        c = self._C({"INV1": "A", "INV2": "B"}, [self._S("INV1", 11.07)] * 18 + [self._S("INV2", 10.0)] * 12
                    + [self._S("INV9", 5.0)])                       # INV9 has no serial on the layout: skipped
        assert MC.dc_kwp_by_serial({"P": c}) == {("P", "A"): 199.26, ("P", "B"): 120.0}

    def test_one_plant_one_basis(self):
        dc = {("P", "A"): 199.26, ("P", "B"): 70.2}
        assert MC.yield_basis("P", ["A", "B"], dc, {"A": 150.0, "B": 60.0}) == ({"A": 199.26, "B": 70.2}, "kWh/kWp")
        # one inverter missing from the register -> AC for everyone, said in the unit
        assert MC.yield_basis("P", ["A", "B", "C"], dc, {"A": 150.0, "B": 60.0, "C": 0}) == \
            ({"A": 150.0, "B": 60.0, "C": None}, "kWh/kW AC")

    def test_dc_basis_removes_the_ac_bias(self):
        # same modules, same sun: 2,000 kWh on 199.26 kWp and 705 kWh on 70.2 kWp are the same yield;
        # on AC (150 vs 60 kW) the small unit looked 6 % behind
        dc = {("P", "A"): 199.26, ("P", "B"): 70.2}
        d, _u = MC.yield_basis("P", ["A", "B"], dc, {"A": 150.0, "B": 60.0})
        assert 705 / d["B"] / (2000 / d["A"]) == pytest.approx(1.0, abs=0.002)
        assert 705 / 60.0 / (2000 / 150.0) == pytest.approx(0.881, abs=0.001)


class TestR7ContractSamePeriod:
    def test_partial_month_prorated_like_the_header(self):
        basis, pct, partial = MC.month_vs_contract("2026-11", 45000.0, 75000.0, "2026-11-22")
        assert partial and basis == pytest.approx(55000.0) and pct == pytest.approx(81.82, abs=0.01)
        # the header tile prorates per day: same number for the same days
        assert pct == pytest.approx(45000.0 / (75000.0 / 30 * 22) * 100)

    def test_closed_months_and_month_end_are_full(self):
        assert MC.month_vs_contract("2026-10", 70000.0, 80000.0, "2026-11-22") == (80000.0, 87.5, False)
        assert MC.month_vs_contract("2026-11", 70000.0, 80000.0, "2026-11-30") == (80000.0, 87.5, False)
        assert MC.month_vs_contract("2026-02", 1.0, 0.0, "2026-02-10")[1] is None      # no contract: no %

    def test_report_table_keeps_the_partial_month_out_of_the_total(self):
        src = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
        seg = src[src.index("cb, pct, partial = MC.month_vs_contract(m, a, cc, asof)"):src.index("parts['months'] = (")]
        assert seg.index("if partial:") < seg.index("continue") < seg.index("ta += a; tc += cc")
        assert "TOTAL closed months" in seg and "partial, to" in seg


class TestR8Thermal:
    T0 = dt.datetime(2026, 11, 8, 15, 0, tzinfo=UTC)

    def _run(self, rows):
        """rows: [(minute, sn, kw, temp)] -> {sn: InverterDay}"""
        smp = [(self.T0 + dt.timedelta(minutes=m), sn, kw * 1000, t, 25.0) for m, sn, kw, t in rows]
        return TH.evaluate_day(smp, {"A": 100.0, "B": 100.0, "C": 100.0})

    def test_a_cold_stopped_peer_does_not_make_a_healthy_unit_hot(self):
        # B and C off at 0 W and cold (Vitalmex 7 Nov): A has no loaded peer -> no deviation, no grade
        rows = [(m, "A", 80.0, 50.0) for m in range(0, 120, 5)] + \
               [(m, sn, 0.0, 25.0) for m in range(0, 120, 5) for sn in ("B", "C")]
        d = self._run(rows)
        assert d["A"].dt_peer_peak_c is None and d["A"].cooling_health == "n/a"

    def test_a_restarted_peer_counts_only_after_an_hour_at_load(self):
        # C restarts at minute 60, still cold for a while (Vitalmex 8 Nov 10:55)
        rows = [(m, sn, 80.0, 50.0) for m in range(0, 240, 5) for sn in ("A", "B")]
        rows += [(m, "C", 0.0 if m < 60 else 80.0, 25.0 if m < 60 else min(50.0, 25.0 + (m - 60) / 2)) for m in range(0, 240, 5)]
        d = self._run(rows)
        assert d["A"].dt_peer_peak_c is not None and d["A"].dt_peer_peak_c < 1.0     # vs B; C joins warm
        assert d["C"].dt_peer_peak_c is not None and abs(d["C"].dt_peer_peak_c) < 1.0

    def test_a_real_cooling_problem_still_shows(self):
        rows = [(m, "A", 80.0, 62.0) for m in range(0, 180, 5)] + \
               [(m, sn, 80.0, 50.0) for m in range(0, 180, 5) for sn in ("B", "C")]
        d = self._run(rows)
        assert d["A"].dt_peer_peak_c == pytest.approx(12.0) and d["B"].dt_peer_peak_c <= 0

    def test_derating_against_running_peers_is_still_found(self):
        # A hot and derated to 15 % of rated (below the load filter) while B, C run cool at 80 %
        rows = [(m, "A", 15.0, 70.0) for m in range(0, 120, 5)] + \
               [(m, sn, 80.0, 50.0) for m in range(0, 120, 5) for sn in ("B", "C")]
        d = self._run(rows)
        assert d["A"].derating_minutes > 0 and d["A"].lost_kwh > 0

    def test_no_cooling_grade_on_any_page(self):
        for f in ("server/bundle/report_gen.py", "server/monitoring_gen.py"):
            src = (V2 / f).read_text(encoding="utf-8")
            assert 't("Cooling","Enfriamiento")' not in src and 'data-en="Cooling"' not in src, f
            assert "No cooling grade" in src or "no cooling grade" in src, f


class TestHygiene:
    def test_no_em_or_en_dash_in_new_code(self):
        for f in ("argia/report/methodology.py", "tests/unit/test_v392_methodology.py"):
            src = (V2 / f).read_text(encoding="utf-8")
            assert chr(0x2014) not in src and chr(0x2013) not in src, f

    def test_recompute_stores_the_inverter_steps(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("avr392", V2 / "scripts" / "availability_recompute.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sql = m.inverter_sql("MEX9", "2026-11-07", {"B": (0, 35, 0, 1), "A": (35, 0, 0)})
        assert sql.splitlines() == [
            "DELETE FROM inverter_availability_daily WHERE plant_key = 'MEX9' AND prod_date = DATE '2026-11-07';",
            "INSERT INTO inverter_availability_daily (plant_key, inverter_sn, prod_date, up_steps, down_steps,"
            " unknown_steps, fresh_steps) VALUES ('MEX9', 'A', DATE '2026-11-07', 35, 0, 0, 35);",
            "INSERT INTO inverter_availability_daily (plant_key, inverter_sn, prod_date, up_steps, down_steps,"
            " unknown_steps, fresh_steps) VALUES ('MEX9', 'B', DATE '2026-11-07', 0, 35, 0, 1);"]
        assert "CREATE TABLE IF NOT EXISTS inverter_availability_daily" in m.INV_TABLE_SQL
        assert "ADD COLUMN IF NOT EXISTS fresh_steps integer" in m.INV_TABLE_SQL      # v393


class TestAcceptanceScript:
    """scripts/methodology_acceptance.py - the server-side regression check of the real examples."""

    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("macc", V2 / "scripts" / "methodology_acceptance.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    @staticmethod
    def _fetch(sql):
        if "FROM inverter_availability_daily" in sql:
            return [["A", "90", "10", "0"], ["B", "100", "0", "0"]]
        if "FROM inverter WHERE" in sql:
            return [["A", "150"], ["B", "150"]]
        if "FROM daily_production WHERE" in sql and "availability" in sql:
            return [["0.95", "1"]]
        if "sum(energy_kwh)" in sql:
            return [["45000"]] if "prod_date) = 11" in sql else [["70000"]]
        if "contract_kwh" in sql:
            return [["75000"]] if "month = 11;" in sql else [["80000"]]
        if "thermal_daily" in sql:
            return [["2.9"]]
        return []

    def test_every_check_type(self):
        m = self._mod()
        checks = [
            {"name": "inv A", "type": "inverter_availability", "plant": "P", "serial": "A", "from": "2026-11-01", "to": "2026-11-22", "expect": 90, "tol": 0.1},
            {"name": "plant", "type": "plant_availability", "plant": "P", "from": "2026-11-01", "to": "2026-11-22", "expect": 95, "tol": 0.1},
            {"name": "nov", "type": "month_vs_contract", "plant": "P", "month": "2026-11", "asof": "2026-11-22", "expect": 81.8, "tol": 0.1},
            {"name": "closed", "type": "closed_months_vs_contract", "plant": "P", "months": ["2026-09", "2026-10"], "expect": 87.5, "tol": 0.1},
            {"name": "thermal", "type": "thermal_dt_peer_max", "plant": "P", "from": "2026-11-01", "to": "2026-11-22", "max": 6},
        ]
        lines = []
        assert m.run(checks, self._fetch, lines.append) == 0
        assert lines[-1] == "5/5 checks pass" and all(x.startswith("PASS") for x in lines[:-1])

    def test_a_wrong_value_or_a_plant_mismatch_fails(self):
        m = self._mod()
        lines = []
        bad = [{"name": "inv A", "type": "inverter_availability", "plant": "P", "serial": "A", "from": "a", "to": "b", "expect": 100, "tol": 0.1},
               {"name": "thermal", "type": "thermal_dt_peer_max", "plant": "P", "from": "a", "to": "b", "max": 2},
               {"name": "unknown", "type": "nope", "plant": "P"}]
        assert m.run(bad, self._fetch, lines.append) == 3
        mism = m.measure({"type": "plant_availability", "plant": "P", "from": "a", "to": "b"},
                         lambda sql: [["0.90", "1"]] if "daily_production" in sql else self._fetch(sql))
        assert "MISMATCH" in mism[1] and not m.judge({"type": "plant_availability", "expect": 95}, *mism)

    def test_missing_file_is_exit_2(self, tmp_path):
        assert self._mod().main(["--file", str(tmp_path / "none.json")]) == 2
