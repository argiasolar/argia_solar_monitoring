# AGS-903 · Project Delivery Control Standard

**Series 9 — Governance and data.** Draft 0.1 for the book builder (2026-09-08). Companion: AGS-904 (Financial Control), AGS-801/802 (Engine), AGS-701/702 (O&M, contracts). Implemented by the portal's Project Management module (docs/PM_FINANCE_ARCHITECTURE.md); the rules below are the ones its code refuses to break, each with a test.

## 1. Purpose and scope

Every ARGIA project — PPA, CAPEX, LaaS, EPC — moves from a won offer to a commissioned asset through the same controlled states, with one baseline, one owner, and one record of every change. This chapter fixes what a project *is* in ARGIA's records, which transitions exist, what a milestone means, how schedule health is judged, and what "closed" requires. It does not prescribe the PM's planning tool (the PMO workbook remains the plan editor); it prescribes what the record must hold and what may never happen to it.

## 2. Definitions

| Term | Meaning |
|---|---|
| Project | One won offer executed for one customer at one site under one contract, identified by an ARG-ID minted once and never reused. |
| Baseline | The approved version 1 of dates and budget. Immutable. Every later plan is compared to it. |
| Milestone | A dated deliverable with a baseline date, a planned date and an actual date. *Commercial* milestones may trigger billing; *technical* milestones gate transitions. |
| Change order (CO) | A proposed change to scope, price or cost. Pending COs are visible exposure; only approved COs move the contract. |
| Health | A deterministic score from schedule slip, budget variance and cash exposure, with the explanation shown next to it (§6). |

## 3. Rules

**R1 — One offer, one project.** A won offer converts into exactly one project; a second conversion is refused. The project keeps the offer reference for life. *(Common error: re-creating a project after a rename, leaving two ARG-IDs for one site.)*

**R2 — Activation needs the commercial facts.** A project may become *active* only with customer, site, legal entity, project type, contract value, PM owner and an approved baseline budget. A project without them stays *draft*.

**R3 — Valid transitions only.** draft → active → (on_hold ⇄ active) → commissioned → closed; cancelled from draft/active/on_hold. Reopening a commissioned or closed project needs authorisation and leaves an audit entry.

**R4 — Baseline dates never move.** Rescheduling changes the planned date; the baseline date stays. Schedule slip is always measured against the baseline.

**R5 — A milestone completes once, in order.** Dependencies must be done first; completion records the actual date and the person; a completed milestone cannot complete again.

**R6 — A commercial milestone bills once, within the contract.** Billing eligibility = completed ∧ billable ∧ not yet billed ∧ amount ≤ remaining authorised contract. The invoice reference is written on the milestone; a second invoice is refused.

**R7 — Pending change orders are exposure, not revenue.** Contract value = baseline contract + Σ approved COs. Pending COs appear on the project as "unapproved exposure" with their cost and revenue; rejected COs are kept as history.

**R8 — Closing requires a clean sheet or a signed override.** No open purchase orders, no unpaid supplier invoices, no completed-but-unbilled milestones, no pending COs, no remaining commitment — or an override with reason and authorisation, recorded.

**R9 — Commissioning creates the asset.** A commissioned project may create/activate the plant record; the plant inherits customer, site, kWp and the ARG-ID, so monitoring links back to its origin (AGS-701 needs this for the design PR and the warranty dates).

**R10 — Documents live in the project's Drive tree.** One folder tree per project, created once by ARG-ID; documents are classified with metadata (contract, PO, invoice, drawing, …), never renamed to carry meaning; replacing a file keeps the previous version's record.

## 4. Schedule health

| Worst open milestone vs baseline | Penalty |
|---|---|
| ≤ 7 days | 0 |
| 8–30 days | 10 |
| 31–60 days | 25 |
| > 60 days | 40 |

Budget penalty (EAC vs active budget): ≤ 2 % 0 · ≤ 5 % 10 · ≤ 10 % 25 · > 10 % 40. Cash penalty (overdue payables + unbilled completed milestones, as % of contract): ≤ 2 % 0 · ≤ 5 % 10 · ≤ 15 % 20 · > 15 % 30. Score = 100 − penalties; green ≥ 80, amber ≥ 60, red below. Thresholds carry a version string; changing them is a versioned decision, not an edit. The same input always yields the same score and the reasons are displayed with it.

## 5. Worked example — 417 kWp San Luis Potosí hotel (the book's reference project)

Offer won 2026-03-02 → project ARG0417 activated 2026-03-10 with contract 7.24 M MXN and baseline budget 5.61 M MXN (margin 1.63 M, 22.5 %). Milestones: M1 engineering release (baseline 04-15), M2 modules on site (05-20, billable 30 %), M3 mechanical complete (06-30), M4 interconnection (07-31, billable 60 %), M5 commissioning (08-15, billable 10 %). On 2026-06-10 the structure supplier slips M3 to 07-21 (planned moves, baseline stays; slip 21 days → schedule penalty 10). A pending CO for a 30 kW BESS expansion (+0.62 M revenue, +0.48 M cost) shows as exposure; the contract stays 7.24 M until it is approved. M2 completed 05-18 and billed on Savio invoice A-1021 (2.17 M); an attempt to bill M2 again is refused (R6). EAC after supplier invoices and open POs: 5.72 M → budget penalty 0 (+2.0 %), forecast margin 1.52 M, erosion 0.11 M explained by the structure change. Health 90, green; the reason line reads "schedule: 21 days behind baseline".

## 6. Records the portal keeps for this chapter

`project`, `project_milestone` (baseline/planned/actual, billed_invoice), `change_order`, `budget_version`, `fin_event` (every transition with who/when/old/new), Drive folder id. Tests: `tests/unit/test_fin_rules_cost.py` (lifecycle, milestones, COs, closure, health).

## 7. Common errors

1. Moving the baseline to hide a slip — the slip disappears from health and reappears in the customer's penalty clause.
2. Billing a milestone from the Gantt rather than from the record — double invoices.
3. Treating a pending CO as sold — margin overstated until the customer signs.
4. Closing a project with an open PO — a supplier invoice lands on a closed project months later.
5. Two ARG-IDs for one site — cost split across records; neither shows the true margin.
