"""v377 - EPI-600 model arithmetic (spec v0.7 steps 2-5 + audit changes).
Synthetic plants only; the private design-year proof is scripts/epi600_design_check.py."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import pathlib
import sys

import pytest

from argia.epi import model as M
from argia.epi import sky

V2 = pathlib.Path(__file__).resolve().parents[2]


def test_spec_vector_arithmetic():
    # numbers stated in spec v0.7 section 6 (string INV1:Istr1, 10 Nov 2026)
    assert M.t_cell(54.7, 692.63) == pytest.approx(56.78, abs=0.005)
    d = M.degradation_factor("LR5-72HTH-590M", dt.date(2024, 10, 29), dt.date(2026, 11, 10))
    assert d == pytest.approx(0.98087, abs=5e-6)           # 1.5 + 0.4 x 1.0315 = 1.9126 %
    assert M.cap_dc_w(136.4) == pytest.approx(138_477, abs=1)  # 136.4 / 0.985 = 138.48 kW, not 1.1 x 136.4


def test_degradation_rules():
    assert M.degradation_factor("JAM72S40605", dt.date(2026, 12, 1), dt.date(2026, 11, 30)) is None   # not energized
    half = M.degradation_factor("JAM72S40605", dt.date(2026, 1, 1), dt.date(2026, 7, 2))
    assert half == pytest.approx(1 - 1.0 * (182 / 365.25) / 100)                   # linear inside year 1
    # ReneSola datasheet: 99 % after 1 year, 87.4 % after 30
    cod = dt.date(2000, 1, 1)
    assert M.degradation_factor("RS5J-655NBG-E1", cod, cod + dt.timedelta(days=round(365.25))) == pytest.approx(0.99, abs=1e-4)
    assert M.degradation_factor("RS5J-655NBG-E1", cod, cod + dt.timedelta(days=round(30 * 365.25))) == pytest.approx(0.874, abs=1e-4)
    with pytest.raises(KeyError):
        M.degradation_factor("UNKNOWN-MODULE", cod, cod)


def test_pr0_and_expected_power():
    b = (0.8, -0.002, 0.3, -0.2)
    assert M.pr0(b, 50.0, 800.0) == pytest.approx(0.8 - 0.1 + 0.24 - 0.128)
    assert M.pr0((-1, 0, 0, 0), 25, 500) == 0.0                                       # never negative
    e = M.expected_string_w(10.0, 0.99, 800.0, 0.812, 1.2, 0.995)
    assert e == pytest.approx(10 * 1000 * 0.99 * 0.8 * 0.812 * 0.988 * 0.995)


def test_inverter_limit_scales_all_strings_together():
    es = [60_000.0, 60_000.0, 40_000.0]                                                # 160 kW DC expected
    s = M.cap_scale(es, 136.4)
    assert sum(e * s for e in es) == pytest.approx(M.cap_dc_w(136.4))
    assert M.cap_scale([50_000.0], 136.4) == 1.0 and M.cap_scale([], 136.4) == 1.0


def test_ratio_of_sums_not_mean_of_ratios():
    t = M.Tally()
    t.add(100.0, 200.0, 1 / 12)      # dawn sample at 50 %
    t.add(900.0, 1000.0, 1 / 12)     # noon sample at 90 %
    assert t.epi == pytest.approx(1000 / 1200)          # 83.3 %, not the 70 % mean of ratios
    u = M.Tally()
    u.merge(t)
    u.merge(t)
    assert u.epi == t.epi and u.n == 4


def test_bands():
    assert M.band(0.98, 500) == "green" and M.band(0.95, 500) == "amber" and M.band(0.90, 500) == "red"
    assert M.band(1.05, 500) == "green" and M.band(0.99, 199) == "insufficient" and M.band(None, 999) == "insufficient"
    assert M.band(0.97, 500) == "green" and M.band(0.93, 500) == "amber" and M.band(1.03, 500) == "green"


def test_bias_recovers_a_known_factor():
    true = [1 + 0.01 * (m - 6) for m in range(1, 13)]
    model = {m: 1000.0 * m for m in range(1, 13)}
    design = {m: model[m] * true[m - 1] for m in range(1, 13)}
    assert M.bias_from_design(model, design) == pytest.approx(tuple(true))
    with pytest.raises(ValueError, match="month 2"):      # the first month without model energy
        M.bias_from_design({1: 1.0}, {1: 1.0} | {3: 1.0})


# --- end to end: the design-year check script on a synthetic plant -----------------
def _synthetic_design(tmp_path, factor):
    """One invented plant, two inverters; HelioScope-style hourly CSV for the 15th of
    every month whose DC is exactly the spec model x `factor` (and no limit binding)."""
    lat, lon, b = 20.0, -100.0, (0.85, -0.0018, 0.31, -0.21)
    soil = [1.2, 1.5, 2.0, 2.5, 2.2, 1.2, 0.6, 0.6, 0.6, 1.0, 1.2, 1.2]
    strings = [("INV1", "PV1", 10.0, 5, 180), ("INV1", "PV2", 10.0, 5, 180), ("INV2", "PV1", 12.0, 10, 90)]
    with open(tmp_path / "A.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant", "inverter", "string_id", "uid", "kwp", "tilt", "azimuth", "module", "cod_date"])
        for inv, sid, kwp, tilt, az in strings:
            w.writerow(["Demo", inv, sid, f"{inv}:{sid}", kwp, tilt, az, "JAM72S40605", "2025-01-01"])
    with open(tmp_path / "B.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["plant", "latitude", "longitude", "kwp_design", "b0", "b1", "b2", "b3"] + [f"bias_m{m:02d}" for m in range(1, 13)])
        w.writerow(["Demo", lat, lon, 32.0, *b] + [1.0] * 12)
    with open(tmp_path / "C.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["month", "design_soiling_pct"])
        for m, s in enumerate(soil, 1):
            w.writerow([m, s])
    tz = dt.timezone(dt.timedelta(hours=-6))
    with open(tmp_path / "PPA Demo.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "global_horizontal_irradiance", "avg_cell_temp", "optimal_dc_power", "actual_dc_power"])
        for m in range(1, 13):
            for h in range(24):
                start = dt.datetime(2002, m, 15, h)
                mid = start.replace(tzinfo=tz) + dt.timedelta(minutes=30)
                z = sky.sun_position(mid, lat, lon).zenith
                ghi = max(0.0, 1050 * math.cos(math.radians(z))) if z < 89 else 0.0
                tc = 25 + 30 * ghi / 1000
                dc = 0.0
                for inv, sid, kwp, tilt, az in strings:
                    g = sky.poa(ghi, mid, lat, lon, tilt, az).poa_global
                    dc += M.expected_string_w(kwp, 1.0, g, M.pr0(b, tc, g), soil[m - 1], 1.0)
                dc *= factor
                w.writerow([start.isoformat(sep=" "), round(ghi, 6), tc, dc, dc])
    (tmp_path / "L.json").write_text(json.dumps({"Demo": {"INV1": 50.0, "INV2": 50.0}}), encoding="utf-8")


def test_design_check_recovers_bias_and_scores_100(tmp_path, capsys):
    sys.path.insert(0, str(V2 / "scripts"))
    import epi600_design_check as chk
    _synthetic_design(tmp_path, 1.03)
    rc = chk.main(["--annex-a", str(tmp_path / "A.csv"), "--annex-b", str(tmp_path / "B.csv"),
                   "--annex-c", str(tmp_path / "C.csv"), "--design-dir", str(tmp_path),
                   "--limits", str(tmp_path / "L.json"), "--out", str(tmp_path / "bias.json")])
    out = capsys.readouterr().out
    assert rc == 0 and "design-year EPI-600 1.0000" in out and "OK" in out
    bias = json.loads((tmp_path / "bias.json").read_text(encoding="utf-8"))["bias"]["Demo"]
    assert bias == pytest.approx([1.03] * 12, abs=2e-5)
