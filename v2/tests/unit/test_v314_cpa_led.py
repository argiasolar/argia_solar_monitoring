"""v314: LED lighting projects on cpa.argia.com.mx.

Tomasz, 6 Oct 2026: "update CPA portal with lighting projects that we have
delivered and that we have in pipeline. add the CO2 emission avoided ...
show them on the map with different statuses ... do not share any
financial data ... Make a switch on the map showing solar, showing LED,
showing both ... make it available for reports".

Synthetic parks, buildings and tenants only: the real list is server-only.
"""
from __future__ import annotations

import csv
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

import test_cpa_site as T  # noqa: E402


@pytest.fixture
def gen(tmp_path, monkeypatch):
    return T.make_gen(tmp_path, monkeypatch)

PARKS = [{"id": "norte", "name": "Parque Norte", "city": "Apodaca, N.L.", "lat": 25.79, "lon": -100.16},
         {"id": "sur", "name": "Parque Sur", "city": "Zapopan, Jal.", "lat": 20.72, "lon": -103.49}]
PROJECTS = [
    {"id": "LED-01", "status": "delivered", "building": "NORTE B001", "park": "norte", "tenant": "Acme Logistics",
     "area_m2": 20000, "fixtures": 500, "kw_before": 200.0, "kw_after": 80.0, "hours": 4992, "kind": "logistic",
     "sensors": True, "lux_before": "200 lx", "lux_after": "350 lx"},
    {"id": "LED-02", "status": "delivered", "building": "NORTE B002", "park": "norte", "tenant": None,
     "area_m2": None, "fixtures": 60, "kw_before": None, "kw_after": 10.0, "hours": 4992, "kind": "office"},
    {"id": "LED-03", "status": "installing", "building": "SUR B010", "park": "sur", "tenant": "Beta Foods",
     "area_m2": 10000, "fixtures": 300, "kw_before": 100.0, "kw_after": 40.0, "hours": 8736, "kind": "production",
     "lat": 20.7215, "lon": -103.4911},
    {"id": "LED-04", "status": "proposal", "building": "SUR B011", "park": "sur", "fixtures": 100,
     "kw_before": 50.0, "kw_after": 50.0, "hours": 4992, "kind": "exterior"},
]


def write_led(dirpath: pathlib.Path, parks=PARKS, projects=PROJECTS) -> pathlib.Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / "led.json"
    p.write_text(json.dumps({"parks": parks, "projects": projects}), encoding="utf-8")
    return p


# ------------------------------------------------------------------ figures
class TestFigures:
    def test_savings_cut_and_co2_use_the_one_register(self, tmp_path):
        parks, ps = LED.load(str(write_led(tmp_path)))
        a, b, c, d = ps
        assert a.saved_kwh == pytest.approx(120 * 4992) and a.cut_pct == pytest.approx(60)
        assert a.co2_t() == pytest.approx(120 * 4992 * co2reg.CURRENT / 1000)
        assert b.new_build and b.saved_kwh is None and b.co2_t() is None and b.cut_pct is None   # nothing invented
        assert c.saved_kwh == pytest.approx(60 * 8736) and c.exact and not a.exact
        assert d.saved_kwh == 0.0 and d.cut_pct == 0.0

    def test_summary_delivered_pipeline_and_parks(self, tmp_path):
        _, ps = LED.load(str(write_led(tmp_path)))
        s = LED.summarise(ps)
        assert (s["delivered"].projects, s["delivered"].fixtures, s["delivered"].parks) == (2, 560, 1)
        assert s["delivered"].saved_kwh == pytest.approx(120 * 4992)              # the new building adds nothing
        assert s["delivered"].kw_before == 200 and s["delivered"].cut_pct == pytest.approx(60)
        assert (s["_pipeline"].projects, s["_pipeline"].parks) == (2, 1)
        assert s["_all"].projects == 4 and s["_all"].co2_t == pytest.approx(s["delivered"].co2_t + s["_pipeline"].co2_t)
        assert s["to_confirm"].projects == 0

    def test_parks_ordered_by_delivered(self, tmp_path):
        parks, ps = LED.load(str(write_led(tmp_path)))
        assert [p.id for p, _ in LED.by_park(ps, parks)] == ["norte", "sur"]
        assert [x.id for x in LED.by_park(ps, parks)[1][1]] == ["LED-03", "LED-04"]      # installing before proposal

    @pytest.mark.parametrize("bad", [
        {"projects": [dict(PROJECTS[0], status="maybe")]},
        {"projects": [dict(PROJECTS[0], park="nowhere")]},
        {"projects": [dict(PROJECTS[0], kw_before=-1)]},
        {"projects": [dict(PROJECTS[0], hours=9000)]},
        {"parks": [dict(PARKS[0], lat=48.85, lon=2.35)]},                         # not in Mexico: a wrong pin
    ])
    def test_load_refuses_what_would_mislead(self, tmp_path, bad):
        parks = bad.get("parks", PARKS) + ([PARKS[1]] if "parks" in bad else [])
        with pytest.raises(ValueError):
            LED.load(str(write_led(tmp_path, parks, bad.get("projects", PROJECTS))))

    def test_csv_has_no_financial_column(self, tmp_path):
        parks, ps = LED.load(str(write_led(tmp_path)))
        rows = list(csv.reader(io.StringIO(LED.csv_projects(ps, parks))))
        head = " ".join(rows[0]).lower()
        assert not re.search(r"mxn|price|cost|usd|\$|precio|costo", head)
        assert rows[1][2] == "NORTE B001" and float(rows[1][11]) == 120 * 4992 and rows[1][12] == f"{co2reg.CURRENT:.3f}"
        b002 = [r for r in rows if r[2] == "NORTE B002"][0]
        assert b002[11] == "" and b002[13] == ""                                   # new building: no saving claimed


# ------------------------------------------------------------------ importer
def sheet_rows(extra_status="Propuesta", summary_fix=2):
    """A sheet shaped like ARGIA's: summary block, header row, projects."""
    hdr = [None, "Id", "Status", "Customer [CPA Park]", "City/Market", "Park / Address", "Tenant", "Area [m2]", "Manufacturer",
           "Before kW", "After kW", "Fixtures", "SAVINGS", "Old fixtures", "Lux level before", "Target lux level",
           "Actual lux level", "Type", "Automation", "Warehouse", "Office", "Operating hours [h/yr]", "Before kWh", "After kWh"]
    dash = chr(0x2014)
    return [
        (None, None, None, "CPA PROJECTS OVERVIEW"),
        (None, None, None, "Status", "# Projects", "Fixtures", "Area [m2]", None, "Estimated annual savings [kWh]", 120 * 4992),
        (None, None, None, "Entregado", summary_fix, 560, 20000),
        (None, None, None, "Propuesta", 1, 100, 0),
        tuple(hdr),
        (None, 1, "Entregado", "CPA NORTE-B001 ", "Monterrey", "x", "Acme Logistics", 20000, "m", 200, 80, 500, None, "MH 400W",
         "200lx", "300lx", "350lx", "Logistic", "Motion/Daylight sensor per fixture", "Yes", "No", 4992, None, None),
        (None, 2, "Entregado", "CPA NORTE B002", "Monterrey", "x", dash, "NA", "m", None, 10, 60, None, "New building",
         dash, "300lx", "320lx", "Office", dash, "No", "Yes", 4992, None, None),
        (None, 4, extra_status, "CPA SUR B011", "Guadalajara", "x", dash, None, "m", 50, 50, 100, None, "MH",
         "10lx", "20lx", "25lx", "Exterior", dash, "No", "No", 4992, None, None),
        (None, None, None, None),
    ]


PARKS_DOC = {"parks": [dict(PARKS[0], match=["NORTE"]), dict(PARKS[1], match=["SUR"]),
                       {"id": "unused", "name": "Otro", "city": "x", "lat": 19.5, "lon": -99.2, "match": ["OTRO"]}],
             "buildings": [{"match": "SUR B011", "lat": 20.7215, "lon": -103.4911}]}


class TestImporter:
    def test_convert_cleans_and_locates(self):
        import cpa_led_import as I
        doc, problems = I.convert(sheet_rows(), PARKS_DOC)
        assert problems == []
        ps = {p["building"]: p for p in doc["projects"]}
        assert set(ps) == {"NORTE B001", "NORTE B002", "SUR B011"}                 # "CPA " and dashes normalised
        a, b, c = ps["NORTE B001"], ps["NORTE B002"], ps["SUR B011"]
        assert a["status"] == "delivered" and a["kw_before"] == 200 and a["lux_before"] == "200 lx" and a["sensors"] is True
        assert b["tenant"] is None and b["area_m2"] is None and b["lux_before"] is None and b["kw_before"] is None
        assert (c["lat"], c["lon"]) == (20.7215, -103.4911) and a["lat"] is None
        assert [p["id"] for p in doc["parks"]] == ["norte", "sur"]                   # an unused park is not published
        assert chr(0x2014) not in json.dumps(doc, ensure_ascii=False)

    def test_unmatched_building_and_unknown_status_stop_the_import(self):
        import cpa_led_import as I
        rows = sheet_rows(extra_status="Quizas")
        rows.insert(-1, (None, 9, "Propuesta", "CPA LEJOS B1", "x", "x", None, None, "m", 1, 1, 1))
        _, problems = I.convert(rows, PARKS_DOC)
        assert any("unknown status 'Quizas'" in p for p in problems) and any("LEJOS B1: no park" in p for p in problems)

    def test_reconciles_with_the_sheet_summary(self):
        import cpa_led_import as I
        doc, _ = I.convert(sheet_rows(), PARKS_DOC)
        assert I.reconcile(doc, sheet_rows()) == []
        bad = I.reconcile(doc, sheet_rows(summary_fix=3))
        assert bad and "delivered: sheet 3 projects" in bad[0]

    def test_main_writes_only_a_clean_reconciled_file(self, tmp_path, monkeypatch):
        import cpa_led_import as I
        openpyxl = pytest.importorskip("openpyxl")
        wb = openpyxl.Workbook()
        for r in sheet_rows():
            wb.active.append(list(r))
        x = tmp_path / "s.xlsx"
        wb.save(x)
        pk = tmp_path / "parks.json"
        pk.write_text(json.dumps(PARKS_DOC), encoding="utf-8")
        out = tmp_path / "led.json"
        assert I.main([str(x), "--parks", str(pk), "--out", str(out)]) == 0
        parks, ps = LED.load(str(out))
        assert len(ps) == 3 and json.loads(out.read_text(encoding="utf-8"))["source"] == "s.xlsx"
        wb2 = openpyxl.Workbook()
        for r in sheet_rows(summary_fix=5):
            wb2.active.append(list(r))
        wb2.save(x)
        out2 = tmp_path / "led2.json"
        assert I.main([str(x), "--parks", str(pk), "--out", str(out2)]) == 3 and not out2.exists()

    def test_importer_reads_no_money_column(self):
        src = (V2 / "scripts" / "cpa_led_import.py").read_text(encoding="utf-8").lower()
        heads = re.search(r"headers = \{(.*?)\}", src, flags=re.S).group(1)
        assert not re.search(r"price|cost|mxn|usd|precio|costo|investment", heads)


# ------------------------------------------------------------------ the site
@pytest.fixture
def site(gen):
    g, tmp = gen
    write_led(tmp / "cpa")
    ctx, stage = T.build_site(g, tmp)
    return g, ctx, pathlib.Path(stage)


def _visible(h):
    return re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)


def test_pages_pass_the_gate_and_carry_no_money(site):
    g, ctx, stage = site
    assert g.check(str(stage), ctx) == []
    for rel in ("index.html", "led/index.html", "report/index.html"):
        vis = _visible((stage / rel).read_text(encoding="utf-8"))
        assert not re.search(r"\bMXN\b|\$\s?\d|\bprice\b|\bprecio\b|\bcost saved\b|\bahorro econ", vis, flags=re.I), rel
    led = (stage / "led" / "index.html").read_text(encoding="utf-8")
    assert 'id="p"' not in led and "mxn" not in led.lower()                    # the estimator lost its price field (v314.1: also in its script)


def test_led_page_tiles_table_and_parks(site):
    g, ctx, stage = site
    h = (stage / "led" / "index.html").read_text(encoding="utf-8")
    d = ctx.led["delivered"]
    assert f'data-count="{d.saved_kwh / 1000:.0f}"' in h and f'data-count="{d.co2_t:.0f}"' in h
    assert f'data-count="{d.fixtures:.0f}"' in h and f'data-count="{d.cut_pct:.0f}"' in h
    for pid in ("LED-01", "LED-02", "LED-03", "LED-04"):
        assert f'id="{pid}"' in h                                                 # map dots link here
    assert "Acme Logistics" in h and "Parque Norte" in h and "To be installed" in h and "Por instalar" in h
    row = re.search(r'id="LED-02".*?</tr>', h, flags=re.S).group(0)
    assert ">10<" in row and row.count(">-<") >= 3                                 # new building: no saving, no cut
    assert "0%" in re.search(r'id="LED-04".*?</tr>', h, flags=re.S).group(0)      # no '-0%'
    assert "Estimated" in h or "estimated" in h


def test_map_switch_and_led_points(site):
    g, ctx, stage = site
    h = (stage / "index.html").read_text(encoding="utf-8")
    assert 'id="mswitch"' in h and all(f'data-m="{m}"' in h for m in ("solar", "led", "both"))
    assert "MODE0='both'" in h and "localStorage.setItem(KEY" in h and "try{" in h
    lp = json.loads(re.search(r"var LP=(\[.*?\]),SL=", h, flags=re.S).group(1))
    assert [p["st"] for p in lp] == ["delivered", "delivered", "installing", "proposal"]
    assert all((p["lat"], p["lon"]) == ((25.79, -100.16) if p["pk"] == "norte" else (20.72, -103.49)) for p in lp)   # ring around the park
    assert lp[0]["url"] == "led/index.html#LED-01" and "Saved per year" in lp[0]["ten"] and "Ahorro por año" in lp[0]["tes"]
    assert "Potential saving" in lp[3]["ten"] and "New building" in lp[1]["ten"] and "building location" in lp[2]["ten"]
    assert not re.search(r"MXN|\$", json.dumps(lp))
    sl = json.loads(re.search(r",SL=(\{.*?\}),MODE0", h, flags=re.S).group(1))
    assert len({v[2] for v in sl.values()}) == 4                                   # one colour per status
    ledpage = (stage / "led" / "index.html").read_text(encoding="utf-8")
    assert "MODE0='led'" in ledpage and '"url": "../led/index.html#LED-01"' in ledpage


def test_overview_section_and_climate_line(site):
    g, ctx, stage = site
    h = (stage / "index.html").read_text(encoding="utf-8")
    assert "LED lighting in your parks" in h and 'href="led/index.html"' in h
    assert f"Plus {ctx.led['delivered'].co2_t:,.0f} t CO2e avoided every year" in h


def test_report_has_the_led_section_and_csv(site):
    g, ctx, stage = site
    h = (stage / "report" / "index.html").read_text(encoding="utf-8")
    assert 'href="CPA_LED_projects.csv"' in h and (stage / "report" / "CPA_LED_projects.csv").exists()
    assert "LED lighting efficiency, delivered retrofits" in h and "NORTE B001" in h
    assert "SUR B010" not in re.search(r"delivered retrofits.*?</table>", h, flags=re.S).group(0)  # not delivered
    assert "Estimated, not metered" in h or "Estimado, no medido" in h


def test_without_led_file_the_site_is_solar_only(gen):
    g, tmp = gen
    ctx, stage = T.build_site(g, tmp)
    h = pathlib.Path(stage, "index.html").read_text(encoding="utf-8")
    assert ctx.projects == [] and 'id="mswitch"' not in h and "MODE0='solar'" in h and "var LP=[]" in h
    assert not pathlib.Path(stage, "report", "CPA_LED_projects.csv").exists()


def test_a_broken_led_file_stops_the_build(gen):
    g, tmp = gen
    write_led(tmp / "cpa", projects=[dict(PROJECTS[0], status="??")])
    with pytest.raises(ValueError):
        T.build_site(g, tmp)


def test_zoomed_out_solar_pins_stay_true_and_led_dots_stay_visible(site):
    """v314.2: on the live site, zoomed out to all of Mexico, the declutter
    push drew Vitalmex next to Guadalajara and the pins hid the CDMX LED
    dots. Now pins are pushed only from zoom 9, shrink when zoomed out, and
    LED dots sit above them."""
    g, ctx, stage = site
    h = (stage / "index.html").read_text(encoding="utf-8")
    assert "push=m.getZoom()>=9" in h and "if(push){" in h and "scale('+sc+')" in h
    assert "zIndexOffset:p.st==='delivered'?2500:2000" in h and "{icon:icon}).addTo(sol)" in h


def test_overview_order_solar_first_and_rows_clear_the_header(site):
    """v314.3 (Tomasz): the overview shows the solar sites and their energy
    first, then the LED improvements, then the climate impact; a project row
    opened from the map was half hidden under the sticky header."""
    g, ctx, stage = site
    h = (stage / "index.html").read_text(encoding="utf-8")
    order = [h.index(x) for x in ('The sites', 'Power today', 'Clean energy by month', 'LED lighting in your parks',
                                  'Climate impact', 'Solar and LED across your parks')]
    assert order == sorted(order)
    css = g.CSS.replace("\n", "")
    assert "[id]{scroll-margin-top:120px}" in css
    assert "@media(max-width:980px){.top{position:static}[id]{scroll-margin-top:16px}}" in css   # phone: header wraps to ~200 px
