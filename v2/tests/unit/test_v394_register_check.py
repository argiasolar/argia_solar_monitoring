"""v394 - Arturo's methodology R3 (string register vs the field) and R4 (index since the
last change). Synthetic data in the shape of SAG (customer data stays on the server)."""
from __future__ import annotations

import datetime as dt
import pathlib

from argia.analytics import register_check as RC
from argia.report import methodology as MC

V2 = pathlib.Path(__file__).resolve().parents[2]
ASOF = dt.date(2026, 11, 22)
REG18 = {("str", i): f"PV{i}" for i in list(range(1, 16)) + [19, 20, 21]}


def _rows(sn, days, live, amps=8.5, plant="P", arr="str", n_inputs=24):
    """noon medians: ``live`` inputs at ``amps``, the rest at 0, for each day."""
    out = []
    for d in days:
        for i in range(1, n_inputs + 1):
            out.append((plant, sn, d, arr, i, amps if i in live else 0.0))
    return out


DAYS = [f"2026-11-{d:02d}" for d in range(16, 23)]


class TestR3:
    def test_register_matching_the_field_raises_nothing(self):
        rows = _rows("A", DAYS, set(range(1, 16)) | {19, 20, 21})
        assert RC.evaluate({("P", "A"): REG18}, rows, ASOF) == []

    def test_sag_like_wiring_before_the_override_is_flagged(self):
        # field: PV4 moved to input 16 (SAG inverter 2) - register still says input 4
        live = (set(range(1, 16)) - {4}) | {16, 19, 20, 21}
        flags = RC.evaluate({("P", "B"): REG18}, _rows("B", DAYS, live), ASOF)
        kinds = {(f.kind, f.idx) for f in flags}
        assert kinds == {("dead", 4), ("unregistered", 16)}
        sev, msg = RC.message(flags, "P Inverter 2 (B)")
        assert sev == "WARNING" and "PV4 (input 4, 0.0 A)" in msg and "16 (8.5 A)" in msg and "confirm on site" in msg

    def test_unregistered_only_is_info(self):
        live = set(range(1, 16)) | {19, 20, 21, 22}
        flags = RC.evaluate({("P", "A"): REG18}, _rows("A", DAYS, live), ASOF)
        assert [(f.kind, f.idx) for f in flags] == [("unregistered", 22)]
        assert RC.message(flags, "x")[0] == "INFO"

    def test_a_stopped_inverter_or_dark_days_say_nothing_about_strings(self):
        rows = _rows("A", DAYS, set())                       # all inputs 0: not producing
        assert RC.evaluate({("P", "A"): REG18}, rows, ASOF) == []
        cloudy = _rows("A", DAYS, set(range(1, 16)) | {19, 20, 21}, amps=0.6)   # overcast noons
        assert RC.evaluate({("P", "A"): REG18}, cloudy, ASOF) == []

    def test_dead_needs_three_producing_days(self):
        live = set(range(1, 16)) - {10} | {19, 20, 21}
        two = _rows("A", DAYS[-2:], live)
        assert RC.evaluate({("P", "A"): REG18}, two, ASOF) == []
        three = _rows("A", DAYS[-3:], live)
        assert [(f.kind, f.idx, f.uid) for f in RC.evaluate({("P", "A"): REG18}, three, ASOF)] == [("dead", 10, "PV10")]
        # a string that came back yesterday is not dead
        back = _rows("A", DAYS[-3:-1], live) + _rows("A", DAYS[-1:], live | {10})
        assert RC.evaluate({("P", "A"): REG18}, back, ASOF) == []

    def test_mppt_registers_read_the_mppt_array_only(self):
        reg = {("mppt", i): f"MPPT{i}" for i in (1, 2, 3)}
        rows = _rows("M", DAYS, {1, 2, 3}, arr="mppt", n_inputs=4) + _rows("M", DAYS, {5, 6}, arr="str", n_inputs=8)
        assert RC.evaluate({("P", "M"): reg}, rows, ASOF) == []      # live str inputs are not "unregistered"

    def test_registered_inputs_from_the_epi_config(self):
        from argia.epi.engine import Channel

        class S:
            def __init__(self, uid, inv):
                self.uid, self.inverter = uid, inv

        class C:
            sn_of = {"INV2": "B"}
            strings = [S("SAG-INV2-PV4", "INV2"), S("SAG-INV2-PV5", "INV2"), S("X", "INV9")]
            channel_of = {"SAG-INV2-PV4": Channel("pv", 16), "SAG-INV2-PV5": Channel("pv", 5), "X": Channel("pv", 1)}
        assert RC.registered_inputs({"MEX1": C()}) == {("MEX1", "B"): {("str", 16): "SAG-INV2-PV4",
                                                                       ("str", 5): "SAG-INV2-PV5"}}

    def test_refresh_sql_rebuilds_the_window_in_one_transaction(self):
        sql = RC.refresh_sql(dt.date(2026, 10, 18), dt.date(2026, 11, 22))
        assert sql.index("BEGIN;") < sql.index("DELETE FROM string_noon_daily WHERE prod_date BETWEEN DATE '2026-10-18'"
                                               " AND DATE '2026-11-22';") < sql.index("COMMIT;")
        assert "unnest(str_a)" in sql and "unnest(mppt_a)" in sql and "BETWEEN 11 AND 13" in sql
        assert sql.count("INSERT INTO string_noon_daily") == 2


class TestR3Alerts:
    def test_candidates_and_metric_registry(self):
        import importlib.util
        import sys
        sys.path.insert(0, str(V2 / "scripts"))
        spec = importlib.util.spec_from_file_location("ad394", V2 / "scripts" / "alerts_daily.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        live = (set(range(1, 16)) - {4}) | {16, 19, 20, 21}
        c = m.register_candidates({("P", "B"): REG18}, _rows("B", DAYS, live), ASOF, {("P", "B"): "Inverter 2"})
        assert len(c) == 1 and c[0].metric == "string_register" and c[0].severity == "WARNING"
        assert c[0].alert_key == "p:inv:b:string_register" and "P Inverter 2 (B):" in c[0].message
        from argia.alerts.engine import ENGINE_METRICS
        from argia.core.thresholds import KNOWN_METRICS
        from argia.alerts.naming import METRIC_PHRASE
        from argia.alerts.explanations import catalog_metrics
        assert "string_register" in ENGINE_METRICS and "string_register" in KNOWN_METRICS
        assert "string_register" in METRIC_PHRASE and "string_register" in catalog_metrics()
        src = (V2 / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        assert "string_noon_for_recent_days(args.dry_run)" in src


class TestR4:
    def test_sag_inverter_1_change_on_1_nov(self):
        before = [f"2026-10-{d}" for d in range(20, 32)]
        after = [f"2026-11-{d:02d}" for d in range(1, 23)]
        full = set(range(1, 16)) | {19, 20, 21}
        rows = _rows("A", before, full - {10, 11, 12, 15}) + _rows("A", after, full)
        rows += _rows("B", before + after, full)                     # a peer without a change
        assert RC.changes_by_inverter(rows) == {("P", "A"): "2026-11-01"}

    def test_one_odd_day_or_a_cloud_is_not_a_change(self):
        full = set(range(1, 16)) | {19, 20, 21}
        rows = _rows("A", DAYS[:3], full) + _rows("A", DAYS[3:4], full - {7}) + _rows("A", DAYS[4:], full)
        rows += _rows("A", ["2026-11-10", "2026-11-11"], full - {1, 2, 3}, amps=2.0)    # overcast: not sunny
        assert RC.changes_by_inverter(rows) == {}

    def test_index_since_uses_the_same_days_for_the_peers(self):
        days = [f"2026-11-{d:02d}" for d in range(1, 11)]
        daily = {"A": {d: (600.0 if d < "2026-11-06" else 1000.0) for d in days},
                 "B": {d: 1000.0 for d in days}, "C": {d: 1000.0 for d in days}}
        denom = {"A": 200.0, "B": 200.0, "C": 200.0}
        assert MC.index_since(daily, denom, "A", "2026-11-06") == 1.0     # repaired: no longer penalized
        assert round(MC.index_since(daily, denom, "A", "2026-11-01"), 3) == 0.8
        assert MC.index_since(daily, {"A": None}, "A", "2026-11-01") is None

    def test_report_shows_the_since_date(self):
        src = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
        assert "MC.index_since(" in src and "inv_change.get((k, sn))" in src and "inputs changed" in src
