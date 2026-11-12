"""v371 - swap the HelioScope design on a string-layout page (Tomasz, 12 Nov 2026: "have you replaced the
helioscope design inside monitoring for each plant drawing?").

Only the HelioScope view changes; the installation drawing, its strings and areas stay as built. The
registration is the v318 one, moved to tools/helio_layout.py. All layouts and pictures here are invented.
"""
from __future__ import annotations

import copy
import pathlib
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "tools"))

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")
pytest.importorskip("PIL")

import helio_layout as HL          # noqa: E402
import string_layout_helio as SH   # noqa: E402

# a drawing in PDF points: two module blocks of two inverters, box 0..200 x 0..100, rendered 400 px wide
RECTS = {"INV1": [[20.0, 20.0, 60.0, 25.0]], "INV2": [[110.0, 50.0, 70.0, 30.0]]}


def layout(with_old_helio=True):
    doc = {"plant": "DEMO1", "version": 2,
           "drawings": [{"id": "drawing", "kind": "drawing", "image": "DEMO1_drawing.jpg", "box": [0, 0, 200, 100],
                         "px": [400, 200]}],
           "strings": [{"sn": "SN1", "ch": "s1", "inv": "INV1", "pos": [{"d": "drawing", "x": 50.0, "y": 32.0, "cad": 1}]},
                       {"sn": "SN2", "ch": "s1", "inv": "INV2", "pos": [{"d": "drawing", "x": 145.0, "y": 65.0, "cad": 1}]}],
           "areas": [{"sn": f"SN{i}", "inv": inv, "d": "drawing", "rects": r} for i, (inv, r) in enumerate(RECTS.items(), 1)],
           "checks": {"inverters": {"workbook": 2, "drawing": 2, "helioscope": 9},
                      "strings": {"workbook": 2, "drawing_labels": 2, "helioscope": 9}},
           "notes": [["A plant note.", "Una nota."]]}
    if with_old_helio:
        doc["drawings"].append({"id": "helio", "kind": "helioscope", "src": "old design", "markers": False})
        doc["areas"].append({"sn": "SN1", "inv": "INV1", "d": "helio", "polys": [[0] * 8]})
        doc["strings"][0]["pos"].append({"d": "helio", "x": 1.0, "y": 1.0, "cad": 1})
        doc["notes"].append([HL.MISFIT_EN + " (overlap 30%)", HL.MISFIT_ES])
    return doc


def helio_picture(offset=(300, 200), scale=3.0, blocks=RECTS):
    """A satellite-like grey picture with the design's modules in HelioScope blue, scaled and shifted."""
    img = np.full((900, 1200, 3), 120, np.uint8)
    for rs in blocks.values():
        for x, y, w, h in rs:
            x0, y0 = int(offset[0] + x * scale), int(offset[1] + y * scale)
            img[y0:y0 + int(h * scale), x0:x0 + int(w * scale)] = (230, 60, 50)      # BGR blue
    return img


class TestStrip:
    def test_everything_helio_goes_the_drawing_stays(self):
        d = SH.strip_helio(layout())
        assert [x["id"] for x in d["drawings"]] == ["drawing"]
        assert all(a["d"] == "drawing" for a in d["areas"]) and len(d["areas"]) == 2
        assert all(p["d"] == "drawing" for s in d["strings"] for p in s["pos"])
        assert d["notes"] == [["A plant note.", "Una nota."]]

    def test_input_is_not_changed(self):
        doc = layout()
        before = copy.deepcopy(doc)
        SH.strip_helio(doc)
        assert doc == before


class TestDrawingInputs:
    def test_rects_back_to_shapes(self):
        shapes, box, px, areas = SH.drawing_inputs(SH.strip_helio(layout()))
        assert shapes == [(20.0, 20.0, 80.0, 45.0, "INV1"), (110.0, 50.0, 180.0, 80.0, "INV2")]
        assert (box, px, len(areas)) == ([0, 0, 200, 100], 400, 2)

    def test_no_module_areas_is_refused(self):
        d = SH.strip_helio(layout())
        d["areas"] = []
        with pytest.raises(ValueError, match="no drawing module areas"):
            SH.drawing_inputs(d)


class TestSwap:
    def test_matching_design_gets_markers_on_its_modules(self, tmp_path):
        new = SH.swap(layout(), helio_picture(), {"inverters": 2, "strings": 2}, "HelioScope 'demo' (Nov 2026)", str(tmp_path))
        h = [x for x in new["drawings"] if x["kind"] == "helioscope"]
        assert len(h) == 1 and h[0]["markers"] and h[0]["src"] == "HelioScope 'demo' (Nov 2026)"
        assert (tmp_path / "DEMO1_helio.jpg").exists()
        hp = [p for s in new["strings"] for p in s["pos"] if p["d"] == "helio"]
        assert len(hp) == 2 and (1.0, 1.0) not in [(p["x"], p["y"]) for p in hp]        # the old position is gone
        # drawing px = 2 x points; the picture holds the design at 3 x points + (300, 200)
        assert hp[0]["x"] == pytest.approx(300 + 50 * 3, abs=6) and hp[0]["y"] == pytest.approx(200 + 32 * 3, abs=6)
        assert hp[1]["x"] == pytest.approx(300 + 145 * 3, abs=6) and hp[1]["y"] == pytest.approx(200 + 65 * 3, abs=6)
        assert new["checks"]["strings"]["helioscope"] == 2 and new["checks"]["inverters"]["helioscope"] == 2
        assert not any(str(n[0]).startswith(HL.MISFIT_EN) for n in new["notes"])
        assert sorted(a["inv"] for a in new["areas"] if a["d"] == "helio") == ["INV1", "INV2"]

    def test_design_that_does_not_line_up_says_so(self, tmp_path):
        other = {"INV1": [[10.0, 70.0, 20.0, 20.0]], "INV2": [[150.0, 5.0, 40.0, 10.0]], "X": [[90.0, 40.0, 15.0, 15.0]]}
        new = SH.swap(layout(False), helio_picture(blocks=other), {"inverters": 2, "strings": None}, "demo", str(tmp_path))
        h = next(x for x in new["drawings"] if x["kind"] == "helioscope")
        assert not h["markers"]
        assert not [p for s in new["strings"] for p in s["pos"] if p["d"] == "helio"]
        assert [n for n in new["notes"] if n[0].startswith(HL.MISFIT_EN)]
        assert new["checks"]["strings"]["helioscope"] is None

    def test_drawing_side_is_untouched(self, tmp_path):
        doc = layout()
        new = SH.swap(doc, helio_picture(), {"inverters": 2, "strings": 2}, "demo", str(tmp_path))
        assert [a for a in new["areas"] if a["d"] == "drawing"] == [a for a in doc["areas"] if a["d"] == "drawing"]
        assert [[p for p in s["pos"] if p["d"] == "drawing"] for s in new["strings"]] == \
               [[p for p in s["pos"] if p["d"] == "drawing"] for s in doc["strings"]]


class TestBuilderUsesTheSharedCode:
    def test_one_implementation(self):
        src = (V2 / "tools" / "string_layout_build.py").read_text(encoding="utf-8")
        assert "def ecc_reg" not in src and "def block_match" not in src
        assert "HL.place(" in src and "HL.helio_image(" in src
