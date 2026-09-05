"""v202 — the portal plant page gets back what the GCS dashboard had:
day-peak inverter temperature with colour, a per-kW peer comparison and
the open ledger alerts (Tomasz 2026-09-05: "inverter temperatures ...
not on new dashboards"; "peer inverter comparisons").

monitoring_gen.py queries PostgreSQL at import, so the pure helpers are
lifted from the source by AST and the wiring is asserted on the text.
"""
import ast
import pathlib

V2 = pathlib.Path(__file__).resolve().parents[2]
SRC = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")


def _names(assign):
    out = set()
    for t in assign.targets:
        for n in ast.walk(t):
            if isinstance(n, ast.Name):
                out.add(n.id)
    return out


def _lift(*names):
    tree = ast.parse(SRC)
    mod = ast.Module(body=[n for n in tree.body
                           if (isinstance(n, ast.FunctionDef) and n.name in names)
                           or (isinstance(n, ast.Assign) and _names(n)
                               & {"TEMP_WARN_C", "TEMP_CRIT_C", "PEER_WARN", "PEER_CRIT"})],
                     type_ignores=[])
    ns = {}
    exec(compile(mod, "monitoring_gen_lifted", "exec"), ns)
    return ns


NS = _lift("temp_class", "peer_ratios", "peer_class")


class TestTemperature:
    def test_thresholds_match_the_alert_engine(self):
        from argia.analytics.acute import TEMP_CRIT_C, TEMP_WARN_C
        assert NS["TEMP_WARN_C"] == TEMP_WARN_C == 65.0
        assert NS["TEMP_CRIT_C"] == TEMP_CRIT_C == 75.0

    def test_temp_class(self):
        tc = NS["temp_class"]
        assert tc(None) == "" and tc(64.9) == "" and tc(65.0) == "warn"
        assert tc(74.9) == "warn" and tc(75.0) == "bad" and tc(81.8) == "bad"

    def test_page_shows_now_and_peak_with_colour(self):
        assert "°C now / peak" in SRC
        assert "PEAK_T.get(d, {}).get(pk, {}).get(i['sn'])" in SRC
        assert "max(temperature_c)" in SRC and "temp_class(peak)" in SRC


class TestPeers:
    def test_leave_one_out_per_kw_median(self):
        pr = NS["peer_ratios"]
        # GTO1 shape: five 124 kW units and one 60 kW unit producing pro rata
        e = {"A": 620.0, "B": 600.0, "C": 610.0, "D": 590.0, "E": 300.0, "S": 290.0}
        rated = {"A": 124, "B": 124, "C": 124, "D": 124, "E": 124, "S": 60}
        r = pr(e, rated)
        assert 0.97 < r["A"] < 1.05 and 0.97 < r["S"] < 1.03      # small unit not flagged
        assert r["E"] < 0.55                                      # half-producing 124 kW unit
        # the rule's thresholds: 82% -> warn, 67% -> bad
        pc = NS["peer_class"]
        assert pc(None) == "" and pc(0.9) == "" and pc(0.82) == "warn" and pc(0.67) == "bad"

    def test_needs_two_producing_peers_and_ratings(self):
        pr = NS["peer_ratios"]
        assert pr({"A": 100.0, "B": 90.0}, {"A": 10, "B": 10}) == {}          # one peer only
        # C unrated: it is neither compared nor part of anyone's pool
        assert pr({"A": 100.0, "B": 90.0, "C": 95.0}, {"A": 10, "B": 10}) == {}
        r = pr({"A": 0.0, "B": 90.0, "C": 95.0}, {"A": 10, "B": 10, "C": 10})
        assert r == {"A": 0.0}          # the dead unit is 0%; B and C have one producing peer each
        r = pr({"A": None, "B": 90.0, "C": 95.0, "D": 92.0}, {"A": 10, "B": 10, "C": 10, "D": 10})
        assert r["A"] == 0.0 and 0.95 < r["B"] < 1.0 and 1.0 < r["C"] < 1.05

    def test_page_has_the_column_and_the_note(self):
        assert '>vs peers</th>' in SRC
        assert "peer_ratios({i['sn']: i['etoday'] for i in invs}, RATED.get(pk, {}))" in SRC
        assert "amber < 85%, red < 70%" in SRC
        # silent-inverter rows keep the column count (7 cells)
        assert "'<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'" in SRC


class TestOpenAlerts:
    def test_alerts_card_reads_the_ledger_without_the_digest(self):
        assert "FROM alert_ledger" in SRC
        assert "state = 'OPEN' AND metric <> 'daily_digest'" in SRC
        assert "{alerts_card(pk) if live else ''}" in SRC
        assert 'data-en="Open alerts' in SRC and 'data-es="Alertas abiertas' in SRC
