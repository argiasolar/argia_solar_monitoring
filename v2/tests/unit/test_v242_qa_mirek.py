"""v242 — Mirek's portal QA (2026-09-08), the items we agreed with.

B  PR / availability on the monitoring performance page came from the
   days that had a value (Ryder: PR 0.996, PR_STC 1.084, 100% available
   from ONE day while dark for six) -> perf_summary, pure and tested.
C  reconciliation notes were cut at 60 characters on the server.
D  the financial report's default window now sits in the HTML too.
E  the overview's revenue is PPA + LaaS; the tile says the split.
F  open months first, the archive collapsed, a filter, CSV export.
G  the four checks explained on the page.
A  (disagreed on the premise, agreed on the wording) the close note says
   when a PASS rests on the vendor alone.
"""
from __future__ import annotations

import pathlib

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
MON = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
RG = (V2 / "server/bundle/report_gen.py").read_text(encoding="utf-8")
PG = (V2 / "server/bundle/portal_gen.py").read_text(encoding="utf-8")


def _mon_ns():
    """Execute the pure pieces of monitoring_gen (it queries PG at import)."""
    import html
    ns = {"html": html, "PLANTS": {"TAM1": {}, "NL1": {}}}
    seg = MON[MON.index("def f(s):"):MON.index("def f(s):") + 200]
    exec(compile(seg[:seg.index("\n\n\n")], "mon_f", "exec"), ns)
    seg = MON[MON.index("def esc(s):"):]
    exec(compile(seg[:seg.index("\n\n\n")], "mon_esc", "exec"), ns)
    seg = MON[MON.index("MIN_PR_DAYS = 7"):MON.index("FIRST_DATES = ")]
    exec(compile(seg, "mon_perf", "exec"), ns)
    seg = MON[MON.index("def recon_split"):MON.index("def _recon_m_row")]
    exec(compile(seg, "mon_recon", "exec"), ns)
    return ns


def _rows(pk, days, pr=None, pr_stc=None, av=None, energy=None, exp=None):
    out = []
    for i, d in enumerate(days):
        g = lambda v: "" if v is None else ("" if v[i] is None else str(v[i]))
        out.append((pk, d, g(pr), g(pr_stc), g(av), g(energy), g(exp)))
    return out


def _days(first, n):
    import datetime as dt
    d = dt.date.fromisoformat(first)
    return [(d + dt.timedelta(days=i)).isoformat() for i in range(n)]


class TestPerfSummary:
    TODAY = "2026-09-08"

    def test_one_day_is_not_a_30_day_pr(self):
        ns = _mon_ns()
        days = _days("2026-08-09", 30)
        pr = [None] * 23 + [0.9961] + [None] * 6
        av = [None] * 23 + [1.0] + [None] * 6
        energy = [1000.0] * 24 + [0.0] * 6
        rows = _rows("TAM1", days, pr=pr, pr_stc=pr, av=av, energy=energy)
        p = ns["perf_summary"](rows, self.TODAY, {"TAM1": "2026-05-13"})["TAM1"]
        assert p["pr"] is None and p["pr_stc"] is None and p["pr_days"] == 1
        # 1 measured day + 6 dark rows -> 1/7
        assert p["avail"] == pytest.approx(1 / 7, abs=1e-3)
        assert p["dark_days"] == 6

    def test_pr_above_cap_is_an_input_error(self):
        ns = _mon_ns()
        days = _days("2026-08-09", 30)
        pr = [0.80] * 20 + [1.35] + [0.80] * 9
        rows = _rows("GTO2", days, pr=pr, av=[1.0] * 30, energy=[100.0] * 30)
        p = ns["perf_summary"](rows, self.TODAY, {"GTO2": "2026-07-01"})["GTO2"]
        assert p["pr"] == 0.8 and p["pr_bad"] == 1

    def test_missing_rows_up_to_yesterday_count_as_dark(self):
        ns = _mon_ns()
        days = _days("2026-08-09", 20)                 # rows stop 2026-08-28
        rows = _rows("QRO1", days, pr=[0.8] * 20, av=[1.0] * 20, energy=[100.0] * 20)
        p = ns["perf_summary"](rows, self.TODAY, {"QRO1": "2026-07-10"})["QRO1"]
        assert p["dark_days"] == 10                    # 08-29 .. 09-07
        assert p["avail"] == pytest.approx(20 / 30, abs=1e-3)

    def test_vendor_only_days_stay_out_of_availability(self):
        ns = _mon_ns()
        days = _days("2026-08-09", 30)
        av = [None] * 29 + [1.0]
        rows = _rows("TAM1", days, av=av, energy=[1000.0] * 30)
        p = ns["perf_summary"](rows, self.TODAY, {"TAM1": "2026-05-13"})["TAM1"]
        assert p["avail"] == 1.0 and p["dark_days"] == 0

    def test_a_plant_younger_than_the_window_owes_only_its_own_days(self):
        ns = _mon_ns()
        days = _days("2026-09-01", 7)
        rows = _rows("NEW1", days, av=[1.0] * 7, energy=[10.0] * 7)
        p = ns["perf_summary"](rows, self.TODAY, {"NEW1": "2026-09-01"})["NEW1"]
        assert p["avail"] == 1.0 and p["dark_days"] == 0

    def test_healthy_plant_unchanged(self):
        ns = _mon_ns()
        days = _days("2026-08-09", 30)
        rows = _rows("NL1", days, pr=[0.78] * 30, pr_stc=[0.83] * 30, av=[0.99] * 30, energy=[3000.0] * 30, exp=[2900.0] * 30)
        p = ns["perf_summary"](rows, self.TODAY, {"NL1": "2025-03-01"})["NL1"]
        assert p == {"pr": 0.78, "pr_stc": 0.83, "avail": 0.99, "prod": 90000.0, "exp": 87000.0,
                     "pr_days": 30, "pr_bad": 0, "dark_days": 0}

    def test_wired_in(self):
        assert "PERF = perf_summary(q(" in MON
        assert "AND pr <= {PR_MAX} ORDER BY 1, 2" in MON and "PR_MAX = 1.05" in MON
        assert "a day above 1.05 is an input error" in MON
        # the report's 30-day PR applies the same cap
        assert "AND pr <= {PR_MAX} " in RG and "PR_MAX = 1.05" in RG


class TestReconPage:
    def test_open_rows_come_apart_from_the_archive(self):
        ns = _mon_ns()
        m = [("GTO2", "2026-08", "53489.9", "vendor_monthly", "FAIL", "", "counter sources disagree beyond 1.5% — investigate before billing"),
             ("MEX1", "2026-08", "82195.5", "vendor_monthly", "PASS", "auto", "CHECK1 …"),
             ("MEX1", "2026-07", "81240.8", "historical_xlsx", "PASS", "historical", "pre-v2")]
        o, c = ns["recon_split"](m)
        assert [r[0] for r in o] == ["GTO2"] and len(c) == 2

    def test_notes_are_no_longer_cut(self):
        assert "note[:60]" not in MON and "r[7][:70]" not in MON
        assert 'class="note recnote" title="{esc(note)}">{esc(note)}' in MON
        assert ".recnote{white-space:normal;max-width:520px" in MON

    def test_filter_legend_and_archive(self):
        assert 'id="rf_plant"' in MON and 'id="rf_status"' in MON and "tr[data-plant]" in MON
        assert "How the four checks work." in MON and "Cómo funcionan las cuatro verificaciones." in MON
        assert "CHECK 1 informs and never fails a close on its own" in MON
        assert "<details" in MON and "Closed and invoiced" in MON
        assert "LIMIT 400" in MON

    def test_csv_export(self):
        ns = _mon_ns()
        m = [("MEX1", "2026-08", "82195.49", "vendor_monthly", "PASS", "auto", 'note, with "quotes"')]
        d = {"MEX1": [("2026-09-07", "1", "2", "3", "100", "+0.10", "PASS", "fine")]}
        csv = ns["recon_csv"](m, d)
        lines = csv.strip().splitlines()
        assert lines[0].startswith("table,month_or_date,plant,")
        assert lines[1] == 'monthly,2026-08,MEX1,82195.49,vendor_monthly,PASS,auto,,,,,"note, with ""quotes"""'
        assert lines[2] == "daily,2026-09-07,MEX1,3,,PASS,,1,2,100,+0.10,fine"
        assert "write('monitoring/recon/reconciliation.csv', MG.recon_csv(MG.RECON_M, MG.RECON_D))" in PG
        assert 'href="reconciliation.csv" download' in MON


class TestOverviewRevenue:
    def test_tile_says_the_split(self):
        assert "def revenue_tile(on, rev, rev_ppa, rev_laas)" in PG
        assert 'PPA {rev_ppa / 1e6:,.1f} M + LaaS {rev_laas / 1e6:,.1f} M · accrued' in PG
        assert "CAPEX · client-owned, no ARGIA revenue" in PG
        assert "rev_laas = sum(a[2] for a in RG.atoms if a[1] in RG.LAAS) if on == '' else 0.0" in PG


class TestDefaultWindowInHtml:
    def test_inputs_carry_values(self):
        assert RG.count('id="d0" class="btn" value="') == 2
        assert 'id="d0" class="btn" value="{V2_START}"' in RG
        assert "V2_START = '2026-07-01'" in RG and "let w0='{V2_START}'" in RG
        assert "let w0='2026-07-01'" not in RG


class TestCloseNote:
    def test_vendor_only_pass_says_so(self):
        from argia.recon.engine import monthly_close
        # MEX1 August 2026: no lifetime register, Σ daily == monthly, 69.6% completeness
        mc = monthly_close(64866.31, 82195.49, 82195.49, None, None, 69.61, 31, 31)
        assert mc.status == "PASS"
        assert "vendor-only cross-check" in mc.note
        assert "our 5-min sampling, not the billing counters" in mc.note

    def test_lifetime_backed_pass_has_no_such_note(self):
        from argia.recon.engine import monthly_close
        mc = monthly_close(80000.0, 100000.0, 100010.0, 0.0, 100005.0, 80.0, 31, 31)
        assert mc.status == "PASS" and "vendor-only" not in mc.note


class TestCharset:
    def test_nginx_declares_utf8(self):
        conf = (V2 / "server/bundle/portal.argia.com.mx.conf").read_text(encoding="utf-8")
        assert "charset utf-8;" in conf and "text/csv" in conf
