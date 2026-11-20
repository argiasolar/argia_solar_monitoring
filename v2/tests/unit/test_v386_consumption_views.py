"""v386 - consumption inside the troubleshooting card, the view buttons and inverter list under
the chart, folding cards, and the Huawei real-time fallback. Invented plant and meters only."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from zoneinfo import ZoneInfo

import pytest

from argia.epi import views as V
from argia.report import epi_views_html as H
from argia.telemetry import meter as M
from scripts import meter_collect as MC
from tests.unit.test_v382_epi_views import DAY, MX as EMX, faults, synth

V2 = pathlib.Path(__file__).resolve().parents[2]
MX = ZoneInfo("America/Mexico_City")
UTC = dt.timezone.utc
EM = chr(0x2014)


def S(minute, w, imp=None, exp=None):
    return M.MeterSample(dt.datetime(2026, 3, 21, 16, 0, tzinfo=UTC) + dt.timedelta(minutes=minute), "P", "1", w, imp, exp)


def test_hourly_uses_the_counters_whatever_the_gap():
    # 15-minute real-time readings (no 5-minute history): counters make the energy exact
    s = [S(0, 9e9, 100.0, 50.0), S(15, 9e9, 110.0, 50.0), S(30, -1.0, 112.0, 53.0), S(75, 0.0, 112.0, 60.0)]
    h = M.hourly(s, MX)                                  # 16:00 UTC = 10:00 MX
    assert h[("2026-03-21", 10)] == pytest.approx([12.0, 10.0])          # 10:30-11:15 booked to 10:00
    assert sum(v[1] for v in h.values()) == pytest.approx(10.0)   # the power (absurd here) is not used


def test_hourly_falls_back_to_power_without_counters():
    s = [S(0, 12000.0), S(5, -6000.0), S(10, 0.0), S(40, 1000.0)]   # 30-min hole not integrated
    h = M.hourly(s, MX)[("2026-03-21", 10)]
    assert h == pytest.approx([1.0, 0.5])


def test_grid_kw_on_the_card_axis():
    s = [S(0, 10000.0), S(15, 40000.0), S(60, 0.0)]
    t = [S(m, 0).ts_utc for m in (-5, 0, 5, 15, 30, 61, 64)]
    assert M.grid_kw_on(s, t) == [None, 10.0, 20.0, 40.0, None, 0.0, None]   # 15->60 is a 45-min hole


def test_huawei_realtime_parse():
    ans = {"success": True, "data": [{"devId": 7, "dataItemMap": {"active_power": 5000, "reverse_active_cap": 10.5, "active_cap": 2}},
                                     {"devId": 9, "dataItemMap": {"active_power": 1}}]}
    now = dt.datetime(2026, 3, 21, 10, 17, 42, tzinfo=MX)
    s = M.parse_huawei_realtime({"7": "SAG"}, ans, now)
    assert len(s) == 1 and s[0].plant_key == "SAG" and s[0].grid_w == -5000 and s[0].imp_total_kwh == 10.5
    assert s[0].ts_utc == dt.datetime(2026, 3, 21, 16, 17, tzinfo=UTC)


def test_407_is_kept_apart_from_other_stops():
    with pytest.raises(MC.RateLimited) as e:
        MC.huawei_check({"success": False, "failCode": 407})
    assert e.value.code == "407"
    with pytest.raises(MC.RateLimited) as e:
        MC.huawei_check({"success": False, "failCode": 305})
    assert e.value.code == ""
    src = (V2 / "scripts" / "meter_collect.py").read_text(encoding="utf-8")
    assert "if huawei_407 and hw and not a.days and not a.backfill and may_call():" in src


def _payload(cons=None):
    (pm, strs, chan, sn_of), station, samples, ac, _tel = synth(faults)
    p = V.apply_mtd(V.build(pm, strs, chan, sn_of, station, samples, ac, DAY, EMX), None, 1)
    if cons:
        p["cons"] = cons
    return p


CONS = {"grid_kw": None, "hours": [None] * 24, "today": {"cons": 1234.4, "share": 0.42, "imp": 700.0, "exp": 10.0, "ok": True},
        "days": [{"d": "2026-03-21", "cons": 1234.4, "share": 0.42, "imp": 700.0, "exp": 10.0, "used": 520.0, "ok": True, "basis": "x"}],
        "s7": 0.4, "sm": 0.41, "cons_m": 20000.0}


def test_card_has_consumption_only_with_meter_data():
    plain = H.card("DEMO", _payload())
    assert 'data-v="c"' not in plain and 'class="evhead2' not in plain
    h = H.card("DEMO", _payload(json.loads(json.dumps(CONS))), live=True)
    assert 'data-v="c"' in h and "Consumption so far today" in h and "<b>1,234 kWh</b>" in h and "solar share" in h and "42 %" in h
    assert "function drawCons" in h and "function consKw" in h and EM not in h


def test_buttons_above_and_inverters_below_the_chart():
    """v386 (Tomasz): the side column looked empty with many inverters - buttons on top, chips under."""
    h = H.card("DEMO", _payload())
    assert "evside" not in h and "evside" not in H.CSS
    assert h.index('class="evseg"') < h.index('class="evsvg"') < h.index('class="evpanel"')
    assert ".evinv{display:flex;flex-wrap:wrap" in H.CSS and ".evseg{display:flex;flex-wrap:wrap" in H.CSS


def test_generator_wiring():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    assert "mp = meter_payload(pk, d, p.get('t'))" in src and "p['cons'] = mp" in src
    assert "body += FOLD_JS" in src and "MTR.hourly(ss, MX)" in src


def test_fold_script():
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    js = re.search(r'FOLD_JS = r"""<script>(.*?)</script>"""', src, re.S).group(1)
    assert "localStorage" in js and "try{" in js                         # storage may be blocked: still folds
    assert ".card" in js and ".evby" in js and EM not in js


@pytest.mark.parametrize("which", ["fold", "epi"])
def test_scripts_parse(which, tmp_path):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
    js = re.search(r'FOLD_JS = r"""<script>(.*?)</script>"""', src, re.S).group(1) if which == "fold" else H.JS
    f = tmp_path / "x.js"
    f.write_text(js, encoding="utf-8")
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
