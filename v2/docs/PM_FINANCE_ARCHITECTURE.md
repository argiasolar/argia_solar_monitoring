# Portal modules 3 and 4 — Project Management and Finance

Architecture, decisions and delivery plan · v243 · 2026-09-08 · status: **Phase 0 shipped (domain rules + tests), Phase 1 not started**

Decisions taken with Tomasz on 2026-09-08: PostgreSQL on pio06 is the system of record for everything that carries money or state; the PMO Google Sheets stay the project managers' plan editor and feed the portal; supplier CFDIs and bank statements arrive as files (Drive drop folders) until Savio or the SAT give us an API path; the finance snapshot (AR / AP / cash + per-project cost) is built first, the PM overview reads the PMO snapshot; the number of legal entities and bank accounts is still to be listed (the model carries N entities from day one).

---

## 1. What the two modules are for — in one paragraph each

**Project Management (PM).** One place that answers, for every ARGIA project from won offer to commissioned plant: where is it (phase, milestones, schedule health), what does it cost (baseline budget, revisions, commitments, actuals, forecast at completion, margin), what is at risk (late milestones, pending change orders, unbilled work, unapproved exposure), and where the documents are (the project's Drive folder, indexed). It does not replace the PMs' Gantt in Google Sheets; it makes the Sheets data trustworthy by owning the money and the state.

**Finance (FIN).** The daily financial picture the accountants do not produce: receivables and payables aged by bucket, cash by bank account reconciled to statements, committed vs actual cost per project, cash-flow forecast, and an exception queue (invoice without PO, invoice above PO, overdue AR, unreconciled bank item, unbilled milestone). Savio remains the invoicing/collections tool of record for customer CFDIs; the accountants keep the books; the portal keeps the *operational* ledger that ties every peso to a project, a PO and a bank line — and refuses the moves the business must never make twice.

## 2. Principles (the same ones the monitoring modules already live by)

1. **One system of record per fact.** A customer invoice exists in Savio (CFDI UUID) and is mirrored, never re-typed. A supplier invoice exists as a CFDI XML (UUID). A bank line exists in the statement. The portal stores the mirror plus the *links* (project, PO, allocation) and the *decisions* (approval, match, close).
2. **Money-moving events carry an invariant, in code, with a test.** An invoice cannot be paid twice; a CFDI UUID exists once; an approved PO cannot change amount silently; a payment never creates a second cost; one bank line reconciles once unless explicitly split; a closed period is frozen. These are pure functions in `argia/fin/` — the same style as `argia/recon/engine.py` — tested before any UI exists.
3. **Files in, decisions in the portal, nothing edited by hand in PG.** Drive drop folders are the only way statements and XMLs enter; every import is idempotent (natural keys), every correction is an audited event, never an UPDATE by hand (the daily_production rule).
4. **Sheets are an editor, not a ledger.** The PMO workbook keeps tasks, dates and the Gantt because the PMs work there; the V8.1 engine already computes the rollup — it will *push a JSON snapshot* to the portal, and the portal is what shareholders and customers read. No formula in a Sheet is ever the source of a money figure.
5. **Same platform, same rules.** Flask app per module under nginx + the session auth (`auth_core` areas), server-rendered bilingual HTML through `portal_chrome` (`t()`/`ti()`), `finance_audit`-style audit tables, `drift_check` smoke, systemd timers documented in OPERATIONS.md (a test fails when a timer is missing), secrets file-to-file only, CSRF on every write, Post/Redirect/Get.
6. **Honest states.** "—" and red where we do not know; never a computed zero that looks like a fact (the Tetra Pak lesson, v241).

## 3. What exists today that these modules must use, not duplicate

| Asset | Where | Reuse |
|---|---|---|
| Plant, contract, tariff, invoicing register (`invoicing`), loans, `finance_audit` | PG `argia_mont` | PPA/LaaS revenue side already exists; FIN adds AR/AP/cash around it, PM links a plant to its origin project |
| Reconciliation close → invoice annex (`reconciliation_monthly`, `invoice_publish`) | PG + Drive `Invoicing/<YYYY-MM>/` | The monthly PPA annex becomes a *customer invoice request* in FIN (scenario 55) |
| Maintenance tickets (`ticket*`) | PG + `/maintenance/` app | Ticket cost lines (labour/material, warranty vs chargeable) flow to FIN cost (scenario 53) |
| Setup catalog (`setup_app`), auth areas, mail subscriptions, Ask ARGIA tools | PG + apps | New drawers "Projects" and "Finance" in Setup; `get_project`, `get_ar_aging` … as Ask tools with scope |
| PMO V8.1 (master + 30 project workbooks, Apps Script web app) | Google Drive `ARGIA PMO` | Stays; gains one function `exportPortalSnapshot()` (JSON to a Drive file the portal pulls nightly / on demand) |
| Drive mirror pattern (`invoice_publish` pushes PDFs; service account) | pio06 → Drive | The same service account reads the drop folders and writes the per-project folder tree |
| Engine (offers) | Apps Script | A won offer's key facts (customer, site, kWp, price, type) are the seed of a project — via the same snapshot pattern, not a live API |

## 4. Integrations — facts first

### 4.1 Savio (verified 2026-09-08 from https://api.savio.mx/docs)
- Base `https://api.savio.mx/api/v1` (sandbox `https://api-sandbox.savio.mx/api/v1`), API key issued by Savio (nacho@savio.mx). 150 req/min, 10,000/day (resets midnight Mexico City), 429 + `Retry-After`, a rejected request burns no quota.
- Resources: `invoice` (list with cursor pagination ordered by `updated_at, invoice_id`; `include=cfdis,items`; create/update/status), `customer`, `contact`, `custom-field`, `csf/extract`, `payment` (record customer payment), CFDI status/cancel.
- Webhooks: `payment.created / applied / deleted`, `credit.created / deleted`, `invoice.deleted`, `invoice.status.updated`, `cfdi.canceled`.
- **Not in the public API:** supplier invoices (gastos), bank movements, pólizas. Savio's UI does bank connection + AI reconciliation + pólizas; those stay in Savio for the accountants. Consequence: the portal's AR mirror is API-driven; AP and cash are file-driven (4.2, 4.3) until Savio exposes them or we go to the SAT.
- Design: a pull job `savio_sync.py` (cursor persisted in PG, idempotent upsert by `invoice_id` + CFDI UUID) every 30 min, plus a webhook receiver `fin_webhook` (Flask, HMAC-verified per Savio's `/webhooks` instructions) for near-real-time payments/cancellations. Every webhook is stored raw (`savio_event`, unique per event id) before processing — replay-safe (scenario 67).
- Custom fields on Savio invoices carry `argia_project_id` and `argia_plant_key`, so the match back to a project needs no heuristics (scenario 17); the PPA annex job creates the Savio invoice with those fields set (scenario 55) and only marks it issued after Savio returns a UUID (scenario 26).

### 4.2 Supplier CFDIs (AP)
- Phase 1: Drive drop folder `FIN/<entity>/inbox/cfdi/` — XML (and its PDF) exported from Savio's expense module or forwarded from suppliers. `cfdi_ingest.py` parses CFDI 4.0 XML (`argia/fin/cfdi.py`: UUID from the TimbreFiscalDigital, emisor/receptor RFC, subtotal, IVA trasladado, retenciones, total, currency, TipoDeComprobante I/E/P, complemento de pago references). UUID is the primary key — the same file twice is one invoice (scenario 16/62/67). RFC → supplier master (duplicate RFC rejected, scenario 60).
- Phase 2 option: SAT *descarga masiva* with the FIEL (complete, authoritative, needs the FIEL on pio06 under the same file-to-file secret rule). Decide after Phase 1 shows how many CFDIs bypass Savio.

### 4.3 Bank statements (cash)
- Drop folder `FIN/<entity>/inbox/bank/<account>/` — CSV/XLSX as exported from the bank. One parser per bank format (`argia/fin/bank_csv.py` — format registry, each format tested against a fixture). Natural key per line = (account, booking date, amount, reference/description hash) or the bank's own transaction id when present; a re-uploaded statement adds nothing (scenario 29/67).
- Opening + Σ activity = closing, per statement, checked at import; a statement that does not reconcile is rejected into the exception queue, never partially loaded (scenario 30).
- Own-account transfers are recognised by counterpart account and excluded from income/expense.

### 4.4 Google Drive (documents)
- Per project: `PROJECTS/<ARG-ID> <customer> <site>/{01_offer, 02_contract, 03_design, 04_procurement, 05_construction, 06_commissioning, 07_closeout}` created once by `drive_project_tree()` (idempotent: looks up by the `ARG-ID` prefix before creating; folder id stored on the project row; scenario 6). Files are indexed (id, name, mime, modified, folder) nightly into `project_document`; classification (contract / PO / invoice / …) is a portal action with metadata, never a rename.
- Permission failures are logged and surfaced; they never roll back a project state change (scenario 6).

### 4.5 PMO Sheets → portal
- New Apps Script function `exportPortalSnapshot()` in the V8.1 engine writes `ARGIA PMO/05_PORTAL/portfolio_snapshot.json` (projects, phases, tasks, milestones with baseline and current dates, progress, resources, conflicts) after every `syncProjects()`; the portal pulls it (`pmo_snapshot_pull.py`, 10-min timer) and upserts `project_task` / `project_milestone` rows **without touching money fields**. Direction of truth: schedule from Sheets, money from the portal; the portal writes back nothing to Sheets in Phase 1 (a later step can write budget/cost summaries into a read-only tab).

## 5. Domain model (PG, `argia/fin/schema.py` ENSURE_SQL — created in Phase 1, designed now)

Entities and their natural keys. Every table with money has `entity_id`, `created_at`, `created_by`; every state change writes `fin_event` (who / when / what / old → new, append-only).

**Master data** — `entity` (legal entity: name, RFC, currency), `bank_account` (entity, bank, CLABE/last4, currency), `cost_code` (hierarchical, active flag), `supplier` (RFC unique per entity, bank details with change log), `customer_master` (RFC unique; Savio customer id), `fx_rate` (date, pair, rate, source).

**Projects** — `project` (ARG-ID unique, entity, customer, site, type PPA/CAPEX/LaaS/EPC, kWp, status per the state machine, PM owner, offer ref, drive_folder_id, plant_key when commissioned), `project_milestone` (planned, baseline, actual dates; kind: commercial/technical; billing flag; billed_invoice_id), `project_task` (from the snapshot), `budget_version` (project, version n, status draft/approved/superseded, approved_by), `budget_line` (version, cost_code, amount, currency), `change_order` (project, status pending/approved/rejected, cost impact, revenue impact, approved_by).

**Procurement** — `purchase_order` (po_number unique per entity, supplier, project, status draft/submitted/approved/partially_received/closed/cancelled, currency, fx, subtotal/tax/total, terms, approvals), `po_line` (cost_code, qty, unit price), `po_approval` (append-only), `receipt` (po_line, qty/amount, date, user; over-receipt blocked).

**AP** — `supplier_invoice` (cfdi_uuid unique, supplier, entity, project?, po?, subtotal, iva, retenciones, total, currency, fx, issue/due date, status received/matched/exception/approved/paid/cancelled, xml_drive_id), `invoice_match` (three-way result, tolerance, exceptions), `credit_note` (references invoice), `payment_request`, `payment` (bank_account, date, amount, currency, bank_transaction?), `payment_allocation` (payment ↔ invoice amount; Σ ≤ invoice outstanding).

**AR** — `customer_invoice` (savio_invoice_id unique, cfdi_uuid unique, project/plant, amounts, status, due date), `customer_payment` (Savio payment id), `customer_allocation`, `customer_credit`.

**Cash** — `bank_statement` (account, period, opening, closing, file id, hash), `bank_transaction` (statement, natural key unique, date, amount, description, counterpart), `bank_match` (transaction ↔ payment/customer_payment/transfer/fee, split allowed, reversible with an event).

**Control** — `period_close` (entity, month, closed_by; frozen), `fin_exception` (kind, owner, status, resolution history), `fin_event` (audit), `savio_event` (raw webhooks), `sync_run` (existing).

## 6. The rules that must hold (implemented in `argia/fin/`, Phase 0)

| Area | Rule | Code |
|---|---|---|
| CFDI | UUID unique; totals reconcile (subtotal − descuento + traslados − retenciones = total, ±0.01); XML is primary | `cfdi.parse`, `cfdi.check_totals` |
| PO | state machine draft→submitted→approved→(partially_received)→closed / cancelled; amount change after approval resets approval; approval route by value; creator ≠ approver above threshold | `rules.PO_TRANSITIONS`, `rules.approval_route`, `rules.po_amend` |
| Three-way match | PO = receipt = invoice within tolerance (default 1 % / 500 MXN); wrong supplier / currency / over-PO / over-received → exception codes | `match.three_way` |
| AP / AR | due date from terms; aging buckets 0–30/31–60/61–90/90+; outstanding = total − Σ allocations − credits; allocation cannot exceed outstanding; cancelled invoice leaves the ledger | `ledger.due_date`, `ledger.aging`, `ledger.outstanding`, `ledger.allocate` |
| Payments | no duplicate (same account, date, amount, counterpart within 24 h → flag); a payment is never a cost | `ledger.duplicate_payment` |
| Bank | opening + Σ = closing; a line reconciles once unless split, and splits must sum exactly | `cash.statement_balances`, `cash.reconcile` |
| Cost | actual = approved supplier invoices (− credit notes); committed = approved open PO − invoiced; EAC = actual + open committed + estimate-to-complete; margin = contract − EAC; erosion vs baseline | `cost.actual`, `cost.committed`, `cost.eac`, `cost.margin` |
| Project | status machine (draft→active→…→commissioned→closed, on_hold, reopen with authorisation); cannot close with open POs / unpaid / unbilled / open COs unless overridden; milestone bills once | `rules.PROJECT_TRANSITIONS`, `rules.closure_blockers`, `rules.bill_milestone` |
| Health | deterministic score from schedule slip, budget variance, cash exposure, with the explanation | `health.score` |
| Period | a closed month rejects edits; adjustments are dated in an open month and reference the original | `rules.period_guard` |

## 7. Pages (Phase 1 → 3) — all bilingual, all with "How the numbers are calculated"

- `/finance/` **Today** — cash by account (statement date, reconciled balance, unreconciled count), AR aging tiles + top 10 overdue, AP aging tiles + due this week, exception queue by kind with owner, 13-week cash-flow forecast (AR expected collection, AP expected payment, open POs per forecast rule, PPA billing calendar). Every tile flips with its reason (v241 pattern).
- `/finance/ar/`, `/finance/ap/`, `/finance/bank/<account>/` — ledgers with filters, CSV export, the matching/allocation actions (server-rendered forms, CSRF, audited).
- `/finance/projects/` — per-project cost view: baseline · revisions · committed · actual · EAC · margin · erosion, drill to POs and invoices.
- `/projects/` **Portfolio** — cards/table: phase, health, next milestone, contract value, forecast margin, PM; filters; `/projects/<ARG-ID>/` — milestones (baseline vs current), tasks (from Sheets), budget, POs, invoices, documents (Drive tree), change orders, exceptions, timeline of events.
- Setup drawers: Finance → entities, bank accounts, cost codes, suppliers, approval thresholds, tolerances; Projects → templates (milestone sets), PM users, Drive root.
- Ask ARGIA tools: `get_project`, `get_portfolio`, `get_ar_aging`, `get_ap_aging`, `get_cash`, scoped by role (scenario 57/58/73): totals from the tool, never model arithmetic (existing rule).

## 8. Security and roles

Areas in `auth_core`: `projects` (PM: own projects unless portfolio role), `finance` (accountant), `finance_approve` (approver), `admin`. Segregation of duties enforced in code: the user who created a PO / payment request / supplier bank change cannot approve it above the threshold (configurable per entity; scenario 59). Supplier bank-detail changes need `finance_approve` and are logged with old/new. Webhook endpoint verifies Savio's signature and is rate-limited; the API key lives in `/root/.argia_savio` (chmod 600, file-to-file). No FIEL on the server in Phase 1.

## 9. Test strategy — the 75 scenarios, sorted honestly

Four levels, as the TDD note suggests: **unit** (formulas and rules — Phase 0, pure, no I/O), **domain** (workflows against a throw-away PG in CI — Phase 1), **integration** (Savio sandbox, Drive test folder, bank fixtures — Phase 1/2, gated by credentials), **end-to-end golden path** (scenario 75 — Phase 3, one script from offer to closed project, run before every deploy of these modules).

| Phase | Scenarios covered (numbers from the TDD list) |
|---|---|
| 0 (shipped) | 16, 18, 19, 21, 24, 27–30, 32–35, 40 (IVA/retention arithmetic), 62, 64 (period guard rule), 67 (dedup keys), 72 — as pure unit tests in `tests/unit/test_fin_*.py` |
| 1 | 10, 11–15 (PO lifecycle), 17, 20, 22, 23, 26, 31, 41, 45, 46, 47, 63, 65, 66, 70 — domain tests on PG + Savio sandbox |
| 2 | 1–9 (offer → project, milestones, schedule health, Drive tree, documents, budget), 25, 36–39, 48–52, 57–61, 68, 69, 71, 74 |
| 3 | 42–44 (advances, retentions), 53–56 (maintenance + PPA billing integration), 73 (Ask), 75 (golden path) |

Rules of the road (same as today): commit scripts with `set -o pipefail`; tests pin `encoding="utf-8"`; laptop 3.14 + sandbox 3.11/3.10 green before push; every timer in OPERATIONS.md; `drift_check` after every deploy; no hand edits in PG; secrets never in chat, repo or logs.

## 10. Roadmap with gates

| Phase | Scope | Gate to pass |
|---|---|---|
| **0 — Rules** (done, v243) | `argia/fin/` pure domain + tests; schema designed (`schema.py`, not applied); this document; AGS-903/904 drafts | suites green, no production change |
| **1 — Finance snapshot** (2–3 weeks) | ENSURE_SQL applied; entities/accounts/cost codes in Setup; Savio API key + sandbox → `savio_sync` + webhook; CFDI + bank drop folders + parsers; AP/AR/cash ledgers; `/finance/` Today page; exception queue; Ask tools; timers documented | parity: AR total = Savio; cash = statement closing per account; every import idempotent (re-run = 0 changes); Playwright render; 48 h of clean timers |
| **2 — Projects** (3–4 weeks) | `exportPortalSnapshot()` in PMO V8.1; project master + milestones + budget versions + change orders; POs with approvals; three-way match live; Drive tree + document index; `/projects/` pages; per-project cost view; health score | snapshot parity with the master workbook (count, phases, dates); PO → invoice → payment on a real supplier end-to-end; PM role scoping verified |
| **3 — Closing the loop** (2–3 weeks) | PPA annex → Savio invoice; customer payments → AR; maintenance ticket costs; advances/retentions; plant creation from commissioned project; period close; golden-path test; shareholder view replaces the Apps Script report | scenario 75 green in CI; shareholders report numbers = portal to the peso (the V8.1 reconciliation repeated) |

Each phase ships as the usual vNNN commits with a project note; nothing lands on pio06 without the drift check, and Phase 1's first production import is a dry run with a diff.

## 11. Open questions for Tomasz (needed before Phase 1 starts)

1. **Legal entities and bank accounts** — list them (name, RFC, currency, bank, account purpose). The model carries N; the first migration seeds them.
2. **Savio** — request the API key + sandbox from Savio; confirm whether supplier expenses (gastos) can be exported as XML in bulk from the Savio UI, and whether they can add AP/bank endpoints (worth asking once).
3. **Bank statement formats** — one sample CSV/XLSX per account into `FIN/<entity>/inbox/bank/<account>/samples/` (redact if you like; the parser fixtures are built from them).
4. **Approval thresholds and tolerances** — PO approval levels (e.g. PM ≤ 50k, director ≤ 500k, board above), three-way tolerance (proposal 1 % / 500 MXN), payment self-approval rule.
5. **Cost code list** — the chart of cost codes per project type (a draft is in AGS-904 §4; confirm or replace with the accountants' catalogue).
6. **Offer → project seed** — which Engine fields define a project (the Engine's TEAM_DATA_REQUESTS pattern); whether the ARG-ID is minted by the Engine or by the portal.
7. **Which project types** get the full cost model on day one (EPC/CAPEX projects yes; PPA plants already have the revenue side — cost side only for construction).

## 12. What this is not

Not an ERP, not a replacement for the accountants' books (CONTPAQ/whatever they use) and not a bank client. It is the operational ledger that makes the daily situation visible and makes the dangerous mistakes impossible. If a figure disagrees with the accountants at month end, the exception queue shows why, and the accountants win.
