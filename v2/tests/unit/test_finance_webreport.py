"""Web financial report tests.

The core guarantee: summing the embedded daily atoms over a range must
reproduce the PDF report builder's totals for the same period — that is
the anti-divergence contract the whole page is built on. Fixtures are
the same seed CSVs + synthetic KPI rows the report tests use.
"""

import pytest

from argia.finance.income import Period
from argia.finance.report import build_finance_report_data
from argia.finance.webreport import (
    build_daily_atoms, render_financial_report_html,
)
from tests.unit.test_finance_report import (
    JULY_MTD_ENERGY, _portfolio, _sheets,
)

WINDOW = Period.from_iso("2026-07-01", "2026-08-31")
JULY_FULL = Period.from_iso("2026-07-01", "2026-07-31")
JULY_MTD = Period.from_iso("2026-07-01", "2026-07-07")


def _atoms(om=8000.0):
    return build_daily_atoms(_sheets(), _portfolio(om), WINDOW)


def _sum_atoms(data, pk, start_iso, end_iso):
    i0 = data["days"].index(start_iso)
    i1 = data["days"].index(end_iso)
    rev = exp = svc = om = 0.0
    rev_any = False
    for i in range(i0, i1 + 1):
        r, e, s, o = data["atoms"][pk][i]
        if r is not None:
            rev += r
            rev_any = True
        if e is not None:
            exp += e
        svc += s
        om += o
    return (rev if rev_any else None), exp, svc, om


class TestAtomsReconcileWithPdfBuilder:
    def test_full_july_totals_match(self):
        data = _atoms()
        ref = build_finance_report_data(_sheets(), _portfolio(),
                                        JULY_FULL)
        for asset in ref.assets:
            rev, exp, svc, om = _sum_atoms(data, asset.plant_key,
                                           "2026-07-01", "2026-07-31")
            assert exp == pytest.approx(asset.expected_mxn, abs=0.5), \
                asset.plant_key
            assert svc == pytest.approx(asset.service_mxn, abs=0.5), \
                asset.plant_key
            assert om == pytest.approx(asset.om_mxn, abs=0.5)

    def test_mtd_actuals_match(self):
        data = _atoms()
        ref = build_finance_report_data(_sheets(), _portfolio(), JULY_MTD)
        for asset in ref.assets:
            rev, _, svc, _ = _sum_atoms(data, asset.plant_key,
                                        "2026-07-01", "2026-07-07")
            if asset.actual_mxn is None:
                assert rev is None, asset.plant_key
            else:
                assert rev == pytest.approx(asset.actual_mxn, abs=0.5), \
                    asset.plant_key
            assert svc == pytest.approx(asset.service_mxn, abs=0.5)

    def test_days_without_kpi_have_null_ppa_revenue(self):
        data = _atoms()
        i = data["days"].index("2026-07-20")   # no KPI fixture there
        for pk in JULY_MTD_ENERGY:
            assert data["atoms"][pk][i][0] is None
        # but expected/service atoms still exist for planning
        assert data["atoms"]["GTO1"][i][1] is not None
        assert data["atoms"]["GTO1"][i][2] > 0

    def test_laas_revenue_accrues_every_day(self):
        data = _atoms()
        i = data["days"].index("2026-07-20")
        assert data["atoms"]["LOAX1"][i][0] == pytest.approx(
            26750 * 17.98 / 31, abs=0.01)

    def test_last_actual_day(self):
        assert _atoms()["last_actual_day"] == "2026-07-07"

    def test_service_atoms_zero_when_month_has_no_installment(self):
        # window extends into 2033 for nobody; use a plant/month check:
        # SLP1-L1 ended 2026-05; only L2 pays in the window
        data = _atoms()
        i = data["days"].index("2026-07-15")
        assert data["atoms"]["SLP1"][i][2] == pytest.approx(
            12500.0 / 31, abs=0.01)


class TestRenderer:
    def _html(self, om=8000.0):
        return render_financial_report_html(_atoms(om),
                                            generated_at="2026-07-09")

    def test_selfcontained_page_with_picker_and_logo(self):
        h = self._html()
        assert '<input type="date" id="from"' in h
        assert '<input type="date" id="to"' in h
        assert "data:image/png;base64," in h
        assert "no financial logic runs in the browser" in \
            " ".join(h.split())

    def test_atoms_embedded_as_json(self):
        h = self._html()
        assert '"atoms"' in h and '"last_actual_day":"2026-07-07"' in h

    def test_footer_from_provenance_registry(self):
        h = self._html()
        assert "projection, not a commitment" in h
        assert "principal+interest combined" in h

    def test_om_missing_flag_travels_to_client(self):
        h = render_financial_report_html(_atoms(om=None),
                                         generated_at="2026-07-09")
        assert '"om_missing":true' in h


class TestApprovedDesign:
    """Pins the 2026-07-09 approved restyle: one design system with the
    performance dashboard."""

    def _html(self):
        return render_financial_report_html(_atoms(),
                                            generated_at="2026-07-09")

    def test_dashboard_design_tokens(self):
        h = self._html()
        assert "background: #f4f3ef" in h          # dashboard page bg
        assert '"Segoe UI", Roboto' in h           # same font stack
        assert "letter-spacing:3.5px" in h         # letterspaced title

    def test_title_left_logo_right(self):
        h = self._html()
        title = h.index("FINANCIAL&nbsp;REPORT")
        logo = h.index("data:image/png;base64,")
        assert title < logo    # title markup precedes logo in the header

    def test_audit_is_collapsed_details(self):
        h = self._html()
        assert '<details class="audit">' in h
        assert "Data sources &amp; audit" in h
        # provenance still lives inside it (registry drift-guard)
        body_start = h.index('<details class="audit">')
        assert h.index("principal+interest combined") > body_start

    def test_stat_cards_present(self):
        h = self._html()
        for card_id in ("c_exp", "c_act", "c_net", "c_de", "c_da"):
            assert 'id="%s"' % card_id in h


class TestEmbeddedJsIsValid:
    """Regression for 2026-07-09: an unescaped apostrophe inside a
    single-quoted JS string killed the entire script at parse time and
    the published page rendered empty. Where node is available, the
    embedded script must pass a real syntax check; the pure-Python
    guard below runs everywhere."""

    def _script(self):
        import re
        h = render_financial_report_html(_atoms(), generated_at="t")
        return re.search(r"<script>(.*)</script>", h, re.S).group(1)

    def test_script_passes_node_syntax_check(self, tmp_path):
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            import pytest as _pytest
            _pytest.skip("node not available on this machine")
        js = tmp_path / "embedded.js"
        js.write_text(self._script())
        res = subprocess.run([node, "--check", str(js)],
                             capture_output=True, text=True)
        assert res.returncode == 0, res.stderr

    def test_logo_is_the_dashboards(self):
        from argia.report.dashboard_html import LOGO_B64
        h = render_financial_report_html(_atoms(), generated_at="t")
        assert LOGO_B64[:60] in h


def test_dscr_definition_in_web_audit():
    """Same guarantee as the PDF: the collapsed audit block explains
    that portfolio DSCR is the debt-weighted aggregate."""
    h = render_financial_report_html(_atoms(), generated_at="t")
    flat = " ".join(h.split())
    assert "Σ revenue ÷ Σ debt service" in flat
    assert "NOT an average of per-asset ratios" in flat
