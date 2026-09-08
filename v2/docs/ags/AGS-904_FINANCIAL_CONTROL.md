# AGS-904 · Financial Control Standard

**Series 9 — Governance and data.** Draft 0.1 for the book builder (2026-09-08). Companion: AGS-903 (Project Delivery Control), AGS-702 (PPA agreements, billing), AGS-901 (data integrity — the completeness and reconciliation doctrine already used by monitoring). Implemented by the portal's Finance module; every rule below is a pure function with a test (`argia/fin/`).

## 1. Purpose and scope

ARGIA needs a daily, trustworthy picture of money — what is owed to us, what we owe, what is in the bank, and what each project costs and earns — without replacing the accountants' books or the invoicing system (Savio, CFDI 4.0). This chapter fixes the *operational ledger*: which facts are recorded from which source, the order of authority, the moves that are refused, and the figures every report must reconcile to.

## 2. Sources of truth (order of authority)

| Fact | Source of record | The portal holds |
|---|---|---|
| Customer invoice | Savio (CFDI UUID, SAT-stamped) | a mirror keyed by Savio id + UUID; project/plant link |
| Supplier invoice | the CFDI XML (UUID) | the parsed facts, the PO/project link, the match verdict, the approval |
| Money movement | the bank statement | the line with its natural key; the reconciliation to a payment/receipt |
| Contract, budget, PO, approval, change order | the portal | the record and its audit trail |
| Books, pólizas, tax | the accountants | nothing — the portal exports, never asserts |

**R1 — A CFDI UUID exists once.** Re-importing a file adds nothing; a second file with the same UUID is a duplicate, not a second invoice. The XML is primary; totals are re-checked (subtotal − discount + traslados − retenciones = total) before the document enters the ledger.

**R2 — A bank line exists once and reconciles once.** Natural key = bank transaction id, else account + date + amount + normalised description. A statement loads whole (opening + Σ lines = closing) or not at all. A line may be split, and the split must sum exactly to the line; reversal is an event, never a deletion.

**R3 — A payment is never a cost; a cost is never cash.** Actual cost = approved supplier invoices − credit notes. Cash = reconciled bank balance. Committed = approved open POs − invoiced. The three are shown side by side and never added to each other.

**R4 — Nothing is paid twice.** Allocations to an invoice cannot exceed its outstanding balance; a cancelled or rejected invoice is not payable; same account + counterpart + amount within 24 h is flagged before it leaves.

**R5 — Approvals are routed by value and cannot be self-granted.** PO and payment approval levels are configured per entity (default: PM ≤ 50 k, director ≤ 500 k, board above); the creator cannot approve above the self-approval limit; a material change after approval resets it; approval history is append-only.

**R6 — Three-way match before AP.** PO = receipt = invoice within tolerance (default 1 % or 500 MXN). Missing PO, wrong supplier, wrong currency, over-PO, over-received, PO not approved → exception with an owner. Overrides need a reason and authorisation, recorded.

**R7 — One legal entity per transaction.** A supplier invoice, a payment and a bank account belong to exactly one entity; paying entity A's invoice from entity B's account is refused. Cross-company projects follow a written policy (open question at draft 0.1).

**R8 — Closed periods are frozen.** A document dated inside a closed month is refused; corrections are booked on the first open day and reference the original. Reports for a closed month are reproducible.

**R9 — Aging is per currency.** Receivables and payables age in buckets current / 0–30 / 31–60 / 61–90 / 90+ from the due date (explicit, else issue + terms); MXN and USD never share a bucket; USD is converted for company cash only with a dated FX rate — a missing rate is an error, not 1.0.

**R10 — Every figure on a dashboard equals its ledger.** AR tiles = Σ open customer invoices; AP tiles = Σ open supplier invoices; cash = Σ reconciled balances; project revenue in PM = project revenue in FIN. A parity script runs before every release and every month end.

## 3. Cost model per project (definitions)

- **Actual** = Σ approved supplier invoices − credit notes, by cost code.
- **Committed** = Σ approved open POs − Σ invoiced against them (partial invoices never double count).
- **ETC** (estimate to complete) = max(0, active budget − actual − committed) per cost code.
- **EAC** = actual + committed + ETC.
- **Contract** = baseline contract + Σ approved change orders.
- **Forecast margin** = contract − EAC; **baseline margin** = baseline contract − baseline budget; **erosion** = baseline margin − forecast margin, with the change orders and budget revisions that explain it.
- **Unapproved exposure** = cost and revenue of pending change orders, shown, never booked.

## 4. Cost codes (draft catalogue — confirm with the accountants)

1 Development (1.1 studies · 1.2 permits · 1.3 interconnection) · 2 Engineering (2.1 design · 2.2 structural · 2.3 third-party review) · 3 Supply (3.1 modules · 3.2 inverters/BESS · 3.3 structure · 3.4 electrical BOS · 3.5 monitoring/comms) · 4 Construction (4.1 civil · 4.2 mechanical · 4.3 electrical · 4.4 site services · 4.5 safety) · 5 Commissioning (5.1 testing · 5.2 CFE/utility · 5.3 handover) · 6 Project overhead (6.1 PM · 6.2 insurance · 6.3 logistics · 6.4 contingency) · 7 O&M (7.1 preventive · 7.2 corrective · 7.3 warranty) · 8 Finance (8.1 fees · 8.2 FX). Inactive codes cannot receive new transactions; hierarchy sums by prefix.

## 5. Worked example — supplier invoice on the San Luis Potosí project

PO-2026-0007 (inverters, 100,000 + IVA 16,000 = 116,000 MXN, approved by PM + director since > 50 k). Receipt 2026-08-12: 10 units, full value. CFDI A-1021 from ELECTRO (UUID 6F1A…5F40) arrives 2026-08-14: subtotal 100,000, IVA 16,000, total 116,000 — totals re-checked, UUID new → supplier invoice created, matched (PO = receipt = invoice), approved into AP, due 2026-09-13 (30 days). Cost code 3.2 actual +100,000 (net of IVA), committed −100,000. Payment 2026-08-30 of 58,000 (first parcialidad) reconciles bank line TX2 once; the supplier's complemento de pago (UUID CCCC…0003) confirms saldo insoluto 58,000 = the ledger's outstanding. A second 58,000 the next day with the same counterpart is flagged R4 until a person confirms it is the second instalment. The same XML dropped again in the inbox: 0 rows changed.

## 6. Reports every month end must reconcile

AR aging (per currency) · AP aging · cash by account with statement date and unreconciled count · 13-week cash-flow (AR expected collection, AP expected payment, open POs per rule, PPA billing calendar) · per-project actual / committed / EAC / margin / erosion · exception queue by kind and owner. Each report carries "How the numbers are calculated" and the parity result of R10.

## 7. Common errors

1. Booking a payment as a cost (double cost, margin understated) — R3.
2. Paying from the PDF instead of the XML — a tampered or wrong-currency total.
3. Matching a bank line twice, once to the invoice and once to the payment — cash overstated.
4. Approving your own PO because "it is urgent" — R5 has a limit for exactly that.
5. Editing a closed month to "fix" a figure — the accountants' books and ours diverge silently; use the adjustment path (R8).
6. Averaging MXN and USD receivables in one bucket — R9.
