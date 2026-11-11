"""Solar site positions and pictures placed by hand in the portal (v369).

Tomasz, 11 Nov 2026, with Prologis's PM package: "try locating these plants and adding google maps or google earth
pictures of these installations if possible, if there is no accurate pin please leave window where we can place pin
and save the picture manually".

* A pin dragged on the site page is kept in ``site_pins`` (the platform's SQLite) and laid over registry.json at
  load time, so a new registry file does not lose it. Every move and reset is in the audit log.
* The site picture (PHOTO_DIR/<CODE>.jpg) is a satellite view at the pin, fetched by the server - Google Static
  Maps when the server has a key, else Esri World Imagery - or a JPEG/PNG uploaded by hand.

Pure helpers + small SQL functions; the network call is ``fetch_satellite`` only (injectable opener for tests).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Callable, Dict, Optional, Tuple

from argia.prologis.lightops import MAX_MOVE_KM, MX_BOX, Invalid, _audit, _km, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS site_pins (
  code TEXT PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL, username TEXT NOT NULL, ts_utc TEXT NOT NULL);
"""
MIN_PHOTO = 4 * 1024                 # a real satellite picture; an error page or a blank tile is smaller
MAX_PHOTO = 12 * 1024 * 1024


def ensure(c: sqlite3.Connection) -> None:
    c.executescript(SCHEMA)
    c.commit()


def pins(c: sqlite3.Connection) -> Dict[str, Dict]:
    return {r["code"]: dict(r) for r in c.execute("SELECT * FROM site_pins")}


def set_pin(c: sqlite3.Connection, code: str, lat, lon, ref: Optional[Tuple[float, float]], user: str,
            ip: str = "") -> Tuple[float, float]:
    """Store the building's position; ``ref`` is the registry's point (the pin may not wander off to another city)."""
    try:
        lat, lon = round(float(lat), 6), round(float(lon), 6)
    except (TypeError, ValueError):
        raise Invalid("position missing")
    if not (MX_BOX[0] <= lat <= MX_BOX[1] and MX_BOX[2] <= lon <= MX_BOX[3]):
        raise Invalid("the position is not in Mexico")
    if ref is not None and _km(ref[0], ref[1], lat, lon) > MAX_MOVE_KM:
        raise Invalid(f"the pin moved more than {MAX_MOVE_KM:.0f} km from the recorded site")
    old = c.execute("SELECT lat, lon FROM site_pins WHERE code=?", (code,)).fetchone()
    was = f"{old['lat']},{old['lon']}" if old else (f"{ref[0]},{ref[1]} (registry)" if ref else "none")
    c.execute("INSERT INTO site_pins (code, lat, lon, username, ts_utc) VALUES (?,?,?,?,?) "
              "ON CONFLICT(code) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, username=excluded.username, "
              "ts_utc=excluded.ts_utc", (code, lat, lon, user, now_utc()))
    _audit(c, user, ip, "site_pin", code, f"{lat},{lon} was {was}")
    c.commit()
    return lat, lon


def clear_pin(c: sqlite3.Connection, code: str, user: str, ip: str = "") -> bool:
    old = c.execute("SELECT lat, lon FROM site_pins WHERE code=?", (code,)).fetchone()
    if not old:
        return False
    c.execute("DELETE FROM site_pins WHERE code=?", (code,))
    _audit(c, user, ip, "site_pin_reset", code, f"was {old['lat']},{old['lon']}")
    c.commit()
    return True


# ------------------------------------------------------------------ pictures
def picture_kind(data: bytes) -> Optional[str]:
    """'jpg' / 'png' by the file's own bytes, else None. Pure."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    return None


def as_jpeg(data: bytes) -> bytes:
    """The picture as JPEG bytes (PNG converted with Pillow when it is installed); refused when it is not a
    picture of a sensible size."""
    if not MIN_PHOTO <= len(data) <= MAX_PHOTO:
        raise Invalid("the picture is empty or larger than 12 MB")
    kind = picture_kind(data)
    if kind == "jpg":
        return data
    if kind == "png":
        try:
            import io
            from PIL import Image
        except ImportError:
            raise Invalid("PNG pictures need Pillow on the server - upload a JPEG")
        im = Image.open(io.BytesIO(data)).convert("RGB")
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88)
        return out.getvalue()
    raise Invalid("only JPEG or PNG pictures")


def google_static_url(lat: float, lon: float, key: str, w: int = 640, h: int = 400, zoom: int = 19) -> str:
    return (f"https://maps.googleapis.com/maps/api/staticmap?center={lat:.6f},{lon:.6f}&zoom={zoom}"
            f"&size={w}x{h}&scale=2&maptype=satellite&format=jpg&key={key}")


def esri_url(lat: float, lon: float, w: int = 1280, h: int = 800, half_m: float = 120) -> str:
    from argia.prologis.lighting import satellite_url
    return satellite_url(lat, lon, w, h, half_m=half_m)


def fetch_satellite(lat: float, lon: float, gkey: str = "", referer: str = "https://prologis.argia.com.mx/",
                    opener: Optional[Callable[[str, Dict[str, str]], bytes]] = None) -> Tuple[bytes, str]:
    """(JPEG bytes, source) of the satellite view centred on the point: Google Static Maps when there is a key
    (the key is restricted to the portal's referrer, so the request carries it), else - or when Google answers
    with something that is not a picture - Esri World Imagery."""
    def _get(url: str, headers: Dict[str, str]) -> bytes:
        import urllib.request
        req = urllib.request.Request(url, headers=dict({"User-Agent": "ARGIA-portal/1.0"}, **headers))
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read(MAX_PHOTO + 1)
    get = opener or _get
    errors = []
    tries = ([("Google Maps", google_static_url(lat, lon, gkey), {"Referer": referer})] if gkey else []) + \
        [("Esri World Imagery", esri_url(lat, lon), {})]
    for source, url, headers in tries:
        try:
            return as_jpeg(get(url, headers)), source
        except Exception as ex:                        # noqa: BLE001 - try the next source, report all at the end
            errors.append(f"{source}: {str(ex)[:120]}")
    raise Invalid("no satellite picture: " + "; ".join(errors))


def save_photo(photo_dir: str, code: str, data: bytes) -> str:
    """Write PHOTO_DIR/<CODE>.jpg atomically (0600); returns the path."""
    import re
    if not re.match(r"^[A-Za-z0-9_-]{1,40}$", code):
        raise Invalid("bad site code")
    os.makedirs(photo_dir, mode=0o700, exist_ok=True)
    path = os.path.join(photo_dir, code + ".jpg")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path
