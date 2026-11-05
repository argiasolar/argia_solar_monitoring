"""v359 - Prologis Essentials offer simulator: the money (argia/prologis/essentials.py). Invented offers only."""
from __future__ import annotations

import copy
import math

import pytest

from argia.prologis import essentials as E

RAW = {"schema": 1, "offers": [{
    "id": "O901-1", "key": "O901", "file": "MXC09901_3-PROFORMA_20260101_Acme.xlsm", "date": "2026-01-01", "code": "MXC09901",
    "park": "Parque Uno", "building": "Building 1", "tenant": "Acme",
    "areas": {"production": {"m2": 0, "rate": 6}, "warehouse": {"m2": 10000, "rate": 3}, "office": {"m2": 500, "rate": 6}},
    "lines": [{"label": "Lighting Fixtures", "mxn": 800000}, {"label": "Subcontractor Labor", "mxn": 200000}],
    "oh": 0.18, "profit": 0.09, "prologis_fee": 0.07, "fx": 18.0, "months": 60, "kwh_price": 2.0,
    "energy": {"before": {"hours": 5000, "kw": 120}, "after": {"hours": 3500, "kw": 50}},
    "total_mxn": 1000000 * 1.18 * 1.09 * 1.07}]}


def offer(**kw):
    d = copy.deepcopy(RAW)
    d["offers"][0].update(kw)
    return E.parse(d)[0]


class TestParse:
    def test_reads_the_offer(self):
        o = offer()
        assert o.direct == 1_000_000 and o.areas["warehouse"] == 10000 and o.rates["office"] == 6 and o.kwh_before == 600000

    @pytest.mark.parametrize("bad", [{"id": "X1"}, {"key": "O902"}, {"code": "mxc1"}, {"oh": 1.5}, {"months": 0},
                                     {"kwh_price": 0}, {"total_mxn": 999}, {"lines": []},
                                     {"areas": {"warehouse": {"m2": 1, "rate": 1}}},
                                     {"areas": {a: {"m2": 0, "rate": 1} for a in E.AREAS}}])
    def test_a_bad_offer_refuses_the_file(self, bad):
        with pytest.raises(E.Invalid):
            offer(**bad)

    def test_duplicate_id_and_schema(self):
        d = copy.deepcopy(RAW)
        d["offers"].append(copy.deepcopy(d["offers"][0]))
        with pytest.raises(E.Invalid):
            E.parse(d)
        with pytest.raises(E.Invalid):
            E.parse({"schema": 2, "offers": []})


class TestMoney:
    def test_investment_is_built_like_the_proforma(self):
        o = offer()
        r = E.compute(o, E.standard(o))
        assert r.oh_mxn == pytest.approx(180000) and r.profit_mxn == pytest.approx(1180000 * 0.09)
        assert r.fee_mxn == pytest.approx(1180000 * 1.09 * 0.07) and r.investment == pytest.approx(RAW["offers"][0]["total_mxn"])

    def test_fee_irr_payback(self):
        o = offer()
        r = E.compute(o, E.standard(o))
        assert r.monthly_fee == 33000 and r.fees_total == 33000 * 60
        # the annualised IRR must make the NPV of the monthly flows zero
        rm = (1 + r.irr) ** (1 / 12) - 1
        npv = -r.outlay + sum(33000 / (1 + rm) ** k for k in range(1, 61))
        assert abs(npv) < 1 and 0.15 < r.irr < 0.30 and r.payback_months == pytest.approx(r.outlay / 33000)

    def test_customer_side(self):
        o = offer()
        r = E.compute(o, E.standard(o))
        assert r.cost_before_m == pytest.approx(600000 * 2 / 12) and r.cost_after_m == pytest.approx(175000 * 2 / 12)
        assert r.gross_saving_m == pytest.approx(70833.33, abs=0.01) and r.net_saving_m == pytest.approx(70833.33 - 33000, abs=0.01)
        assert r.customer_keeps == pytest.approx(r.net_saving_m / r.gross_saving_m) and r.fee_breakeven == r.gross_saving_m

    def test_fee_for_target_irr_round_trips(self):
        o = offer()
        s = E.standard(o)
        r = E.compute(o, s)
        fee = r.fee_for_target
        assert E.annual(E.irr_monthly(r.outlay, fee, 60)) == pytest.approx(s.target_irr, abs=1e-9)

    def test_levers_move_the_right_way(self):
        o = offer()
        base = E.compute(o, E.standard(o))
        s = E.standard(o)
        cheaper = E.compute(o, E.Scenario(s.rates, 60, 0.0, 0.05, 2.0, None, 0.0))
        assert cheaper.investment < base.investment and cheaper.irr > base.irr
        longer = E.compute(o, E.Scenario(s.rates, 84, s.prologis_fee, s.profit, 2.0, None, 0.0))
        assert longer.irr > base.irr and longer.fee_for_target < base.fee_for_target
        contrib = E.compute(o, E.Scenario(s.rates, 60, s.prologis_fee, s.profit, 2.0, None, 300000))
        assert contrib.outlay == pytest.approx(base.investment - 300000) and contrib.irr > base.irr
        dearer_power = E.compute(o, E.Scenario(s.rates, 60, s.prologis_fee, s.profit, 3.0, None, 0.0))
        assert dearer_power.net_saving_m > base.net_saving_m and dearer_power.irr == base.irr

    def test_never_repaid_no_saving_and_missing_consumption(self):
        o = offer(areas={"production": {"m2": 0, "rate": 6}, "warehouse": {"m2": 100, "rate": 3}, "office": {"m2": 0, "rate": 6}})
        r = E.compute(o, E.standard(o))
        assert r.irr is None and "not_repaid" in r.warnings
        o = offer(energy={"before": {"hours": None, "kw": None}, "after": {"hours": 3500, "kw": 50}})
        r = E.compute(o, E.standard(o))
        assert r.gross_saving_m is None and "no_before" in r.warnings
        r = E.compute(o, E.Scenario(o.rates, 60, 0.07, 0.09, 2.0, 600000, 0.0))      # entered by the user
        assert r.gross_saving_m == pytest.approx(70833.33, abs=0.01) and r.warnings == ()
        o = offer(energy={"before": {"hours": 3500, "kw": 40}, "after": {"hours": 3500, "kw": 50}})
        assert "no_saving" in E.compute(o, E.standard(o)).warnings

    def test_irr_edge_cases(self):
        assert E.irr_monthly(0, 10, 60) is None and E.irr_monthly(100, 0, 60) is None
        assert E.irr_monthly(600, 10, 60) == 0.0 and E.irr_monthly(601, 10, 60) is None
        assert E.payment_for_irr(600, 0.0, 60) == pytest.approx(10)


class TestScenario:
    def test_from_args_clamps_and_keeps_defaults(self):
        o = offer()
        s = E.from_args(o, {"r_warehouse": "2.5", "m": "999", "pf": "-3", "pr": "abc", "kp": "nan", "c": "1e12", "t": "15"})
        assert s.rates == {"production": 6, "warehouse": 2.5, "office": 6} and s.months == 120 and s.prologis_fee == 0
        assert s.profit == 0.09 and s.kwh_price == 2.0 and s.contribution == 1e9 and s.target_irr == pytest.approx(0.15)
        assert E.from_args(o, {}) == E.standard(o)

    def test_args_round_trip(self):
        o = offer()
        s = E.Scenario({"production": 6, "warehouse": 2.75, "office": 5.5}, 72, 0.03, 0.06, 2.4, 650000, 50000, 0.14)
        assert E.from_args(o, E.to_args(s)) == s

    def test_suggestions_each_change_one_thing(self):
        o = offer()
        s = E.standard(o)
        sug = dict(E.suggestions(o, s))
        assert set(sug) == {"target", "share", "longer", "nofee"}
        r = E.compute(o, sug["target"])
        assert r.irr == pytest.approx(s.target_irr, abs=0.002)                         # rounding of the rates to 0.01 MXN
        assert E.compute(o, sug["share"]).customer_keeps == pytest.approx(E.CUSTOMER_SHARE, abs=0.002)
        assert sug["longer"].months == 84 and E.compute(o, sug["longer"]).irr == pytest.approx(s.target_irr, abs=0.002)
        assert sug["nofee"].prologis_fee == 0 and sug["nofee"].rates == s.rates
        assert sug["target"].rates["warehouse"] / sug["target"].rates["office"] == pytest.approx(0.5, abs=0.01)   # proportions kept
