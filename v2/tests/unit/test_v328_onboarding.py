"""v328 - Teneria Panamericana (GTO3) and Holiday Inn Quijote (SLP3) join
the CAPEX fleet; one plant may now be several Growatt plants.

Teneria Panamericana is ONE 960 kWp project (one address, one internet
line, both stations registered on 2026-10-16) whose two inverter stations
each have their own ShineMaster, so Growatt shows it as two plants
(11234539 "-319" + 11234544 "-426", 3 x MAX 125KTL3-X2 MV each).
plant.site_id holds both ids; every plant-level Growatt call is made per
id and summed, and a missing part makes the total unknown, never a
partial sum (a partial sum would read as lost energy).

Specs are customer data and stay on the server; every spec here is
synthetic.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from argia.core.config import InverterConfig, PlantConfig
from argia.core.normalize import growatt_site_ids
from argia.recon import counters as C

V2 = Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(V2 / "scripts"))
import onboard_plant as OB  # noqa: E402
import vendor_history_import as VHI  # noqa: E402

NEW = ("GTO3", "SLP3")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plant(site_id="111111,222222", key="TST1", wpid="999999"):
    fields = {f: None for f in PlantConfig.__dataclass_fields__}
    fields.update(plant_key=key, customer="X", brand="GROWATT", site_id=site_id,
                  kwp_dc=100.0, kwp_ac=80.0, lat=21.0, lon=-101.0,
                  expected_factor=0.75, pr_target=0.85, installation_date="2026-10-01",
                  secret_api_name="", secret_user_name="", secret_pass_name="",
                  weather_plant_id=wpid, datalogger_sn="DL", datalogger_addr=1, active=True)
    return PlantConfig(**{k: v for k, v in fields.items()
                          if k in PlantConfig.__dataclass_fields__})


# ------------------------------------------------------------ site ids
class TestGrowattSiteIds:
    @pytest.mark.parametrize("raw,want", [
        ("10069072", ["10069072"]),
        ("11234539,11234544", ["11234539", "11234544"]),
        (" 11234539 , 11234544 ", ["11234539", "11234544"]),
        ("11234539+11234544", ["11234539", "11234544"]),
        ("11234539,11234539", ["11234539"]),
        ("", []),
        (None, []),
    ])
    def test_split(self, raw, want):
        assert growatt_site_ids(raw) == want


class TestSumCounters:
    def test_parts_add_up(self):
        assert C.sum_counters([(265.5, None, 12992.0), (280.9, None, 17533.9)]) == (546.4, None, 30525.9)

    def test_one_unknown_part_makes_the_field_unknown(self):
        assert C.sum_counters([(265.5, None, 12992.0), (None, None, 17533.9)]) == (None, None, 30525.9)

    def test_single_part_is_unchanged(self):
        assert C.sum_counters([(1.0, 2.0, 3.0)]) == (1.0, 2.0, 3.0)

    def test_no_parts(self):
        assert C.sum_counters([]) == (None, None, None)


# ---------------------------------------------------- Growatt client
class TestGrowattClientMultiSite:
    def _client(self, **kw):
        from argia.vendors.growatt import GrowattClient
        return GrowattClient(web_username="u", web_password="p", **kw)

    def test_day_energy_is_the_sum_of_the_parts(self, monkeypatch):
        c = self._client()
        asked = []
        vals = {"111111": 265.5, "222222": 280.9}
        monkeypatch.setattr(c, "_fetch_day_kwh_web",
                            lambda p, d: asked.append(p.site_id) or vals[p.site_id])
        assert c.fetch_day_kwh(_plant(), "2026-10-27") == 546.4
        assert asked == ["111111", "222222"]

    def test_one_part_unknown_makes_the_day_unknown(self, monkeypatch):
        c = self._client()
        vals = {"111111": 265.5, "222222": None}
        monkeypatch.setattr(c, "_fetch_day_kwh_web", lambda p, d: vals[p.site_id])
        assert c.fetch_day_kwh(_plant(), "2026-10-27") is None

    def test_single_site_plant_is_asked_exactly_as_before(self, monkeypatch):
        c = self._client()
        seen = []
        monkeypatch.setattr(c, "_fetch_day_kwh_web", lambda p, d: seen.append(p) or 10.0)
        p = _plant(site_id="10069072")
        assert c.fetch_day_kwh(p, "2026-10-27") == 10.0
        assert seen == [p]                       # the same object, not a copy

    def test_inverters_are_collected_from_every_part(self, monkeypatch):
        from argia.vendors.growatt import GrowattClient
        c = GrowattClient(api_token="t", web_username="u", web_password="p")
        by_site = {"111111": ["A", "B"], "222222": ["C"]}
        monkeypatch.setattr(c, "_fetch_inverters_open_api",
                            lambda p, invs: [SimpleNamespace(inverter_sn=s) for s in by_site[p.site_id]])
        monkeypatch.setattr(c, "_fetch_inverters_web", lambda p, invs: pytest.fail("web not needed"))
        invs = [InverterConfig("TST1", s, s, 10.0, True) for s in "ABC"]
        got = c.fetch_inverter_snapshots(_plant(), invs)
        assert sorted(s.inverter_sn for s in got) == ["A", "B", "C"]

    def test_inverters_web_fallback_is_not_repeated(self, monkeypatch):
        c = self._client()                       # no token: web per SN
        calls = []
        monkeypatch.setattr(c, "_fetch_inverters_web",
                            lambda p, invs: calls.append(p.site_id)
                            or [SimpleNamespace(inverter_sn=i.inverter_sn) for i in invs])
        invs = [InverterConfig("TST1", s, s, 10.0, True) for s in "ABC"]
        got = c.fetch_inverter_snapshots(_plant(), invs)
        assert len(got) == 3 and calls == ["111111"]   # all SNs came with the first part


# ----------------------------------------------------- nightly counters
class TestReconSnapshotMultiSite:
    def test_counters_are_summed_per_plant(self, monkeypatch):
        import recon_snapshot as RS
        import argia.vendors.growatt_web as GW
        totals = {"111111": ("265.5", "12992"), "222222": ("280.9", "17533.9")}

        class Fake:
            def __init__(self, **kw):
                pass

            def login(self):
                pass

            def get_max_total_data(self, pid):
                d, t = totals[pid]
                return {"_meta": {}, "response": {"result": 1, "obj": {"eToday": d, "eTotal": t}}}

        monkeypatch.setattr(GW, "GrowattWebClient", Fake)
        monkeypatch.setenv("GROWATT_USERNAME", "u")
        monkeypatch.setenv("GROWATT_PASSWORD", "p")
        out = RS.snap_growatt([_plant(), _plant(site_id="111111", key="TST2")], "2026-10-27")
        by = {s.plant_key: s for s in out}
        assert by["TST1"].daily_kwh == pytest.approx(546.4)
        assert by["TST1"].lifetime_kwh == pytest.approx(30525.9)
        assert by["TST2"].daily_kwh == pytest.approx(265.5)


# --------------------------------------------- vendor history (charts)
class TestSumCaptures:
    def test_element_wise_sum(self):
        a = {"2026-10": [0.0, 192.8, 276.6], "year_2026": [0.0] * 9 + [12992.0, 0, 0], "captured_utc": "x1"}
        b = {"2026-10": [94.8, 283.5, 283.5], "year_2026": [0.0] * 9 + [17533.9, 0, 0], "captured_utc": "x2"}
        s = VHI.sum_captures([a, b])
        assert s["2026-10"] == [94.8, 476.3, 560.1]
        assert s["year_2026"][9] == pytest.approx(30525.9)
        assert s["captured_utc"] == "x2"

    def test_an_unknown_day_stays_unknown(self):
        a = {"2026-10": [1.0, None], "captured_utc": "x"}
        b = {"2026-10": [2.0, 3.0], "captured_utc": "x"}
        assert VHI.sum_captures([a, b])["2026-10"] == [3.0, None]

    def test_one_capture_is_returned_as_is(self):
        a = {"2026-10": [1.0], "captured_utc": "x"}
        assert VHI.sum_captures([a]) is a


# ------------------------------------------- telemetry degraded mode
class TestDegradedModeAsksThePlantsOwnIds:
    """Regression: the token fallback asked plant.weather_plant_id - the
    plant hosting the borrowed sensor (NL1 for Budenheim/Ryder, Taigene
    for Teneria) - so ANOTHER plant's energy would have been cached as
    this plant's day."""

    def test_uses_site_ids_not_the_weather_plant(self, monkeypatch, tmp_path):
        import telemetry_5m as T
        asked = []
        token = SimpleNamespace(plant_today_energy=lambda pid: asked.append(pid) or 100.0)
        cached = {}
        monkeypatch.setattr(T.growatt_token, "cache_energy",
                            lambda d, pk, kwh: cached.update({(d, pk): kwh}))

        class Web:
            def get_max_history(self, sn, date_iso):
                raise RuntimeError("session blocked")

        inv = [InverterConfig("TST1", "A", "Inverter 1", 10.0, True)]
        T._process_growatt_plant(_plant(wpid="999999"), inv, "2026-10-27", None, Web(),
                                 None, True, __import__("logging").getLogger("t"), token_client=token)
        assert asked == ["111111", "222222"]
        assert "999999" not in asked
        assert cached == {("2026-10-27", "TST1"): 200.0}

    def test_unknown_part_caches_nothing(self):
        import telemetry_5m as T
        vals = {"111111": 100.0, "222222": None}
        token = SimpleNamespace(plant_today_energy=lambda pid: vals[pid])
        assert T.degraded_today_energy(_plant(), token) is None


# ------------------------------------------------------ onboarding spec
def _spec(**over):
    plant = {"plant_key": "TST9", "customer": "O'BRIEN TEST (Leon, GTO)", "brand": "GROWATT",
             "site_id": "111111,222222", "kwp_dc": 960.0, "kwp_ac": 750, "lat": 21.16741,
             "lon": -101.67529, "portfolio": "CAPEX", "installation_date": "2026-10-07",
             "module_count": "1500", "module_wp": "640", "show_dashboard": False,
             "expected_factor": "0.75", "active": True}
    invs = [{"inverter_sn": f"SN{i}", "inverter_label": f"Inverter {i}", "rated_kw": 125,
             "rated_kw_dc": 160.0, "date_producing": "2026-10-07"} for i in range(1, 7)]
    spec = {"plant": plant, "inverters": invs,
            "design": {"annual_kwh": 1650000, "shape_from": "GTO1", "years": [2026, 2027], "from_month": 10}}
    for k, v in over.items():
        spec[k] = v
    return spec


SHAPE = {m: v for m, v in enumerate([60, 70, 90, 100, 110, 105, 95, 95, 85, 80, 65, 55], 1)}


class TestValidate:
    def test_good_spec_has_no_problem(self):
        assert OB.validate(_spec()) == []

    @pytest.mark.parametrize("mutate,needle", [
        (lambda s: s["plant"].update(colour="red"), "unknown column"),
        (lambda s: s["plant"].pop("site_id"), "site_id is required"),
        (lambda s: s["plant"].update(plant_key="teneria"), "not like GTO3"),
        (lambda s: s["plant"].update(brand="FRONIUS"), "brand"),
        (lambda s: s["plant"].update(portfolio="LEASE"), "portfolio"),
        (lambda s: s["plant"].update(site_id="11234539,NE=1"), "Growatt site_id"),
        (lambda s: s["plant"].update(lat=48.2, lon=16.4), "not in Mexico"),
        (lambda s: s["plant"].update(kwp_dc=-1), "kwp_dc must be a positive"),
        (lambda s: s["plant"].update(module_wp="600"), "module_count x module_wp"),
        (lambda s: s["plant"].update(installation_date="16/10/2026"), "not YYYY-MM-DD"),
        (lambda s: s.update(inverters=[]), "at least one"),
        (lambda s: s["inverters"][1].update(inverter_sn="SN1"), "duplicate SN"),
        (lambda s: s["inverters"][0].update(rated_kw=70), "rated_kw add up"),
        (lambda s: s["inverters"][0].update(rated_kw_dc=10), "rated_kw_dc add up"),
        (lambda s: s["inverters"][0].pop("inverter_label"), "inverter_label is required"),
        (lambda s: s["inverters"][0].update(plant_key="GTO1"), "!= 'TST9'"),
        (lambda s: s["design"].update(years=[2027, 2026]), "years"),
        (lambda s: s["design"].update(from_month=13), "from_month"),
        (lambda s: s["design"].update(annual_kwh=0), "annual_kwh"),
    ])
    def test_each_problem_is_named(self, mutate, needle):
        s = _spec()
        mutate(s)
        errs = OB.validate(s)
        assert any(needle in e for e in errs), errs


class TestDesign:
    def test_the_year_adds_up_to_the_annual_figure(self):
        d = OB.design_months(1650000, SHAPE)
        assert sum(d.values()) == pytest.approx(1650000, abs=1.0)
        assert d[5] > d[12]                       # the shape is kept

    def test_from_month_drops_the_months_before(self):
        assert sorted(OB.design_months(1650000, SHAPE, 10)) == [10, 11, 12]

    def test_years_after_the_first_are_whole(self):
        rows = OB.design_rows(1650000, SHAPE, [2026, 2027], 10)
        assert sorted(m for y, m in rows if y == 2026) == [10, 11, 12]
        assert sum(v for (y, m), v in rows.items() if y == 2027) == pytest.approx(1650000, abs=1.0)

    def test_bad_shape_is_refused(self):
        with pytest.raises(ValueError):
            OB.design_months(1, {1: 1.0})

    def test_pick_shape_takes_the_latest_full_year(self):
        rows = [["2025", str(m), "10"] for m in range(1, 13)] + \
               [["2026", str(m), str(m)] for m in range(1, 13)] + \
               [["2027", "1", "5"]]
        assert OB.pick_shape(rows) == {m: float(m) for m in range(1, 13)}
        assert OB.pick_shape([["2026", "1", "5"]]) == {}


class TestBuildSql:
    def test_one_transaction_with_a_timeout_and_no_overwrites(self):
        sql = OB.build_sql(_spec(), OB.design_rows(1650000, SHAPE, [2026], 10))
        lines = sql.strip().splitlines()
        assert lines[0] == "BEGIN;" and lines[-1] == "COMMIT;"
        assert "statement_timeout" in lines[1]
        inserts = [x for x in lines if x.startswith("INSERT")]
        assert len(inserts) == 1 + 6 + 3
        assert all(x.endswith("DO NOTHING;") for x in inserts)
        assert "DO UPDATE" not in sql and "DELETE" not in sql

    def test_literals_are_quoted_and_typed(self):
        sql = OB.build_sql(_spec())
        assert "'O''BRIEN TEST (Leon, GTO)'" in sql
        assert "DATE '2026-10-07'" in sql
        assert "FALSE" in sql and "'111111,222222'" in sql
        assert "'GROWATT'" in sql

    def test_a_bad_flag_is_refused(self):
        s = _spec()
        s["plant"]["show_dashboard"] = "maybe"
        with pytest.raises(ValueError):
            OB.build_sql(s)


class TestDifferences:
    def test_reports_only_what_differs(self):
        spec = {"kwp_dc": 960.0, "active": True, "customer": "A"}
        stored = {"kwp_dc": "960.000", "active": "t", "customer": "B"}
        assert OB.differences(spec, stored) == ["customer: stored 'B', spec 'A'"]


# ------------------------------------------ the surfaces a plant needs
class TestNewPlantsAreEverywhereAPlantMustBe:
    def test_report_gen_counts_them_as_capex_not_ppa(self):
        src = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
        capex = re.search(r"^CAPEX = \[(.*?)\]", src, re.M).group(1)
        ppa = re.search(r"^PPA = \[(.*?)\]", src, re.M).group(1)
        for pk in NEW:
            assert f"'{pk}'" in capex and pk not in ppa

    def test_auth_areas_and_paths(self):
        sys.path.insert(0, str(BUNDLE))
        ac = _load("ac_v328", BUNDLE / "auth_core.py")
        for pk, slug in (("gto3", "teneria-panamericana"), ("slp3", "holiday-inn-quijote")):
            assert pk in ac.PLANTS and pk in ac.AREAS and pk in ac.CAPEX_PLANTS
            assert ac.area_for_path(f"/{pk}/index.html") == pk
            assert ac.area_for_path(f"/monitoring/{pk}/") == pk
            assert ac.area_for_path(f"/report/{slug}/") == pk
            assert ac.area_for_path(f"/monitoring/{slug}/") == pk

    def test_capex_list_is_the_old_one_plus_the_new(self):
        sys.path.insert(0, str(BUNDLE))
        ac = _load("ac_v328b", BUNDLE / "auth_core.py")
        assert ac.CAPEX_PLANTS == ("gto2", "qro1", "nl2", "mex3", "tam1", "gto3", "slp3")
        assert not set(ac.CAPEX_PLANTS) & {"gto1", "mex1", "mex2", "nl1", "slp1", "slp2"}

    def test_setup_app_offers_them(self):
        src = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        m = re.search(r"^PLANTS = \[(.*?)\]", src, re.M | re.S)
        assert "'gto3'" in m.group(1) and "'slp3'" in m.group(1)

    def test_nginx_serves_and_redirects_them(self):
        sess = (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")
        rep = (BUNDLE / "report.argia.com.mx.conf").read_text(encoding="utf-8")
        for pk in ("gto3", "slp3"):
            assert re.search(rf"location /{pk}/ \{{ try_files", sess)
            assert re.search(rf"\(.*\b{pk}\b.*\)/", rep)

    def test_reference_sheets_in_both_languages_and_photos(self):
        mg = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        refs = V2 / "server" / "assets" / "refs"
        for pk, base in (("GTO3", "ARGIA_ref_Teneria_Panamericana_Leon"),
                         ("SLP3", "ARGIA_ref_Holiday_Inn_Quijote_SLP")):
            assert f"'{pk}': '/monitoring/assets/refs/'" in mg
            assert f"{base}_ES.pdf" in mg
            for lang in ("ES", "EN"):
                f = refs / f"{base}_{lang}.pdf"
                assert f.read_bytes()[:5] == b"%PDF-" and f.stat().st_size > 100_000
            for name in (f"{pk.lower()}.jpg", f"{pk.lower()}_t.jpg"):
                assert (V2 / "server" / "assets" / "photos" / name).read_bytes()[:2] == b"\xff\xd8"

    def test_registry_pins_the_inverters(self):
        from argia.core import inverter_registry as R
        p = R.plants(R.load())
        assert len(p["GTO3"]) == 6 and len(p["SLP3"]) == 4
        # station 319 is inverters 1-3, station 426 is 4-6 (numbering ours, noted)
        assert all("319" in p["GTO3"][sn]["note"] for sn in ("JGMNFZC00H", "JGMPG1X004", "JGMPG1X006"))
        assert all("426" in p["GTO3"][sn]["note"] for sn in ("JGMNFZC008", "JGMNFZC009", "JGMNFZC00K"))


def _png_pixels(data):
    """Tiny PNG decoder for the tests (8-bit palette or RGB, no interlace):
    width, height, rows of (r, g, b). Pillow is not a test dependency."""
    import struct
    import zlib
    pos, idat, plte = 8, b"", None
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b"IHDR":
            w, h, depth, ctype, _c, _f, inter = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = [tuple(body[i:i + 3]) for i in range(0, n, 3)]
        elif typ == b"IDAT":
            idat += body
        pos += 12 + n
    assert depth == 8 and ctype in (2, 3) and inter == 0, (depth, ctype, inter)
    bpp = 3 if ctype == 2 else 1
    raw, stride, rows, prev = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp)
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b, c = prev[x], (prev[x - bpp] if x >= bpp else 0)
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        rows.append([tuple(line[i:i + 3]) for i in range(0, stride, 3)] if ctype == 2
                    else [plte[v] for v in line])
    return w, h, rows


class TestV3281ClientLogos:
    """v328.1 - the customers' logos (Tomasz, 28 Oct): a real PNG, 160 px
    high (the 80 px @2x convention), on a clean white background - the
    Holiday Inn source carried a baked-in grey transparency checkerboard."""

    @staticmethod
    def _logo(pk):
        import base64
        logos = _load(f"logos_{pk}", BUNDLE / "argia_client_logos.py")
        name, uri = logos.CLIENT_LOGOS[pk]
        assert uri.startswith("data:image/png;base64,")
        return name, base64.b64decode(uri.split(",", 1)[1])

    @pytest.mark.parametrize("pk,name", [("GTO3", "TENERIA PANAMERICANA"), ("SLP3", "HOLIDAY INN")])
    def test_logo_is_a_real_png_on_white(self, pk, name):
        got, data = self._logo(pk)
        assert got == name
        w, h, rows = _png_pixels(data)
        assert h == 160 and w > 100
        corners = [rows[0][0], rows[0][-1], rows[-1][0], rows[-1][-1]]
        assert all(min(c) >= 250 for c in corners), corners
        colored = sum(1 for r in rows for p in r if max(p) - min(p) > 40)
        assert colored > 0.05 * w * h            # a logo, not a blank tile

    def test_no_checkerboard_left_in_the_holiday_inn_logo(self):
        _n, data = self._logo("SLP3")
        w, h, rows = _png_pixels(data)
        greys = sum(1 for r in rows for (a, b, c) in r
                    if 215 <= a <= 245 and abs(a - b) <= 4 and abs(b - c) <= 4)
        assert greys < 0.01 * w * h, greys
