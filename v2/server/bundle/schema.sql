-- Argia_Mont — PostgreSQL schema v1 (pio06), 2026-08-25
-- Plain SQL, no ORM. Idempotent: CREATE IF NOT EXISTS only.

CREATE TABLE IF NOT EXISTS plant (
    plant_key           text PRIMARY KEY,
    customer            text NOT NULL,
    brand               text NOT NULL CHECK (brand IN ('GROWATT','HUAWEI','SOLAREDGE','SMA')),
    site_id             text,
    kwp_dc              numeric(10,3) NOT NULL CHECK (kwp_dc > 0),
    kwp_ac              numeric(10,3) CHECK (kwp_ac > 0),
    lat                 numeric(9,6),
    lon                 numeric(9,6) CHECK (lon < 0),   -- Mexico is west of Greenwich
    portfolio           text CHECK (portfolio IN ('PPA','CAPEX')),
    tariff_mxn_per_kwh  numeric(8,4),
    pr_baseline         numeric(5,4),
    contracted_kwh      numeric(12,3),
    om_cost_monthly_mxn numeric(10,2),
    investment_mxn      numeric(14,2),   -- CAPEX plants: owner's investment, for payback tracking
    active              boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS inverter (
    plant_key           text NOT NULL REFERENCES plant(plant_key),
    inverter_sn         text NOT NULL,
    inverter_label      text,
    rated_kw            numeric(10,3) CHECK (rated_kw > 0),
    phase               text,
    date_producing      date,
    date_decommissioned date,
    active              boolean NOT NULL DEFAULT true,
    PRIMARY KEY (plant_key, inverter_sn)
);

CREATE TABLE IF NOT EXISTS daily_production (
    plant_key           text NOT NULL REFERENCES plant(plant_key),
    prod_date           date NOT NULL,
    energy_kwh          numeric(12,3) CHECK (energy_kwh >= 0),
    irradiance_kwh_m2   numeric(8,4) CHECK (irradiance_kwh_m2 >= 0),
    pr                  numeric(6,4),
    pr_stc              numeric(6,4),
    expected_kwh        numeric(12,3),
    billable_kwh        numeric(12,3),
    cloud_cover_pct     numeric(6,2),
    availability        numeric(6,4),
    inverters_reporting int,
    data_class          text,
    status_note         text,
    source              text NOT NULL CHECK (source IN ('v1','v2')),
    loaded_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plant_key, prod_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily_production (prod_date);

-- NOTE: plant_key here is NOT a strict FK — the sheet also carries LaaS
-- contract entities (LGTO1, LOAX1) whose fees are keyed like plants.
CREATE TABLE IF NOT EXISTS contract_monthly (
    plant_key        text NOT NULL,
    year             int NOT NULL,
    month            int NOT NULL CHECK (month BETWEEN 1 AND 12),
    design_kwh       numeric(12,3),
    contract_kwh     numeric(12,3),
    tariff_mxn       numeric(8,4),
    fixed_income_ccy numeric(12,2),
    ccy              text,
    PRIMARY KEY (plant_key, year, month)
);

CREATE TABLE IF NOT EXISTS loan (
    loan_id            text PRIMARY KEY,
    plant_key          text,
    project_name       text,
    bank               text,
    currency           text,
    principal_mxn      numeric(14,2),
    total_installments int,
    first_month        date,
    last_month         date
);

CREATE TABLE IF NOT EXISTS loan_schedule (
    loan_id        text NOT NULL REFERENCES loan(loan_id),
    ref_month      date NOT NULL,
    installment_no int,
    payment_mxn    numeric(14,2),
    payment_ccy    numeric(14,2),
    xr             numeric(10,4),
    due_after_mxn  numeric(14,2),
    PRIMARY KEY (loan_id, ref_month)
);

-- 5-minute telemetry, common columns only; full CSVs stay archived on disk
-- per-MPPT / per-string daily reduction of Growatt getMAXHistory
-- (v170; written by scripts/string_daily.py, argia-strings.timer)
CREATE TABLE IF NOT EXISTS string_daily (
    plant_key    text NOT NULL,
    inverter_sn  text NOT NULL,
    prod_date    date NOT NULL,
    channel      text NOT NULL,   -- 'pv1'..'pv16' or 's1'..'s32'
    kind         text NOT NULL CHECK (kind IN ('mppt','string')),
    energy_kwh   numeric(10,3),   -- mppt: epvXToday counter; string: allocated by current share
    q_ah         numeric(12,3),   -- string only: integrated current
    v_avg        numeric(8,2),    -- mppt only: mean active voltage
    share        numeric(6,4),    -- string only: share of its MPPT pair
    samples      int NOT NULL,
    str_unmatch  int,
    str_unblance int,
    loaded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plant_key, inverter_sn, prod_date, channel)
);

CREATE TABLE IF NOT EXISTS telemetry (
    ts_utc          timestamptz NOT NULL,
    plant_key       text NOT NULL,
    inverter_sn     text NOT NULL,
    status          int,
    power_w         numeric(12,2),
    etoday_kwh      numeric(12,3),
    temperature_c   numeric(6,2),
    irradiance_wm2  numeric(8,2),
    module_temp_c   numeric(6,2),
    ambient_temp_c  numeric(6,2),
    cloud_cover_pct numeric(6,2),
    PRIMARY KEY (plant_key, inverter_sn, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_tele_ts ON telemetry (ts_utc);
