"""v317 end to end: the string layout card on the real portal generator.

A seeded private PostgreSQL (conftest), a layout directory written here
(synthetic: the real layouts are customer drawings, server-only), and
portal_gen run for real. Checked: the card sits under the intraday chart
of a plant with a layout and only there, the drawing is written beside
the page under a content-hashed name and the page points at it through
the portal slug, an archived day falls back to the nightly string_daily
amp-hours, a layout naming an inverter the plant does not have is not
used (and the generator says why), the demo never shows a drawing.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import glob
import io
import json
import os
import pathlib
import runpy
import sys

import pytest

from tests.portal.conftest import V2, _mx_today

PIL = pytest.importorskip("PIL.Image")


def _layout(plant, sns):
    strings = []
    for sn in sns:
        for n in range(1, 5):
            strings.append({"sn": sn, "ch": f"s{n}", "inv": f"INV{sns.index(sn) + 1}", "mppt": (n + 1) // 2,
                            "name": f"T_{sn}_{n}", "kwp": 10.6, "modules": 18, "module": "LR5-72HTH-590M",
                            "tilt": 5.0, "az": 148.7, "orient": "SE", "group": "LR5-72HTH-590M|5|148.7",
                            "pos": [{"d": "main", "x": 20.0 * n, "y": 30.0 + 40 * sns.index(sn), "cad": n}],
                            "conf": "order"})
    return {"plant": plant, "built": "2026-10-07", "version": 1, "source": "test",
            "drawings": [{"id": "main", "image": f"{plant}_main.jpg", "src": "test drawing rev. V1",
                          "title_en": "Roof", "title_es": "Techo", "box": [0, 0, 120, 120], "px": [60, 60]}],
            "strings": strings, "areas": [{"sn": sns[0], "inv": "INV1", "d": "main", "rects": [[5, 5, 10, 10]]}],
            "notes": []}


@pytest.fixture(scope="module")
def site(pg_env, psql, tmp_path_factory):
    from tests.portal.conftest import reset_db
    reset_db(pg_env)
    today = _mx_today()
    yday = today - dt.timedelta(days=1)
    # today 10:00-11:55 MX: INV01 string 3 dead, INV02 string 2 weak
    psql(f"""INSERT INTO string_sample (ts_utc, plant_key, inverter_sn, str_a, mppt_a, mppt_v)
        SELECT (DATE '{today}' + time '10:00' + m * interval '5 minutes') AT TIME ZONE 'America/Mexico_City',
               'GTO1', sn, CASE WHEN sn = 'GTO1INV01' THEN ARRAY[10.1, 10.0, 0.0, 8.5]::real[]
                                ELSE ARRAY[10.0, 7.0, 10.2, 9.8]::real[] END, ARRAY[20.1, 9.9]::real[], NULL
        FROM generate_series(0, 23) m, (VALUES ('GTO1INV01'), ('GTO1INV02')) v(sn);""")
    psql(f"""INSERT INTO string_daily (plant_key, inverter_sn, prod_date, channel, kind, energy_kwh, q_ah, samples)
        SELECT 'GTO1', 'GTO1INV01', DATE '{yday}', 's' || n, 'string', 50, CASE WHEN n = 4 THEN 30 ELSE 70 END, 150
        FROM generate_series(1, 4) n;""")
    # v318: ten earlier days - INV01 string 4 always reads 85 % of its neighbours (the Growatt pair bias)
    psql(f"""INSERT INTO string_daily (plant_key, inverter_sn, prod_date, channel, kind, energy_kwh, q_ah, samples)
        SELECT 'GTO1', sn, DATE '{yday}' - k, 's' || n, 'string', 50,
               CASE WHEN sn = 'GTO1INV01' AND n = 4 THEN 59.5 ELSE 70 END, 150
        FROM generate_series(1, 10) k, generate_series(1, 4) n, (VALUES ('GTO1INV01'), ('GTO1INV02')) v(sn);""")
    lay = tmp_path_factory.mktemp("layouts")
    (lay / "GTO1.json").write_text(json.dumps(_layout("GTO1", ["GTO1INV01", "GTO1INV02"])), encoding="utf-8")
    (lay / "MEX1.json").write_text(json.dumps(_layout("MEX1", ["MEX1INV01", "NOT-A-SERIAL"])), encoding="utf-8")
    for pk in ("GTO1", "MEX1"):
        PIL.new("RGB", (60, 60), (200, 210, 220)).save(lay / f"{pk}_main.jpg", "JPEG")
    out = tmp_path_factory.mktemp("www_sl")
    saved_env, saved_argv, saved_path = dict(os.environ), list(sys.argv), list(sys.path)
    buf, err = io.StringIO(), io.StringIO()
    try:
        os.environ.update({k: pg_env[k] for k in ("PGHOST", "PGPORT", "PGUSER", "PGTZ", "PATH")})
        os.environ["ARGIA_LAYOUT_DIR"] = str(lay)
        sys.argv = [str(V2 / "server/bundle/portal_gen.py"), str(out)]
        for m in ("portal_gen", "report_gen", "monitoring_gen"):
            sys.modules.pop(m, None)
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            runpy.run_path(str(V2 / "server/bundle/portal_gen.py"), run_name="__main__")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.argv, sys.path[:] = saved_argv, saved_path
        for m in ("portal_gen", "report_gen", "monitoring_gen"):
            sys.modules.pop(m, None)
    return out, err.getvalue(), today, yday


def _plant_dir(out, card_id):
    hits = [p for p in glob.glob(str(out / "monitoring" / "*" / "index.html"))
            if card_id in pathlib.Path(p).read_text(encoding="utf-8")]
    assert len(hits) == 1, hits
    return pathlib.Path(hits[0]).parent


def _data(page):
    raw = page.split('<script type="application/json" class="sldata">')[1].split("</script>")[0]
    return json.loads(raw.replace("<\\/", "</"))


def test_the_card_sits_under_the_intraday_chart_with_live_strings(site):
    out, _err, _today, _y = site
    d = _plant_dir(out, 'id="slGTO1"')
    page = (d / "index.html").read_text(encoding="utf-8")
    assert page.index("Intraday production") < page.index('id="slGTO1"') < page.index("latest sample")
    p = _data(page)
    assert p["src"] == "5min" and p["live"] and len(p["t"]) == 24 and p["t"][0] == "10:00"
    by = {(s["sn"], s["ch"]): k for k, s in enumerate(p["s"])}
    assert set(p["c"][by[("GTO1INV01", "s3")]]) == {"z"}            # dead string
    assert set(p["c"][by[("GTO1INV02", "s2")]]) == {"r"}            # 70 % of its peers
    assert set(p["c"][by[("GTO1INV01", "s1")]]) == {"g"}
    k4 = by[("GTO1INV01", "s4")]                                     # v318: 86 % of its neighbours = its normal
    assert set(p["c"][k4]) == {"g"} and abs(p["s"][k4]["nm"] - 0.85) < 0.01
    assert p["chk"]["inverters"]["monitoring"] == 2 and [v["sn"] for v in p["inv"]] == ["GTO1INV01", "GTO1INV02"]
    assert p["s"][0]["il"] == "INV-01"                               # the inverter's portal label


def test_the_drawing_is_served_beside_the_page_under_its_slug(site):
    out, _err, _t, _y = site
    d = _plant_dir(out, 'id="slGTO1"')
    p = _data((d / "index.html").read_text(encoding="utf-8"))
    img = p["d"][0]["img"]
    assert img.startswith(f"/monitoring/{d.name}/layout-main-") and img.endswith(".jpg")
    assert (d / img.rsplit("/", 1)[1]).read_bytes()[:2] == b"\xff\xd8"


def test_an_archived_day_uses_the_nightly_amp_hours(site):
    out, _err, _t, yday = site
    d = _plant_dir(out, 'id="slGTO1"')
    page = (d / "d" / f"{yday}.html").read_text(encoding="utf-8")
    p = _data(page)
    assert p["src"] == "daily" and p["t"] == [] and not p["live"]
    cls = {s["ch"]: p["dy"][k]["c"] for k, s in enumerate(p["s"]) if s["sn"] == "GTO1INV01"}
    assert cls == {"s1": "g", "s2": "g", "s3": "g", "s4": "r"}       # 30 Ah vs 70 Ah (normal 85 %: still very low)
    assert 'type="range"' not in page.split('id="slGTO1"')[1].split("</div>")[0]


def test_a_layout_with_an_unknown_inverter_is_not_used(site):
    out, err, _t, _y = site
    assert "string layout of MEX1 not used" in err and "NOT-A-SERIAL" in err
    pages = glob.glob(str(out / "monitoring" / "*" / "index.html"))
    assert not any('id="slMEX1"' in pathlib.Path(p).read_text(encoding="utf-8") for p in pages)
    assert sum('class="card slcard"' in pathlib.Path(p).read_text(encoding="utf-8") for p in pages) == 1


def test_the_demo_never_shows_a_customer_drawing():
    src = (V2 / "server/bundle/demo_gen.py").read_text(encoding="utf-8")
    assert "MG.LAYOUTS.clear()" in src
