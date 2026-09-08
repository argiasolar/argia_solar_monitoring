"""v241 — the plant report tells the truth when a plant is dark.

Tomasz 2026-09-08: 'in Tetra Pak we do not have data, everything should
be in red with errors, and it is showing like nothing is happening';
'the tool tip for issue in plastic omnium has too big font or the tile
is a bit too small'; 'some logos do not fit the space correctly'; 'in
Ryder we do not see the expected from weather kWp in Daily production'.

The range engine is JavaScript, so its behaviour is exercised for real
under node with a 40-line DOM stub (skipped where node is absent — the
pure-Python assertions below still guard the source).
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
SRC = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
CHROME = (V2 / "server/bundle/portal_chrome.py").read_text(encoding="utf-8")
PGEN = (V2 / "server/bundle/portal_gen.py").read_text(encoding="utf-8")
sys.path.insert(0, str(V2 / "scripts"))

NODE = shutil.which("node")

# ------------------------------------------------------------ JS harness
DOM_STUB = r"""
const store = {};
const localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = v; } };
class El {
  constructor(id, cls) { this.id = id; this.textContent = ''; this._html = ''; this.value = ''; this.cls = new Set((cls||'').split(' ').filter(Boolean)); this.parent = null;
    this.classList = { add: c => this.cls.add(c), remove: (...cs) => cs.forEach(c => this.cls.delete(c)), toggle: (c, on) => { on ? this.cls.add(c) : this.cls.delete(c); },
                       contains: c => this.cls.has(c) }; }
  set innerHTML(h) { this._html = h; this.textContent = h.replace(/<[^>]+>/g, '').replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&lt;/g, '<'); }
  get innerHTML() { return this._html; }
  closest(sel) { let e = this; while (e) { if (e.cls.has(sel.slice(1))) return e; e = e.parent; } return null; }
  addEventListener() {}
  get className() { return [...this.cls].join(' '); }
}
const els = {};
const mk = (id, cls, parentId) => { const e = new El(id, cls); els[id] = e; if (parentId) e.parent = els[parentId]; return e; };
['t_prod','t_avail','t_loss','t_dq','t_pr'].forEach(id => mk(id, 'tile flip'));
mk('r_prodwhy', 'twhy', 't_prod'); mk('r_avwhy', 'twhy', 't_avail');
['d0','d1','r_prod','r_rev','r_rev_u','r_avail','r_range','r_vsctr','r_sla','r_co2','r_loss','r_loss_sub','r_dq','d_unit','dchart'].forEach(id => mk(id));
const document = { getElementById: id => els[id] || null, querySelectorAll: () => [], documentElement: { lang: 'en' } };
const window = { addEventListener: () => {} };
"""

DUMP = r"""
const out = {};
for (const id in els) out[id] = { text: els[id].textContent, cls: els[id].className, html: els[id]._html };
console.log(JSON.stringify(out));
"""


def _plant_js():
    m = re.search(r"PLANT_JS = r'''\n<script>(.*?)</script>'''", SRC, re.S)
    assert m, "PLANT_JS not found"
    return m.group(1)


def run_engine(globals_js: str, d0: str, d1: str, lang: str = "en", tmp_path=None):
    js = (DOM_STUB + f"store.argia_lang='{lang}';\n" + globals_js + "\n" + _plant_js()
          + f"\nels.d0.value='{d0}';els.d1.value='{d1}';compute();\n" + DUMP)
    p = tmp_path / "engine.js"
    p.write_text(js, encoding="utf-8")
    # encoding pinned: on Windows the default is cp1252 and '≈' comes back mangled
    res = subprocess.run([NODE, str(p)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def globals_for(days, energy, avail, dq, expected=None, contract=None, asof=None, tariff=0.0, sla=0.98):
    g = {"D": days, "E": energy, "RV": None, "C": contract, "X": expected,
         "AV": avail, "DQ": dq, "CO2Y": {}}
    s = "".join(f"const {k}={json.dumps(v)};" for k, v in g.items())
    s += f'const VSL="vs expected";const CO2F=0.444;const SLA={sla};const TARIFF={tariff};const ASOF="{asof or days[-1]}";'
    s += 'const CH_L={"actual":"actual","contract":"expected","weather":"expected from weather","money":"savings"};'
    return s


def _days(first: str, n: int):
    import datetime as dt
    d = dt.date.fromisoformat(first)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


@pytest.mark.skipif(not NODE, reason="node not available")
class TestRangeEngineDarkPlant:
    """The JavaScript that fills the tiles, run for real."""

    def test_healthy_range_is_unchanged(self, tmp_path):
        days = _days("2026-08-01", 10)
        g = globals_for(days, [1000.0] * 10, {d: 1.0 for d in days}, {d: 1 for d in days}, expected=[950.0] * 10)
        o = run_engine(g, days[0], days[-1], tmp_path=tmp_path)
        assert o["r_prod"]["text"] == "10"
        assert o["r_avail"]["text"] == "100.0%" and o["r_sla"]["text"] == "MET"
        assert "good" in o["t_avail"]["cls"] and "haswhy" not in o["t_avail"]["cls"]
        assert o["r_dq"]["text"] == "100%" and "bad" not in o["t_dq"]["cls"]
        assert o["r_loss"]["text"] == "≈ 0"

    def test_empty_range_is_red_and_says_so(self, tmp_path):
        # Tetra Pak: rows end 07-28, the page opens on the last 30 days
        days = _days("2026-07-01", 28)
        g = globals_for(days, [1000.0] * 28, {d: 1.0 for d in days}, {d: 1 for d in days},
                        expected=[900.0] * 28, asof="2026-09-07")
        o = run_engine(g, "2026-08-09", "2026-09-07", tmp_path=tmp_path)
        assert o["r_prod"]["text"] == "—" and "bad" in o["t_prod"]["cls"] and "haswhy" in o["t_prod"]["cls"]
        assert "nothing arrived for the 30 selected day(s)" in o["r_prodwhy"]["text"]
        assert "the last data is from 2026-07-28" in o["r_prodwhy"]["text"]
        assert o["r_avail"]["text"] == "—" and o["r_sla"]["text"] == "NO DATA"
        assert "bad" in o["t_avail"]["cls"] and "haswhy" in o["t_avail"]["cls"]
        assert o["r_loss"]["text"] == "—" and "bad" in o["t_loss"]["cls"]
        assert "nothing was measured" in o["r_loss_sub"]["text"]
        assert o["r_dq"]["text"] == "0%" and "bad" in o["t_dq"]["cls"]
        assert o["r_co2"]["text"] == "—"
        assert "No data in the selected range." in o["dchart"]["html"]

    def test_empty_range_before_the_plant_existed(self, tmp_path):
        days = _days("2026-08-01", 5)
        g = globals_for(days, [1.0] * 5, {}, {}, asof="2026-08-05")
        o = run_engine(g, "2026-07-01", "2026-07-10", tmp_path=tmp_path)
        assert "no data in the selected range" in o["r_prodwhy"]["text"]
        assert o["r_dq"]["text"] == "—"           # nothing was owed either

    def test_dark_days_count_as_unavailable_and_uncovered(self, tmp_path):
        # Ryder: 7 lit days, then 3 days with a 0.0 vendor row and no telemetry
        days = _days("2026-09-01", 10)
        e = [1900.0] * 7 + [0.0] * 3
        av = {d: 1.0 for d in days[:7]}
        dq = {d: 1 for d in days[:7]}
        g = globals_for(days, e, av, dq, expected=[1800.0] * 10)
        o = run_engine(g, days[0], days[-1], tmp_path=tmp_path)
        assert o["r_avail"]["text"] == "70.0%" and o["r_sla"]["text"] == "BREACH"
        assert "bad" in o["t_avail"]["cls"]
        assert "3 day(s) with no telemetry and no energy" in o["r_avwhy"]["text"]
        assert "data ends 2026-09-10" in o["r_avwhy"]["text"]
        assert "09-08: 0%" in o["r_avwhy"]["text"]          # dark days lead the worst list
        assert o["r_dq"]["text"] == "70%"
        # the dark days' whole weather expectation is at risk
        assert o["r_loss"]["text"] == "≤ 5.4 MWh"
        # production tile (no contract) turns red and says why
        assert "bad" in o["t_prod"]["cls"] and "went dark" in o["r_prodwhy"]["text"]

    def test_dark_days_never_read_as_produced_through_the_gap(self, tmp_path):
        # energy on the lit days beats the weather expectation, but dark
        # days are not a telemetry gap — REVIEW must not appear
        days = _days("2026-09-01", 10)
        e = [2500.0] * 8 + [0.0] * 2
        g = globals_for(days, e, {d: 1.0 for d in days[:8]}, {d: 1 for d in days[:8]}, expected=[1000.0] * 10)
        o = run_engine(g, days[0], days[-1], tmp_path=tmp_path)
        assert o["r_sla"]["text"] == "BREACH"

    def test_missing_rows_up_to_the_data_edge_are_dark(self, tmp_path):
        # Tetra Pak, 'Previous month' = July with rows to the 28th
        days = _days("2026-07-01", 28)
        g = globals_for(days, [1000.0] * 28, {d: 1.0 for d in days}, {d: 1 for d in days}, asof="2026-09-07")
        o = run_engine(g, "2026-07-01", "2026-07-31", tmp_path=tmp_path)
        assert o["r_dq"]["text"] == "90%"                  # 28 of 31
        assert o["r_avail"]["text"] == "90.3%" and o["r_sla"]["text"] == "BREACH"
        assert "3 day(s) with no telemetry" in o["r_avwhy"]["text"]

    def test_days_after_the_fleet_edge_are_not_owed(self, tmp_path):
        days = _days("2026-09-01", 5)
        g = globals_for(days, [1000.0] * 5, {d: 1.0 for d in days}, {d: 1 for d in days}, asof="2026-09-05")
        o = run_engine(g, "2026-09-01", "2026-09-30", tmp_path=tmp_path)
        assert o["r_avail"]["text"] == "100.0%" and o["r_dq"]["text"] == "100%"

    def test_vendor_only_days_stay_out_of_availability(self, tmp_path):
        # a backfilled month: energy from the vendor, no telemetry —
        # the plant produced; only coverage says we were not watching
        days = _days("2026-08-01", 10)
        g = globals_for(days, [1000.0] * 10, {days[-1]: 1.0}, {days[-1]: 1})
        o = run_engine(g, days[0], days[-1], tmp_path=tmp_path)
        assert o["r_avail"]["text"] == "100.0%" and o["r_sla"]["text"] == "MET"
        assert o["r_dq"]["text"] == "10%"

    def test_reasons_are_bilingual(self, tmp_path):
        days = _days("2026-07-01", 5)
        g = globals_for(days, [1000.0] * 5, {d: 1.0 for d in days}, {d: 1 for d in days}, asof="2026-09-07")
        o = run_engine(g, "2026-08-09", "2026-09-07", lang="es", tmp_path=tmp_path)
        assert "no llegó nada en los 30 día(s) elegidos" in o["r_prodwhy"]["text"]
        assert o["r_sla"]["text"] == "SIN DATOS"
        assert 'data-en="' in o["r_prodwhy"]["html"] and 'data-es="' in o["r_prodwhy"]["html"]
        # existing reasons went bilingual too
        assert "recurso, no desempeño" in SRC and "Peores días" in SRC


class TestSourceGuards:
    def test_why_texts_carry_both_languages(self):
        assert "const T=(en,es)=>" in SRC
        assert "tl.classList.toggle('haswhy',!!html)" in SRC
        for en in ("nothing arrived for the", "with no telemetry and no energy", "NO DATA",
                   "unknown — nothing was measured", "No data in the selected range."):
            assert en in SRC, en

    def test_stale_card_is_red_and_counts_the_silent_days(self):
        assert 'class="card bad"' in SRC and "No data since" in SRC and "Sin datos desde" in SRC
        assert "day(s) without any telemetry or energy" in SRC
        # it sits ABOVE the tiles on the portal page
        assert "parts['warn'] + parts['tiles']" in PGEN

    def test_coverage_tooltip_admits_the_red_case(self):
        assert "Days with no telemetry at all count as zero" in SRC
        assert "Informational — never colored" not in SRC


class TestFlipTileBackFace:
    """(a) the reason must never be cut: the back face is as tall as its text."""

    def test_back_face_grows_with_its_text(self):
        assert ".face.back{position:absolute;left:0;right:0;top:0;min-height:100%;transform:rotateY(180deg)" in CHROME
        assert ".face.back{position:absolute;inset:0" not in CHROME

    def test_reason_font_is_report_sized(self):
        assert ".twhy{font-size:12.5px;line-height:1.4;font-weight:500" in CHROME


class TestLogoCard:
    """(c) wide marks fit the header card."""

    def test_header_uses_the_logo_card(self):
        assert 'class="card logocard"' in PGEN
        assert 'style="width:64px;height:64px' not in PGEN

    def test_logo_card_grows_and_fits(self):
        assert ".logocard{min-width:64px;height:64px;" in CHROME
        assert ".logocard .clogo{height:36px;max-height:100%;width:auto;max-width:150px;object-fit:contain" in CHROME


class TestCalibrationNeedsASample:
    """(d) Ryder's PR 99.6% came from ONE snapshot-undercounted day."""

    def _fn(self):
        ns = {"MIN_CAL_DAYS": 10}
        seg = SRC[SRC.index("def per_irr_factor"):]
        exec(compile(seg[:seg.index("\n\n\n")], "per_irr", "exec"), ns)
        return ns["per_irr_factor"]

    def test_one_day_is_not_a_calibration(self):
        f = self._fn()
        # Ryder: config 273.5 kWh per kWh/m2, median PR 0.996 on 1 day
        assert f(273.49, 0.9961, 1, 364.65, min_days=10) == 273.49

    def test_enough_days_lift_the_floor(self):
        f = self._fn()
        assert f(273.49, 0.80, 30, 364.65, min_days=10) == pytest.approx(291.72)

    def test_config_stays_the_floor_for_a_sick_plant(self):
        f = self._fn()
        assert f(421.2, 0.46, 60, 561.6, min_days=10) == 421.2

    def test_no_config_no_line(self):
        assert self._fn()(None, 0.8, 30, 100.0, min_days=10) == 80.0
        assert self._fn()(None, None, 0, 100.0, min_days=10) == 0.0

    def test_thresholds_in_the_queries(self):
        assert "MIN_CAL_DAYS = 10" in SRC and "MIN_PR_DAYS = 7" in SRC
        assert SRC.count("GROUP BY plant_key HAVING count(*) >= {MIN_PR_DAYS}") == 2
        assert "percentile_cont(0.5) WITHIN GROUP (ORDER BY pr), count(*)" in SRC


class TestDarkPlantIrradiance:
    """(d) kpi_eod: a plant without telemetry still gets the weather
    device's irradiance and the expected kWh it implies."""

    def _mod(self):
        import importlib
        return importlib.import_module("kpi_eod")

    class _Plant:
        plant_key = "TAM1"
        datalogger_sn = "DYD0DXH00M"
        weather_plant_id = "10078094"
        datalogger_addr = "33"
        kwp_dc = 364.65
        expected_factor = 0.75

    def test_no_client_or_device_means_nothing(self, monkeypatch):
        m = self._mod()
        assert m.dark_plant_stamps(None, self._Plant(), "2026-09-05", {}) is None
        p = self._Plant(); p.datalogger_sn = ""
        assert m.dark_plant_stamps(object(), p, "2026-09-05", {}) is None

    def test_device_answer_becomes_irradiance_and_expected(self, monkeypatch):
        m = self._mod()
        from argia.kpi.irradiance import IrradianceDay, IrradianceSource
        monkeypatch.setattr(m, "try_dense_irradiance",
                            lambda web, plant, d: IrradianceDay(7.002, IrradianceSource.SHINEMASTER_HISTORY, 294))
        monkeypatch.setattr(m, "design_kwh_for_day", lambda design, pk, d: 1916.0)
        out = m.dark_plant_stamps(object(), self._Plant(), "2026-09-05", {})
        assert out["irradiance_kwh_m2"] == 7.002
        assert out["irradiance_source"] == "shinemaster_history"
        assert out["expected_kwh"] == pytest.approx(364.65 * 7.002 * 0.75, abs=0.01)
        assert out["design_kwh"] == 1916.0

    def test_unusable_answer_means_nothing(self, monkeypatch):
        m = self._mod()
        monkeypatch.setattr(m, "try_dense_irradiance", lambda web, plant, d: None)
        assert m.dark_plant_stamps(object(), self._Plant(), "2026-09-05", {}) is None

    def test_main_loop_stamps_the_dark_plant(self):
        src = (V2 / "scripts/kpi_eod.py").read_text(encoding="utf-8")
        assert "dark = dark_plant_stamps(dense_web, plant, date_iso, design)" in src
        assert 'stamp_column(sheets, "irradiance_kwh_m2", irr_stamps' in src
        assert 'stamp_column(sheets, "irradiance_source", irrsrc_stamps' in src
        # energy/PR are never invented for a dark plant
        assert "plants_without += 1" in src


class TestProxyBackfill:
    """(d) the one-off that gives Ryder's history its irradiance."""

    def _mod(self):
        import importlib
        return importlib.import_module("irradiance_proxy_backfill")

    PLANTS = [("NL1", "10078094", "DYD0DXH00M"), ("NL2", "10078094", "DYD0DXH00M"),
              ("TAM1", "10078094", "DYD0DXH00M"), ("GTO1", "9309575", "DYD0E8501G"),
              ("SLP1", "9275498", "DYDADA602S")]

    def test_donor_is_the_device_twin(self):
        m = self._mod()
        assert m.donor_for("TAM1", self.PLANTS) == "NL1"
        assert m.donor_for("NL2", self.PLANTS) == "NL1"
        assert m.donor_for("GTO1", self.PLANTS) is None          # own device, no twin
        assert m.donor_for("XXX", self.PLANTS) is None
        assert m.donor_for("TAM1", [("TAM1", "", "")]) is None

    def test_plan_fills_only_null_cells(self):
        m = self._mod()
        target = [("2026-08-01", "", ""), ("2026-08-02", "4.92", "1345.6"), ("2026-08-03", "", "500"), ("2026-08-04", "", "")]
        donor = {"2026-08-01": 7.0, "2026-08-02": 7.1, "2026-08-03": 6.0}
        out = m.plan("TAM1", target, donor, 364.65, 0.75)
        assert set(out) == {"2026-08-01", "2026-08-03"}           # 08-02 stored, 08-04 no donor value
        assert out["2026-08-01"] == {"irradiance_kwh_m2": 7.0, "expected_kwh": pytest.approx(1914.41, abs=0.01)}
        assert out["2026-08-03"] == {"irradiance_kwh_m2": 6.0}    # expected already there

    def test_sql_is_bounded_and_quoted(self):
        m = self._mod()
        s = m.select_target_sql("TAM1", "2026-05-13", "2026-09-07")
        assert "plant_key = 'TAM1'" in s and "BETWEEN DATE '2026-05-13' AND DATE '2026-09-07'" in s
        assert "irradiance_kwh_m2 > 0" in m.select_donor_sql("NL1", "2026-05-13", "2026-09-07")
        assert m._q("O'Neil") == "'O''Neil'"

    def test_dry_run_is_the_default(self):
        src = (V2 / "scripts/irradiance_proxy_backfill.py").read_text(encoding="utf-8")
        assert 'ap.add_argument("--apply", action="store_true"' in src
        assert "build_fill_nulls_sql" in src           # COALESCE(stored, new): NULLs only

    def test_fill_sql_touches_null_cells_only(self):
        m = self._mod()
        sql = m.fill_sql("TAM1", "NL1", {"2026-08-01": {"irradiance_kwh_m2": 7.0, "expected_kwh": 1914.41},
                                         "2026-08-03": {"irradiance_kwh_m2": 6.0}})
        stmts = [x for x in sql.splitlines() if x.strip()]
        assert len(stmts) == 2
        assert "irradiance_kwh_m2 = COALESCE(daily_production.irradiance_kwh_m2, 7.0)" in stmts[0]
        assert "irradiance_source = COALESCE(daily_production.irradiance_source, 'proxy:NL1')" in stmts[0]
        assert "expected_kwh = COALESCE(daily_production.expected_kwh, 1914.41)" in stmts[0]
        assert "expected_kwh = COALESCE" not in stmts[1]
        assert "WHERE plant_key = 'TAM1' AND prod_date = DATE '2026-08-01'" in stmts[0]
        assert "irradiance_kwh_m2 IS NULL OR irradiance_source IS NULL OR expected_kwh IS NULL" in stmts[0]
        # never an INSERT, never a bare SET
        assert "INSERT" not in sql and "SET energy_kwh" not in sql and "pr =" not in sql
