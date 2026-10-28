"""v332 - demo.argia.com.mx/savings/: the solar + battery savings video.

Tomasz, 28 Oct 2026: the two 40-second videos (EN and ES) on the demo, so
sales people can show in meetings how a battery and solar generate savings.
The pure parts are pinned here; the end-to-end run is in
tests/portal/test_demo_site.py.

* the video files are server-only and only savings_<en|es>.<mp4|jpg> is
  copied (nothing else from that folder, no symlink);
* the page plays the video of the page language, can switch the video
  language on its own, and still works with one language only;
* chapters match the cut of the videos (measured: 0, 5, 10, 18, 22, 29, 32 s);
* no video on the server = a plain "being prepared" note, no broken player.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
import demo_gen as DG                                      # noqa: E402

EM = chr(0x2014)


def _pg():
    """The three things savings_page / savings_card use from portal_gen."""
    return SimpleNamespace(
        C=SimpleNamespace(page=lambda title, body, section=None, *a, **k: f"<title>{title}</title><sec>{section}</sec>{body}"),
        t=lambda en, es: f'<span data-en="{en}" data-es="{es}">{en}</span>',
        ico=lambda name, *a, **k: f'<svg data-i="{name}"></svg>')


def _videos(root, names):
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / n).write_bytes(b"\x00\x00\x00\x18ftypisom" + n.encode())
    return root


class TestCopy:
    def test_only_the_savings_files_are_copied(self, tmp_path):
        src = _videos(tmp_path / "videos", ["savings_en.mp4", "savings_es.mp4", "savings_en.jpg", "savings_es.jpg",
                                            "other.mp4", "savings_fr.mp4", "savings_en.mp4.part", "notes.txt"])
        os.symlink("/etc/passwd", src / "savings_es.jpg.lnk")
        out = tmp_path / "www"
        assert DG.copy_videos(str(src), str(out)) == 4
        got = sorted(p.name for p in (out / "assets" / "video").iterdir())
        assert got == ["savings_en.jpg", "savings_en.mp4", "savings_es.jpg", "savings_es.mp4"]
        assert (out / "assets/video/savings_es.mp4").read_bytes() == (src / "savings_es.mp4").read_bytes()

    def test_a_symlink_named_like_a_video_is_refused(self, tmp_path):
        src = tmp_path / "videos"
        src.mkdir()
        os.symlink("/etc/hostname", src / "savings_en.mp4")
        assert DG.copy_videos(str(src), str(tmp_path / "www")) == 0

    def test_no_folder_no_files_no_crash(self, tmp_path):
        assert DG.copy_videos(str(tmp_path / "missing"), str(tmp_path / "www")) == 0
        assert DG.video_files(str(tmp_path / "www")) == {}

    def test_video_files_lists_what_was_copied(self, tmp_path):
        out = tmp_path / "www"
        _videos(out / "assets" / "video", ["savings_es.mp4", "savings_es.jpg", "savings_en.jpg"])
        assert DG.video_files(str(out)) == {"es": {"mp4": "/assets/video/savings_es.mp4", "jpg": "/assets/video/savings_es.jpg"}}

    @pytest.mark.parametrize("have,lang,kind,want", [
        ({"en": {"mp4": "E", "jpg": ""}, "es": {"mp4": "S", "jpg": "SJ"}}, "en", "jpg", "SJ"),   # no EN poster: the ES one
        ({"en": {"mp4": "E", "jpg": "EJ"}}, "es", "mp4", "E"),                                   # no ES video: the EN one
        ({}, "en", "mp4", ""),
    ])
    def test_pick_falls_back_to_the_other_language(self, have, lang, kind, want):
        assert DG._pick(have, lang, kind) == want


BOTH = {"en": {"mp4": "/assets/video/savings_en.mp4", "jpg": "/assets/video/savings_en.jpg"},
        "es": {"mp4": "/assets/video/savings_es.mp4", "jpg": "/assets/video/savings_es.jpg"}}


class TestPage:
    def test_both_languages(self):
        s = DG.savings_page(_pg(), BOTH)
        assert "<sec>savings</sec>" in s
        assert 'data-src-en="/assets/video/savings_en.mp4"' in s and 'data-src-es="/assets/video/savings_es.mp4"' in s
        assert '<source src="/assets/video/savings_en.mp4" type="video/mp4">' in s
        assert 'poster="/assets/video/savings_en.jpg"' in s
        assert "playsinline" in s and "controls" in s
        assert 'class="vlang" data-l="en">EN<' in s and 'class="vlang" data-l="es">ES<' in s
        assert "disabled" not in s

    def test_one_language_only_still_plays_and_greys_the_other(self):
        s = DG.savings_page(_pg(), {"es": BOTH["es"]})
        assert '<source src="/assets/video/savings_es.mp4"' in s
        assert 'data-src-en="/assets/video/savings_es.mp4"' in s           # EN page plays the ES video
        assert 'data-l="en" disabled>EN<' in s and 'data-l="es">ES<' in s

    def test_no_video_says_it_is_being_prepared(self):
        s = DG.savings_page(_pg(), {})
        assert 'id="savings-missing"' in s and "being prepared" in s and "en preparación" in s
        assert "<video" not in s and "<script" not in s

    def test_seven_chapters_at_the_measured_cuts(self):
        assert [c[0] for c in DG.SAVINGS_CHAPTERS] == [0, 5, 10, 18, 22, 29, 32]
        s = DG.savings_page(_pg(), BOTH)
        assert re.findall(r'<span class="tm">(0:\d\d)</span>', s) == ["0:00", "0:05", "0:10", "0:18", "0:22", "0:29", "0:32"]
        assert 'data-chapters="[0, 5, 10, 18, 22, 29, 32]"' in s
        assert "Annual savings with ARGIA" in s and "Ahorro anual con ARGIA" in s

    def test_presenting_controls(self):
        s = DG.savings_page(_pg(), BOTH)
        for el in ('id="vplay"', 'id="vfull"', 'id="vrestart"', 'id="vloop"', "requestFullscreen", "webkitEnterFullscreen",
                   "ArrowRight", "ArrowLeft", "argia:lang"):
            assert el in s, el

    def test_bilingual_numbers_marked_illustrative_no_em_dash(self):
        s = DG.savings_page(_pg(), BOTH)
        assert EM not in s and EM not in DG.DEMO_CSS and EM not in DG.DEMO_JS
        for en, es in (("43%", "43%"), ("MXN 3.83 M", "3.83 M MXN"), ("600 → 430 kW", "600 → 430 kW")):
            assert f'data-en="{en}" data-es="{es}"' in s
        assert "Illustrative simulation" in s and "Simulación ilustrativa" in s and "not current CFE rates" in s

    def test_the_video_language_buttons_are_not_the_page_language_buttons(self):
        """test_demo_site counts exactly two class="lang-btn" per page (the header switch)."""
        assert 'lang-btn' not in DG.savings_page(_pg(), BOTH)


class TestCard:
    def test_card_with_poster_following_the_language(self):
        s = DG.savings_card(_pg(), BOTH)
        assert 'href="/savings/" id="feature-savings"' in s
        assert 'data-src-en="/assets/video/savings_en.jpg" data-src-es="/assets/video/savings_es.jpg"' in s
        assert "EN / ES" in s

    def test_no_video_no_card(self):
        assert DG.savings_card(_pg(), {}) == ""

    def test_the_language_hook_swaps_only_images_and_keeps_set_lang(self):
        js = DG.DEMO_JS
        assert "img[data-src-en]" in js and "base(l,s)" in js and "argia:lang" in js

    def test_tab_title(self):
        assert DG.demo_title("savings/index.html", "<title>x</title>") == "<title>DEMO - Solar + Battery - ARGIA</title>"
