"""TAM1 (Ryder Nuevo Laredo) onboarding — the 2026-09-01 additions.

A new plant touches four hardcoded surfaces; each one silently drops
the plant if missed, so each is pinned here:

  * report_gen.CAPEX        — CAPEX overview + per-plant page loops
  * auth_core/setup_app     — per-plant access areas ('tam1')
  * nginx session conf      — location /tam1/ (no location, no page)
  * argia_client_logos      — RYDER logo for client-facing pages

Plant facts (Growatt plant 10902835, verified live 2026-09-01):
364.65 kWp DC (510 x CS7N-715TB-AG), 4 x 75 kW Growatt MID,
producing since 2026-05-13, interconnection in process.
"""
import importlib.util
import re
import sys
from pathlib import Path

V2 = Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestTam1IsEverywhereANewPlantMustBe:
    def test_report_gen_counts_tam1_as_capex(self):
        src = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
        m = re.search(r"^CAPEX = \[(.*?)\]", src, re.M)
        assert m and "'TAM1'" in m.group(1)

    def test_report_gen_did_not_leak_tam1_into_ppa(self):
        src = (BUNDLE / "report_gen.py").read_text(encoding="utf-8")
        m = re.search(r"^PPA = \[(.*?)\]", src, re.M)
        assert m and "TAM1" not in m.group(1)

    def test_auth_core_has_the_tam1_area(self):
        ac = _load("ac_tam1", BUNDLE / "auth_core.py")
        assert "tam1" in ac.PLANTS
        assert "tam1" in ac.AREAS

    def test_auth_maps_the_tam1_path_prefix(self):
        ac = _load("ac_tam1b", BUNDLE / "auth_core.py")
        assert ac.area_for_path("/tam1/index.html") == "tam1"

    def test_setup_app_offers_tam1(self):
        src = (BUNDLE / "setup_app.py").read_text(encoding="utf-8")
        m = re.search(r"^PLANTS = \[(.*?)\]", src, re.M | re.S)
        assert m and "'tam1'" in m.group(1)

    def test_nginx_serves_the_tam1_location(self):
        conf = (BUNDLE / "nginx-argia_session.conf").read_text(
            encoding="utf-8")
        assert re.search(r"location /tam1/ \{ try_files", conf)

    def test_client_logo_is_ryder_and_renderable(self):
        logos = _load("logos_tam1", BUNDLE / "argia_client_logos.py")
        name, uri = logos.CLIENT_LOGOS["TAM1"]
        assert name == "RYDER"
        assert uri.startswith("data:image/")
        assert len(uri) > 1000          # a real image, not a stub
