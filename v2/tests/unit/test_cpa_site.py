"""v296 - ARGIA for CPA (cpa.argia.com.mx): the partner clean-energy site.

Pure figures (energy, CO2 with the one factor register, equivalences, the
LED estimator and its browser twin), the whole static site built from
synthetic data (every page, links, both languages, no plant code, no em
dash, the report and its PDFs/CSV, the publish gate), and - against the
throwaway PostgreSQL - the real queries end to end. Synthetic names only:
which plants belong to CPA is server-only configuration."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

from argia.core import co2 as co2reg
from argia.cpa import charts as C
from argia.cpa import report as RP

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
CFG = V2 / "tests" / "fixtures" / "cpa" / "cpa.json"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

NOW = dt.datetime(2026, 10, 2, 13, 5)


def sites3():
    return [RP.Site("MEX1", "Northwind Foods", "Tlalnepantla", 600.0, 19.57, -99.19, False, "northwind-foods"),
            RP.Site("GTO1", "Altamira Labs", "Leon", 800.0, 21.1, -101.6, False, "altamira-labs"),
            RP.Site("MEX3", "Sierra Plastics", "Mexico City", 155.0, 19.43, -99.13, True, "sierra-plastics")]


def daily3():
    out = {}
    for key, start, kwh in (("MEX1", dt.date(2024, 12, 1), 2000.0), ("GTO1", dt.date(2026, 1, 15), 3000.0),
                            ("MEX3", dt.date(2025, 4, 5), 600.0)):
        d, rows = start, []
        while d < NOW.date():
            rows.append((d, kwh))
            d += dt.timedelta(days=1)
        out[key] = rows
    out["MEX3"].append((dt.date(2025, 4, 4), None))          # unknown day: no energy, not the start
    return out


def live3():
    return {"MEX1": RP.Live(310.0, 1100.0, 3.0, [("07:00", 20.0), ("12:00", 400.0)]),
            "GTO1": RP.Live(500.0, 1500.0, 50.0, [("12:00", 520.0)]),
            "MEX3": RP.Live()}


# ------------------------------------------------------------------ pure
class TestFigures:
    def test_monthly_skips_unknown_days(self):
        m = RP.monthly({"A": [(dt.date(2026, 1, 1), 10.0), (dt.date(2026, 1, 2), None), (dt.date(2026, 2, 1), 5.0)]})
        assert m == {"A": {"2026-01": 10.0, "2026-02": 5.0}}

    def test_co2_uses_the_one_register_with_the_contracted_override(self):
        assert RP.co2_t("GTO1", "2023-05", 1000) == pytest.approx(0.438)        # SEMARNAT/CRE 2023
        assert RP.co2_t("GTO1", "2026-05", 1000) == pytest.approx(co2reg.CURRENT)
        assert RP.co2_t("MEX1", "2026-05", 1000) == pytest.approx(co2reg.PLANT_OVERRIDE["MEX1"])

    def test_period_totals_and_life(self):
        s, mon = sites3(), RP.monthly(daily3())
        t = RP.period_totals(s, mon, "2026-01", "2026-09")
        assert t["GTO1"].kwh == pytest.approx(3000.0 * (dt.date(2026, 10, 1) - dt.date(2026, 1, 15)).days)
        assert t["_all"].kwh == pytest.approx(sum(t[x.key].kwh for x in s))
        assert t["_all"].co2_t == pytest.approx(sum(t[x.key].co2_t for x in s))
        lt = RP.live_totals(s, live3(), NOW.date())
        assert lt["_all"].kwh == 2600.0 and lt["MEX3"].kwh == 0

    def test_first_day_ignores_unknown(self):
        assert RP.first_day(daily3(), "MEX3") == dt.date(2025, 4, 5)
        assert RP.first_day({}, "X") is None

    def test_equivalents_epa(self):
        eq = RP.equivalents(46.0)
        assert eq["cars_year"] == pytest.approx(10.0) and eq["trees"] == pytest.approx(766.67, abs=0.01)
        assert eq["gasoline_l"] == pytest.approx(46000 / 2.3477, rel=1e-3)

    def test_specific_yield_needs_twelve_full_months(self):
        s, mon = sites3(), RP.monthly(daily3())
        assert RP.specific_yield(s[0], mon, "2026-09") == pytest.approx(2000.0 * 365 / 600.0)
        assert RP.specific_yield(s[1], mon, "2026-09") is None                   # started Jan 2026

    def test_months_and_previous_month(self):
        assert RP.months_between("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
        assert RP.previous_month(dt.date(2026, 1, 9)) == "2025-12"
        assert RP.previous_month(dt.date(2026, 10, 2)) == "2026-09"

    @pytest.mark.parametrize("live,win,want", [(None, True, "dark"), (RP.Live(age_min=None), True, "dark"),
                                               (RP.Live(age_min=12), True, "live"), (RP.Live(age_min=31), True, "stale"),
                                               (RP.Live(age_min=2), False, "night")])
    def test_status(self, live, win, want):
        assert RP.status(live, win) == want

    def test_slugify(self):
        assert RP.slugify("  Sierra Plastics, S.A. ") == "sierra-plastics-s-a"
        assert RP.slugify("***") == "site"

    def test_csv(self):
        rows = list(csv.reader(io.StringIO(RP.csv_monthly(sites3(), RP.monthly(daily3()), ["2026-08", "2026-09"]))))
        assert rows[0] == ["site", "month", "solar_energy_kwh", "grid_factor_kg_co2e_per_kwh", "avoided_t_co2e", "installed_kwp"]
        r = [x for x in rows if x[0] == "Northwind Foods" and x[1] == "2026-09"][0]
        assert float(r[2]) == 60000.0 and float(r[3]) == 0.202 and float(r[4]) == pytest.approx(12.12)
        assert not any("MEX" in c for row in rows for c in row)                  # names, never plant codes


class TestLed:
    def test_math(self):
        r = RP.led_estimate(200, 458, 150, 16, 360, 2.4, 0.444)
        assert r["kwh_year"] == pytest.approx(200 * 308 * 16 * 360 / 1000)
        assert r["mxn_year"] == pytest.approx(r["kwh_year"] * 2.4) and r["t_co2_year"] == pytest.approx(r["kwh_year"] * 0.444 / 1000)
        assert r["pct"] == pytest.approx(308 / 458 * 100)

    @pytest.mark.parametrize("bad", [(1, 100, 150, 10, 300, 2, .4), (1, 100, 50, 25, 300, 2, .4), (-1, 100, 50, 10, 300, 2, .4),
                                     (1, 100, 50, 10, 400, 2, .4)])
    def test_rejects_nonsense(self, bad):
        with pytest.raises(ValueError):
            RP.led_estimate(*bad)

    @pytest.mark.skipif(not shutil.which("node"), reason="node not installed here")
    def test_browser_twin_agrees(self):
        cases = [(200, 458, 150, 16, 360, 2.4, 0.444), (35, 250, 100, 24, 365, 3.1, 0.438), (1, 60, 9, 8, 250, 1.9, 0.202)]
        js = RP.LED_JS + f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(function(c){{return ledEst.apply(null,c);}})));"
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True, timeout=30).stdout)
        for c, got in zip(cases, out):
            want = RP.led_estimate(*c)
            for k in want:
                assert got[k] == pytest.approx(want[k], rel=1e-12), k


class TestCharts:
    def test_nice_max(self):
        assert [C.nice_max(v) for v in (0, 0.7, 3, 4.2, 9.9, 101)] == [1, 1, 5, 5, 10, 200]

    def test_stacked_bars_tips_and_total(self):
        svg = C.stacked_bars(["2026-08", "2026-09"], [("A", "#111", {"2026-08": 2.0, "2026-09": 1.0}), ("B", "#222", {"2026-09": 3.0})],
                             "MWh", ["Aug 26", "Sep 26"], ["ago 26", "sep 26"])
        tips = re.findall(r'data-tip="([^"]+)"', svg)
        assert tips == ["Aug 26|A: 2 MWh", "Sep 26|A: 1 MWh|B: 3 MWh|Total: 4 MWh"]
        assert 'data-tip-es="sep 26|' in svg and svg.count("<path") == 2                 # one rounded top per column

    def test_gaps_are_bridged_never_invented(self):
        t = ["10:00", "10:15", "10:30", "10:45", "11:00"]
        assert C.fill_gaps(t, {"10:15": 10.0, "10:45": 30.0}) == {"10:15": 10.0, "10:30": 20.0, "10:45": 30.0}
        svg = C.stacked_area(t, [("A", "#000", {"10:00": 5.0, "10:30": 15.0})], "kW")
        assert "10:30|A: 15 kW" in svg and "10:15|A: 10 kW" in svg and "10:45" not in svg     # stops at the newest reading

    def test_a_long_gap_is_not_drawn_as_production(self):
        """v302: dropped frozen readings leave a gap; only up to 30 minutes are bridged."""
        t = ["10:00", "10:15", "10:30", "10:45", "11:00", "11:15"]
        assert C.fill_gaps(t, {"10:00": 10.0, "11:15": 60.0}) == {"10:00": 10.0, "11:15": 60.0}     # 1 h gap: empty
        assert C.fill_gaps(t, {"10:00": 10.0, "10:45": 40.0}) == {"10:00": 10.0, "10:15": 20.0, "10:30": 30.0, "10:45": 40.0}

    def test_empty(self):
        assert "nodata" in C.stacked_bars([], [], "MWh", [], []) and "nodata" in C.area([], "#000", "t", [], [])
        assert "No readings yet today" in C.stacked_area(["06:00"], [("A", "#000", {})], "kW")


# ------------------------------------------------------------------ the whole site (synthetic)
@pytest.fixture
def gen(tmp_path, monkeypatch):
    return make_gen(tmp_path, monkeypatch)


def make_gen(tmp_path, monkeypatch):
    """The generator with a throwaway CPA folder (v314: shared with test_v314_cpa_led)."""
    monkeypatch.setenv("ARGIA_CPA_DIR", str(tmp_path / "cpa"))
    monkeypatch.setenv("ARGIA_PORTAL_ROOT", str(tmp_path / "portal"))
    (tmp_path / "cpa" / "brand").mkdir(parents=True)
    (tmp_path / "cpa" / "brand" / "cpa_logo_white.png").write_bytes(b"\x89PNG fake logo")
    (tmp_path / "portal" / "monitoring" / "assets").mkdir(parents=True)
    (tmp_path / "portal" / "monitoring" / "assets" / "mex1.jpg").write_bytes(b"\xff\xd8 photo")
    import importlib
    import cpa_gen
    g = importlib.reload(cpa_gen)
    calls = []

    def fake_pdf(src, dst, frag=""):
        calls.append((src, frag))
        pathlib.Path(dst).write_bytes(b"%PDF-1.7 " + b"x" * 6000)
        return True
    g.RENDER_PDF = fake_pdf
    g._calls = calls
    return g, tmp_path


def build_site(g, tmp, now=NOW):
    cfg = g.load_config(str(CFG))
    stage = str(tmp / "stage")
    ctx = g.build(cfg, sites3(), daily3(), live3(), now, stage, pdf_cache=str(tmp / "cpa" / "reports"))
    return ctx, stage


def read(stage, rel):
    return pathlib.Path(stage, rel).read_text(encoding="utf-8")


def test_every_page_exists_and_passes_the_publish_gate(gen):
    g, tmp = gen
    ctx, stage = build_site(g, tmp)
    for rel in ("index.html", "sites/index.html", "sites/northwind-foods/index.html", "sites/altamira-labs/index.html",
                "sites/sierra-plastics/index.html", "report/index.html", "led/index.html",
                "report/CPA_Clean_Energy_Report_2026-09_EN.pdf", "report/CPA_Clean_Energy_Report_2026-09_ES.pdf",
                "report/CPA_clean_energy_monthly.csv", "assets/cpa_logo_white.png", "assets/photos/northwind-foods.jpg"):
        assert pathlib.Path(stage, rel).exists(), rel
    assert g.check(stage, ctx) == []
    assert not pathlib.Path(stage, "assets/photos/altamira-labs.jpg").exists()          # no photo -> none copied


def test_both_languages_and_no_plant_code(gen):
    g, tmp = gen
    _, stage = build_site(g, tmp)
    for rel in ("index.html", "report/index.html", "led/index.html", "sites/sierra-plastics/index.html"):
        h = read(stage, rel)
        assert h.count('lang="en"') > 5 and h.count('lang="es"') > 5, rel
        vis = re.sub(r"<script.*?</script>", "", h, flags=re.S)
        assert not re.search(r"\b(MEX1|GTO1|MEX3)\b", vis), rel
        assert chr(0x2014) not in h


def test_overview_totals_equal_the_parts(gen):
    g, tmp = gen
    ctx, stage = build_site(g, tmp)
    life = sum(ctx.closed[s.key].kwh for s in ctx.sites) + 2600.0
    assert ctx.life["_all"].kwh == pytest.approx(life)
    h = read(stage, "index.html")
    assert f'data-count="{life / 1000:.1f}"' in h                          # the hero counter
    assert f'data-count="{ctx.kw_now():.0f}"' in h and ctx.kw_now() == 810.0
    assert "Northwind Foods" in h and "No data today" in h and "Delayed data" in h  # dark and stale sites say so
    assert "assets/photos/northwind-foods.jpg" in h


def test_report_numbers_period_and_downloads(gen):
    g, tmp = gen
    ctx, stage = build_site(g, tmp)
    h = read(stage, "report/index.html")
    assert "Sep 26" in h and "2026" in h
    y = ctx.ytd["_all"]
    assert f"{y.kwh / 1000:,.1f}" in h and f"{y.co2_t:,.1f}" in h
    for href in ("CPA_Clean_Energy_Report_2026-09_EN.pdf", "CPA_Clean_Energy_Report_2026-09_ES.pdf", "CPA_clean_energy_monthly.csv"):
        assert f'href="{href}"' in h
    assert "Northwind Foods" in h and "0.202" in h                           # the contracted factor is named
    assert {f for _, f in g._calls} == {"#en", "#es"}


def test_pdfs_are_printed_once_a_day(gen):
    g, tmp = gen
    build_site(g, tmp)
    assert len(g._calls) == 2
    shutil.rmtree(tmp / "stage")
    build_site(g, tmp)
    assert len(g._calls) == 2                                               # same day: cached copies
    shutil.rmtree(tmp / "stage")
    build_site(g, tmp, NOW + dt.timedelta(days=1))
    assert len(g._calls) == 4


def test_no_pdf_renderer_means_no_pdf_link(gen):
    g, tmp = gen
    g.RENDER_PDF = lambda *a: False
    ctx, stage = build_site(g, tmp)
    h = read(stage, "report/index.html")
    assert ".pdf" not in h and "CPA_clean_energy_monthly.csv" in h and g.check(stage, ctx) == []


def test_the_gate_refuses_codes_dashes_and_broken_links(gen):
    g, tmp = gen
    ctx, stage = build_site(g, tmp)
    p = pathlib.Path(stage, "led", "index.html")
    p.write_text(p.read_text(encoding="utf-8").replace("</main>", f"<p>GTO1 {chr(0x2014)} <a href=\"nope.html\">x</a> <b>None</b></p></main>"),
                 encoding="utf-8")
    errs = " ".join(g.check(stage, ctx))
    assert "plant code GTO1" in errs and "em dash" in errs and "broken link nope.html" in errs and "None/nan" in errs


def test_main_publishes_only_a_clean_site(gen, monkeypatch):
    g, tmp = gen
    out = tmp / "www"
    out.mkdir()
    (out / "index.html").write_text("last good site")
    monkeypatch.setattr(g, "load_config", lambda path=None: json.loads(CFG.read_text(encoding="utf-8")))
    monkeypatch.setattr(g, "fetch", lambda cfg, now: (sites3(), daily3(), live3()))
    monkeypatch.setattr(g, "check", lambda stage, ctx: ["planted problem"])
    assert g.main([str(out)]) == 1
    assert (out / "index.html").read_text() == "last good site" and not (tmp / "www.stage").exists()
    monkeypatch.undo()
    monkeypatch.setenv("ARGIA_CPA_DIR", str(tmp / "cpa"))
    monkeypatch.setattr(g, "load_config", lambda path=None: json.loads(CFG.read_text(encoding="utf-8")))
    monkeypatch.setattr(g, "fetch", lambda cfg, now: (sites3(), daily3(), live3()))
    assert g.main([str(out)]) == 0
    assert "Clean energy" in (out / "index.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", [{"sites": []}, {"sites": [{"key": "MEX1'; DROP TABLE plant; --", "name": "x"}]},
                                 {"sites": [{"key": "MEX1", "name": " "}]}])
def test_config_is_validated_before_any_query(gen, tmp_path, bad):
    g, _ = gen
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        g.load_config(str(p))


def test_generator_has_no_outbound_channel_and_only_reads(gen, monkeypatch):
    g, _ = gen
    import ast
    mods = set()
    for n in ast.walk(ast.parse((BUNDLE / "cpa_gen.py").read_text(encoding="utf-8"))):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
    assert not mods & {"smtplib", "requests", "urllib.request", "http.client", "socket", "googleapiclient"}
    assert "default_transaction_read_only=on" in g.PG_OPTS
    seen = []

    def fake_q(sql):
        seen.append(sql)
        return [["MEX1", "600", "19.5", "-99.1"], ["GTO1", "800", "21.1", "-101.6"], ["MEX3", "155", "19.4", "-99.1"]] \
            if "FROM plant" in sql else []
    monkeypatch.setattr(g, "q", fake_q)
    sites, daily, live = g.fetch(g.load_config(str(CFG)), NOW)
    assert len(sites) == 3 and len(seen) == 5
    assert all(sql.lstrip().upper().startswith(("SELECT", "WITH REP AS (SELECT")) for sql in seen)
    assert not any(w in sql.upper() for sql in seen for w in ("INSERT ", "UPDATE ", "DELETE ", "DROP "))
    assert all("NOT EXISTS (SELECT 1 FROM rep" in sql for sql in seen[2:])      # v302: repeats are not live data
    assert "prod_date < '2026-10-02'" in seen[1]                                # closed days: before MX today
    assert "= '2026-10-02'" in seen[2] and "= '2026-10-02'" in seen[4]          # today's counters and curve
    assert "interval '30 minutes'" in seen[3]                                   # power now


def test_map_has_photo_pins_hover_cards_and_a_legend(gen):
    """v299 (Tomasz): pictures on the map like the portal, a map legend, blue colours."""
    g, tmp = gen
    ctx, stage = build_site(g, tmp)
    h = read(stage, "index.html")
    pts = json.loads(re.search(r"var P=(\[.*?\]);var m=L\.map", h, flags=re.S).group(1))
    assert [p["name"] for p in pts] == ["Northwind Foods", "Altamira Labs", "Sierra Plastics"]
    assert pts[0]["photo"] == "assets/photos/northwind-foods.jpg" and pts[1]["photo"] == ""     # no photo -> coloured pin
    assert {p["st"] for p in pts} == {"live", "stale", "dark"} and pts[2]["approx"] is True
    assert pts[0]["mwh"] == round(ctx.life["MEX1"].kwh / 1000, 1) and pts[0]["co2"] == round(ctx.life["MEX1"].co2_t, 1)
    leg = h[h.index('class="mlegend'):h.index('class="mkey')]          # v314: + solar-only (the map switch)
    assert leg.count('<a href="sites/') == 3 and "approximate location" in leg
    assert "assets/photos/northwind-foods.jpg" in leg
    key = h[h.index('class="mkey'):]
    for word in ("Live", "Delayed data", "No data today", "Night", "installed kWp"):
        assert word in key
    assert "bindTooltip" in h and "declutter" in h
    assert not re.search(r"\b(MEX1|GTO1|MEX3)\b", json.dumps(pts))


def test_site_colours_are_one_blue_ramp():
    import colorsys
    import cpa_gen as g
    for c in g.SITE_COLOURS[:3]:
        r, gg, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        hue = colorsys.rgb_to_hls(r, gg, b)[0] * 360
        assert 200 <= hue <= 225, (c, hue)                              # blue, no orange/teal
    light = [colorsys.rgb_to_hls(*(int(c[i:i + 2], 16) / 255 for i in (1, 3, 5)))[1] for c in g.SITE_COLOURS[:3]]
    assert light == sorted(light) and light[1] - light[0] > .15 and light[2] - light[1] > .15   # clearly different shades


def test_map_scrolls_under_the_sticky_header():
    """v301: the same stacking fix as the Prologis site (Leaflet panes go up to z-index 1000)."""
    import cpa_gen as g
    css = g.CSS.replace("\n", "")
    m = re.search(r"#map\{([^}]*)\}", css).group(1)
    assert "isolation:isolate" in m and "position:relative" in m
    top = re.search(r"\.top\{([^}]*)\}", css).group(1)
    assert "position:sticky" in top and int(re.search(r"z-index:(\d+)", top).group(1)) > 1000
