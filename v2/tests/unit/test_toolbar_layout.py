"""Toolbar layout, portal feedback 2026-08-28 (items 2 and 3).

  2. "the button reports should be last not first" — it leaves the
     monitoring portal, so it sits at the end of the row beside the
     user menu rather than ahead of the views the page is made of.
  3. "the buttons on each page are too busy ... full range is useless,
     also last 30 days is useless so remove them" — nine loose controls
     became two tight groups (dates, then shortcuts as one segmented
     control) with live monitoring pushed to the far edge.

Note on the two dropped presets: only the BUTTONS go.  preset('30d')
stays in the JS because it is what every plant page opens on, and the
'all' branch stays as the fall-through.  Removing either would change
what the page shows, not just what it offers.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REP = (ROOT / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
MON = (ROOT / "server" / "monitoring_gen.py").read_text(encoding="utf-8")


def code(src):
    """Source with Python comment lines dropped — a comment naming a
    removed button is documentation, not a button."""
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


REP_CODE = code(REP)


def controls_block():
    i = MON.index("def controls(")
    return MON[i:MON.index("\ndef ", i + 10)]


class TestReportsGoesLast:
    def test_it_is_still_there(self):
        b = controls_block()
        assert 'href="/"' in b and "Reports" in b

    def test_it_comes_after_every_monitoring_view(self):
        b = controls_block()
        r = b.index("Reports</a>")
        for view in ("Fleet</a>", "All PPA</a>", "CAPEX</a>",
                     "Performance</a>", "Reconciliation</a>"):
            assert b.index(view) < r, f"{view} must precede Reports"

    def test_it_stays_left_of_the_user_menu(self):
        b = controls_block()
        assert b.index("Reports</a>") < b.index('class="usermenu"')

    def test_the_extra_slot_still_renders_before_it(self):
        """Per-page buttons are passed in as `extra`; they are part of
        this portal, so they too come before the way out."""
        b = controls_block()
        assert b.index("{extra}") < b.index("Reports</a>")


class TestDeadPresetsRemoved:
    def test_no_last_30_days_button(self):
        assert "Last 30 days" not in REP_CODE
        assert "Últimos 30 días" not in REP_CODE

    def test_no_full_range_button(self):
        assert "Full range" not in REP_CODE
        assert "Rango completo" not in REP_CODE

    def test_the_default_view_is_still_thirty_days(self):
        """The button is gone; the behaviour it named is not."""
        assert "preset('30d')" in REP
        assert "if(w==='30d')" in REP

    def test_the_full_range_branch_survives_as_the_fallback(self):
        i = REP.index("function preset(w){")
        assert "else{d0=D[0];}" in REP[i:i + 700]

    def test_every_remaining_preset_has_a_js_branch(self):
        """A button calling a branch that does not exist silently
        shows the fall-through range instead."""
        for i in (REP.index("function preset(w){"),
                  REP.index("function preset(w){{")):
            js = REP[i:i + 900]
            for w in ("mtd", "prev", "ytd"):
                assert f"w==='{w}'" in js, f"{w} missing near offset {i}"


class TestGroupedLayout:
    def test_both_range_bars_are_grouped(self):
        assert REP.count('class="controls rangebar noprint"') == 2

    def test_each_bar_has_a_date_group_and_a_segment_group(self):
        for m in re.finditer(r'class="controls rangebar noprint"', REP):
            bar = REP[m.start():REP.index("</div>\n", m.start()) + 700]
            assert bar.count('class="rgroup"') >= 1
            assert 'class="rgroup seg"' in bar

    def test_the_segment_group_holds_exactly_the_three_presets(self):
        for m in re.finditer(r'class="rgroup seg"', REP):
            seg = REP[m.start():REP.index("</div>", m.start())]
            assert seg.count("<button") == 3

    def test_live_monitoring_is_pushed_to_the_far_edge(self):
        assert 'class="btn live"' in REP
        assert ".rangebar .live{margin-left:auto;}" in REP

    def test_it_does_not_float_away_on_a_phone(self):
        assert "@media (max-width:720px){.rangebar .live{margin-left:0;}}" in REP

    def test_the_segmented_control_reads_as_one_control(self):
        for rule in (".seg .btn{border-radius:0;margin-left:-1px;}",
                     ".seg .btn:first-child{",
                     ".seg .btn:last-child{"):
            assert rule in REP
