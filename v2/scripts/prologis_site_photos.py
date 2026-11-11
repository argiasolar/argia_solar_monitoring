#!/usr/bin/env python3
"""Satellite pictures for the Prologis solar sites whose pin marks the building (v369).

    prologis_site_photos.py            # dry run: which sites would get a picture, nothing fetched or written
    prologis_site_photos.py --apply    # fetch and save the missing ones (PHOTO_DIR/<CODE>.jpg)
    prologis_site_photos.py --apply --code REY00102 --force    # one site, replacing its picture

A site whose position is still approximate (park level) gets no picture here: the portal's site page shows
"No picture yet" with the map, where a Prologis manager or ARGIA places the pin and saves the picture (Tomasz,
11 Nov 2026: "if there is no accurate pin please leave window where we can place pin and save the picture
manually"). Pins placed in the portal (site_pins) count as the building.

Environment as the portal service: ARGIA_PL_REGISTRY, ARGIA_PL_DB, ARGIA_PL_DIR (photos in <dir>/photos),
the Google key in <dir>/lighting/google_maps_key (else Esri World Imagery).
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.prologis import registry as R            # noqa: E402
from argia.prologis import sitepins as SP           # noqa: E402
from argia.prologis import store as S               # noqa: E402

DATA_DIR = os.environ.get("ARGIA_PL_DIR", "/opt/argia/prologis")
PHOTO_DIR = os.environ.get("ARGIA_PL_PHOTO_DIR", os.path.join(DATA_DIR, "photos"))
KEY_FILE = os.path.join(DATA_DIR, "lighting", "google_maps_key")


def plan(rg: R.Registry, have: set, only: str = "", force: bool = False):
    """[(site, reason)] to fetch, and [(site, reason)] skipped. Pure."""
    todo, skip = [], []
    for s in rg.sites:
        if only and s.code != only.upper():
            continue
        if s.geo_approx:
            skip.append((s, "approximate position - place the pin in the portal"))
        elif s.code in have and not force:
            skip.append((s, "has a picture"))
        else:
            todo.append((s, "pinned by hand" if s.pinned_by else "building position"))
    return todo, skip


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--code", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    c = S.connect()
    rg = R.apply_pins(R.load(), SP.pins(c))
    have = {f[:-4] for f in os.listdir(PHOTO_DIR) if f.endswith(".jpg")} if os.path.isdir(PHOTO_DIR) else set()
    todo, skip = plan(rg, have, a.code, a.force)
    try:
        key = open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        key = ""
    print(f"{len(rg.sites)} sites: {len(todo)} to fetch, {len(skip)} skipped; source: {'Google Maps' if key else 'Esri World Imagery'}"
          f"{'' if a.apply else ' (dry run)'}")
    for s, why in skip:
        print(f"  skip  {s.code:11s} {s.name[:40]:40s} {why}")
    ok = fail = 0
    for s, why in todo:
        if not a.apply:
            print(f"  would {s.code:11s} {s.name[:40]:40s} {s.lat:.6f},{s.lon:.6f} ({why})")
            continue
        try:
            data, src = SP.fetch_satellite(s.lat, s.lon, key)
            SP.save_photo(PHOTO_DIR, s.code, data)
            S.audit(c, "argia-script", "", "site_photo", s.code, f"satellite ({src}) at {s.lat},{s.lon}")
            ok += 1
            print(f"  saved {s.code:11s} {s.name[:40]:40s} {src} {len(data) // 1024} kB")
        except SP.Invalid as ex:
            fail += 1
            print(f"  FAIL  {s.code:11s} {s.name[:40]:40s} {ex}")
    if a.apply:
        print(f"saved {ok}, failed {fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
