"""v399 - Arturo's answers to the methodology evaluation (24 Nov 2026):
* an EPI above 100 % is displayed as 100 %, with no "doubtful data" flag; the calculation is not altered;
* the "without event days" variant of the EPI headline is removed (EPI-600 already keeps unavailable
  time out; Availability-150 covers outages)."""
from __future__ import annotations

import pathlib

from argia.epi import model as M
from argia.report import epi_views_html as EVH
from argia.report import methodology as MC

V2 = pathlib.Path(__file__).resolve().parents[2]


def _payload():
    return {'plant': {'epi600_day': 1.08, 'epi600_mtd': 1.012, 'ac_kw': [120.0], 'band_mtd': M.band(1.012, 500)},
            'inverters': [{'inv': 'INV1', 'epi600_day': 1.2, 'epi600_mtd': 0.95, 'epi600_mtd_producing': 1.04,
                           'w': [1050.0], 'avail_mtd': 1.0}],
            'strings': {'INV1': {'epi600_mtd': {'S1': 1.3, 'S2': 0.9, 'S3': None}, 'w': {'S1': [990.0]}}},
            'hist': {'plant': [1.1, 0.9, None], 'inv': {'INV1': [1.05, None]}, 'str': {'S1': [1.4]},
                     'sum': {'plant': 1.02}, 'sum7': {'INV1': 0.98}, 'insol': [6.2]}}


def test_band_has_no_suspect_any_more():
    assert M.band(1.05, 500) == "green" and M.band(1.25, 500) == "green"
    assert M.band(0.96, 500) == "amber" and M.band(1.05, 10) == "insufficient"


def test_display_copy_caps_every_epi_and_leaves_the_rest():
    p = _payload()
    q = EVH.capped(p)
    assert q['plant']['epi600_day'] == 1.0 and q['plant']['epi600_mtd'] == 1.0
    assert q['inverters'][0]['epi600_day'] == 1.0 and q['inverters'][0]['epi600_mtd'] == 0.95
    assert q['inverters'][0]['epi600_mtd_producing'] == 1.0
    assert q['strings']['INV1']['epi600_mtd'] == {'S1': 1.0, 'S2': 0.9, 'S3': None}
    assert q['hist']['plant'] == [1.0, 0.9, None] and q['hist']['inv']['INV1'] == [1.0, None]
    assert q['hist']['str']['S1'] == [1.0] and q['hist']['sum']['plant'] == 1.0 and q['hist']['sum7']['INV1'] == 0.98
    # power series, insolation and availability untouched; the original payload (the cache) unchanged
    assert q['plant']['ac_kw'] == [120.0] and q['inverters'][0]['w'] == [1050.0] and q['hist']['insol'] == [6.2]
    assert p['plant']['epi600_day'] == 1.08 and p['strings']['INV1']['epi600_mtd']['S1'] == 1.3


def test_card_renders_capped_values():
    src = (V2 / "argia/report/epi_views_html.py").read_text(encoding="utf-8")
    assert "p = capped(p)" in src[src.index("def render("):src.index("def render(") + 400]


def test_report_tile_and_inverter_table():
    rg = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
    seg = rg[rg.index("def epi_tile(k):"):rg.index("def plant_parts(k):")]
    assert "without event days" not in seg and "min(v, 1.0)" in seg
    mg = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
    assert "f'{100 * min(x, 1.0):.1f}%'" in mg


def test_the_ratio_itself_is_not_capped():
    days = {"2026-11-01": {'epi600': {'plant': [1100.0, 1000.0, 300]}}}
    assert MC.epi_summary(days, "2026-11-01", {})['mtd'] == 1.1
