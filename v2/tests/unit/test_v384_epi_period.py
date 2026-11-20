"""v384 - EPI-600 by string per inverter (Arturo's mock-up), the week / month history, the
sunshine context and the EPI columns of the inverter table. Invented plant only."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

import pytest

from argia.epi import config as C
from argia.epi import live as L
from argia.epi import views as V
from argia.report import epi_views_html as H
from tests.unit.test_v382_epi_views import DAY, MX, FakeDB, config_dirs, faults, synth

V2 = pathlib.Path(__file__).resolve().parents[2]
EM = chr(0x2014)


@pytest.fixture(scope="module")
def payload():
    (pm, strs, chan, sn_of), station, samples, ac, _tel = synth(faults)
    return V.build(pm, strs, chan, sn_of, station, samples, ac, DAY, MX)


def test_producing_strings_and_epi_on_producing(payload):
    p = V.apply_mtd(json.loads(json.dumps(payload)), None, 1)
    inv = {i["inv"]: i for i in p["inverters"]}
    sv = p["strings"]["INV2"]
    assert sv["idle_mtd"] == ["INV2:S3", "INV2:S4", "INV2:S6"]            # the three dead strings
    assert inv["INV2"]["producing_mtd"] == 3 and inv["INV1"]["producing_mtd"] == 6
    # on producing strings = ratio of the sums of the live strings' tallies
    t = p["tallies"]["epi600"]
    live = [t[u] for u in ("INV2:S1", "INV2:S2", "INV2:S5")]
    want = sum(x[0] for x in live) / sum(x[1] for x in live)
    assert inv["INV2"]["epi600_mtd_producing"] == pytest.approx(want, abs=1e-4)
    assert inv["INV2"]["epi600_mtd_producing"] > 0.9 > inv["INV2"]["epi600_mtd"]   # dead strings pull it down
    assert inv["INV1"]["epi600_mtd_producing"] == inv["INV1"]["epi600_mtd"]


def test_insolation_of_the_day(payload):
    g = [x for x in payload["ghi"] if x is not None]
    assert payload["tallies"]["insol"] == round(sum(g) * 5 / 60 / 1000, 2)
    assert 5.0 < payload["tallies"]["insol"] < 9.0                        # a clear March day at 20 N


def _tl(real, exp, n=10, insol=5.0):
    return {"epi600": {"plant": [real, exp, n], "INV1": [real, exp, n], "S1": [real, exp, n]},
            "avail": {}, "steps600": n, "insol": insol}


def test_period_is_a_ratio_of_sums_never_an_average():
    days = [("2026-03-01", _tl(90, 100)), ("2026-03-02", None), ("2026-03-03", _tl(300, 300, insol=6.1))]
    out = V.period(days, {"inv": ["INV1"], "str": ["S1"]})
    assert out["days"] == ["2026-03-01", "2026-03-02", "2026-03-03"] and out["computed"] == 2
    assert out["plant"] == [0.9, None, 1.0] and out["inv"]["INV1"] == [0.9, None, 1.0]
    assert out["insol"] == [5.0, None, 6.1]
    assert out["sum"]["plant"] == pytest.approx(390 / 400)              # not (0.9 + 1.0) / 2
    assert set(out["sum"]) == {"plant", "INV1"}                         # strings are not summed here


def test_week_sum_uses_the_last_seven_days():
    days = [(f"2026-03-{k:02d}", _tl(50, 100)) for k in range(1, 24)] + \
           [(f"2026-03-{k:02d}", _tl(100, 100)) for k in range(24, 31)]
    out = V.period(days, {"inv": ["INV1"]})
    assert out["sum7"]["INV1"] == 1.0 and out["sum"]["INV1"] == pytest.approx((23 * 50 + 7 * 100) / 3000, abs=1e-4)


def test_live_history_reads_the_cached_days(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "HIST_DAYS", 4)
    epi, lay = config_dirs(tmp_path)
    cfgs = C.load(epi, lay)
    fp = {"DEMO": L.fingerprint(epi, lay, "DEMO")}
    days = [dt.date(2026, 3, d) for d in (1, 2, 3, 4)]
    db = FakeDB(days)
    now = dt.datetime(2026, 3, 4, 12, 0, tzinfo=MX)
    b = L.Builder(db, cfgs, MX, now, fp, cache_dir=str(tmp_path / "c"), fill_per_run=10)
    p = b.card_payload("DEMO", days[3])
    h = p["hist"]
    assert h["days"] == ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04"] and h["computed"] == 4
    assert all(e == pytest.approx(1.0, abs=2e-3) for e in h["plant"])   # the fake plant delivers its design
    assert set(h["inv"]) == {"INV1", "INV2", "INV3", "INV4"} and len(h["str"]) == 24
    calls = db.calls
    p2 = L.Builder(db, cfgs, MX, now, fp, cache_dir=str(tmp_path / "c"), fill_per_run=0).card_payload("DEMO", days[3])
    assert p2["hist"]["computed"] == 4 and db.calls - calls == 2          # only today queried; history from the cache


def test_rules_version_changed_so_old_caches_are_recomputed():
    assert L.RULES_VERSION == "v384" and L.FILL_PER_RUN == 6


def test_card_has_the_new_views(payload):
    p = V.apply_mtd(json.loads(json.dumps(payload)), None, 1)
    p["hist"] = V.period([(DAY.isoformat(), p["tallies"])], {"inv": [i["inv"] for i in p["inverters"]],
                                                             "str": [u for s in p["strings"].values() for u in s["uids"]]})
    h = H.card("DEMO", p)
    assert 'data-v="w"' in h and 'data-v="m"' in h and '<div class="evby"></div>' in h
    for fn in ("function byStrings", "function drawPeriod", "function sun(", "function sunLeg"):
        assert fn in h
    assert ".evcard .evleg svg{display:inline-block" in H.CSS            # the portal's svg{display:block} split the legend
    assert EM not in h
    blob = re.search(r'<script type="application/json" class="evdata">(.*?)</script>', h, re.S).group(1)
    data = json.loads(blob.replace("<\\/", "</"))
    assert "hist" in data and data["inverters"][1]["producing_mtd"] == 3


def test_inverter_table_columns_line_up():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    page = src[src.index("def plant_page"):]
    head = page[page.index('<table><tr><th data-en="Inverter"'):]
    head = head[:head.index("</tr>")]
    n_th = head.count("<th")
    row = page[page.index("inv_cells.append(\n            f'<tr><td>{inverter_html"):]
    row = row[:row.index("</tr>')")]
    n_td = row.count("<td") + 3                                           # epi_inv_cols adds three
    silent = page[page.index("' activo pero sin reportar"):]
    silent = silent[:silent.index("</tr>')")]
    assert n_th == 10 and n_td == 10 and silent.count("<td>") == 8
    cols = src[src.index("def epi_inv_cols"):src.index("def epi_views_card")]
    assert cols.count("<td>") == 6                                        # 3 for the dash row, 3 for values
