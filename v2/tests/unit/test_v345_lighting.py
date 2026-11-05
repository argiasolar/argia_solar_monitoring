"""v345 - Prologis platform: the LED lighting programme (/lighting/).

Tomasz, 5 Nov 2026: "add lighting page to prologis.argia.com.mx/lighting ... pictures of the customer plants
even if it is only google maps satellite view ... customer logos ... overview of plants by city, by park ...
table of all plants and statistics ... CO2 saving counter, area covered ... separate page for plants that are
still not changed to LED even though we have audit or ... from Prologis database (only over 5000 m2)".

Every name, code and number below is invented - the real data is server-only (the repo is public).
"""
from __future__ import annotations

import copy
import datetime as dt
import importlib
import json
import pathlib
import re
import struct
import sys
import zlib

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.core import co2 as co2reg                       # noqa: E402
from argia.prologis import lighting as LT                  # noqa: E402
from argia.prologis import store as S                      # noqa: E402

TODAY = dt.date(2026, 11, 5)


def _p(i, **kw):
    p = {"id": i, "park": "Parque Uno", "market": "cdmx", "status": "delivered", "kind": "tenant",
         "customer": "Acme Logistics", "group": "acme", "yardi": "MXC09901", "bldg": "Building 1", "year": 2022,
         "wo_number": f"2022-90{i}", "po_number": f"200000000{i}", "wo_date": "2022-03-01", "handover_date": "2022-07-01",
         "handover_planned": False, "fixtures": 300, "area_m2": 6000, "kwh_before": 200000, "kwh_after": 80000,
         "lat": 19.70, "lon": -99.20, "geo": "park",
         "docs": {"audit": True, "study": True, "proforma": True, "wo": True, "handover": True}}
    p.update(kw)
    return p


DATA = {
    "meta": {"schema": 1, "generated": "2026-11-05", "note": "test"},
    "projects": [
        _p(1),                                                                         # full building 1 (6000 of 7000 m2 -> 86 %)
        _p(2, yardi="MXC09902", bldg="Building 2", area_m2=2000, customer="Bravo Foods", group="bravo",
           year=2024, handover_date="2024-05-01", kwh_before=90000, kwh_after=40000, fixtures=100),   # partial of 9000
        _p(3, yardi="GDL09903", park="Parque Dos", market="gdl", status="in_progress", customer="Charlie Parts",
           group="charlie", year=None, handover_date="2027-01-15", handover_planned=True, lat=20.6, lon=-103.3),
        _p(4, yardi=None, park="Parque Dos", market="gdl", kind="exterior", customer="Exterior lighting", group=None,
           area_m2=None, kwh_before=None, kwh_after=None, fixtures=40, lat=20.6, lon=-103.31, year=2021,
           handover_date="2021-02-01"),
        _p(5, yardi="MXC09901", bldg="Building 1", customer="Acme Logistics", year=2019, handover_date="2019-11-01",
           area_m2=500, kwh_before=30000, kwh_after=12000, fixtures=20),
        _p(6, yardi="MXC09905", bldg="Building 5", customer="Delta Med", group="delta", year=2025,
           handover_date="2025-08-01", area_m2=1500, kwh_before=60000, kwh_after=25000, fixtures=60),
        _p(7, yardi="MXC09906", bldg="Building 6", customer="Echo Paper", group="echo", year=2023,
           handover_date="2023-04-01", area_m2=2500, kwh_before=110000, kwh_after=40000, fixtures=90),
    ],
    "buildings": [
        {"yardi": "MXC09901", "market": "cdmx", "park": "Parque Uno", "building": 1, "name": "Uno 1", "area_m2": 7000, "lat": 19.70, "lon": -99.20, "geo": "park"},
        {"yardi": "MXC09902", "market": "cdmx", "park": "Parque Uno", "building": 2, "name": "Uno 2", "area_m2": 9000, "lat": 19.701, "lon": -99.201, "geo": "park"},
        {"yardi": "GDL09903", "market": "gdl", "park": "Parque Dos", "building": 3, "name": "Dos 3", "area_m2": 12000, "lat": 20.6, "lon": -103.3, "geo": "park"},
        {"yardi": "MXC09904", "market": "cdmx", "park": "Parque Uno", "building": 4, "name": "Uno 4", "area_m2": 20000, "lat": 19.702, "lon": -99.202, "geo": "building"},
        {"yardi": "MXC09907", "market": "cdmx", "park": "Parque Uno", "building": 7, "name": "Uno 7", "area_m2": 15000, "lat": 19.703, "lon": -99.203, "geo": "park"},
        {"yardi": "MXC09908", "market": "cdmx", "park": "Parque Uno", "building": 8, "name": "Uno 8", "area_m2": 4000, "lat": 19.704, "lon": -99.204, "geo": "park"},
        {"yardi": "MXC09909", "market": "cdmx", "park": "Parque Uno", "building": 9, "name": "Uno 9", "area_m2": None, "lat": None, "lon": None, "geo": None},
    ],
    "opportunities": [{"id": 1, "yardi": "MXC09904", "customer": "Vacante", "area_m2": 20000, "audit": True,
                       "study": True, "proforma": True, "last_proforma": "2025-06-05"}],
    "customers": {"acme": {"name": "Acme Logistics", "domain": "acme.example", "logo": "acme.png"},
                  "bravo": {"name": "Bravo Foods", "domain": None, "logo": None},
                  "charlie": {"name": "Charlie Parts", "logo": None}, "delta": {"name": "Delta Med", "logo": None},
                  "echo": {"name": "Echo Paper", "logo": None}},
}


def _png(w=4, h=2) -> bytes:
    raw = b"".join(b"\x00" + b"\x00\x80\x60\xff" * w for _ in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


@pytest.fixture
def ds():
    return LT.parse(copy.deepcopy(DATA))


# ------------------------------------------------------------------ pure: loading fails closed
class TestParse:
    @pytest.mark.parametrize("path,value,msg", [
        (("projects", 0, "status"), "done", "status"), (("projects", 0, "kind"), "office", "kind"),
        (("projects", 0, "market"), "xyz", "market"), (("projects", 0, "yardi"), "MXC1", "building code"),
        (("projects", 0, "group"), "../etc", "customer key"), (("projects", 0, "lat"), 48.8, "outside Mexico"),
        (("customers", "acme", "logo"), "acme.svg", "logo"), (("meta", "schema"), 2, "schema"),
    ])
    def test_a_bad_file_is_refused(self, path, value, msg):
        d = copy.deepcopy(DATA)
        node = d
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
        with pytest.raises(ValueError, match=msg):
            LT.parse(d)

    def test_duplicate_project_refused(self):
        d = copy.deepcopy(DATA)
        d["projects"].append(copy.deepcopy(d["projects"][0]))
        with pytest.raises(ValueError, match="duplicate"):
            LT.parse(d)


# ------------------------------------------------------------------ pure: numbers
class TestNumbers:
    def test_totals_count_delivered_only(self, ds):
        t = LT.totals(ds.projects, TODAY)
        assert t.projects == 6 and t.fixtures == 300 + 100 + 40 + 20 + 60 + 90
        assert t.area_m2 == 6000 + 2000 + 500 + 1500 + 2500
        saved = 120000 + 50000 + 18000 + 35000 + 70000
        assert t.mwh_saved == pytest.approx(saved / 1000)
        assert t.co2_t_year == pytest.approx(saved * co2reg.factor(2026) / 1000)
        assert t.parks == 2 and t.markets == 2 and t.first_year == 2019
        assert t.buildings == 5                                  # MXC09901 twice + 3 codes + 1 without code
        assert t.with_savings == 5                               # the exterior has no study figures

    def test_co2_since_handover_uses_each_years_factor(self, ds):
        p = ds.project(5)                                         # 18,000 kWh/yr from 2019-11-01
        got = p.co2_t_since(dt.date(2021, 1, 1))
        want = (18000 * 61 / 365 * co2reg.factor(2019) + 18000 * co2reg.factor(2020)) / 1000
        assert got == pytest.approx(want, rel=1e-3)
        assert ds.project(3).co2_t_since(TODAY) is None          # planned handover: nothing claimed
        assert ds.project(4).co2_t_since(TODAY) is None          # no savings figure: nothing invented

    def test_live_counter_rate_is_the_yearly_amount_per_second(self, ds):
        t = LT.totals(ds.projects, TODAY)
        assert t.co2_rate_t_s * 365.25 * 86400 == pytest.approx(t.co2_t_year)

    def test_per_year_has_no_gaps(self, ds):
        assert [y for y, _, _ in LT.per_year(ds.projects)] == [2019, 2020, 2021, 2022, 2023, 2024, 2025]
        assert dict((y, n) for y, n, _ in LT.per_year(ds.projects))[2020] == 0


# ------------------------------------------------------------------ pure: buildings not yet on LED
class TestPipeline:
    def test_building_status(self, ds):
        st = {s.building.yardi: s for s in LT.building_status(ds)}
        assert st["MXC09901"].status == "led" and st["MXC09901"].share == pytest.approx(6500 / 7000)
        assert st["MXC09902"].status == "partial" and st["MXC09902"].remaining_m2 == 7000
        assert st["GDL09903"].status == "in_progress"
        assert st["MXC09904"].status == "audited"                  # an ARGIA audit / proforma, no project
        assert st["MXC09907"].status == "not_started"

    def test_pipeline_is_5000m2_plus_and_not_converted(self, ds):
        rows = LT.pipeline(ds)
        codes = [r.building.yardi for r in rows]
        assert "MXC09901" not in codes                             # converted
        assert "MXC09908" not in codes                             # 4,000 m2: below Tomasz's 5,000 m2 line
        assert "MXC09909" not in codes                             # no area: cannot be sized
        assert codes == ["GDL09903", "MXC09904", "MXC09902", "MXC09907"]   # in progress, audited, partial, not started

    def test_potential_is_an_estimate_from_the_median(self, ds):
        it = LT.intensity(ds)
        assert it is not None and it.n == 5
        want = sorted([120000 / 6000, 50000 / 2000, 18000 / 500, 35000 / 1500, 70000 / 2500])[2]
        assert it.kwh_per_m2 == pytest.approx(want)
        pot = LT.potential(LT.pipeline(ds), it, 2026)
        assert pot["m2"] == 12000 + 20000 + 7000 + 15000
        assert pot["mwh"] == pytest.approx(pot["m2"] * want / 1000)

    def test_too_few_projects_means_no_estimate(self, ds):
        ds.projects = ds.projects[:3]
        assert LT.intensity(ds) is None


class TestHelpers:
    def test_csv_has_references_but_no_amounts(self, ds):
        rows = LT.csv_rows(ds.projects, 2026)
        head = rows[0]
        assert "wo_number" in head and "po_number" in head
        assert not [h for h in head if re.search(r"price|cost|amount|usd|mxn|paid", h)]
        assert len(rows) == 1 + len(ds.projects)
        r3 = dict(zip(head, rows[3]))
        assert r3["handover_date"] == ""                           # a planned date is not a handover

    def test_satellite_picture_is_centred_and_esri(self):
        u = LT.satellite_url(19.7, -99.2, half_m=200)
        assert u.startswith("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export?")
        w, s, e, n = map(float, re.search(r"bbox=([^&]+)", u).group(1).split(","))
        assert (w + e) / 2 == pytest.approx(-99.2) and (s + n) / 2 == pytest.approx(19.7)
        assert 350 < (e - w) * 111320 * 0.941 < 450                # about 400 m wide on the ground
        assert "1e3" in LT.google_maps_url(19.7, -99.2)            # Google Maps satellite layer

    def test_slug(self):
        assert LT.slug("Tres Ríos / CDMX") == "tres-rios-cdmx"


# ------------------------------------------------------------------ the pages
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
from tests.unit.test_prologis_app import sign_in     # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    light = tmp_path / "lighting"
    (light / "logos").mkdir(parents=True)
    (light / "lighting.json").write_text(json.dumps(DATA), encoding="utf-8")
    (light / "logos" / "acme.png").write_bytes(_png())
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    monkeypatch.delenv("ARGIA_PL_LIGHTING_DIR", raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    return mod, pws, tmp_path


PAGES = ("/lighting/", "/lighting/buildings/", "/lighting/p/1", "/lighting/customers/", "/lighting/pipeline/")


def _clean(h: str, where: str):
    body = re.sub(r"<script.*?</script>", "", h, flags=re.S)
    assert chr(0x2014) not in h and "None" not in body and "Traceback" not in h and "nan" not in body.split(), where


class TestPages:
    def test_every_page_needs_the_login(self, env):
        mod, _, _ = env
        cl = mod.app.test_client()
        for path in PAGES + ("/lighting/logo/acme.png", "/lighting/projects.csv", "/lighting/pipeline.csv"):
            r = cl.get(path)
            assert r.status_code == 302 and "/login" in r.headers["Location"], path

    def test_viewer_sees_every_page_clean(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        for path in PAGES:
            r = cl.get(path)
            assert r.status_code == 200, path
            _clean(r.get_data(as_text=True), path)

    def test_menu_entry_only_with_data(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert 'href="/lighting/"' in cl.get("/").get_data(as_text=True)
        (tmp / "lighting" / "lighting.json").unlink()
        assert 'href="/lighting/"' not in cl.get("/").get_data(as_text=True)
        for path in PAGES:
            assert cl.get(path).status_code == 404, path

    def test_a_broken_file_keeps_the_last_good_data(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        assert "Acme Logistics" in cl.get("/lighting/customers/").get_data(as_text=True)
        import os
        f = tmp / "lighting" / "lighting.json"
        f.write_text('{"meta": {"schema": 1}, "projects": [{"id": 1, "status": "bogus"}]}', encoding="utf-8")
        os.utime(f, (1, 1))
        assert "Acme Logistics" in cl.get("/lighting/customers/").get_data(as_text=True)

    def test_overview_numbers_counter_and_tables(self, env):
        mod, pws, tmp = env
        h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
        t = LT.totals(LT.parse(copy.deepcopy(DATA)).projects, mod.today_mx())
        assert f">{t.projects}<" in h and f">{t.fixtures:,}<" in h
        assert 'id="co2c"' in h and f'data-v="{t.co2_t_since:.4f}"' in h
        assert "Mexico City" in h and "Guadalajara" in h and "Parque Uno" in h
        assert "/lighting/pipeline/" in h and "server.arcgisonline.com" in h

    def test_filters_restrict_the_table(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        table = lambda url: cl.get(url).get_data(as_text=True).split("<h2>Projects")[1]   # the result table, not the dropdowns
        h = table("/lighting/buildings/?m=gdl")
        assert "Charlie Parts" in h and "Bravo Foods" not in h
        h = table("/lighting/buildings/?c=bravo")
        assert "Bravo Foods" in h and "Echo Paper" not in h
        h = table("/lighting/buildings/?q=2022-901")
        assert "/lighting/p/1" in h and "/lighting/p/6" not in h
        h = table("/lighting/buildings/?q=zzz")
        assert "No project matches" in h

    def test_project_page_has_picture_maps_link_logo_and_no_amounts(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/p/1").get_data(as_text=True)
        assert "World_Imagery/MapServer/export" in h and "google.com/maps/@" in h and "data=!3m1!1e3" in h
        assert '/lighting/logo/acme.png' in h and "2022-901" in h and "2000000001" in h
        assert not re.search(r"\$|\bMXN\b|\bUSD\b", re.sub(r"<script.*?</script>", "", h, flags=re.S))
        assert cl.get("/lighting/p/999").status_code == 404
        h3 = cl.get("/lighting/p/3").get_data(as_text=True)
        assert "planned" in h3                                     # a planned handover says so

    def test_logo_is_served_only_for_known_customers_as_png(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        r = cl.get("/lighting/logo/acme.png")
        assert r.status_code == 200 and r.mimetype == "image/png" and r.data.startswith(b"\x89PNG")
        assert cl.get("/lighting/logo/bravo.png").status_code == 404          # no logo recorded
        assert cl.get("/lighting/logo/..%2F..%2Fpl.db.png").status_code == 404
        (tmp / "lighting" / "logos" / "acme.png").write_bytes(b"<svg onload=alert(1)>")
        assert cl.get("/lighting/logo/acme.png").status_code == 404           # not a PNG: refused

    def test_pipeline_page_lists_only_buildings_to_convert(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        h = cl.get("/lighting/pipeline/").get_data(as_text=True)
        for code in ("GDL09903", "MXC09904", "MXC09902", "MXC09907"):
            assert code in h, code
        for code in ("MXC09901", "MXC09908", "MXC09909"):
            assert code not in h, code
        assert "estimate" in h.lower()
        h = cl.get("/lighting/pipeline/?s=audited").get_data(as_text=True)
        assert "MXC09904" in h and "MXC09907" not in h

    def test_csv_exports_need_the_export_right_and_are_audited(self, env):
        mod, pws, tmp = env
        assert sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/projects.csv").status_code == 403
        cl = sign_in(mod, "manager", pws["manager"], tmp)
        r = cl.get("/lighting/projects.csv?m=cdmx")
        assert r.status_code == 200 and r.mimetype == "text/csv"
        txt = r.data.decode("utf-8-sig")
        assert "Acme Logistics" in txt and "Charlie Parts" not in txt
        r = cl.get("/lighting/pipeline.csv")
        assert r.status_code == 200 and "MXC09907" in r.data.decode("utf-8-sig")
        c = S.connect(str(tmp / "pl.db"))
        acts = [r[0] for r in c.execute("SELECT target FROM audit WHERE action='export'")]
        c.close()
        assert "lighting projects csv" in acts and "lighting pipeline csv" in acts

    def test_security_headers_unchanged(self, env):
        mod, pws, tmp = env
        r = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/")
        csp = r.headers["Content-Security-Policy"]
        assert "img-src 'self' data: https://server.arcgisonline.com https://cdnjs.cloudflare.com;" in csp
        assert "frame-src" not in csp and r.headers["X-Frame-Options"] == "DENY"

    def test_spanish(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        cl.get("/lang/es")
        h = cl.get("/lighting/").get_data(as_text=True)
        assert "Iluminación" in h and "Por ciudad" in h and "desde la entrega" in h


def test_customer_cards_count_in_the_right_number(env):
    mod, pws, tmp = env
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/customers/").get_data(as_text=True)
    assert "<b>1</b> project<" in h and "<b>1</b> projects" not in h
    assert "<b>2</b> projects" in h                                      # Acme has two


def test_no_real_lighting_data_in_the_repo():
    """The data is Prologis business data and this repo is public."""
    hits = [p for p in V2.rglob("lighting.json")] + [p for p in V2.rglob("*.png") if "lighting" in str(p).lower()]
    assert not hits, hits


def test_lighting_grids_shrink_on_phones(env):
    """Found on a 390 px phone (5 Nov): the overview was 119 px wider than the screen - a 1fr grid track cannot
    shrink below its widest table. The lighting grids carry .lg, whose cards may shrink (tables scroll inside)."""
    mod, pws, tmp = env
    import prologis_ui as UI
    h = sign_in(mod, "viewer", pws["viewer"], tmp).get("/lighting/").get_data(as_text=True)
    assert 'class="grid g2 lg"' in h and 'class="grid g2e lg"' in h
    assert ".grid.lg>*{min-width:0}" in UI.CSS
    assert h.count('<div style="overflow-x:auto"><table class="t">') >= 2
