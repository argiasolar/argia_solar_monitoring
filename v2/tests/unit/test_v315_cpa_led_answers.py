"""v315: Eduardo's answers on the CPA LED projects (6 Oct 2026).

Handover dates -> CO2 avoided since handover (each calendar year at that
year's register factor); a missing "before" load; lighting controls share;
vacant buildings; design light levels; proposals no longer active become
"opportunities" (design ready) shown apart from the active pipeline. The
answers are applied by the importer AFTER the sheet is reconciled, keyed by
project id and building name. Synthetic data only.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import sys

import pytest

from argia.core import co2 as co2reg
from argia.cpa import led as LED

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2 / "scripts"))
sys.path.insert(0, str(V2 / "tests" / "unit"))

import test_cpa_site as T          # noqa: E402
import test_v314_cpa_led as V      # noqa: E402

TODAY = T.NOW.date()               # 2026-10-02


@pytest.fixture
def gen(tmp_path, monkeypatch):
    return T.make_gen(tmp_path, monkeypatch)


def proj(**kw):
    base = dict(id="X", status="delivered", building="B", park="norte", kw_before=100.0, kw_after=40.0, hours=4000.0)
    base.update(kw)
    return LED.Project(**base)


class TestFigures:
    def test_to_date_splits_years_at_their_factor(self):
        p = proj(delivered="2023-07-01")
        kwh, t = p.to_date(dt.date(2024, 7, 1))
        s = 60 * 4000.0
        d23, d24 = (dt.date(2024, 1, 1) - dt.date(2023, 7, 1)).days, (dt.date(2024, 7, 1) - dt.date(2024, 1, 1)).days
        assert kwh == pytest.approx(s * (d23 + d24) / 365.25)
        assert t == pytest.approx(s * d23 / 365.25 * co2reg.factor(2023) / 1000 + s * d24 / 365.25 * co2reg.factor(2024) / 1000)

    def test_to_date_needs_a_delivered_project_with_a_date_and_a_saving(self):
        assert proj().to_date(TODAY) == (None, None)
        assert proj(delivered="2024-01-01", kw_before=None).to_date(TODAY) == (None, None)
        assert proj(status="installing", delivered="2024-01-01").to_date(TODAY) == (None, None)
        assert proj(delivered="2030-01-01").to_date(TODAY) == (0.0, 0.0)              # not yet

    def test_controls_cut_the_new_load_hours(self):
        """ADN B017 (Eduardo): already LED, 63.63 -> 63.51 kW, 35% from the controls."""
        p = proj(kw_before=63.63, kw_after=63.51, hours=4992, controls_pct=35)
        assert p.saved_kwh == pytest.approx(63.63 * 4992 - 63.51 * 4992 * 0.65)
        assert p.cut_pct == pytest.approx((1 - 63.51 / 63.63) * 100)                    # the load cut stays the load cut

    def test_summary_pipeline_is_active_only_and_to_date_adds_up(self):
        ps = [proj(id="a", delivered="2022-05-10"), proj(id="b", delivered="2025-02-21"),
              proj(id="c", status="installing", planned="2026-10-26"), proj(id="d", status="proposal"),
              proj(id="e", status="opportunity")]
        s = LED.summarise(ps, today=TODAY)
        assert (s["_pipeline"].projects, s["opportunity"].projects) == (2, 1)
        assert s["delivered"].first == "2022-05-10"
        assert s["delivered"].co2_to_date == pytest.approx(sum(p.to_date(TODAY)[1] for p in ps[:2]))
        assert s["_all"].co2_to_date == s["delivered"].co2_to_date
        assert LED.summarise(ps)["delivered"].co2_to_date == 0.0                         # no today, no to-date

    @pytest.mark.parametrize("bad", [{"delivered": "17/08/2026"}, {"delivered": "1999-01-01"}, {"planned": "2026-13-01"},
                                     {"controls_pct": 100}, {"controls_pct": "lots"}])
    def test_load_refuses_bad_dates_and_shares(self, tmp_path, bad):
        with pytest.raises(ValueError):
            LED.load(str(V.write_led(tmp_path, projects=[dict(V.PROJECTS[0], **bad)])))


ANSWERS = {"projects": {
    "LED-02": {"building": "NORTE B002", "set": {"kw_before": 25, "delivered": "2023-12-06", "vacant": True}},
    "LED-04": {"building": "SUR B011", "set": {"status": "opportunity", "controls_pct": 35, "lat": 20.73, "lon": -103.5}},
    "LED-01": {"building": "NORTE B001", "set": {"delivered": "2022-05-10", "lux_after": "323 lx WH / 753 lx office"}},
}}


class TestImporterAnswers:
    def _doc(self):
        import cpa_led_import as I
        doc, problems = I.convert(V.sheet_rows(), V.PARKS_DOC)
        assert problems == []
        return I, doc

    def test_answers_change_only_what_they_say_and_list_it(self):
        I, doc = self._doc()
        changes, bad = I.apply_answers(doc, ANSWERS, V.PARKS_DOC)
        assert bad == []
        ps = {p["id"]: p for p in doc["projects"]}
        assert ps["LED-02"]["kw_before"] == 25 and ps["LED-02"]["vacant"] is True and ps["LED-02"]["delivered"] == "2023-12-06"
        assert ps["LED-04"]["status"] == "opportunity" and ps["LED-04"]["controls_pct"] == 35
        assert ps["LED-01"]["lux_after"] == "323 lx WH / 753 lx office"
        assert any("LED-02 NORTE B002: kw_before None -> 25" in c for c in changes) and len(changes) == 9

    @pytest.mark.parametrize("bad, words", [
        ({"LED-99": {"building": "X", "set": {}}}, "no such project"),
        ({"LED-01": {"building": "SUR B011", "set": {"kw_before": 1}}}, "answer is for"),        # a moved row
        ({"LED-01": {"building": "NORTE B001", "set": {"price": 1}}}, "unknown field"),
        ({"LED-01": {"building": "NORTE B001", "set": {"status": "won"}}}, "unknown status"),
        ({"LED-01": {"building": "NORTE B001", "set": {"park": "luna"}}}, "unknown park"),
    ])
    def test_a_wrong_answer_is_refused(self, bad, words):
        I, doc = self._doc()
        _, problems = I.apply_answers(doc, {"projects": bad}, V.PARKS_DOC)
        assert problems and words in problems[0]

    def test_the_sheet_is_reconciled_before_the_answers(self, tmp_path):
        """The answers change totals (a new 'before' load): the reconcile still
        checks the sheet as Eduardo wrote it, and the file is written."""
        import cpa_led_import as I
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        for r in V.sheet_rows():
            wb.active.append(list(r))
        x, pk, ans, out = tmp_path / "s.xlsx", tmp_path / "parks.json", tmp_path / "answers.json", tmp_path / "led.json"
        wb.save(x)
        pk.write_text(json.dumps(V.PARKS_DOC), encoding="utf-8")
        ans.write_text(json.dumps(ANSWERS), encoding="utf-8")
        assert I.main([str(x), "--parks", str(pk), "--answers", str(ans), "--out", str(out)]) == 0
        _, ps = LED.load(str(out))
        assert {p.id: p.status for p in ps}["LED-04"] == "opportunity"
        assert json.loads(out.read_text(encoding="utf-8"))["answers"] == "answers.json"
        ans.write_text(json.dumps({"projects": {"LED-01": {"building": "SUR B011", "set": {}}}}), encoding="utf-8")
        out.unlink()
        assert I.main([str(x), "--parks", str(pk), "--answers", str(ans), "--out", str(out)]) == 2 and not out.exists()


# ------------------------------------------------------------------ the site
PROJ = [dict(V.PROJECTS[0], delivered="2022-05-10"),                                  # LED-01 delivered, dated
        dict(V.PROJECTS[1], kw_before=25.0, delivered="2023-12-06", vacant=True),     # LED-02 vacant, now with a before
        dict(V.PROJECTS[2], planned="2026-10-26"),                                     # LED-03 installing
        dict(V.PROJECTS[3], status="opportunity", controls_pct=35)]                    # LED-04 opportunity


@pytest.fixture
def site(gen):
    g, tmp = gen
    V.write_led(tmp / "cpa", projects=PROJ)
    ctx, stage = T.build_site(g, tmp)
    return g, ctx, pathlib.Path(stage)


def test_gate_and_no_money(site):
    g, ctx, stage = site
    assert g.check(str(stage), ctx) == []
    for rel in ("index.html", "led/index.html", "report/index.html"):
        vis = V._visible((stage / rel).read_text(encoding="utf-8"))
        assert not re.search(r"\bMXN\b|\$\s?\d|\bprice\b|\bprecio\b", vis, flags=re.I), rel


def test_to_date_on_the_tiles_cards_table_and_report(site):
    g, ctx, stage = site
    d = ctx.led["delivered"]
    assert d.first == "2022-05-10" and d.co2_to_date > 0
    led = (stage / "led" / "index.html").read_text(encoding="utf-8")
    assert f'data-count="{d.co2_to_date:.0f}"' in led and "since May 2022" in led and "desde mayo de 2022" in led
    row = re.search(r'id="LED-01".*?</tr>', led, flags=re.S).group(0)
    t01 = LED.Project(**PROJ[0]).to_date(ctx.today)[1]
    assert "Handed over 10/05/2022" in row and f"{t01:,.0f} " in row
    row2 = re.search(r'id="LED-02".*?</tr>', led, flags=re.S).group(0)
    assert ">Vacant<" in row2 and "Vacante" in row2 and "-60%" in row2                 # 25 -> 10 kW
    assert "Planned 26/10/2026" in re.search(r'id="LED-03".*?</tr>', led, flags=re.S).group(0)
    assert "Opportunity, design ready" in led and "class=\"grid g3 stat3\"" in led          # delivered, installing, opportunity
    ov = (stage / "index.html").read_text(encoding="utf-8")
    assert f"Plus {d.co2_to_date:,.0f} t CO2e avoided to date" in ov and "Ready to go: 1 more buildings" in ov
    rep = (stage / "report" / "index.html").read_text(encoding="utf-8")
    assert "10/05/2022" in rep and f"{d.co2_to_date:,.1f}" in rep and "Opportunities (design ready, not active): 1 buildings" in rep


def test_tooltips_say_design_level_controls_dates_and_vacant(site):
    g, ctx, stage = site
    h = (stage / "index.html").read_text(encoding="utf-8")
    lp = {p["url"].split("#")[1]: p for p in json.loads(re.search(r"var LP=(\[.*?\]),SL=", h, flags=re.S).group(1))}
    assert "Light level (design)" in lp["LED-01"]["ten"] and "CO2e avoided to date" in lp["LED-01"]["ten"]
    assert "Handed over 10/05/2022" in lp["LED-01"]["ten"] and "Entregado el 10/05/2022" in lp["LED-01"]["tes"]
    assert "Vacant" in lp["LED-02"]["ten"] and "Vacante" in lp["LED-02"]["tes"]
    assert "-35% hours on" in lp["LED-04"]["ten"] and lp["LED-04"]["st"] == "opportunity"
    assert "Planned 26/10/2026" in lp["LED-03"]["ten"]


def test_csv_carries_dates_controls_and_to_date(site):
    g, ctx, stage = site
    rows = list(csv.DictReader(io.StringIO((stage / "report" / "CPA_LED_projects.csv").read_text(encoding="utf-8"))))
    r = {x["building"]: x for x in rows}
    assert r["NORTE B001"]["handover_date"] == "2022-05-10" and float(r["NORTE B001"]["estimated_t_co2e_avoided_to_date"]) > 0
    assert r["NORTE B002"]["tenant"] == "Vacant" and r["SUR B011"]["controls_saving_pct"] == "35"
    assert r["SUR B010"]["planned_date"] == "2026-10-26" and r["SUR B010"]["estimated_t_co2e_avoided_to_date"] == ""
    assert "light_level_after_design" in rows[0]
