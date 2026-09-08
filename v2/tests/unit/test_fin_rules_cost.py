"""Scenarios 2–5 (project lifecycle, milestones), 8/9 (budget versions),
11–14 (PO approval, committed cost), 25/49 (milestone billing), 31–37
(revenue, actual, EAC, margin, erosion, change orders), 51 (closure),
59 (segregation of duties), 64 (period close), 72 (health score) —
plus the schema's natural keys."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from argia.fin import cost as C, health, rules as R, schema
from argia.fin.money import D

T = dt.date(2026, 9, 8)
FACTS = {"customer": "Ryder", "site": "Nuevo Laredo", "entity_id": "ARGIA-MX", "project_type": "CAPEX",
         "contract_value": "7004698", "pm_user": "tomasz", "budget_approved": True}


class TestProjectLifecycle:
    def test_valid_and_invalid_moves(self):
        assert R.project_transition("draft", "active", facts=FACTS) == "active"
        assert R.project_transition("active", "on_hold") == "on_hold"
        with pytest.raises(R.RuleError, match="cannot become"):
            R.project_transition("draft", "closed")
        with pytest.raises(R.RuleError, match="unknown"):
            R.project_transition("nope", "active")

    def test_activation_needs_the_commercial_facts(self):
        with pytest.raises(R.RuleError, match="without contract_value, pm_user"):
            R.project_transition("draft", "active", facts={**FACTS, "contract_value": None, "pm_user": ""})

    def test_reopen_needs_authorisation(self):
        with pytest.raises(R.RuleError, match="authorisation"):
            R.project_transition("closed", "active")
        assert R.project_transition("closed", "active", authorised=True) == "active"

    def test_closure_blockers_and_override(self):
        b = R.closure_blockers(2, 1, 1, 0, D("15000"))
        assert len(b) == 4 and "2 open purchase order(s)" in b[0]
        with pytest.raises(R.RuleError, match="cannot close"):
            R.close_project(b)
        with pytest.raises(R.RuleError):
            R.close_project(b, override_reason="warranty retention", authorised=False)
        assert R.close_project(b, override_reason="warranty retention", authorised=True) == "closed"
        assert R.close_project([]) == "closed"


def ms(ref="M1", planned="2026-09-01", actual=None, billable=True, amount="100000", deps=(), kind="commercial"):
    return R.Milestone(ref, kind, dt.date.fromisoformat(planned), dt.date.fromisoformat(planned),
                       dt.date.fromisoformat(actual) if actual else None, billable, "", D(amount), tuple(deps))


class TestMilestones:
    def test_reschedule_keeps_baseline(self):
        m = R.reschedule(ms(), dt.date(2026, 10, 15))
        assert m.planned == dt.date(2026, 10, 15) and m.baseline == dt.date(2026, 9, 1)

    def test_dependencies_and_double_completion(self):
        m = ms("M2", deps=("M1",))
        with pytest.raises(R.RuleError, match="depends on M1"):
            R.complete(m, T, done=[])
        done = R.complete(m, T, done=["M1"])
        assert done.actual == T
        with pytest.raises(R.RuleError, match="already completed"):
            R.complete(done, T, done=["M1"])

    def test_billing_once_and_within_contract(self):
        m = R.complete(ms(), T, [])
        billed = R.bill_milestone(m, "SAVIO-77", remaining_contract=D("500000"))
        assert billed.billed_ref == "SAVIO-77"
        with pytest.raises(R.RuleError, match="already billed"):
            R.bill_milestone(billed, "SAVIO-78", D("500000"))
        with pytest.raises(R.RuleError, match="exceeds remaining contract"):
            R.bill_milestone(m, "X", D("99999"))
        with pytest.raises(R.RuleError, match="not completed"):
            R.bill_milestone(ms(), "X", D("500000"))
        with pytest.raises(R.RuleError, match="not a billing milestone"):
            R.bill_milestone(R.complete(ms(billable=False), T, []), "X", D("500000"))

    def test_late_and_unbilled_lists(self):
        late = R.late_milestones([ms("A", "2026-08-01"), ms("B", "2026-10-01"), ms("C", "2026-07-01", actual="2026-07-05")], T)
        assert [(m.ref, d) for m, d in late] == [("A", 38)]
        assert [m.ref for m in R.unbilled([ms("A", actual="2026-09-01"), ms("B"), R.bill_milestone(R.complete(ms("C"), T, []), "I", D("1e9"))])] == ["A"]


class TestPurchaseOrders:
    def test_state_machine(self):
        assert R.po_transition("draft", "submitted") == "submitted"
        assert R.po_transition("submitted", "approved") == "approved"
        with pytest.raises(R.RuleError):
            R.po_transition("draft", "approved")
        with pytest.raises(R.RuleError):
            R.po_transition("closed", "approved")

    def test_approval_route_by_value(self):
        assert R.approval_route(D("20000")) == ["pm"]
        assert R.approval_route(D("50000")) == ["pm"]
        assert R.approval_route(D("50000.01")) == ["pm", "director"]
        assert R.approval_route(D("2000000")) == ["pm", "director", "board"]

    def test_segregation_of_duties(self):
        with pytest.raises(R.RuleError, match="cannot approve"):
            R.can_approve("tomasz", "Tomasz", D("100"))
        R.can_approve("arturo", "tomasz", D("100"))
        R.can_approve("tomasz", "tomasz", D("100"), self_approve_limit=D("1000"))

    def test_amendment_resets_approval(self):
        assert R.po_amend("approved", D("100000"), D("120000")) == "submitted"
        assert R.po_amend("approved", D("100000"), D("100050"), material_pct=D("1")) == "approved"
        assert R.po_amend("draft", D("100000"), D("300000")) == "draft"
        with pytest.raises(R.RuleError):
            R.po_amend("closed", D("1"), D("2"))


class TestPeriod:
    def test_closed_period_rejects_and_adjustment_moves_forward(self):
        closed = dt.date(2026, 8, 31)
        with pytest.raises(R.RuleError, match="closed period"):
            R.period_guard(dt.date(2026, 8, 15), closed)
        R.period_guard(dt.date(2026, 9, 1), closed)
        R.period_guard(dt.date(2026, 8, 15), None)
        assert R.adjustment_date(dt.date(2026, 8, 20), closed) == dt.date(2026, 9, 1)
        assert R.adjustment_date(dt.date(2026, 9, 8), closed) == dt.date(2026, 9, 8)


LINES = [
    C.CostLine("3.1", D("116000"), "invoice", "approved", "U1"),
    C.CostLine("3.1", D("16000"), "credit_note", "approved", "U2"),
    C.CostLine("3.1", D("50000"), "invoice", "exception", "U3"),        # not approved -> not a cost
    C.CostLine("3.2", D("200000"), "po", "approved", "PO-1"),
    C.CostLine("3.2", D("80000"), "po_invoiced", "approved", "PO-1"),  # already billed on that PO
    C.CostLine("3.3", D("30000"), "po", "cancelled", "PO-2"),          # cancelled PO releases commitment
]
VERSIONS = [
    C.BudgetVersion(1, "superseded", {"3.1": D("100000"), "3.2": D("250000"), "3.3": D("50000")}),
    C.BudgetVersion(2, "approved", {"3.1": D("120000"), "3.2": D("250000"), "3.3": D("50000")}),
    C.BudgetVersion(3, "draft", {"3.1": D("900000")}),
    C.BudgetVersion(4, "rejected", {"3.1": D("900000")}),
]


class TestCost:
    def test_actual_is_approved_invoices_minus_credits(self):
        assert C.actual(LINES) == {"3.1": Decimal("100000.00")}

    def test_committed_excludes_invoiced_and_cancelled(self):
        assert C.committed(LINES) == {"3.2": Decimal("120000.00")}        # 3.3 cancelled: no commitment at all

    def test_budget_versions(self):
        assert C.active_budget(VERSIONS).version == 2
        assert C.baseline_budget(VERSIONS).version == 1 and C.baseline_budget(VERSIONS).total == Decimal("400000.00")
        assert C.budget_variance(VERSIONS) == Decimal("20000.00")
        assert C.active_budget([C.BudgetVersion(1, "draft", {})]) is None

    def test_etc_and_eac(self):
        act, com = C.actual(LINES), C.committed(LINES)
        e = C.etc(C.active_budget(VERSIONS), act, com)
        assert e == {"3.1": Decimal("20000.00"), "3.2": Decimal("130000.00"), "3.3": Decimal("50000.00")}
        assert C.eac(act, com, e) == Decimal("420000.00")     # 100000 + 120000 + 200000

    def test_over_budget_code_contributes_zero_etc(self):
        e = C.etc(C.BudgetVersion(1, "approved", {"x": D("10")}), {"x": D("50")}, {})
        assert e == {"x": Decimal("0.00")}

    def test_contract_and_change_orders(self):
        cos = [C.ChangeOrder("CO-1", "approved", D("50000"), D("30000")), C.ChangeOrder("CO-2", "pending", D("80000"), D("70000")),
               C.ChangeOrder("CO-3", "rejected", D("999"), D("999"))]
        assert C.contract_value(D("500000"), cos) == Decimal("550000.00")
        assert C.pending_exposure(cos) == {"cost": Decimal("70000.00"), "revenue": Decimal("80000.00")}

    def test_margin_and_erosion(self):
        m = C.margin(D("500000"), [C.ChangeOrder("CO-1", "approved", D("50000"), D("0"))], VERSIONS, LINES)
        assert m.contract == Decimal("550000.00") and m.eac == Decimal("420000.00")
        assert m.margin == Decimal("130000.00") and m.margin_pct == Decimal("23.64")
        assert m.baseline_margin == Decimal("100000.00")      # 500000 − 400000
        assert m.erosion == Decimal("-30000.00")              # forecast margin improved by the CO

    def test_payment_is_never_a_cost(self):
        # nothing in the cost model takes payments: the CostLine kinds are the whole vocabulary
        assert C.actual([C.CostLine("3.1", D("1"), "payment", "approved")]) == {}


class TestHealth:
    def test_deterministic_and_explained(self):
        h1 = health.score(45, D("420000"), D("400000"), D("20000"), D("550000"))
        h2 = health.score(45, D("420000"), D("400000"), D("20000"), D("550000"))
        assert h1 == h2
        assert h1.schedule_penalty == 25 and h1.budget_penalty == 10 and h1.cash_penalty == 10
        assert h1.score == 55 and h1.band == "red"
        assert any("45 days behind" in r for r in h1.reasons) and h1.thresholds_version == health.THRESHOLDS_VERSION

    def test_on_plan(self):
        h = health.score(0, D("380000"), D("400000"), D("0"), D("550000"))
        assert h.score == 100 and h.band == "green" and h.reasons == ["on plan"]

    def test_no_contract_no_cash_penalty(self):
        assert health.score(0, D("1"), D("1"), D("100"), D("0")).cash_penalty == 0


class TestSchema:
    def test_natural_keys_are_in_the_ddl(self):
        s = schema.ENSURE_SQL
        assert "cfdi_uuid       text PRIMARY KEY" in s
        assert "UNIQUE (entity_id, po_number)" in s
        assert "line_key        text PRIMARY KEY" in s
        assert "savio_invoice_id text PRIMARY KEY" in s
        assert "PRIMARY KEY (project_id, version)" in s
        assert "savio_payment_id text UNIQUE" in s
        assert s.count("CREATE TABLE IF NOT EXISTS") == len(schema.TABLES)
        for t in schema.TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {t} (" in s, t
        assert "DROP" not in s.upper() and "ALTER" not in s.upper()

    def test_audit_is_append_only_by_shape(self):
        assert "fin_event" in schema.ENSURE_SQL and "old_value       jsonb, new_value jsonb" in schema.ENSURE_SQL


class TestDocs:
    """The architecture and the two AGS drafts ship with the rules."""

    def test_docs_exist_and_agree_on_thresholds(self):
        import pathlib
        v2 = pathlib.Path(__file__).resolve().parents[2]
        arch = (v2 / "docs/PM_FINANCE_ARCHITECTURE.md").read_text(encoding="utf-8")
        a903 = (v2 / "docs/ags/AGS-903_PROJECT_DELIVERY_CONTROL.md").read_text(encoding="utf-8")
        a904 = (v2 / "docs/ags/AGS-904_FINANCIAL_CONTROL.md").read_text(encoding="utf-8")
        assert "https://api.savio.mx/api/v1" in arch and "150 req/min" in arch
        assert "1 % / 500 MXN" in arch and "1 % or 500 MXN" in a904
        assert "PM ≤ 50 k, director ≤ 500 k" in a904
        assert "green ≥ 80, amber ≥ 60" in a903 and health.THRESHOLDS_VERSION in a903 or "version string" in a903
        for n in ("R1", "R10"):
            assert f"**{n} —" in a903 and f"**{n} —" in a904
