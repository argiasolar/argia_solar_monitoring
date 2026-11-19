"""v377 - sky module (sun position, Erbs, Perez) against pvlib 0.16.

The reference values below were produced with pvlib 0.16.1
(solarposition.get_solarposition [NREL SPA], irradiance.erbs,
get_total_irradiance(model='perez', albedo=0.2)) for an invented site
(20.0 N, -100.0 E). Tolerances: zenith 0.02 deg (NOAA vs SPA), POA 0.1 %.
"""
from __future__ import annotations

import datetime as dt
import math
import random

import pytest

from argia.epi import sky

MX = dt.timezone(dt.timedelta(hours=-6))
LAT, LON = 20.0, -100.0
#        local time,          GHI,  tilt, az,   zenith,  azimuth,  DNI,      DHI,      POA (pvlib)
PVLIB = [("2026-03-21 12:00", 850.0, 10, 200, 22.6924, 148.0884, 647.9070, 252.2478, 880.2869),
         ("2026-06-21 09:30", 520.0, 3, 90, 44.4928, 76.4968, 329.9049, 284.6660, 537.6476),
         ("2026-12-21 16:00", 180.0, 15, 270, 65.5993, 230.9554, 25.0432, 169.6543, 185.7192),
         ("2026-09-10 13:05", 1010.0, 5, 148.7, 16.7545, 205.0319, 880.2133, 167.1531, 1022.9765)]


def ts(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=MX)


@pytest.mark.parametrize("t,ghi,tilt,az,zen,saz,dni,dhi,poa", PVLIB)
def test_matches_pinned_pvlib(t, ghi, tilt, az, zen, saz, dni, dhi, poa):
    sp = sky.sun_position(ts(t), LAT, LON)
    assert abs(sp.zenith - zen) < 0.02
    assert abs((sp.azimuth - saz + 180) % 360 - 180) < 0.1
    p = sky.poa(ghi, ts(t), LAT, LON, tilt, az)
    assert p.poa_global == pytest.approx(poa, rel=1e-3)
    # with pvlib's own sun position the irradiance equations agree to rounding
    q = sky.poa(ghi, ts(t), LAT, LON, tilt, az, sun=sky.SunPos(zen, saz))
    assert q.dni == pytest.approx(dni, abs=2e-3) and q.dhi == pytest.approx(dhi, abs=2e-3)
    assert q.poa_global == pytest.approx(poa, abs=2e-3)


def test_erbs_branches_and_closure():
    ea = 1400.0
    for ghi, zen in ((50.0, 80.0), (500.0, 40.0), (1000.0, 10.0)):
        dni, dhi, kt = sky.erbs(ghi, zen, ea)
        assert dni * math.cos(math.radians(zen)) + dhi == pytest.approx(ghi)   # GHI = DNI cos z + DHI
    assert sky.erbs(1000.0, 10.0, ea)[2] <= 1.0                                 # kt capped at 1
    dni, dhi, _ = sky.erbs(20.0, 88.0, ea)                                      # beyond 87 deg: all diffuse
    assert dni == 0.0 and dhi == 20.0
    assert sky.erbs(0.0, 30.0, ea)[:2] == (0.0, 0.0)


def test_night_negative_ghi_and_flat_plane():
    night = ts("2026-06-21 23:00")
    assert sky.poa(0.0, night, LAT, LON, 10, 180).poa_global == 0.0
    assert sky.poa(-3.0, ts("2026-06-21 12:00"), LAT, LON, 10, 180).poa_global == 0.0   # spec: GHI < 0 -> 0
    # a horizontal plane sees exactly GHI (Perez with tilt 0 reduces to DHI + beam)
    p = sky.poa(700.0, ts("2026-03-21 12:00"), LAT, LON, 0, 180)
    assert p.poa_global == pytest.approx(700.0, rel=1e-9)


def test_naive_time_refused():
    with pytest.raises(ValueError, match="timezone"):
        sky.sun_position(dt.datetime(2026, 1, 1, 12), LAT, LON)


def test_equivalence_with_pvlib_when_installed():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    pvlib = pytest.importorskip("pvlib")
    rng = random.Random(7)
    idx = pd.date_range("2026-01-01", "2026-12-31 23:00", freq="37min", tz="Etc/GMT+6")
    spv = pvlib.solarposition.get_solarposition(idx, LAT, LON)
    ea = pvlib.irradiance.get_extra_radiation(idx).values
    ghi = np.array([rng.uniform(0, 1100) for _ in idx]) * np.clip(np.cos(np.radians(spv["zenith"].values)), 0, None)
    er = pvlib.irradiance.erbs(ghi, spv["zenith"].values, idx)
    ref = np.asarray(pvlib.irradiance.get_total_irradiance(
        12, 205, spv["zenith"].values, spv["azimuth"].values, er["dni"], ghi, er["dhi"],
        dni_extra=ea, model="perez", albedo=0.2)["poa_global"], dtype=float)
    worst_z = worst_p = 0.0
    for k in range(0, len(idx), 3):
        z = spv["zenith"].values[k]
        if z > 88 or math.isnan(ref[k]):
            continue
        t = idx[k].to_pydatetime()
        worst_z = max(worst_z, abs(sky.sun_position(t, LAT, LON).zenith - z))
        q = sky.poa(ghi[k], t, LAT, LON, 12, 205, sun=sky.SunPos(z, spv["azimuth"].values[k]))
        worst_p = max(worst_p, abs(q.poa_global - ref[k]))
    assert worst_z < 0.02 and worst_p < 1e-6
