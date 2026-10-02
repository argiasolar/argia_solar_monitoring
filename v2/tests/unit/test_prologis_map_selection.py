"""v295 - ARGIA for Prologis: plant legend on both maps, and on the map
page tick boxes whose selection the tiles total.

The totals exist twice - selection_kpis() in Python (first paint) and
SELECTION_JS in the browser (after each tick) - so the tests pin both to
the same answer, and the whole-portfolio selection to portfolio_kpis()."""
from __future__ import annotations

import datetime as dt
import importlib
import json
import pathlib
import random
import re
import shutil
import subprocess
import sys

import pytest

from argia.prologis import metering as M
from argia.prologis import registry as R

V2 = pathlib.Path(__file__).resolve().parents[2]
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
TIMES = [dt.datetime(2026, 10, 2, 13, 0), dt.datetime(2026, 6, 21, 9, 30), dt.datetime(2026, 12, 21, 22, 0)]


@pytest.fixture(scope="module")
def rg():
    return R.load(str(FIX))


@pytest.mark.parametrize("now", TIMES)
def test_all_sites_selected_equals_the_portfolio_tiles(rg, now):
    assert M.selection_kpis(M.site_stats(rg.sites, now)) == M.portfolio_kpis(rg.sites, now)


def test_subsets_add_up_and_empty_is_zero(rg):
    rows = M.site_stats(rg.sites, TIMES[0])
    a, b = rows[:4], rows[4:]
    ka, kb, kall = M.selection_kpis(a), M.selection_kpis(b), M.selection_kpis(rows)
    assert abs(ka["kwh_today"] + kb["kwh_today"] - kall["kwh_today"]) < 0.2
    assert abs(ka["mwh_30d"] + kb["mwh_30d"] - kall["mwh_30d"]) < 0.02
    assert min(ka["pr_30d"], kb["pr_30d"]) - 0.001 <= kall["pr_30d"] <= max(ka["pr_30d"], kb["pr_30d"]) + 0.001
    z = M.selection_kpis([])
    assert z["kw_now"] == 0 and z["pr_30d"] == 0 and z["availability_30d"] == 0 and z["sites"] == 0
    pre = [r for r in rows if not r["op"]]
    assert pre and M.selection_kpis(pre)["mwh_30d"] == 0 and M.selection_kpis(pre)["kwp"] > 0   # sites before PTO add size only


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed here")
def test_browser_totals_match_python(rg):
    rows = M.site_stats(rg.sites, TIMES[0])
    rnd = random.Random(7)
    subsets = [rows, rows[:1], rows[-2:], []] + [[r for r in rows if rnd.random() < 0.5] for _ in range(6)]
    js = M.SELECTION_JS + "\nvar S=" + json.dumps(subsets) + ";console.log(JSON.stringify(S.map(plSel)));"
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True, timeout=30).stdout)
    nd = {"kwp": 1, "kwp_operating": 1, "kw_now": 1, "kwh_today": 1, "mwh_30d": 2, "pr_30d": 3, "availability_30d": 4, "co2_t_30d": 1}
    for sub, got in zip(subsets, out):
        want = M.selection_kpis(sub)
        for k, v in want.items():
            g = round(got[k], nd[k]) if k in nd else got[k]
            assert abs(g - v) < 1e-9, (k, g, v)


# ------------------------------------------------------------------ web pages
sys.path.insert(0, str(V2 / "server" / "bundle"))
sys.path.insert(0, str(V2))
from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402


@pytest.fixture
def cl(tmp_path, monkeypatch):
    pytest.importorskip("flask", reason="flask not installed here")
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pw = S.create_user(c, "viewer", "Viewer", "v@x.test", "Prologis", "viewer", "test")
    S.create_ticket(c, "TST001", "fault", "", "OTHER", "op")
    c.close()
    client = app_mod.app.test_client()
    client.post("/login", data={"username": "viewer", "password": pw, "next": "/"})
    page = client.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    client.post("/mfa", data={"code": TOTP.code_at(secret, int(__import__("time").time() // 30)), "pending": secret, "csrf": csrf})
    page = client.get("/password").get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
    client.post("/password", data={"pw1": "Sunny-rooftops-2026", "pw2": "Sunny-rooftops-2026", "csrf": csrf})
    return client


def test_map_page_has_selectable_legend_and_tiles(cl, rg):
    h = cl.get("/map/").get_data(as_text=True)
    boxes = re.findall(r'class="ptog" data-k="([^"]+)" checked', h)
    assert sorted(boxes) == sorted([s.code for s in rg.sites] + ["EPC-TST-1"])        # every site + the mapped EPC project
    assert sorted(re.findall(r'class="gtog" data-g="(\w+)"', h)) == ["epc", "op", "pre"]
    for tid in ("st_n", "st_kw", "st_kwh", "st_e30", "st_av", "st_pr", "st_tk"):
        assert f'id="{tid}"' in h, tid
    assert re.search(r'id="st_tk">1<', h)                                             # the one open incident
    assert re.search(r'id="st_n">9<', h)
    # regression (found in v295 build): the boxes must exist before the script that wires them
    assert h.index('class="ptog"') < h.index("querySelectorAll('.ptog')")
    assert h.index("window.plTiles=") < h.index("window.plTiles(HID)")
    assert "function plSel(R)" in h and "pl_map_hide_v1" in h
    assert chr(0x2014) not in h


def test_map_first_paint_equals_portfolio(cl, rg):
    h = cl.get("/map/").get_data(as_text=True)
    rows = json.loads(re.search(r"var R=(\[.*?\]),LB=", h).group(1))
    assert {r["code"] for r in rows} == {s.code for s in rg.sites} and all("tk" in r for r in rows)
    k = M.selection_kpis(rows)
    assert re.search(r'id="st_kw">' + re.escape(f'{k["kw_now"] / 1000:,.2f}') + "<", h)
    assert re.search(r'id="st_pr">' + re.escape(f'{k["pr_30d"] * 100:.1f}') + "<", h)


def test_overview_legend_names_and_sizes_without_boxes(cl, rg):
    h = cl.get("/").get_data(as_text=True)
    leg = h[h.index('class="plegend'):]
    for s in rg.sites:
        assert s.name in leg and f"{s.kwp:,.0f} kWp" in leg, s.code
    assert 'class="ptog"' not in h and 'class="gtog"' not in h                        # overview: legend only
    assert "EPC-TST-1" not in leg                                                     # projects stay off the mini map
    assert "localStorage" not in h.split('class="plegend')[1].split("</script>")[0]


def test_spanish_legend(cl):
    cl.get("/lang/es")
    h = cl.get("/map/").get_data(as_text=True)
    assert "Sitios en operación" in h and "Sitios seleccionados" in h and "se recuerda en este navegador" in h
