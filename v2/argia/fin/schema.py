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
)
