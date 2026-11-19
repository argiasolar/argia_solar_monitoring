"""Sun position and plane-of-array irradiance, pure Python.

Why not pvlib at run time: the server and the Pi run without numpy/pandas
(pio06 has had an out-of-memory reboot). The equations below are the ones
pvlib 0.16 uses for ``irradiance.erbs``, ``get_total_irradiance(model='perez')``
with the 1990 all-sites coefficients, ``get_extra_radiation`` (Spencer) and
``atmosphere.get_relative_airmass`` (Kasten-Young 1989). Equivalence with
pvlib is asserted in tests/unit/test_v377_epi_sky.py, and the spec accepts any
library that reproduces its test vector within 1 % (spec v0.7, step 1).

Sun position: NOAA / Meeus low-precision algorithm (true, not refracted,
zenith - the spec test vector needs the true zenith). Its error against
pvlib's NREL SPA is bounded in the same test (< 0.02 degrees over a year).

Conventions: angles in degrees; azimuth from north, clockwise (180 = south,
270 = west, as in the spec); irradiance in W/m2; times are timezone-aware.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import NamedTuple, Optional

ALBEDO = 0.2                 # spec v0.7 step 1
SOLAR_CONSTANT = 1366.1      # pvlib get_extra_radiation default
ERBS_MIN_COS_ZENITH = 0.065  # pvlib erbs defaults (0.16)
ERBS_MAX_KT = 1.0
ERBS_MAX_ZENITH = 87.0

# Perez 1990 all-sites composite: rows = clearness bins 1..8, columns =
# f11 f12 f13 f21 f22 f23 (Perez et al. 1990, Solar Energy 44(5), 271-289)
_PEREZ_1990 = (
    (-0.0080, 0.5880, -0.0620, -0.0600, 0.0720, -0.0220),
    (0.1300, 0.6830, -0.1510, -0.0190, 0.0660, -0.0290),
    (0.3300, 0.4870, -0.2210, 0.0550, -0.0640, -0.0260),
    (0.5680, 0.1870, -0.2950, 0.1090, -0.1520, -0.0140),
    (0.8730, -0.3920, -0.3620, 0.2260, -0.4620, 0.0010),
    (1.1320, -1.2370, -0.4120, 0.2880, -0.8230, 0.0560),
    (1.0600, -1.6000, -0.3590, 0.2640, -1.1270, 0.1310),
    (0.6780, -0.3270, -0.2500, 0.1560, -1.3770, 0.2510),
)
_PEREZ_EPS_EDGES = (1.065, 1.23, 1.5, 1.95, 2.8, 4.5, 6.2)

_R = math.radians
_D = math.degrees


class SunPos(NamedTuple):
    zenith: float     # true (geometric) zenith, degrees
    azimuth: float    # degrees from north, clockwise


class Poa(NamedTuple):
    poa_global: float
    poa_direct: float
    poa_sky_diffuse: float
    poa_ground_diffuse: float
    dni: float
    dhi: float


def _utc(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return ts.astimezone(dt.timezone.utc)


def _julian_day(ts_utc: dt.datetime) -> float:
    # days since J2000.0 (2000-01-01 12:00 UTC) + 2451545
    j2000 = dt.datetime(2000, 1, 1, 12, tzinfo=dt.timezone.utc)
    return 2451545.0 + (ts_utc - j2000).total_seconds() / 86400.0


def sun_position(ts: dt.datetime, lat: float, lon: float) -> SunPos:
    """NOAA solar position (Meeus, Astronomical Algorithms, ch. 25)."""
    u = _utc(ts)
    t = (_julian_day(u) - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    c = (math.sin(_R(m)) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(_R(2 * m)) * (0.019993 - 0.000101 * t)
         + math.sin(_R(3 * m)) * 0.000289)
    true_long = l0 + c
    omega = 125.04 - 1934.136 * t
    app_long = true_long - 0.00569 - 0.00478 * math.sin(_R(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = eps0 + 0.00256 * math.cos(_R(omega))
    decl = math.asin(math.sin(_R(eps)) * math.sin(_R(app_long)))
    y = math.tan(_R(eps) / 2.0) ** 2
    eot = 4.0 * _D(y * math.sin(2 * _R(l0)) - 2 * e * math.sin(_R(m))
                   + 4 * e * y * math.sin(_R(m)) * math.cos(2 * _R(l0))
                   - 0.5 * y * y * math.sin(4 * _R(l0)) - 1.25 * e * e * math.sin(2 * _R(m)))
    minutes = u.hour * 60.0 + u.minute + (u.second + u.microsecond / 1e6) / 60.0
    tst = minutes + eot + 4.0 * lon
    ha = _R(tst / 4.0 - 180.0)
    phi = _R(lat)
    cosz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    zen = _D(math.acos(max(-1.0, min(1.0, cosz))))
    az = (_D(math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180.0) % 360.0
    return SunPos(zen, az)


def extra_radiation(ts: dt.datetime) -> float:
    """Spencer (1971) as in pvlib: day of year taken in UTC."""
    doy = _utc(ts).timetuple().tm_yday
    b = 2.0 * math.pi / 365.0 * (doy - 1)
    r = (1.00011 + 0.034221 * math.cos(b) + 0.00128 * math.sin(b)
         + 0.000719 * math.cos(2 * b) + 7.7e-05 * math.sin(2 * b))
    return SOLAR_CONSTANT * r


def erbs(ghi: float, zenith: float, dni_extra: float):
    """Erbs, Klein, Duffie (1982) diffuse fraction -> (dni, dhi, kt)."""
    cosz = math.cos(_R(zenith))
    kt = ghi / (dni_extra * max(cosz, ERBS_MIN_COS_ZENITH))
    kt = min(max(kt, 0.0), ERBS_MAX_KT)
    if kt <= 0.22:
        df = 1 - 0.09 * kt
    elif kt <= 0.8:
        df = 0.9511 - 0.1604 * kt + 4.388 * kt ** 2 - 16.638 * kt ** 3 + 12.336 * kt ** 4
    else:
        df = 0.165
    dhi = df * ghi
    dni = (ghi - dhi) / cosz if cosz != 0 else 0.0
    if zenith > ERBS_MAX_ZENITH or ghi < 0 or dni < 0:
        return 0.0, ghi, kt
    return dni, dhi, kt


def airmass_kasten_young(zenith: float) -> Optional[float]:
    if zenith > 90:
        return None
    return 1.0 / (math.cos(_R(zenith)) + 0.50572 * (6.07995 + (90 - zenith)) ** -1.6364)


def aoi_projection(tilt: float, surf_az: float, zenith: float, sun_az: float) -> float:
    p = (math.cos(_R(tilt)) * math.cos(_R(zenith))
         + math.sin(_R(tilt)) * math.sin(_R(zenith)) * math.cos(_R(sun_az - surf_az)))
    return max(-1.0, min(1.0, p))


def perez_sky_diffuse(tilt: float, surf_az: float, dhi: float, dni: float, dni_extra: float,
                      zenith: float, sun_az: float) -> float:
    """Perez (1990) sky diffuse on the tilted plane, pvlib semantics."""
    am = airmass_kasten_young(zenith)
    if am is None or dhi <= 0:
        return 0.0          # pvlib: 0 for sun below horizon; dhi = 0 gives 0
    kappa = 1.041
    z = _R(zenith)
    delta = dhi * am / dni_extra
    eps = ((dhi + dni) / dhi + kappa * z ** 3) / (1 + kappa * z ** 3)
    ebin = 0
    for edge in _PEREZ_EPS_EDGES:
        if eps >= edge:
            ebin += 1
    f = _PEREZ_1990[ebin]
    f1 = max(f[0] + f[1] * delta + f[2] * z, 0.0)
    f2 = f[3] + f[4] * delta + f[5] * z
    a = max(aoi_projection(tilt, surf_az, zenith, sun_az), 0.0)
    b = max(math.cos(z), math.cos(_R(85)))
    term1 = 0.5 * (1 - f1) * (1 + math.cos(_R(tilt)))
    term2 = f1 * a / b
    term3 = f2 * math.sin(_R(tilt))
    return max(dhi * (term1 + term2 + term3), 0.0)


def poa(ghi: float, ts: dt.datetime, lat: float, lon: float, tilt: float, surf_az: float,
        albedo: float = ALBEDO, sun: Optional[SunPos] = None) -> Poa:
    """Spec v0.7 step 1: station GHI -> G_poa of one plane (Erbs + Perez)."""
    ghi = max(0.0, float(ghi))                       # spec: GHI below zero -> 0
    sp = sun or sun_position(ts, lat, lon)
    ea = extra_radiation(ts)
    dni, dhi, _ = erbs(ghi, sp.zenith, ea)
    sky = perez_sky_diffuse(tilt, surf_az, dhi, dni, ea, sp.zenith, sp.azimuth)
    ground = ghi * albedo * (1 - math.cos(_R(tilt))) * 0.5
    direct = max(dni * aoi_projection(tilt, surf_az, sp.zenith, sp.azimuth), 0.0)
    return Poa(direct + sky + ground, direct, sky, ground, dni, dhi)
