"""The finance + project tables (designed v243, applied in Phase 1 by
``scripts/fin_schema.py --apply``). Idempotent: CREATE IF NOT EXISTS
only, the house style (server/bundle/schema.sql, maintenance/tickets.py).

Natural keys carry the invariants the code also enforces: a CFDI UUID
once, a PO number once per entity, a bank line once per account, a
Savio invoice once, one budget version number per project.
"""
from __future__ import annotations

ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS entity (
    entity_id       text PRIMARY KEY,                -- 'ARGIA-MX', 'ARGIA-CZ' ...
    name            text NOT NULL,
    rfc             text UNIQUE,
    currency        text NOT NULL DEFAULT 'MXN',
    active          boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS bank_account (
    account_id      text PRIMARY KEY,                -- 'BBVA-MXN-1234'
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    bank            text NOT NULL,
    currency        text NOT NULL,
    clabe_last4     text,
    purpose         text,
    statement_format text NOT NULL DEFAULT 'argia_generic',
    active          boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS cost_code (
    code            text PRIMARY KEY,                -- '3.2' style, hierarchical by prefix
    parent          text REFERENCES cost_code(code),
    name_en         text NOT NULL,
    name_es         text NOT NULL,
    active          boolean NOT NULL DEFAULT true
);
CREATE TABLE IF NOT EXISTS supplier (
    supplier_id     serial PRIMARY KEY,
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    rfc             text NOT NULL,
    name            text NOT NULL,
    bank_clabe      text,
    bank_name       text,
    terms_days      integer NOT NULL DEFAULT 30,
    active          boolean NOT NULL DEFAULT true,
    UNIQUE (entity_id, rfc)
);
CREATE TABLE IF NOT EXISTS customer_master (
    customer_id     serial PRIMARY KEY,
    rfc             text UNIQUE,
    name            text NOT NULL,
    savio_customer_id text UNIQUE,
    terms_days      integer NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS fx_rate (
    rate_date       date NOT NULL,
    pair            text NOT NULL,                   -- 'USD/MXN'
    rate            numeric(12,6) NOT NULL,
    source          text NOT NULL,
    PRIMARY KEY (rate_date, pair)
);

CREATE TABLE IF NOT EXISTS project (
    project_id      text PRIMARY KEY,                -- 'ARG1414'
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    customer_id     integer REFERENCES customer_master(customer_id),
    name            text NOT NULL,
    site            text,
    project_type    text NOT NULL CHECK (project_type IN ('PPA','CAPEX','LAAS','EPC','OM','OTHER')),
    kwp_dc          numeric(10,3),
    status          text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','active','on_hold','commissioned','closed','cancelled')),
    pm_user         text,
    offer_ref       text,
    contract_value  numeric(14,2),
    contract_ccy    text NOT NULL DEFAULT 'MXN',
    drive_folder_id text,
    pmo_sheet_id    text,
    plant_key       text REFERENCES plant(plant_key),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text
);
CREATE TABLE IF NOT EXISTS project_milestone (
    project_id      text NOT NULL REFERENCES project(project_id),
    ref             text NOT NULL,
    name            text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('commercial','technical')),
    baseline_date   date NOT NULL,
    planned_date    date NOT NULL,
    actual_date     date,
    billable        boolean NOT NULL DEFAULT false,
    amount          numeric(14,2),
    billed_invoice  text,
    depends_on      text[],
    PRIMARY KEY (project_id, ref)
);
CREATE TABLE IF NOT EXISTS project_task (                  -- from the PMO snapshot, read-only here
    project_id      text NOT NULL REFERENCES project(project_id),
    task_id         text NOT NULL,
    name            text NOT NULL,
    phase           text,
    start_date      date, end_date date,
    progress_pct    numeric(5,2),
    resource        text,
    snapshot_at     timestamptz NOT NULL,
    PRIMARY KEY (project_id, task_id)
);
CREATE TABLE IF NOT EXISTS budget_version (
    project_id      text NOT NULL REFERENCES project(project_id),
    version         integer NOT NULL,
    status          text NOT NULL CHECK (status IN ('draft','approved','superseded','rejected')),
    approved_by     text, approved_at timestamptz,
    note            text,
    PRIMARY KEY (project_id, version)
);
CREATE TABLE IF NOT EXISTS budget_line (
    project_id      text NOT NULL,
    version         integer NOT NULL,
    cost_code       text NOT NULL REFERENCES cost_code(code),
    amount          numeric(14,2) NOT NULL,
    PRIMARY KEY (project_id, version, cost_code),
    FOREIGN KEY (project_id, version) REFERENCES budget_version(project_id, version)
);
CREATE TABLE IF NOT EXISTS change_order (
    co_id           serial PRIMARY KEY,
    project_id      text NOT NULL REFERENCES project(project_id),
    ref             text NOT NULL,
    status          text NOT NULL CHECK (status IN ('pending','approved','rejected')),
    revenue_impact  numeric(14,2) NOT NULL DEFAULT 0,
    cost_impact     numeric(14,2) NOT NULL DEFAULT 0,
    approved_by     text, approved_at timestamptz,
    UNIQUE (project_id, ref)
);

CREATE TABLE IF NOT EXISTS purchase_order (
    po_id           serial PRIMARY KEY,
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    po_number       text NOT NULL,
    supplier_id     integer NOT NULL REFERENCES supplier(supplier_id),
    project_id      text REFERENCES project(project_id),
    status          text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','submitted','approved','partially_received','closed','cancelled')),
    currency        text NOT NULL DEFAULT 'MXN',
    fx_rate         numeric(12,6),
    subtotal        numeric(14,2) NOT NULL DEFAULT 0,
    tax             numeric(14,2) NOT NULL DEFAULT 0,
    total           numeric(14,2) NOT NULL DEFAULT 0,
    terms_days      integer,
    expected_delivery date,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (entity_id, po_number)
);
CREATE TABLE IF NOT EXISTS po_line (
    po_id           integer NOT NULL REFERENCES purchase_order(po_id),
    line_no         integer NOT NULL,
    cost_code       text NOT NULL REFERENCES cost_code(code),
    description     text,
    qty             numeric(14,4) NOT NULL DEFAULT 1,
    unit_price      numeric(14,4) NOT NULL,
    PRIMARY KEY (po_id, line_no)
);
CREATE TABLE IF NOT EXISTS po_approval (                   -- append-only
    approval_id     serial PRIMARY KEY,
    po_id           integer NOT NULL REFERENCES purchase_order(po_id),
    level_name      text NOT NULL,
    approver        text NOT NULL,
    decision        text NOT NULL CHECK (decision IN ('approved','rejected','reset')),
    total_at        numeric(14,2) NOT NULL,
    at              timestamptz NOT NULL DEFAULT now(),
    note            text
);
CREATE TABLE IF NOT EXISTS receipt (
    receipt_id      serial PRIMARY KEY,
    po_id           integer NOT NULL REFERENCES purchase_order(po_id),
    line_no         integer NOT NULL,
    value           numeric(14,2) NOT NULL,
    received_on     date NOT NULL,
    received_by     text NOT NULL
);

CREATE TABLE IF NOT EXISTS supplier_invoice (
    cfdi_uuid       text PRIMARY KEY,
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    supplier_id     integer REFERENCES supplier(supplier_id),
    emisor_rfc      text NOT NULL,
    project_id      text REFERENCES project(project_id),
    po_id           integer REFERENCES purchase_order(po_id),
    cost_code       text REFERENCES cost_code(code),
    issue_date      date NOT NULL,
    due_date        date,
    currency        text NOT NULL,
    fx_rate         numeric(12,6),
    subtotal        numeric(14,2) NOT NULL,
    tax             numeric(14,2) NOT NULL,
    retention       numeric(14,2) NOT NULL DEFAULT 0,
    total           numeric(14,2) NOT NULL,
    tipo            text NOT NULL,                   -- I / E / P
    related_uuid    text,                            -- credit note -> original
    status          text NOT NULL DEFAULT 'received' CHECK (status IN ('received','matched','exception','approved','partially_paid','paid','rejected','cancelled')),
    xml_drive_id    text,
    imported_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS customer_invoice (
    savio_invoice_id text PRIMARY KEY,
    cfdi_uuid       text UNIQUE,
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    customer_id     integer REFERENCES customer_master(customer_id),
    project_id      text REFERENCES project(project_id),
    plant_key       text REFERENCES plant(plant_key),
    issue_date      date NOT NULL,
    due_date        date,
    currency        text NOT NULL,
    subtotal        numeric(14,2) NOT NULL,
    tax             numeric(14,2) NOT NULL,
    total           numeric(14,2) NOT NULL,
    status          text NOT NULL,
    savio_updated_at timestamptz,
    synced_at       timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS payment (                       -- money out or in, one bank movement
    payment_id      serial PRIMARY KEY,
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    account_id      text NOT NULL REFERENCES bank_account(account_id),
    direction       text NOT NULL CHECK (direction IN ('out','in')),
    pay_date        date NOT NULL,
    amount          numeric(14,2) NOT NULL CHECK (amount > 0),
    currency        text NOT NULL,
    counterpart     text,
    savio_payment_id text UNIQUE,
    bank_line_key   text,
    created_by      text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS allocation (
    allocation_id   serial PRIMARY KEY,
    payment_id      integer REFERENCES payment(payment_id),
    credit_uuid     text,                            -- credit note applied instead of a payment
    invoice_ref     text NOT NULL,                   -- supplier cfdi_uuid or savio_invoice_id
    invoice_side    text NOT NULL CHECK (invoice_side IN ('ap','ar')),
    amount          numeric(14,2) NOT NULL CHECK (amount > 0),
    at              timestamptz NOT NULL DEFAULT now(),
    by_user         text NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_statement (
    statement_id    serial PRIMARY KEY,
    account_id      text NOT NULL REFERENCES bank_account(account_id),
    period_start    date NOT NULL, period_end date NOT NULL,
    opening         numeric(14,2) NOT NULL, closing numeric(14,2) NOT NULL,
    file_drive_id   text, file_sha256 text UNIQUE,
    imported_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (account_id, period_start, period_end)
);
CREATE TABLE IF NOT EXISTS bank_transaction (
    line_key        text PRIMARY KEY,                -- cash.BankLine.key
    statement_id    integer NOT NULL REFERENCES bank_statement(statement_id),
    account_id      text NOT NULL REFERENCES bank_account(account_id),
    tx_date         date NOT NULL,
    amount          numeric(14,2) NOT NULL,
    description     text,
    counterpart     text,
    own_transfer    boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS bank_match (
    match_id        serial PRIMARY KEY,
    line_key        text NOT NULL REFERENCES bank_transaction(line_key),
    target_kind     text NOT NULL CHECK (target_kind IN ('payment','customer_payment','transfer','fee','tax','other')),
    target_ref      text NOT NULL,
    amount          numeric(14,2) NOT NULL,
    at              timestamptz NOT NULL DEFAULT now(),
    by_user         text NOT NULL,
    reversed_at     timestamptz, reversed_by text, reversed_reason text
);

CREATE TABLE IF NOT EXISTS period_close (
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    month           date NOT NULL,                   -- first day
    closed_at       timestamptz NOT NULL DEFAULT now(),
    closed_by       text NOT NULL,
    PRIMARY KEY (entity_id, month)
);
CREATE TABLE IF NOT EXISTS fin_exception (
    exception_id    serial PRIMARY KEY,
    kind            text NOT NULL,                   -- MISSING_PO, OVER_PO, OVERDUE_AR, UNRECONCILED, UNBILLED_MILESTONE, BUDGET_OVERRUN, LATE_DELIVERY, PENDING_APPROVAL, STATEMENT_REJECTED, FOREIGN_CFDI
    ref             text NOT NULL,
    entity_id       text REFERENCES entity(entity_id),
    project_id      text REFERENCES project(project_id),
    owner           text,
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open','in_progress','resolved','overridden')),
    detail          text,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz, resolved_by text, resolution text,
    UNIQUE (kind, ref)
);
CREATE TABLE IF NOT EXISTS fin_event (                      -- append-only audit
    event_id        bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    by_user         text NOT NULL,
    subject_kind    text NOT NULL,                   -- project, po, supplier_invoice, payment, bank_match, budget, change_order, supplier
    subject_ref     text NOT NULL,
    action          text NOT NULL,
    old_value       jsonb, new_value jsonb,
    reason          text
);
CREATE TABLE IF NOT EXISTS savio_event (                    -- raw webhooks, replay-safe
    event_id        text PRIMARY KEY,
    event_type      text NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    payload         jsonb NOT NULL,
    processed_at    timestamptz, error text
);
CREATE TABLE IF NOT EXISTS savio_cursor (
    resource        text PRIMARY KEY,
    cursor          text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS supplier_invoice_project_idx ON supplier_invoice(project_id);
CREATE INDEX IF NOT EXISTS supplier_invoice_status_idx ON supplier_invoice(status);
CREATE INDEX IF NOT EXISTS customer_invoice_project_idx ON customer_invoice(project_id);
-- ------------------------------------------------------------------ v245: the books (CONTPAQi + the accountants' workbook + the business-side workbooks)
CREATE TABLE IF NOT EXISTS fin_source_file (                -- every file the Drive ingest ever read, by content hash
    sha256          text PRIMARY KEY,
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    kind            text NOT NULL,                   -- polizas | auxiliares | acctbook | overview | tracker | pmo_sheet
    name            text NOT NULL,
    drive_id        text,
    modified        timestamptz,
    period          text,                            -- 'YYYY-MM' the file reports through, when it has one
    imported_at     timestamptz NOT NULL DEFAULT now(),
    rows            integer NOT NULL DEFAULT 0,
    notes           text NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gl_account (
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    account         text NOT NULL,                   -- '102-01-001'
    name            text NOT NULL,
    name_en         text NOT NULL DEFAULT '',
    bs_pl           text NOT NULL DEFAULT '',        -- BS | PL
    a_p             text NOT NULL DEFAULT '',        -- A | P | R | C
    report_code     text NOT NULL DEFAULT '',        -- BS_100 | PL_040 …
    report_account  text NOT NULL DEFAULT '',
    nature          text NOT NULL DEFAULT 'debit',   -- debit | credit (how the print shows the balance)
    PRIMARY KEY (entity_id, account)
);
CREATE TABLE IF NOT EXISTS gl_journal (
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    jkey            text NOT NULL,                   -- 'YYYY-MM-DD:kind:number'
    jdate           date NOT NULL,
    kind            text NOT NULL,                   -- Ingresos | Egresos | Diario
    number          integer NOT NULL,
    concept         text NOT NULL DEFAULT '',
    control         text NOT NULL DEFAULT '',
    posted          boolean NOT NULL DEFAULT true,   -- false when the auxiliares print does not carry it
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256),
    PRIMARY KEY (entity_id, jkey)
);
CREATE TABLE IF NOT EXISTS gl_line (
    entity_id       text NOT NULL,
    jkey            text NOT NULL,
    line_no         integer NOT NULL,
    account         text NOT NULL,
    account_name    text NOT NULL DEFAULT '',
    reference       text NOT NULL DEFAULT '',
    segment         integer,                         -- business-case number (project) or NULL
    debit           numeric(16,2) NOT NULL DEFAULT 0,
    credit          numeric(16,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_id, jkey, line_no),
    FOREIGN KEY (entity_id, jkey) REFERENCES gl_journal(entity_id, jkey) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS gl_line_account_idx ON gl_line(entity_id, account);
CREATE INDEX IF NOT EXISTS gl_line_segment_idx ON gl_line(entity_id, segment);
CREATE TABLE IF NOT EXISTS gl_balance (                     -- per account, per period end (from the auxiliares / balanza)
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    period          text NOT NULL,                   -- 'YYYY-MM' (period end) — the YTD print gives one row per account
    account         text NOT NULL,
    name            text NOT NULL DEFAULT '',
    opening         numeric(16,2) NOT NULL DEFAULT 0,   -- as shown (nature-signed)
    debits          numeric(16,2) NOT NULL DEFAULT 0,
    credits         numeric(16,2) NOT NULL DEFAULT 0,
    closing         numeric(16,2) NOT NULL DEFAULT 0,   -- as shown (a supplier in credit reads positive)
    nature          text NOT NULL DEFAULT 'debit',
    movements       integer NOT NULL DEFAULT 0,
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256),
    PRIMARY KEY (entity_id, period, account)
);
CREATE TABLE IF NOT EXISTS fin_report_line (                -- the accountants' P&L / BS / budget by month (thousands MXN)
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    period          text NOT NULL,                   -- workbook period 'YYYY-MM'
    sheet           text NOT NULL,                   -- pl | bs | budget
    code            text NOT NULL,                   -- PL_040 | BS_100 | label:Gross Margin
    label           text NOT NULL,
    ord             integer NOT NULL,
    m01 numeric(14,2), m02 numeric(14,2), m03 numeric(14,2), m04 numeric(14,2), m05 numeric(14,2), m06 numeric(14,2),
    m07 numeric(14,2), m08 numeric(14,2), m09 numeric(14,2), m10 numeric(14,2), m11 numeric(14,2), m12 numeric(14,2),
    ytd             numeric(14,2),
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256),
    PRIMARY KEY (entity_id, period, sheet, code)
);
CREATE TABLE IF NOT EXISTS biz_case (                       -- the accountants' project list (CONTPAQi segments)
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    code            integer NOT NULL,
    name            text NOT NULL DEFAULT '',
    project_type    text NOT NULL DEFAULT '',        -- GM | LAAS | PL_xxx (cost centre) | _
    business_manager text NOT NULL DEFAULT '',
    PRIMARY KEY (entity_id, code)
);
CREATE TABLE IF NOT EXISTS project_margin (                 -- GM_per_Projects: booked vs planned, per workbook period
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    period          text NOT NULL,
    code            integer NOT NULL,
    name            text NOT NULL DEFAULT '',
    revenue_prior   numeric(16,2) NOT NULL DEFAULT 0,
    cos_prior       numeric(16,2) NOT NULL DEFAULT 0,
    revenue_ytd     numeric(16,2) NOT NULL DEFAULT 0,
    cos_ytd         numeric(16,2) NOT NULL DEFAULT 0,
    revenue_total   numeric(16,2) NOT NULL DEFAULT 0,
    cos_total       numeric(16,2) NOT NULL DEFAULT 0,
    gm              numeric(16,2) NOT NULL DEFAULT 0,
    gm_pct          numeric(12,4),
    planned_value   numeric(16,2) NOT NULL DEFAULT 0,
    planned_cost    numeric(16,2) NOT NULL DEFAULT 0,
    planned_margin  numeric(16,2) NOT NULL DEFAULT 0,
    planned_margin_pct numeric(12,4),
    business_manager text NOT NULL DEFAULT '',
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256),
    PRIMARY KEY (entity_id, period, code)
);
CREATE TABLE IF NOT EXISTS portfolio_project (              -- Argia_Projects_Overview_MX.xlsx, sheet Data
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    code            integer NOT NULL,
    project_id      text NOT NULL,                   -- 'ARG1473'
    name            text NOT NULL,
    phase           text NOT NULL,                   -- 0_closing … 8_on hold
    status          text NOT NULL DEFAULT '',
    country         text NOT NULL DEFAULT '',
    business_manager text NOT NULL DEFAULT '',
    project_manager text NOT NULL DEFAULT '',
    value_usd       numeric(16,2),
    value_mxn       numeric(16,2),
    planned_cost_mxn numeric(16,2),
    margin_planned_pct numeric(12,4),
    contract_start  date,
    contract_end    date,
    planned_start   date,
    planned_finish  date,
    subcontractor   text NOT NULL DEFAULT '',
    progress        numeric(12,4),
    handover        date,
    invoiced_mxn    numeric(16,2),
    paid_mxn        numeric(16,2),
    po              text NOT NULL DEFAULT '',
    comment         text NOT NULL DEFAULT '',
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256),
    PRIMARY KEY (entity_id, code, name)             -- a code can carry two rows (a project and its extension)
);
CREATE TABLE IF NOT EXISTS open_item (                      -- the AR/AP tracker workbook, replaced whole on every import
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    item_key        text NOT NULL,                   -- sha of side+company+invoice+folio+issued+amount
    side            text NOT NULL,                   -- ar | ap
    status          text NOT NULL,
    invoice         text NOT NULL DEFAULT '',
    company         text NOT NULL,
    project_code    integer,
    project_name    text NOT NULL DEFAULT '',
    po              text NOT NULL DEFAULT '',
    issued          date,
    due             date,
    new_due         date,
    final_due       date,
    days_to_due     integer,
    currency        text NOT NULL,
    total           numeric(16,2) NOT NULL DEFAULT 0,
    net             numeric(16,2) NOT NULL DEFAULT 0,
    mxn_equiv_net   numeric(16,2) NOT NULL DEFAULT 0,
    folio_fiscal    text NOT NULL DEFAULT '',
    paid_on         date,
    kind            text NOT NULL DEFAULT '',
    comment         text NOT NULL DEFAULT '',
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256),
    PRIMARY KEY (entity_id, item_key)
);
CREATE TABLE IF NOT EXISTS pmo_project (                    -- one ARGIA PROJECT workbook
    project_id      text PRIMARY KEY,                -- 'ARG1473'
    entity_id       text NOT NULL REFERENCES entity(entity_id),
    code            integer,
    name            text NOT NULL DEFAULT '',
    customer        text NOT NULL DEFAULT '',
    location        text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT '',
    phase           text NOT NULL DEFAULT '',
    contract_type   text NOT NULL DEFAULT '',
    manager         text NOT NULL DEFAULT '',
    supervisor      text NOT NULL DEFAULT '',
    start_date      date,
    end_date        date,
    value           numeric(16,2),
    cost            numeric(16,2),
    progress        numeric(12,4),
    sheet_id        text NOT NULL DEFAULT '',
    modified        timestamptz,
    source_sha      text NOT NULL REFERENCES fin_source_file(sha256)
);
CREATE TABLE IF NOT EXISTS pmo_task (
    project_id      text NOT NULL REFERENCES pmo_project(project_id) ON DELETE CASCADE,
    task_id         text NOT NULL,
    wbs             text NOT NULL DEFAULT '',
    name            text NOT NULL DEFAULT '',
    is_phase        boolean NOT NULL DEFAULT false,
    is_milestone    boolean NOT NULL DEFAULT false,
    resource        text NOT NULL DEFAULT '',
    start_date      date,
    end_date        date,
    duration_days   integer,
    priority        text NOT NULL DEFAULT '',
    status          text NOT NULL DEFAULT '',
    progress        numeric(12,4),
    PRIMARY KEY (project_id, task_id)
);
CREATE TABLE IF NOT EXISTS pmo_cost (
    project_id      text NOT NULL REFERENCES pmo_project(project_id) ON DELETE CASCADE,
    cost_id         text NOT NULL,
    cost_date       date,
    category        text NOT NULL DEFAULT '',
    vendor          text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    net             numeric(16,2) NOT NULL DEFAULT 0,
    vat             numeric(16,2) NOT NULL DEFAULT 0,
    total           numeric(16,2) NOT NULL DEFAULT 0,
    cost_status     text NOT NULL DEFAULT '',
    approval        text NOT NULL DEFAULT '',
    approved_by     text NOT NULL DEFAULT '',
    paid            numeric(16,2) NOT NULL DEFAULT 0,
    payment_status  text NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, cost_id)
);
CREATE TABLE IF NOT EXISTS pmo_invoice (
    project_id      text NOT NULL REFERENCES pmo_project(project_id) ON DELETE CASCADE,
    invoice_id      text NOT NULL,
    customer        text NOT NULL DEFAULT '',
    milestone       text NOT NULL DEFAULT '',
    number          text NOT NULL DEFAULT '',
    inv_date        date,
    due             date,
    net             numeric(16,2) NOT NULL DEFAULT 0,
    vat             numeric(16,2) NOT NULL DEFAULT 0,
    total           numeric(16,2) NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT '',
    payment_status  text NOT NULL DEFAULT '',
    paid_on         date,
    received        numeric(16,2) NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, invoice_id)
);
CREATE INDEX IF NOT EXISTS bank_transaction_account_date_idx ON bank_transaction(account_id, tx_date);
CREATE INDEX IF NOT EXISTS fin_event_subject_idx ON fin_event(subject_kind, subject_ref);
"""

TABLES = (
    "entity", "bank_account", "cost_code", "supplier", "customer_master", "fx_rate",
    "project", "project_milestone", "project_task", "budget_version", "budget_line", "change_order",
    "purchase_order", "po_line", "po_approval", "receipt",
    "supplier_invoice", "customer_invoice", "payment", "allocation",
    "bank_statement", "bank_transaction", "bank_match",
    "period_close", "fin_exception", "fin_event", "savio_event", "savio_cursor",
    # v245 — the books
    "fin_source_file", "gl_account", "gl_journal", "gl_line", "gl_balance", "fin_report_line",
    "biz_case", "project_margin", "portfolio_project", "open_item", "pmo_project", "pmo_task", "pmo_cost", "pmo_invoice",
)
