-- Production schema of argia_mont (pio06), DDL only - no rows. pg_dump --schema-only
-- --no-owner --no-privileges, PostgreSQL 17.11, captured 2026-09-25. Used by tests/portal to
-- build a throwaway database. Refresh it when the schema changes: docs/TEST_HARNESS.md.

CREATE TABLE public.alert_ledger (
    alert_id text NOT NULL,
    alert_key text NOT NULL,
    plant_key text DEFAULT ''::text NOT NULL,
    inverter_sn text DEFAULT ''::text NOT NULL,
    metric text DEFAULT ''::text NOT NULL,
    severity text DEFAULT ''::text NOT NULL,
    state text DEFAULT 'OPEN'::text NOT NULL,
    opened_utc text DEFAULT ''::text NOT NULL,
    last_seen_utc text DEFAULT ''::text NOT NULL,
    resolved_utc text DEFAULT ''::text NOT NULL,
    value numeric,
    threshold numeric,
    message text DEFAULT ''::text NOT NULL,
    channels_sent text DEFAULT ''::text NOT NULL,
    explanation text DEFAULT ''::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.alert_state (
    key text NOT NULL,
    severity text,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_sent timestamp with time zone,
    active boolean DEFAULT true NOT NULL
);

CREATE TABLE public.allocation (
    allocation_id integer NOT NULL,
    payment_id integer,
    credit_uuid text,
    invoice_ref text NOT NULL,
    invoice_side text NOT NULL,
    amount numeric(14,2) NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    by_user text NOT NULL,
    CONSTRAINT allocation_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT allocation_invoice_side_check CHECK ((invoice_side = ANY (ARRAY['ap'::text, 'ar'::text])))
);

CREATE SEQUENCE public.allocation_allocation_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.allocation_allocation_id_seq OWNED BY public.allocation.allocation_id;

CREATE TABLE public.ask_log (
    id integer NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    username text DEFAULT ''::text NOT NULL,
    question text NOT NULL,
    answer text,
    tools jsonb,
    model text,
    input_tokens integer,
    output_tokens integer,
    latency_ms integer,
    turns integer,
    error text
);

CREATE SEQUENCE public.ask_log_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.ask_log_id_seq OWNED BY public.ask_log.id;

CREATE TABLE public.bank_account (
    account_id text NOT NULL,
    entity_id text NOT NULL,
    bank text NOT NULL,
    currency text NOT NULL,
    clabe_last4 text,
    purpose text,
    statement_format text DEFAULT 'argia_generic'::text NOT NULL,
    active boolean DEFAULT true NOT NULL
);

CREATE TABLE public.bank_match (
    match_id integer NOT NULL,
    line_key text NOT NULL,
    target_kind text NOT NULL,
    target_ref text NOT NULL,
    amount numeric(14,2) NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    by_user text NOT NULL,
    reversed_at timestamp with time zone,
    reversed_by text,
    reversed_reason text,
    CONSTRAINT bank_match_target_kind_check CHECK ((target_kind = ANY (ARRAY['payment'::text, 'customer_payment'::text, 'transfer'::text, 'fee'::text, 'tax'::text, 'other'::text])))
);

CREATE SEQUENCE public.bank_match_match_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.bank_match_match_id_seq OWNED BY public.bank_match.match_id;

CREATE TABLE public.bank_statement (
    statement_id integer NOT NULL,
    account_id text NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    opening numeric(14,2) NOT NULL,
    closing numeric(14,2) NOT NULL,
    file_drive_id text,
    file_sha256 text,
    imported_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.bank_statement_statement_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.bank_statement_statement_id_seq OWNED BY public.bank_statement.statement_id;

CREATE TABLE public.bank_transaction (
    line_key text NOT NULL,
    statement_id integer NOT NULL,
    account_id text NOT NULL,
    tx_date date NOT NULL,
    amount numeric(14,2) NOT NULL,
    description text,
    counterpart text,
    own_transfer boolean DEFAULT false NOT NULL
);

CREATE TABLE public.biz_case (
    entity_id text NOT NULL,
    code integer NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    project_type text DEFAULT ''::text NOT NULL,
    business_manager text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.budget_line (
    project_id text NOT NULL,
    version integer NOT NULL,
    cost_code text NOT NULL,
    amount numeric(14,2) NOT NULL
);

CREATE TABLE public.budget_version (
    project_id text NOT NULL,
    version integer NOT NULL,
    status text NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    note text,
    CONSTRAINT budget_version_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'approved'::text, 'superseded'::text, 'rejected'::text])))
);

CREATE TABLE public.cfe_pipeline_status (
    id integer DEFAULT 1 NOT NULL,
    heartbeat_ts timestamp with time zone,
    probe_status text,
    probe_rows integer,
    sent_month text,
    last_csv text,
    last_csv_result text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.cfe_tariff (
    tariff_code text NOT NULL,
    region text NOT NULL,
    month date NOT NULL,
    charge_type text NOT NULL,
    unit text,
    value_mxn numeric(14,6),
    source text NOT NULL,
    loaded_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.change_order (
    co_id integer NOT NULL,
    project_id text NOT NULL,
    ref text NOT NULL,
    status text NOT NULL,
    revenue_impact numeric(14,2) DEFAULT 0 NOT NULL,
    cost_impact numeric(14,2) DEFAULT 0 NOT NULL,
    approved_by text,
    approved_at timestamp with time zone,
    CONSTRAINT change_order_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'approved'::text, 'rejected'::text])))
);

CREATE SEQUENCE public.change_order_co_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.change_order_co_id_seq OWNED BY public.change_order.co_id;

CREATE TABLE public.contract_monthly (
    plant_key text NOT NULL,
    year integer NOT NULL,
    month integer NOT NULL,
    design_kwh numeric(12,3),
    contract_kwh numeric(12,3),
    tariff_mxn numeric(8,4),
    fixed_income_ccy numeric(12,2),
    ccy text,
    CONSTRAINT contract_monthly_month_check CHECK (((month >= 1) AND (month <= 12)))
);

CREATE TABLE public.cost_center (
    entity_id text NOT NULL,
    code integer NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    kind text DEFAULT 'other'::text NOT NULL,
    grp text DEFAULT ''::text NOT NULL,
    manual boolean DEFAULT false NOT NULL
);

CREATE TABLE public.cost_code (
    code text NOT NULL,
    parent text,
    name_en text NOT NULL,
    name_es text NOT NULL,
    active boolean DEFAULT true NOT NULL
);

CREATE TABLE public.customer_invoice (
    savio_invoice_id text NOT NULL,
    cfdi_uuid text,
    entity_id text NOT NULL,
    customer_id integer,
    project_id text,
    plant_key text,
    issue_date date NOT NULL,
    due_date date,
    currency text NOT NULL,
    subtotal numeric(14,2) NOT NULL,
    tax numeric(14,2) NOT NULL,
    total numeric(14,2) NOT NULL,
    status text NOT NULL,
    savio_updated_at timestamp with time zone,
    synced_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.customer_master (
    customer_id integer NOT NULL,
    rfc text,
    name text NOT NULL,
    savio_customer_id text,
    terms_days integer DEFAULT 30 NOT NULL
);

CREATE SEQUENCE public.customer_master_customer_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.customer_master_customer_id_seq OWNED BY public.customer_master.customer_id;

CREATE TABLE public.daily_production (
    plant_key text NOT NULL,
    prod_date date NOT NULL,
    energy_kwh numeric(12,3),
    irradiance_kwh_m2 numeric(8,4),
    pr numeric(6,4),
    pr_stc numeric(6,4),
    expected_kwh numeric(12,3),
    billable_kwh numeric(12,3),
    cloud_cover_pct numeric(6,2),
    availability numeric(6,4),
    inverters_reporting integer,
    data_class text,
    status_note text,
    source text NOT NULL,
    loaded_at timestamp with time zone DEFAULT now() NOT NULL,
    irradiance_source text,
    pr_confidence text,
    capacity_factor numeric,
    capacity_factor_confidence text,
    inverters_with_reboot integer,
    notes text,
    written_at_utc text,
    specific_yield numeric,
    soiling_loss_pct numeric,
    production_pct numeric,
    design_kwh numeric,
    CONSTRAINT daily_production_energy_kwh_check CHECK ((energy_kwh >= (0)::numeric)),
    CONSTRAINT daily_production_irradiance_kwh_m2_check CHECK ((irradiance_kwh_m2 >= (0)::numeric)),
    CONSTRAINT daily_production_source_check CHECK ((source = ANY (ARRAY['v1'::text, 'v2'::text])))
);

CREATE TABLE public.dashboard_inverter (
    date_mx date,
    bucket_ts timestamp without time zone,
    hour_label text,
    plant_key text,
    customer text,
    inverter_sn text,
    inverter_label text,
    energy_kwh numeric,
    power_w numeric,
    temperature_c numeric,
    status text,
    status_reason text,
    peer_median_kwh numeric,
    expected_share_kwh numeric,
    production_pct numeric,
    est_loss_kwh numeric,
    fault_events integer,
    written_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.dashboard_plant (
    date_mx date,
    bucket_ts timestamp without time zone,
    hour_label text,
    plant_key text,
    customer text,
    kwp_dc numeric,
    tariff_mxn_per_kwh numeric,
    data_start text,
    total_kwh numeric,
    theoretical_kwh numeric,
    irradiance_kwh_m2 numeric,
    irradiance_wm2 numeric,
    cloud_cover_pct numeric,
    module_temp_c numeric,
    ambient_temp_c numeric,
    inverters_total integer,
    inverters_reporting integer,
    inverters_faulted integer,
    production_pct numeric,
    written_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.entity (
    entity_id text NOT NULL,
    name text NOT NULL,
    rfc text,
    currency text DEFAULT 'MXN'::text NOT NULL,
    active boolean DEFAULT true NOT NULL
);

CREATE TABLE public.fin_event (
    event_id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    by_user text NOT NULL,
    subject_kind text NOT NULL,
    subject_ref text NOT NULL,
    action text NOT NULL,
    old_value jsonb,
    new_value jsonb,
    reason text
);

CREATE SEQUENCE public.fin_event_event_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.fin_event_event_id_seq OWNED BY public.fin_event.event_id;

CREATE TABLE public.fin_exception (
    exception_id integer NOT NULL,
    kind text NOT NULL,
    ref text NOT NULL,
    entity_id text,
    project_id text,
    owner text,
    status text DEFAULT 'open'::text NOT NULL,
    detail text,
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    resolved_at timestamp with time zone,
    resolved_by text,
    resolution text,
    CONSTRAINT fin_exception_status_check CHECK ((status = ANY (ARRAY['open'::text, 'in_progress'::text, 'resolved'::text, 'overridden'::text])))
);

CREATE SEQUENCE public.fin_exception_exception_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.fin_exception_exception_id_seq OWNED BY public.fin_exception.exception_id;

CREATE TABLE public.fin_report_line (
    entity_id text NOT NULL,
    period text NOT NULL,
    sheet text NOT NULL,
    code text NOT NULL,
    label text NOT NULL,
    ord integer NOT NULL,
    m01 numeric(14,2),
    m02 numeric(14,2),
    m03 numeric(14,2),
    m04 numeric(14,2),
    m05 numeric(14,2),
    m06 numeric(14,2),
    m07 numeric(14,2),
    m08 numeric(14,2),
    m09 numeric(14,2),
    m10 numeric(14,2),
    m11 numeric(14,2),
    m12 numeric(14,2),
    ytd numeric(14,2),
    source_sha text NOT NULL
);

CREATE TABLE public.fin_source_file (
    sha256 text NOT NULL,
    entity_id text NOT NULL,
    kind text NOT NULL,
    name text NOT NULL,
    drive_id text,
    modified timestamp with time zone,
    period text,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    rows integer DEFAULT 0 NOT NULL,
    notes text DEFAULT ''::text NOT NULL,
    mime text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.finance_audit (
    id integer NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    username text NOT NULL,
    plant_key text,
    loan_id text,
    action text NOT NULL,
    detail text
);

CREATE SEQUENCE public.finance_audit_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.finance_audit_id_seq OWNED BY public.finance_audit.id;

CREATE TABLE public.fx_rate (
    rate_date date NOT NULL,
    pair text NOT NULL,
    rate numeric(12,6) NOT NULL,
    source text NOT NULL
);

CREATE TABLE public.gl_account (
    entity_id text NOT NULL,
    account text NOT NULL,
    name text NOT NULL,
    name_en text DEFAULT ''::text NOT NULL,
    bs_pl text DEFAULT ''::text NOT NULL,
    a_p text DEFAULT ''::text NOT NULL,
    report_code text DEFAULT ''::text NOT NULL,
    report_account text DEFAULT ''::text NOT NULL,
    nature text DEFAULT 'debit'::text NOT NULL
);

CREATE TABLE public.gl_balance (
    entity_id text NOT NULL,
    period text NOT NULL,
    account text NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    opening numeric(16,2) DEFAULT 0 NOT NULL,
    debits numeric(16,2) DEFAULT 0 NOT NULL,
    credits numeric(16,2) DEFAULT 0 NOT NULL,
    closing numeric(16,2) DEFAULT 0 NOT NULL,
    nature text DEFAULT 'debit'::text NOT NULL,
    movements integer DEFAULT 0 NOT NULL,
    source_sha text NOT NULL
);

CREATE TABLE public.gl_journal (
    entity_id text NOT NULL,
    jkey text NOT NULL,
    jdate date NOT NULL,
    kind text NOT NULL,
    number integer NOT NULL,
    concept text DEFAULT ''::text NOT NULL,
    control text DEFAULT ''::text NOT NULL,
    posted boolean DEFAULT true NOT NULL,
    source_sha text NOT NULL
);

CREATE TABLE public.gl_line (
    entity_id text NOT NULL,
    jkey text NOT NULL,
    line_no integer NOT NULL,
    account text NOT NULL,
    account_name text DEFAULT ''::text NOT NULL,
    reference text DEFAULT ''::text NOT NULL,
    segment integer,
    debit numeric(16,2) DEFAULT 0 NOT NULL,
    credit numeric(16,2) DEFAULT 0 NOT NULL
);

CREATE TABLE public.inverter (
    plant_key text NOT NULL,
    inverter_sn text NOT NULL,
    inverter_label text,
    rated_kw numeric(10,3),
    phase text,
    date_producing date,
    date_decommissioned date,
    active boolean DEFAULT true NOT NULL,
    mppt_count text,
    strings_total text,
    rated_kw_dc text,
    in_service_today boolean,
    CONSTRAINT inverter_rated_kw_check CHECK ((rated_kw > (0)::numeric))
);

CREATE TABLE public.invoicing (
    plant_key text NOT NULL,
    ref_month date NOT NULL,
    billable_kwh numeric(14,3),
    tariff_mxn numeric(10,4),
    amount_mxn numeric(14,2),
    billing_kwh numeric(14,3),
    delta_kwh numeric(14,3),
    delta_pct numeric(9,4),
    check_status text NOT NULL,
    published_at timestamp with time zone DEFAULT now() NOT NULL,
    produced_kwh numeric(14,3),
    penalty_kwh numeric(14,3),
    expected_kwh numeric(14,3),
    source text
);

CREATE TABLE public.knowledge (
    doc text NOT NULL,
    lang text NOT NULL,
    n integer NOT NULL,
    title text DEFAULT ''::text NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    tsv tsvector GENERATED ALWAYS AS ((setweight(to_tsvector('simple'::regconfig, COALESCE(title, ''::text)), 'A'::"char") || setweight(to_tsvector('simple'::regconfig, COALESCE(body, ''::text)), 'B'::"char"))) STORED,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.loan (
    loan_id text NOT NULL,
    plant_key text,
    project_name text,
    bank text,
    currency text,
    principal_mxn numeric(14,2),
    total_installments integer,
    first_month date,
    last_month date
);

CREATE TABLE public.loan_schedule (
    loan_id text NOT NULL,
    ref_month date NOT NULL,
    installment_no integer,
    payment_mxn numeric(14,2),
    payment_ccy numeric(14,2),
    xr numeric(10,4),
    due_after_mxn numeric(14,2)
);

CREATE TABLE public.mail_recipient (
    email text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    note text,
    added_by text,
    added_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.mail_subscription (
    email text NOT NULL,
    channel text NOT NULL,
    plants text DEFAULT ''::text NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    username text DEFAULT ''::text NOT NULL,
    added_by text,
    added_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT mail_subscription_channel_check CHECK ((channel = ANY (ARRAY['maintenance'::text, 'financial'::text, 'daily'::text, 'reports'::text])))
);

CREATE TABLE public.maintenance_event (
    id integer NOT NULL,
    plant_key text NOT NULL,
    start_ts timestamp with time zone NOT NULL,
    end_ts timestamp with time zone,
    category text DEFAULT 'customer'::text NOT NULL,
    cost_type text,
    cost_mxn numeric(12,2),
    note text,
    approved_by text,
    created_by text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.maintenance_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.maintenance_event_id_seq OWNED BY public.maintenance_event.id;

CREATE TABLE public.open_item (
    entity_id text NOT NULL,
    item_key text NOT NULL,
    side text NOT NULL,
    status text NOT NULL,
    invoice text DEFAULT ''::text NOT NULL,
    company text NOT NULL,
    project_code integer,
    project_name text DEFAULT ''::text NOT NULL,
    po text DEFAULT ''::text NOT NULL,
    issued date,
    due date,
    new_due date,
    final_due date,
    days_to_due integer,
    currency text NOT NULL,
    total numeric(16,2) DEFAULT 0 NOT NULL,
    net numeric(16,2) DEFAULT 0 NOT NULL,
    mxn_equiv_net numeric(16,2) DEFAULT 0 NOT NULL,
    folio_fiscal text DEFAULT ''::text NOT NULL,
    paid_on date,
    kind text DEFAULT ''::text NOT NULL,
    comment text DEFAULT ''::text NOT NULL,
    source_sha text NOT NULL,
    src_sheet text DEFAULT ''::text NOT NULL,
    src_row integer DEFAULT 0 NOT NULL,
    src_gid text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.payment (
    payment_id integer NOT NULL,
    entity_id text NOT NULL,
    account_id text NOT NULL,
    direction text NOT NULL,
    pay_date date NOT NULL,
    amount numeric(14,2) NOT NULL,
    currency text NOT NULL,
    counterpart text,
    savio_payment_id text,
    bank_line_key text,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT payment_amount_check CHECK ((amount > (0)::numeric)),
    CONSTRAINT payment_direction_check CHECK ((direction = ANY (ARRAY['out'::text, 'in'::text])))
);

CREATE SEQUENCE public.payment_payment_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.payment_payment_id_seq OWNED BY public.payment.payment_id;

CREATE TABLE public.period_close (
    entity_id text NOT NULL,
    month date NOT NULL,
    closed_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_by text NOT NULL
);

CREATE TABLE public.plant (
    plant_key text NOT NULL,
    customer text NOT NULL,
    brand text NOT NULL,
    site_id text,
    kwp_dc numeric(10,3) NOT NULL,
    kwp_ac numeric(10,3),
    lat numeric(9,6),
    lon numeric(9,6),
    portfolio text,
    tariff_mxn_per_kwh numeric(8,4),
    pr_baseline numeric(5,4),
    contracted_kwh numeric(12,3),
    active boolean DEFAULT true NOT NULL,
    om_cost_monthly_mxn numeric(10,2),
    investment_mxn numeric(14,2),
    sla_target numeric(5,4),
    expected_factor text,
    pr_target text,
    installation_date date,
    secret_api_name text,
    secret_user_name text,
    secret_pass_name text,
    weather_plant_id text,
    datalogger_sn text,
    datalogger_addr text,
    module_count text,
    module_wp text,
    string_count text,
    tilt_deg text,
    azimuth_deg text,
    notes text,
    kwp_dc_override text,
    kwp_dc_check text,
    pr_stc_model text,
    gamma_pmax text,
    monitoring_class text,
    p90_annual_kwh text,
    date_interconnection date,
    billing_scheme text,
    module_model text,
    show_dashboard boolean,
    show_daily_report boolean,
    show_financial boolean,
    client_channel text,
    CONSTRAINT plant_brand_check CHECK ((brand = ANY (ARRAY['GROWATT'::text, 'HUAWEI'::text, 'SOLAREDGE'::text, 'SMA'::text]))),
    CONSTRAINT plant_kwp_ac_check CHECK ((kwp_ac > (0)::numeric)),
    CONSTRAINT plant_kwp_dc_check CHECK ((kwp_dc > (0)::numeric)),
    CONSTRAINT plant_lon_check CHECK ((lon < (0)::numeric)),
    CONSTRAINT plant_portfolio_check CHECK ((portfolio = ANY (ARRAY['PPA'::text, 'CAPEX'::text])))
);

CREATE TABLE public.pmo_cost (
    project_id text NOT NULL,
    cost_id text NOT NULL,
    cost_date date,
    category text DEFAULT ''::text NOT NULL,
    vendor text DEFAULT ''::text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    net numeric(16,2) DEFAULT 0 NOT NULL,
    vat numeric(16,2) DEFAULT 0 NOT NULL,
    total numeric(16,2) DEFAULT 0 NOT NULL,
    cost_status text DEFAULT ''::text NOT NULL,
    approval text DEFAULT ''::text NOT NULL,
    approved_by text DEFAULT ''::text NOT NULL,
    paid numeric(16,2) DEFAULT 0 NOT NULL,
    payment_status text DEFAULT ''::text NOT NULL,
    src_sheet text DEFAULT ''::text NOT NULL,
    src_row integer DEFAULT 0 NOT NULL,
    src_gid text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.pmo_invoice (
    project_id text NOT NULL,
    invoice_id text NOT NULL,
    customer text DEFAULT ''::text NOT NULL,
    milestone text DEFAULT ''::text NOT NULL,
    number text DEFAULT ''::text NOT NULL,
    inv_date date,
    due date,
    net numeric(16,2) DEFAULT 0 NOT NULL,
    vat numeric(16,2) DEFAULT 0 NOT NULL,
    total numeric(16,2) DEFAULT 0 NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    payment_status text DEFAULT ''::text NOT NULL,
    paid_on date,
    received numeric(16,2) DEFAULT 0 NOT NULL,
    src_sheet text DEFAULT ''::text NOT NULL,
    src_row integer DEFAULT 0 NOT NULL,
    src_gid text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.pmo_project (
    project_id text NOT NULL,
    entity_id text NOT NULL,
    code integer,
    name text DEFAULT ''::text NOT NULL,
    customer text DEFAULT ''::text NOT NULL,
    location text DEFAULT ''::text NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    phase text DEFAULT ''::text NOT NULL,
    contract_type text DEFAULT ''::text NOT NULL,
    manager text DEFAULT ''::text NOT NULL,
    supervisor text DEFAULT ''::text NOT NULL,
    start_date date,
    end_date date,
    value numeric(16,2),
    cost numeric(16,2),
    progress numeric(12,4),
    sheet_id text DEFAULT ''::text NOT NULL,
    modified timestamp with time zone,
    source_sha text NOT NULL
);

CREATE TABLE public.pmo_task (
    project_id text NOT NULL,
    task_id text NOT NULL,
    wbs text DEFAULT ''::text NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    is_phase boolean DEFAULT false NOT NULL,
    is_milestone boolean DEFAULT false NOT NULL,
    resource text DEFAULT ''::text NOT NULL,
    start_date date,
    end_date date,
    duration_days integer,
    priority text DEFAULT ''::text NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    progress numeric(12,4),
    src_sheet text DEFAULT ''::text NOT NULL,
    src_row integer DEFAULT 0 NOT NULL,
    src_gid text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.po_approval (
    approval_id integer NOT NULL,
    po_id integer NOT NULL,
    level_name text NOT NULL,
    approver text NOT NULL,
    decision text NOT NULL,
    total_at numeric(14,2) NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    note text,
    CONSTRAINT po_approval_decision_check CHECK ((decision = ANY (ARRAY['approved'::text, 'rejected'::text, 'reset'::text])))
);

CREATE SEQUENCE public.po_approval_approval_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.po_approval_approval_id_seq OWNED BY public.po_approval.approval_id;

CREATE TABLE public.po_line (
    po_id integer NOT NULL,
    line_no integer NOT NULL,
    cost_code text NOT NULL,
    description text,
    qty numeric(14,4) DEFAULT 1 NOT NULL,
    unit_price numeric(14,4) NOT NULL
);

CREATE TABLE public.portfolio_project (
    entity_id text NOT NULL,
    code integer NOT NULL,
    project_id text NOT NULL,
    name text NOT NULL,
    phase text NOT NULL,
    status text DEFAULT ''::text NOT NULL,
    country text DEFAULT ''::text NOT NULL,
    business_manager text DEFAULT ''::text NOT NULL,
    project_manager text DEFAULT ''::text NOT NULL,
    value_usd numeric(16,2),
    value_mxn numeric(16,2),
    planned_cost_mxn numeric(16,2),
    margin_planned_pct numeric(12,4),
    contract_start date,
    contract_end date,
    planned_start date,
    planned_finish date,
    subcontractor text DEFAULT ''::text NOT NULL,
    progress numeric(12,4),
    handover date,
    invoiced_mxn numeric(16,2),
    paid_mxn numeric(16,2),
    po text DEFAULT ''::text NOT NULL,
    comment text DEFAULT ''::text NOT NULL,
    source_sha text NOT NULL,
    src_sheet text DEFAULT ''::text NOT NULL,
    src_row integer DEFAULT 0 NOT NULL,
    src_gid text DEFAULT ''::text NOT NULL
);

CREATE TABLE public.project (
    project_id text NOT NULL,
    entity_id text NOT NULL,
    customer_id integer,
    name text NOT NULL,
    site text,
    project_type text NOT NULL,
    kwp_dc numeric(10,3),
    status text DEFAULT 'draft'::text NOT NULL,
    pm_user text,
    offer_ref text,
    contract_value numeric(14,2),
    contract_ccy text DEFAULT 'MXN'::text NOT NULL,
    drive_folder_id text,
    pmo_sheet_id text,
    plant_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    created_by text,
    CONSTRAINT project_project_type_check CHECK ((project_type = ANY (ARRAY['PPA'::text, 'CAPEX'::text, 'LAAS'::text, 'EPC'::text, 'OM'::text, 'OTHER'::text]))),
    CONSTRAINT project_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'active'::text, 'on_hold'::text, 'commissioned'::text, 'closed'::text, 'cancelled'::text])))
);

CREATE TABLE public.project_margin (
    entity_id text NOT NULL,
    period text NOT NULL,
    code integer NOT NULL,
    name text DEFAULT ''::text NOT NULL,
    revenue_prior numeric(16,2) DEFAULT 0 NOT NULL,
    cos_prior numeric(16,2) DEFAULT 0 NOT NULL,
    revenue_ytd numeric(16,2) DEFAULT 0 NOT NULL,
    cos_ytd numeric(16,2) DEFAULT 0 NOT NULL,
    revenue_total numeric(16,2) DEFAULT 0 NOT NULL,
    cos_total numeric(16,2) DEFAULT 0 NOT NULL,
    gm numeric(16,2) DEFAULT 0 NOT NULL,
    gm_pct numeric(12,4),
    planned_value numeric(16,2) DEFAULT 0 NOT NULL,
    planned_cost numeric(16,2) DEFAULT 0 NOT NULL,
    planned_margin numeric(16,2) DEFAULT 0 NOT NULL,
    planned_margin_pct numeric(12,4),
    business_manager text DEFAULT ''::text NOT NULL,
    source_sha text NOT NULL
);

CREATE TABLE public.project_milestone (
    project_id text NOT NULL,
    ref text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    baseline_date date NOT NULL,
    planned_date date NOT NULL,
    actual_date date,
    billable boolean DEFAULT false NOT NULL,
    amount numeric(14,2),
    billed_invoice text,
    depends_on text[],
    CONSTRAINT project_milestone_kind_check CHECK ((kind = ANY (ARRAY['commercial'::text, 'technical'::text])))
);

CREATE TABLE public.project_task (
    project_id text NOT NULL,
    task_id text NOT NULL,
    name text NOT NULL,
    phase text,
    start_date date,
    end_date date,
    progress_pct numeric(5,2),
    resource text,
    snapshot_at timestamp with time zone NOT NULL
);

CREATE TABLE public.purchase_order (
    po_id integer NOT NULL,
    entity_id text NOT NULL,
    po_number text NOT NULL,
    supplier_id integer NOT NULL,
    project_id text,
    status text DEFAULT 'draft'::text NOT NULL,
    currency text DEFAULT 'MXN'::text NOT NULL,
    fx_rate numeric(12,6),
    subtotal numeric(14,2) DEFAULT 0 NOT NULL,
    tax numeric(14,2) DEFAULT 0 NOT NULL,
    total numeric(14,2) DEFAULT 0 NOT NULL,
    terms_days integer,
    expected_delivery date,
    created_by text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT purchase_order_status_check CHECK ((status = ANY (ARRAY['draft'::text, 'submitted'::text, 'approved'::text, 'partially_received'::text, 'closed'::text, 'cancelled'::text])))
);

CREATE SEQUENCE public.purchase_order_po_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.purchase_order_po_id_seq OWNED BY public.purchase_order.po_id;

CREATE TABLE public.receipt (
    receipt_id integer NOT NULL,
    po_id integer NOT NULL,
    line_no integer NOT NULL,
    value numeric(14,2) NOT NULL,
    received_on date NOT NULL,
    received_by text NOT NULL
);

CREATE SEQUENCE public.receipt_receipt_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.receipt_receipt_id_seq OWNED BY public.receipt.receipt_id;

CREATE TABLE public.reconciliation_daily (
    plant_key text NOT NULL,
    prod_date date NOT NULL,
    interval_kwh numeric(14,3),
    vendor_daily_kwh numeric(14,3),
    kpi_kwh numeric(14,3),
    completeness_pct numeric(6,2),
    variance_pct numeric(9,3),
    status text NOT NULL,
    note text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL,
    reference_kwh numeric(12,3),
    reference_basis text
);

CREATE TABLE public.reconciliation_monthly (
    plant_key text NOT NULL,
    ref_month date NOT NULL,
    interval_sum_kwh numeric(14,3),
    vendor_daily_sum_kwh numeric(14,3),
    vendor_monthly_kwh numeric(14,3),
    lifetime_delta_kwh numeric(14,3),
    completeness_pct numeric(6,2),
    check1_pct numeric(9,3),
    check2_pct numeric(9,3),
    check4_pct numeric(9,3),
    billing_kwh numeric(14,3),
    billing_basis text,
    status text NOT NULL,
    note text,
    closed_at timestamp with time zone,
    closed_by text,
    checked_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.satellite_check (
    plant_key text NOT NULL,
    check_date date NOT NULL,
    status text NOT NULL,
    drift_pct numeric,
    recent_median numeric,
    baseline_median numeric,
    n_recent integer NOT NULL,
    n_baseline integer NOT NULL,
    note text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.savio_check (
    check_id integer NOT NULL,
    entity_id text NOT NULL,
    checked_at timestamp with time zone NOT NULL,
    source text NOT NULL,
    kind text NOT NULL,
    severity text DEFAULT 'info'::text NOT NULL,
    savio_ref text DEFAULT ''::text NOT NULL,
    our_ref text DEFAULT ''::text NOT NULL,
    amount numeric(16,2) DEFAULT 0 NOT NULL,
    currency text DEFAULT ''::text NOT NULL,
    detail text DEFAULT ''::text NOT NULL
);

CREATE SEQUENCE public.savio_check_check_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.savio_check_check_id_seq OWNED BY public.savio_check.check_id;

CREATE TABLE public.savio_cursor (
    resource text NOT NULL,
    cursor text,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE public.savio_event (
    event_id text NOT NULL,
    event_type text NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    payload jsonb NOT NULL,
    processed_at timestamp with time zone,
    error text
);

CREATE TABLE public.string_daily (
    plant_key text NOT NULL,
    inverter_sn text NOT NULL,
    prod_date date NOT NULL,
    channel text NOT NULL,
    kind text NOT NULL,
    energy_kwh numeric(10,3),
    q_ah numeric(12,3),
    v_avg numeric(8,2),
    share numeric(6,4),
    samples integer NOT NULL,
    str_unmatch integer,
    str_unblance integer,
    loaded_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT string_daily_kind_check CHECK ((kind = ANY (ARRAY['mppt'::text, 'string'::text])))
);

CREATE TABLE public.supplier (
    supplier_id integer NOT NULL,
    entity_id text NOT NULL,
    rfc text NOT NULL,
    name text NOT NULL,
    bank_clabe text,
    bank_name text,
    terms_days integer DEFAULT 30 NOT NULL,
    active boolean DEFAULT true NOT NULL
);

CREATE TABLE public.supplier_invoice (
    cfdi_uuid text NOT NULL,
    entity_id text NOT NULL,
    supplier_id integer,
    emisor_rfc text NOT NULL,
    project_id text,
    po_id integer,
    cost_code text,
    issue_date date NOT NULL,
    due_date date,
    currency text NOT NULL,
    fx_rate numeric(12,6),
    subtotal numeric(14,2) NOT NULL,
    tax numeric(14,2) NOT NULL,
    retention numeric(14,2) DEFAULT 0 NOT NULL,
    total numeric(14,2) NOT NULL,
    tipo text NOT NULL,
    related_uuid text,
    status text DEFAULT 'received'::text NOT NULL,
    xml_drive_id text,
    imported_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT supplier_invoice_status_check CHECK ((status = ANY (ARRAY['received'::text, 'matched'::text, 'exception'::text, 'approved'::text, 'partially_paid'::text, 'paid'::text, 'rejected'::text, 'cancelled'::text])))
);

CREATE SEQUENCE public.supplier_supplier_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.supplier_supplier_id_seq OWNED BY public.supplier.supplier_id;

CREATE TABLE public.sync_run (
    id bigint NOT NULL,
    run_id text NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    script text NOT NULL,
    status text NOT NULL,
    processed integer DEFAULT 0 NOT NULL,
    rows_written integer DEFAULT 0 NOT NULL,
    error text DEFAULT ''::text NOT NULL,
    host text DEFAULT ''::text NOT NULL,
    logged_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.sync_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.sync_run_id_seq OWNED BY public.sync_run.id;

CREATE TABLE public.telemetry (
    ts_utc timestamp with time zone NOT NULL,
    plant_key text NOT NULL,
    inverter_sn text NOT NULL,
    status integer,
    power_w numeric(12,2),
    etoday_kwh numeric(12,3),
    temperature_c numeric(6,2),
    irradiance_wm2 numeric(8,2),
    module_temp_c numeric(6,2),
    ambient_temp_c numeric(6,2),
    cloud_cover_pct numeric(6,2),
    vendor text,
    inverter_label text,
    fault_code text,
    irradiance_kwh_m2_5m numeric(10,4)
);

CREATE TABLE public.telemetry_detail (
    ts_utc timestamp with time zone NOT NULL,
    plant_key text NOT NULL,
    inverter_sn text NOT NULL,
    pac_w numeric(12,2),
    iac_a numeric(8,2),
    pf numeric(6,3),
    pacr_w numeric(12,2),
    pacs_w numeric(12,2),
    pact_w numeric(12,2),
    vacr_v numeric(7,2),
    vacs_v numeric(7,2),
    vact_v numeric(7,2),
    vac_rs_v numeric(7,2),
    vac_st_v numeric(7,2),
    vac_tr_v numeric(7,2),
    fac_hz numeric(6,3),
    ppv_w numeric(12,2),
    warn_code integer,
    warn_code_1 integer,
    fault_code_1 integer,
    fault_code_2 integer,
    fault_type integer,
    pid_status integer,
    pid_fault_code integer,
    apf_status integer,
    afci_status integer,
    derating_mode integer,
    real_op_percent integer,
    pv_iso numeric(10,2),
    p_bus_voltage_v numeric(7,2),
    n_bus_voltage_v numeric(7,2),
    str_unmatch integer,
    str_unblance integer,
    str_break integer,
    gfci_ma numeric(8,2),
    vpv_v numeric[],
    ppv_mppt_w numeric[],
    istring_a numeric[],
    epv_today_kwh numeric[]
);

CREATE TABLE public.thermal_bins (
    plant_key text NOT NULL,
    inverter_sn text NOT NULL,
    prod_date date NOT NULL,
    bin_c numeric(5,1) NOT NULL,
    n integer NOT NULL,
    ratio_sum numeric(12,4) NOT NULL,
    ratio_min numeric(6,3)
);

CREATE TABLE public.thermal_daily (
    plant_key text NOT NULL,
    inverter_sn text NOT NULL,
    prod_date date NOT NULL,
    samples integer,
    peak_c numeric(5,1),
    mean_c numeric(5,1),
    minutes_over_65 integer,
    minutes_over_70 integer,
    events integer,
    dt_peer_peak_c numeric(5,1),
    dt_ambient_peak_c numeric(5,1),
    derating_minutes integer,
    lost_kwh numeric(10,2),
    energy_kwh numeric(10,1),
    cool_ratio numeric(6,3),
    band text,
    cooling_health text,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    vendor_derating_minutes integer
);

CREATE TABLE public.ticket (
    id integer NOT NULL,
    number text NOT NULL,
    plant_key text NOT NULL,
    inverter_sn text DEFAULT ''::text NOT NULL,
    title text NOT NULL,
    description text DEFAULT ''::text NOT NULL,
    category text DEFAULT 'other'::text NOT NULL,
    priority text DEFAULT 'P3'::text NOT NULL,
    status text DEFAULT 'NEW'::text NOT NULL,
    created_by text NOT NULL,
    assigned_to text DEFAULT ''::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    verification_at timestamp with time zone,
    resolved_at timestamp with time zone,
    closed_at timestamp with time zone,
    root_cause text DEFAULT ''::text NOT NULL,
    resolution text DEFAULT ''::text NOT NULL,
    lost_kwh numeric(12,1)
);

CREATE TABLE public.ticket_alert (
    ticket_id integer NOT NULL,
    alert_key text NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    occurrences integer DEFAULT 1 NOT NULL
);

CREATE TABLE public.ticket_attachment (
    id integer NOT NULL,
    ticket_id integer NOT NULL,
    event_id integer,
    filename text NOT NULL,
    stored_as text NOT NULL,
    bytes integer DEFAULT 0 NOT NULL,
    mime text DEFAULT ''::text NOT NULL,
    uploaded_by text DEFAULT ''::text NOT NULL,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE SEQUENCE public.ticket_attachment_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.ticket_attachment_id_seq OWNED BY public.ticket_attachment.id;

CREATE TABLE public.ticket_event (
    id integer NOT NULL,
    ticket_id integer NOT NULL,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    actor text DEFAULT ''::text NOT NULL,
    kind text NOT NULL,
    body text DEFAULT ''::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL
);

CREATE SEQUENCE public.ticket_event_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.ticket_event_id_seq OWNED BY public.ticket_event.id;

CREATE TABLE public.ticket_follower (
    ticket_id integer NOT NULL,
    username text NOT NULL
);

CREATE SEQUENCE public.ticket_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE public.ticket_id_seq OWNED BY public.ticket.id;

CREATE TABLE public.vendor_counter_snapshot (
    plant_key text NOT NULL,
    vendor text NOT NULL,
    snap_date date NOT NULL,
    daily_kwh numeric(14,3),
    monthly_kwh numeric(14,3),
    lifetime_kwh numeric(16,3),
    note text,
    captured_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.allocation ALTER COLUMN allocation_id SET DEFAULT nextval('public.allocation_allocation_id_seq'::regclass);

ALTER TABLE ONLY public.ask_log ALTER COLUMN id SET DEFAULT nextval('public.ask_log_id_seq'::regclass);

ALTER TABLE ONLY public.bank_match ALTER COLUMN match_id SET DEFAULT nextval('public.bank_match_match_id_seq'::regclass);

ALTER TABLE ONLY public.bank_statement ALTER COLUMN statement_id SET DEFAULT nextval('public.bank_statement_statement_id_seq'::regclass);

ALTER TABLE ONLY public.change_order ALTER COLUMN co_id SET DEFAULT nextval('public.change_order_co_id_seq'::regclass);

ALTER TABLE ONLY public.customer_master ALTER COLUMN customer_id SET DEFAULT nextval('public.customer_master_customer_id_seq'::regclass);

ALTER TABLE ONLY public.fin_event ALTER COLUMN event_id SET DEFAULT nextval('public.fin_event_event_id_seq'::regclass);

ALTER TABLE ONLY public.fin_exception ALTER COLUMN exception_id SET DEFAULT nextval('public.fin_exception_exception_id_seq'::regclass);

ALTER TABLE ONLY public.finance_audit ALTER COLUMN id SET DEFAULT nextval('public.finance_audit_id_seq'::regclass);

ALTER TABLE ONLY public.maintenance_event ALTER COLUMN id SET DEFAULT nextval('public.maintenance_event_id_seq'::regclass);

ALTER TABLE ONLY public.payment ALTER COLUMN payment_id SET DEFAULT nextval('public.payment_payment_id_seq'::regclass);

ALTER TABLE ONLY public.po_approval ALTER COLUMN approval_id SET DEFAULT nextval('public.po_approval_approval_id_seq'::regclass);

ALTER TABLE ONLY public.purchase_order ALTER COLUMN po_id SET DEFAULT nextval('public.purchase_order_po_id_seq'::regclass);

ALTER TABLE ONLY public.receipt ALTER COLUMN receipt_id SET DEFAULT nextval('public.receipt_receipt_id_seq'::regclass);

ALTER TABLE ONLY public.savio_check ALTER COLUMN check_id SET DEFAULT nextval('public.savio_check_check_id_seq'::regclass);

ALTER TABLE ONLY public.supplier ALTER COLUMN supplier_id SET DEFAULT nextval('public.supplier_supplier_id_seq'::regclass);

ALTER TABLE ONLY public.sync_run ALTER COLUMN id SET DEFAULT nextval('public.sync_run_id_seq'::regclass);

ALTER TABLE ONLY public.ticket ALTER COLUMN id SET DEFAULT nextval('public.ticket_id_seq'::regclass);

ALTER TABLE ONLY public.ticket_attachment ALTER COLUMN id SET DEFAULT nextval('public.ticket_attachment_id_seq'::regclass);

ALTER TABLE ONLY public.ticket_event ALTER COLUMN id SET DEFAULT nextval('public.ticket_event_id_seq'::regclass);

ALTER TABLE ONLY public.alert_ledger
    ADD CONSTRAINT alert_ledger_pkey PRIMARY KEY (alert_id);

ALTER TABLE ONLY public.alert_state
    ADD CONSTRAINT alert_state_pkey PRIMARY KEY (key);

ALTER TABLE ONLY public.allocation
    ADD CONSTRAINT allocation_pkey PRIMARY KEY (allocation_id);

ALTER TABLE ONLY public.ask_log
    ADD CONSTRAINT ask_log_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.bank_account
    ADD CONSTRAINT bank_account_pkey PRIMARY KEY (account_id);

ALTER TABLE ONLY public.bank_match
    ADD CONSTRAINT bank_match_pkey PRIMARY KEY (match_id);

ALTER TABLE ONLY public.bank_statement
    ADD CONSTRAINT bank_statement_account_id_period_start_period_end_key UNIQUE (account_id, period_start, period_end);

ALTER TABLE ONLY public.bank_statement
    ADD CONSTRAINT bank_statement_file_sha256_key UNIQUE (file_sha256);

ALTER TABLE ONLY public.bank_statement
    ADD CONSTRAINT bank_statement_pkey PRIMARY KEY (statement_id);

ALTER TABLE ONLY public.bank_transaction
    ADD CONSTRAINT bank_transaction_pkey PRIMARY KEY (line_key);

ALTER TABLE ONLY public.biz_case
    ADD CONSTRAINT biz_case_pkey PRIMARY KEY (entity_id, code);

ALTER TABLE ONLY public.budget_line
    ADD CONSTRAINT budget_line_pkey PRIMARY KEY (project_id, version, cost_code);

ALTER TABLE ONLY public.budget_version
    ADD CONSTRAINT budget_version_pkey PRIMARY KEY (project_id, version);

ALTER TABLE ONLY public.cfe_pipeline_status
    ADD CONSTRAINT cfe_pipeline_status_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.cfe_tariff
    ADD CONSTRAINT cfe_tariff_pkey PRIMARY KEY (tariff_code, region, month, charge_type);

ALTER TABLE ONLY public.change_order
    ADD CONSTRAINT change_order_pkey PRIMARY KEY (co_id);

ALTER TABLE ONLY public.change_order
    ADD CONSTRAINT change_order_project_id_ref_key UNIQUE (project_id, ref);

ALTER TABLE ONLY public.contract_monthly
    ADD CONSTRAINT contract_monthly_pkey PRIMARY KEY (plant_key, year, month);

ALTER TABLE ONLY public.cost_center
    ADD CONSTRAINT cost_center_pkey PRIMARY KEY (entity_id, code);

ALTER TABLE ONLY public.cost_code
    ADD CONSTRAINT cost_code_pkey PRIMARY KEY (code);

ALTER TABLE ONLY public.customer_invoice
    ADD CONSTRAINT customer_invoice_cfdi_uuid_key UNIQUE (cfdi_uuid);

ALTER TABLE ONLY public.customer_invoice
    ADD CONSTRAINT customer_invoice_pkey PRIMARY KEY (savio_invoice_id);

ALTER TABLE ONLY public.customer_master
    ADD CONSTRAINT customer_master_pkey PRIMARY KEY (customer_id);

ALTER TABLE ONLY public.customer_master
    ADD CONSTRAINT customer_master_rfc_key UNIQUE (rfc);

ALTER TABLE ONLY public.customer_master
    ADD CONSTRAINT customer_master_savio_customer_id_key UNIQUE (savio_customer_id);

ALTER TABLE ONLY public.daily_production
    ADD CONSTRAINT daily_production_pkey PRIMARY KEY (plant_key, prod_date);

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_pkey PRIMARY KEY (entity_id);

ALTER TABLE ONLY public.entity
    ADD CONSTRAINT entity_rfc_key UNIQUE (rfc);

ALTER TABLE ONLY public.fin_event
    ADD CONSTRAINT fin_event_pkey PRIMARY KEY (event_id);

ALTER TABLE ONLY public.fin_exception
    ADD CONSTRAINT fin_exception_kind_ref_key UNIQUE (kind, ref);

ALTER TABLE ONLY public.fin_exception
    ADD CONSTRAINT fin_exception_pkey PRIMARY KEY (exception_id);

ALTER TABLE ONLY public.fin_report_line
    ADD CONSTRAINT fin_report_line_pkey PRIMARY KEY (entity_id, period, sheet, code);

ALTER TABLE ONLY public.fin_source_file
    ADD CONSTRAINT fin_source_file_pkey PRIMARY KEY (sha256);

ALTER TABLE ONLY public.finance_audit
    ADD CONSTRAINT finance_audit_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.fx_rate
    ADD CONSTRAINT fx_rate_pkey PRIMARY KEY (rate_date, pair);

ALTER TABLE ONLY public.gl_account
    ADD CONSTRAINT gl_account_pkey PRIMARY KEY (entity_id, account);

ALTER TABLE ONLY public.gl_balance
    ADD CONSTRAINT gl_balance_pkey PRIMARY KEY (entity_id, period, account);

ALTER TABLE ONLY public.gl_journal
    ADD CONSTRAINT gl_journal_pkey PRIMARY KEY (entity_id, jkey);

ALTER TABLE ONLY public.gl_line
    ADD CONSTRAINT gl_line_pkey PRIMARY KEY (entity_id, jkey, line_no);

ALTER TABLE ONLY public.inverter
    ADD CONSTRAINT inverter_pkey PRIMARY KEY (plant_key, inverter_sn);

ALTER TABLE ONLY public.invoicing
    ADD CONSTRAINT invoicing_pkey PRIMARY KEY (plant_key, ref_month);

ALTER TABLE ONLY public.knowledge
    ADD CONSTRAINT knowledge_pkey PRIMARY KEY (doc, lang, n);

ALTER TABLE ONLY public.loan
    ADD CONSTRAINT loan_pkey PRIMARY KEY (loan_id);

ALTER TABLE ONLY public.loan_schedule
    ADD CONSTRAINT loan_schedule_pkey PRIMARY KEY (loan_id, ref_month);

ALTER TABLE ONLY public.mail_recipient
    ADD CONSTRAINT mail_recipient_pkey PRIMARY KEY (email);

ALTER TABLE ONLY public.mail_subscription
    ADD CONSTRAINT mail_subscription_pkey PRIMARY KEY (email, channel);

ALTER TABLE ONLY public.maintenance_event
    ADD CONSTRAINT maintenance_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.open_item
    ADD CONSTRAINT open_item_pkey PRIMARY KEY (entity_id, item_key);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_pkey PRIMARY KEY (payment_id);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_savio_payment_id_key UNIQUE (savio_payment_id);

ALTER TABLE ONLY public.period_close
    ADD CONSTRAINT period_close_pkey PRIMARY KEY (entity_id, month);

ALTER TABLE ONLY public.plant
    ADD CONSTRAINT plant_pkey PRIMARY KEY (plant_key);

ALTER TABLE ONLY public.pmo_cost
    ADD CONSTRAINT pmo_cost_pkey PRIMARY KEY (project_id, cost_id);

ALTER TABLE ONLY public.pmo_invoice
    ADD CONSTRAINT pmo_invoice_pkey PRIMARY KEY (project_id, invoice_id);

ALTER TABLE ONLY public.pmo_project
    ADD CONSTRAINT pmo_project_pkey PRIMARY KEY (project_id);

ALTER TABLE ONLY public.pmo_task
    ADD CONSTRAINT pmo_task_pkey PRIMARY KEY (project_id, task_id);

ALTER TABLE ONLY public.po_approval
    ADD CONSTRAINT po_approval_pkey PRIMARY KEY (approval_id);

ALTER TABLE ONLY public.po_line
    ADD CONSTRAINT po_line_pkey PRIMARY KEY (po_id, line_no);

ALTER TABLE ONLY public.portfolio_project
    ADD CONSTRAINT portfolio_project_pkey PRIMARY KEY (entity_id, code, name);

ALTER TABLE ONLY public.project_margin
    ADD CONSTRAINT project_margin_pkey PRIMARY KEY (entity_id, period, code);

ALTER TABLE ONLY public.project_milestone
    ADD CONSTRAINT project_milestone_pkey PRIMARY KEY (project_id, ref);

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_pkey PRIMARY KEY (project_id);

ALTER TABLE ONLY public.project_task
    ADD CONSTRAINT project_task_pkey PRIMARY KEY (project_id, task_id);

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_entity_id_po_number_key UNIQUE (entity_id, po_number);

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_pkey PRIMARY KEY (po_id);

ALTER TABLE ONLY public.receipt
    ADD CONSTRAINT receipt_pkey PRIMARY KEY (receipt_id);

ALTER TABLE ONLY public.reconciliation_daily
    ADD CONSTRAINT reconciliation_daily_pkey PRIMARY KEY (plant_key, prod_date);

ALTER TABLE ONLY public.reconciliation_monthly
    ADD CONSTRAINT reconciliation_monthly_pkey PRIMARY KEY (plant_key, ref_month);

ALTER TABLE ONLY public.satellite_check
    ADD CONSTRAINT satellite_check_pkey PRIMARY KEY (plant_key, check_date);

ALTER TABLE ONLY public.savio_check
    ADD CONSTRAINT savio_check_pkey PRIMARY KEY (check_id);

ALTER TABLE ONLY public.savio_cursor
    ADD CONSTRAINT savio_cursor_pkey PRIMARY KEY (resource);

ALTER TABLE ONLY public.savio_event
    ADD CONSTRAINT savio_event_pkey PRIMARY KEY (event_id);

ALTER TABLE ONLY public.string_daily
    ADD CONSTRAINT string_daily_pkey PRIMARY KEY (plant_key, inverter_sn, prod_date, channel);

ALTER TABLE ONLY public.supplier
    ADD CONSTRAINT supplier_entity_id_rfc_key UNIQUE (entity_id, rfc);

ALTER TABLE ONLY public.supplier_invoice
    ADD CONSTRAINT supplier_invoice_pkey PRIMARY KEY (cfdi_uuid);

ALTER TABLE ONLY public.supplier
    ADD CONSTRAINT supplier_pkey PRIMARY KEY (supplier_id);

ALTER TABLE ONLY public.sync_run
    ADD CONSTRAINT sync_run_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.telemetry_detail
    ADD CONSTRAINT telemetry_detail_pkey PRIMARY KEY (plant_key, inverter_sn, ts_utc);

ALTER TABLE ONLY public.telemetry
    ADD CONSTRAINT telemetry_pkey PRIMARY KEY (plant_key, inverter_sn, ts_utc);

ALTER TABLE ONLY public.thermal_bins
    ADD CONSTRAINT thermal_bins_pkey PRIMARY KEY (plant_key, inverter_sn, prod_date, bin_c);

ALTER TABLE ONLY public.thermal_daily
    ADD CONSTRAINT thermal_daily_pkey PRIMARY KEY (plant_key, inverter_sn, prod_date);

ALTER TABLE ONLY public.ticket_alert
    ADD CONSTRAINT ticket_alert_pkey PRIMARY KEY (ticket_id, alert_key);

ALTER TABLE ONLY public.ticket_attachment
    ADD CONSTRAINT ticket_attachment_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.ticket_event
    ADD CONSTRAINT ticket_event_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.ticket_follower
    ADD CONSTRAINT ticket_follower_pkey PRIMARY KEY (ticket_id, username);

ALTER TABLE ONLY public.ticket
    ADD CONSTRAINT ticket_number_key UNIQUE (number);

ALTER TABLE ONLY public.ticket
    ADD CONSTRAINT ticket_pkey PRIMARY KEY (id);

ALTER TABLE ONLY public.vendor_counter_snapshot
    ADD CONSTRAINT vendor_counter_snapshot_pkey PRIMARY KEY (plant_key, snap_date);

CREATE INDEX alert_ledger_key_state ON public.alert_ledger USING btree (alert_key, state);

CREATE INDEX bank_transaction_account_date_idx ON public.bank_transaction USING btree (account_id, tx_date);

CREATE INDEX customer_invoice_project_idx ON public.customer_invoice USING btree (project_id);

CREATE INDEX dashboard_inverter_day ON public.dashboard_inverter USING btree (date_mx, plant_key);

CREATE INDEX dashboard_plant_day ON public.dashboard_plant USING btree (date_mx, plant_key);

CREATE INDEX fin_event_subject_idx ON public.fin_event USING btree (subject_kind, subject_ref);

CREATE INDEX gl_line_account_idx ON public.gl_line USING btree (entity_id, account);

CREATE INDEX gl_line_segment_idx ON public.gl_line USING btree (entity_id, segment);

CREATE INDEX idx_cfe_month ON public.cfe_tariff USING btree (month);

CREATE INDEX idx_daily_date ON public.daily_production USING btree (prod_date);

CREATE INDEX idx_knowledge_tsv ON public.knowledge USING gin (tsv);

CREATE INDEX idx_tdetail_ts ON public.telemetry_detail USING btree (ts_utc);

CREATE INDEX idx_tele_ts ON public.telemetry USING btree (ts_utc);

CREATE INDEX idx_ticket_alert_key ON public.ticket_alert USING btree (alert_key);

CREATE INDEX idx_ticket_event_ticket ON public.ticket_event USING btree (ticket_id, ts);

CREATE INDEX idx_ticket_plant_status ON public.ticket USING btree (plant_key, status);

CREATE INDEX supplier_invoice_project_idx ON public.supplier_invoice USING btree (project_id);

CREATE INDEX supplier_invoice_status_idx ON public.supplier_invoice USING btree (status);

CREATE INDEX sync_run_script_started ON public.sync_run USING btree (script, started_at DESC);

ALTER TABLE ONLY public.allocation
    ADD CONSTRAINT allocation_payment_id_fkey FOREIGN KEY (payment_id) REFERENCES public.payment(payment_id);

ALTER TABLE ONLY public.bank_account
    ADD CONSTRAINT bank_account_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.bank_match
    ADD CONSTRAINT bank_match_line_key_fkey FOREIGN KEY (line_key) REFERENCES public.bank_transaction(line_key);

ALTER TABLE ONLY public.bank_statement
    ADD CONSTRAINT bank_statement_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.bank_account(account_id);

ALTER TABLE ONLY public.bank_transaction
    ADD CONSTRAINT bank_transaction_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.bank_account(account_id);

ALTER TABLE ONLY public.bank_transaction
    ADD CONSTRAINT bank_transaction_statement_id_fkey FOREIGN KEY (statement_id) REFERENCES public.bank_statement(statement_id);

ALTER TABLE ONLY public.biz_case
    ADD CONSTRAINT biz_case_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.budget_line
    ADD CONSTRAINT budget_line_cost_code_fkey FOREIGN KEY (cost_code) REFERENCES public.cost_code(code);

ALTER TABLE ONLY public.budget_line
    ADD CONSTRAINT budget_line_project_id_version_fkey FOREIGN KEY (project_id, version) REFERENCES public.budget_version(project_id, version);

ALTER TABLE ONLY public.budget_version
    ADD CONSTRAINT budget_version_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.change_order
    ADD CONSTRAINT change_order_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.cost_center
    ADD CONSTRAINT cost_center_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.cost_code
    ADD CONSTRAINT cost_code_parent_fkey FOREIGN KEY (parent) REFERENCES public.cost_code(code);

ALTER TABLE ONLY public.customer_invoice
    ADD CONSTRAINT customer_invoice_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customer_master(customer_id);

ALTER TABLE ONLY public.customer_invoice
    ADD CONSTRAINT customer_invoice_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.customer_invoice
    ADD CONSTRAINT customer_invoice_plant_key_fkey FOREIGN KEY (plant_key) REFERENCES public.plant(plant_key);

ALTER TABLE ONLY public.customer_invoice
    ADD CONSTRAINT customer_invoice_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.daily_production
    ADD CONSTRAINT daily_production_plant_key_fkey FOREIGN KEY (plant_key) REFERENCES public.plant(plant_key);

ALTER TABLE ONLY public.fin_exception
    ADD CONSTRAINT fin_exception_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.fin_exception
    ADD CONSTRAINT fin_exception_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.fin_report_line
    ADD CONSTRAINT fin_report_line_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.fin_report_line
    ADD CONSTRAINT fin_report_line_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.fin_source_file
    ADD CONSTRAINT fin_source_file_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.gl_account
    ADD CONSTRAINT gl_account_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.gl_balance
    ADD CONSTRAINT gl_balance_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.gl_balance
    ADD CONSTRAINT gl_balance_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.gl_journal
    ADD CONSTRAINT gl_journal_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.gl_journal
    ADD CONSTRAINT gl_journal_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.gl_line
    ADD CONSTRAINT gl_line_entity_id_jkey_fkey FOREIGN KEY (entity_id, jkey) REFERENCES public.gl_journal(entity_id, jkey) ON DELETE CASCADE;

ALTER TABLE ONLY public.inverter
    ADD CONSTRAINT inverter_plant_key_fkey FOREIGN KEY (plant_key) REFERENCES public.plant(plant_key);

ALTER TABLE ONLY public.loan_schedule
    ADD CONSTRAINT loan_schedule_loan_id_fkey FOREIGN KEY (loan_id) REFERENCES public.loan(loan_id);

ALTER TABLE ONLY public.open_item
    ADD CONSTRAINT open_item_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.open_item
    ADD CONSTRAINT open_item_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.bank_account(account_id);

ALTER TABLE ONLY public.payment
    ADD CONSTRAINT payment_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.period_close
    ADD CONSTRAINT period_close_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.pmo_cost
    ADD CONSTRAINT pmo_cost_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.pmo_project(project_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.pmo_invoice
    ADD CONSTRAINT pmo_invoice_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.pmo_project(project_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.pmo_project
    ADD CONSTRAINT pmo_project_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.pmo_project
    ADD CONSTRAINT pmo_project_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.pmo_task
    ADD CONSTRAINT pmo_task_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.pmo_project(project_id) ON DELETE CASCADE;

ALTER TABLE ONLY public.po_approval
    ADD CONSTRAINT po_approval_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.purchase_order(po_id);

ALTER TABLE ONLY public.po_line
    ADD CONSTRAINT po_line_cost_code_fkey FOREIGN KEY (cost_code) REFERENCES public.cost_code(code);

ALTER TABLE ONLY public.po_line
    ADD CONSTRAINT po_line_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.purchase_order(po_id);

ALTER TABLE ONLY public.portfolio_project
    ADD CONSTRAINT portfolio_project_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.portfolio_project
    ADD CONSTRAINT portfolio_project_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.customer_master(customer_id);

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.project_margin
    ADD CONSTRAINT project_margin_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.project_margin
    ADD CONSTRAINT project_margin_source_sha_fkey FOREIGN KEY (source_sha) REFERENCES public.fin_source_file(sha256);

ALTER TABLE ONLY public.project_milestone
    ADD CONSTRAINT project_milestone_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.project
    ADD CONSTRAINT project_plant_key_fkey FOREIGN KEY (plant_key) REFERENCES public.plant(plant_key);

ALTER TABLE ONLY public.project_task
    ADD CONSTRAINT project_task_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.purchase_order
    ADD CONSTRAINT purchase_order_supplier_id_fkey FOREIGN KEY (supplier_id) REFERENCES public.supplier(supplier_id);

ALTER TABLE ONLY public.receipt
    ADD CONSTRAINT receipt_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.purchase_order(po_id);

ALTER TABLE ONLY public.savio_check
    ADD CONSTRAINT savio_check_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.supplier
    ADD CONSTRAINT supplier_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.supplier_invoice
    ADD CONSTRAINT supplier_invoice_cost_code_fkey FOREIGN KEY (cost_code) REFERENCES public.cost_code(code);

ALTER TABLE ONLY public.supplier_invoice
    ADD CONSTRAINT supplier_invoice_entity_id_fkey FOREIGN KEY (entity_id) REFERENCES public.entity(entity_id);

ALTER TABLE ONLY public.supplier_invoice
    ADD CONSTRAINT supplier_invoice_po_id_fkey FOREIGN KEY (po_id) REFERENCES public.purchase_order(po_id);

ALTER TABLE ONLY public.supplier_invoice
    ADD CONSTRAINT supplier_invoice_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.project(project_id);

ALTER TABLE ONLY public.supplier_invoice
    ADD CONSTRAINT supplier_invoice_supplier_id_fkey FOREIGN KEY (supplier_id) REFERENCES public.supplier(supplier_id);

ALTER TABLE ONLY public.ticket_alert
    ADD CONSTRAINT ticket_alert_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.ticket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.ticket_attachment
    ADD CONSTRAINT ticket_attachment_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.ticket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.ticket_event
    ADD CONSTRAINT ticket_event_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.ticket(id) ON DELETE CASCADE;

ALTER TABLE ONLY public.ticket_follower
    ADD CONSTRAINT ticket_follower_ticket_id_fkey FOREIGN KEY (ticket_id) REFERENCES public.ticket(id) ON DELETE CASCADE;


