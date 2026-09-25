-- Synthetic fleet for the portal end-to-end test (tests/portal/). No real data:
-- plant keys are the fleet's codes (already public in the repo), every number is
-- invented. All dates are relative to "today in Mexico" so the pages always see a
-- live fleet, whatever day the test runs.
--
-- The whole fleet as report_gen.py lists it (PPA / CAPEX lists are hardcoded
-- there - a plant missing from the database crashes the generator). Branches:
--   GTO1  PPA    live, INV-02 runs hot (thermal warning)
--   MEX1  PPA    HUAWEI, an open ticket linked to a resolved outage
--   QRO1  CAPEX  SOLAREDGE, a customer maintenance window
--   MEX3  CAPEX  dark today (no telemetry, CRITICAL plant_offline open)

\set ON_ERROR_STOP 1
BEGIN;

CREATE TEMP TABLE _d AS SELECT (now() AT TIME ZONE 'America/Mexico_City')::date AS today;

INSERT INTO plant (plant_key, customer, brand, site_id, kwp_dc, kwp_ac, lat, lon, portfolio,
                   tariff_mxn_per_kwh, pr_baseline, active, om_cost_monthly_mxn, investment_mxn,
                   sla_target, installation_date, billing_scheme, show_dashboard, show_daily_report,
                   show_financial, date_interconnection)
SELECT k, c, b, 'S-' || k, kwp, kwp * 0.9, lat, lon, pf,
       CASE WHEN pf = 'PPA' THEN 2.3 END, 0.80, true,
       CASE WHEN pf = 'PPA' THEN 10000 END, CASE WHEN pf = 'PPA' THEN kwp * 18000 END,
       CASE WHEN pf = 'PPA' THEN 0.98 END, date '2023-03-01', CASE WHEN pf = 'PPA' THEN 'measured' END,
       true, true, pf = 'PPA', date '2023-03-15'
FROM (VALUES
  ('GTO1', 'TAIGENE PPA roof (Leon, GTO)',               'GROWATT',   500, 21.10, -101.60, 'PPA'),
  ('MEX1', 'SAG PPA roof (CDMX, MEX)',                   'HUAWEI',    300, 19.40, -99.10,  'PPA'),
  ('MEX2', 'VITALMEX PPA roof (CDMX, MEX)',              'GROWATT',   250, 19.35, -99.15,  'PPA'),
  ('NL1',  'PLASTIC OMNIUM PPA land (Monterrey, NL)',    'GROWATT',   800, 25.70, -100.30, 'PPA'),
  ('SLP1', 'HOLIDAY INN EXPRESS PPA roof (SLP, SLP)',    'GROWATT',   150, 22.12, -100.92, 'PPA'),
  ('SLP2', 'QUIMICA COYOACAN PPA roof (SLP, SLP)',       'GROWATT',   200, 22.14, -100.94, 'PPA'),
  ('GTO2', 'HIRSCHMANN-MEXICO CAPEX roof (San Miguel, GTO)', 'SOLAREDGE', 400, 20.90, -100.70, 'CAPEX'),
  ('QRO1', 'RYDER CAPEX roof (Queretaro, QRO)',          'SOLAREDGE', 200, 20.60, -100.40, 'CAPEX'),
  ('NL2',  'BUDENHEIM CAPEX roof (Monterrey, NL)',       'GROWATT',   350, 25.65, -100.20, 'CAPEX'),
  ('MEX3', 'SMS CAPEX roof (Tlalnepantla, MEX)',         'GROWATT',   100, 19.50, -99.20,  'CAPEX'),
  ('TAM1', 'TETRA PAK CAPEX roof (Tampico, TAM)',        'HUAWEI',    600, 22.25, -97.85,  'CAPEX')
) v(k, c, b, kwp, lat, lon, pf);

INSERT INTO inverter (plant_key, inverter_sn, inverter_label, rated_kw, active, in_service_today)
SELECT plant_key, plant_key || 'INV0' || n, 'INV-0' || n, round(kwp_ac / 2, 3), true, true
FROM plant, generate_series(1, 2) n;

-- 5-minute telemetry for yesterday (whole day) and today up to now, 06:30-19:30 MX,
-- a clear-sky bell curve. MEX3 is dark today.
INSERT INTO telemetry (ts_utc, plant_key, inverter_sn, status, power_w, etoday_kwh, temperature_c,
                       irradiance_wm2, module_temp_c, ambient_temp_c, cloud_cover_pct, vendor, inverter_label)
SELECT ts, i.plant_key, i.inverter_sn, 1,
       round((i.rated_kw * 1000 * 0.85 * greatest(0, sin(pi() * (h - 6.5) / 13)))::numeric, 2),
       round((i.rated_kw * 0.85 * 13 / pi() * (1 - cos(pi() * greatest(0, least(h - 6.5, 13)) / 13)) / 2)::numeric, 3),
       round((35 + 20 * greatest(0, sin(pi() * (h - 6.5) / 13)) + CASE WHEN i.inverter_sn = 'GTO1INV02' THEN 12 ELSE 0 END)::numeric, 2),
       round((1000 * greatest(0, sin(pi() * (h - 6.5) / 13)))::numeric, 2),
       round((30 + 25 * greatest(0, sin(pi() * (h - 6.5) / 13)))::numeric, 2),
       24, 10, lower(p.brand), i.inverter_label
FROM inverter i JOIN plant p USING (plant_key),
     LATERAL (SELECT g AS ts,
                     extract(hour FROM g AT TIME ZONE 'America/Mexico_City')
                     + extract(minute FROM g AT TIME ZONE 'America/Mexico_City') / 60.0 AS h
              FROM generate_series(((SELECT today FROM _d) - 8 + time '06:30') AT TIME ZONE 'America/Mexico_City',
                                   least(now(), ((SELECT today FROM _d) + time '19:30') AT TIME ZONE 'America/Mexico_City'),
                                   interval '5 minutes') g) s
WHERE extract(hour FROM ts AT TIME ZONE 'America/Mexico_City') BETWEEN 6 AND 19
  AND NOT (i.plant_key = 'MEX3' AND (ts AT TIME ZONE 'America/Mexico_City')::date = (SELECT today FROM _d));

-- 430 days of daily production, deterministic wobble, MEX3 short yesterday
INSERT INTO daily_production (plant_key, prod_date, energy_kwh, irradiance_kwh_m2, pr, expected_kwh, billable_kwh,
                              cloud_cover_pct, availability, inverters_reporting, data_class, status_note, source,
                              specific_yield, design_kwh)
SELECT p.plant_key, d::date,
       round((p.kwp_dc * 5.2 * p.pr_baseline * (0.92 + 0.08 * sin(extract(doy FROM d) / 9.0)))::numeric, 3),
       round((5.2 * (0.95 + 0.05 * cos(extract(doy FROM d) / 30.0)))::numeric, 4),
       round((p.pr_baseline * (0.97 + 0.03 * sin(extract(doy FROM d) / 9.0)))::numeric, 4),
       round((p.kwp_dc * 5.2 * p.pr_baseline)::numeric, 3),
       round((p.kwp_dc * 5.2 * p.pr_baseline * (0.92 + 0.08 * sin(extract(doy FROM d) / 9.0)))::numeric, 3),
       15, 1.0, 2, 'measured', '', 'v2',
       round((5.2 * p.pr_baseline)::numeric, 3), round((p.kwp_dc * 5.0 * 0.8)::numeric, 3)
FROM plant p, generate_series((SELECT today FROM _d) - 430, (SELECT today FROM _d) - 1, interval '1 day') d;
UPDATE daily_production SET energy_kwh = energy_kwh * 0.3, availability = 0.3, status_note = 'inverter offline part of the day'
 WHERE plant_key = 'MEX3' AND prod_date = (SELECT today FROM _d) - 1;

INSERT INTO contract_monthly (plant_key, year, month, design_kwh, contract_kwh, tariff_mxn, fixed_income_ccy, ccy)
SELECT p.plant_key, y, m, p.kwp_dc * 150, p.kwp_dc * 140, p.tariff_mxn_per_kwh, NULL, 'MXN'
FROM plant p, generate_series(extract(year FROM (SELECT today FROM _d))::int - 2, extract(year FROM (SELECT today FROM _d))::int + 1) y,
     generate_series(1, 12) m
WHERE p.portfolio = 'PPA';

INSERT INTO loan (loan_id, plant_key, project_name, bank, currency, principal_mxn, total_installments, first_month, last_month)
VALUES ('L-GTO1', 'GTO1', 'Taigene PPA', 'Demo Bank', 'MXN', 5000000, 84,
        date_trunc('month', (SELECT today FROM _d) - interval '24 months')::date,
        date_trunc('month', (SELECT today FROM _d) + interval '59 months')::date);
INSERT INTO loan_schedule (loan_id, ref_month, installment_no, payment_mxn, payment_ccy, xr, due_after_mxn)
SELECT 'L-GTO1', (date_trunc('month', (SELECT today FROM _d) - interval '24 months') + (n - 1) * interval '1 month')::date,
       n, 80000, 80000, 1, 5000000 - n * 60000
FROM generate_series(1, 84) n;

INSERT INTO cfe_tariff (tariff_code, region, month, charge_type, unit, value_mxn, source)
SELECT t, r, make_date(extract(year FROM (SELECT today FROM _d))::int, m, 1), c, u,
       v * (1 + 0.01 * m) * CASE r WHEN 'BAJIO' THEN 1.0 WHEN 'GOLFO CENTRO' THEN 0.93 ELSE 0.97 END,
       CASE WHEN m <= extract(month FROM (SELECT today FROM _d)) THEN 'cfe_scrape' ELSE 'master_db_10' END
FROM (VALUES ('GDMTH'), ('GDMTO')) tc(t),
     (VALUES ('BAJIO'), ('GOLFO CENTRO'), ('CENTRO')) rg(r),
     (VALUES ('ENERGIA BASE', 'MXN/kWh', 0.88), ('ENERGIA INTERMEDIA', 'MXN/kWh', 1.71),
             ('ENERGIA PUNTA', 'MXN/kWh', 1.98), ('CAPACIDAD', 'MXN/kW', 380.0),
             ('DISTRIBUCION', 'MXN/kW', 110.0)) cv(c, u, v),
     generate_series(1, 12) m;
INSERT INTO cfe_pipeline_status (id, heartbeat_ts, probe_status, probe_rows, sent_month, last_csv, last_csv_result)
VALUES (1, now() - interval '2 hours', 'ok', 102, to_char((SELECT today FROM _d), 'YYYY-MM'), 'cfe_gapfill_demo.csv', 'loaded');

INSERT INTO alert_ledger (alert_id, alert_key, plant_key, inverter_sn, metric, severity, state, opened_utc,
                          last_seen_utc, resolved_utc, value, threshold, message, channels_sent, explanation) VALUES
 ('A1', 'plant_offline|MEX3|', 'MEX3', '', 'plant_offline', 'CRITICAL', 'OPEN',
  to_char(now() - interval '3 hours', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), '',
  0, 1, 'Plant offline: no power since 09:00', 'email', 'All inverters report 0 W in daylight.'),
 ('A2', 'inverter_hot|GTO1|GTO1INV02', 'GTO1', 'GTO1INV02', 'inverter_temp', 'WARNING', 'OPEN',
  to_char(now() - interval '2 hours', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), '',
  67, 65, 'Inverter INV-02 at 67 C', '', 'Runs 12 C above its peer.'),
 ('A3', 'plant_offline|MEX1|', 'MEX1', '', 'plant_offline', 'CRITICAL', 'RESOLVED',
  to_char(now() - interval '5 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), to_char(now() - interval '4 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
  to_char(now() - interval '4 days', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'), 0, 1, 'Plant offline', 'email', 'Resolved by the data.');

INSERT INTO reconciliation_daily (plant_key, prod_date, interval_kwh, vendor_daily_kwh, kpi_kwh, completeness_pct,
                                  variance_pct, status, note, reference_kwh, reference_basis)
SELECT plant_key, prod_date, energy_kwh, energy_kwh * 1.002, energy_kwh, 99.5, 0.2, 'PASS', '', energy_kwh, 'vendor_daily'
FROM daily_production WHERE prod_date >= (SELECT today FROM _d) - 40;
INSERT INTO reconciliation_monthly (plant_key, ref_month, interval_sum_kwh, vendor_daily_sum_kwh, vendor_monthly_kwh,
                                    lifetime_delta_kwh, completeness_pct, check1_pct, check2_pct, check4_pct,
                                    billing_kwh, billing_basis, status, note, closed_at, closed_by)
SELECT plant_key, date_trunc('month', prod_date)::date, sum(energy_kwh), sum(energy_kwh), sum(energy_kwh), sum(energy_kwh),
       99.5, 0.1, 0.1, 0.1, sum(energy_kwh), 'vendor_monthly', 'CLOSED', '', now(), 'demo'
FROM daily_production
WHERE prod_date >= date_trunc('month', (SELECT today FROM _d) - interval '3 months')
  AND prod_date < date_trunc('month', (SELECT today FROM _d))
GROUP BY plant_key, date_trunc('month', prod_date);

INSERT INTO invoicing (plant_key, ref_month, billable_kwh, tariff_mxn, amount_mxn, billing_kwh, delta_kwh, delta_pct,
                       check_status, produced_kwh, penalty_kwh, expected_kwh, source)
SELECT r.plant_key, r.ref_month, r.billing_kwh, p.tariff_mxn_per_kwh, round(r.billing_kwh * p.tariff_mxn_per_kwh, 2),
       r.billing_kwh, 0, 0, 'OK', r.billing_kwh, 0, r.billing_kwh, 'recon'
FROM reconciliation_monthly r JOIN plant p USING (plant_key) WHERE p.portfolio = 'PPA';

INSERT INTO thermal_daily (plant_key, inverter_sn, prod_date, samples, peak_c, mean_c, minutes_over_65, minutes_over_70,
                           events, dt_peer_peak_c, dt_ambient_peak_c, derating_minutes, lost_kwh, energy_kwh, cool_ratio,
                           band, cooling_health, vendor_derating_minutes)
SELECT i.plant_key, i.inverter_sn, d::date, 150, CASE WHEN i.inverter_sn = 'GTO1INV02' THEN 68 ELSE 55 END, 45,
       CASE WHEN i.inverter_sn = 'GTO1INV02' THEN 40 ELSE 0 END, 0, 0, 3, 30, 0, 0, 900, 1.0,
       CASE WHEN i.inverter_sn = 'GTO1INV02' THEN 'warn' ELSE 'ok' END, 'ok', 0
FROM inverter i, generate_series((SELECT today FROM _d) - 14, (SELECT today FROM _d) - 1, interval '1 day') d;

INSERT INTO vendor_counter_snapshot (plant_key, vendor, snap_date, daily_kwh, monthly_kwh, lifetime_kwh, note)
SELECT plant_key, lower(brand), (SELECT today FROM _d) - 1, kwp_dc * 4, kwp_dc * 100, kwp_dc * 2500, '' FROM plant;

INSERT INTO maintenance_event (plant_key, start_ts, end_ts, category, cost_type, cost_mxn, note, approved_by, created_by)
VALUES ('QRO1', now() - interval '10 days', now() - interval '10 days' + interval '4 hours', 'customer', 'opex', 1500,
        'Customer shutdown for roof work', 'demo', 'demo');

INSERT INTO ticket (number, plant_key, inverter_sn, title, description, category, priority, status, created_by)
VALUES ('T-0001', 'MEX1', '', 'Check datalogger connection', 'Created from the plant_offline alert.', 'comms', 'P1', 'NEW', 'demo');
INSERT INTO ticket_alert (ticket_id, alert_key) SELECT id, 'plant_offline|MEX1|' FROM ticket WHERE number = 'T-0001';

-- one subscriber per channel so the mail jobs build real messages (dry-run never sends)
INSERT INTO mail_subscription (email, channel, plants, enabled, username, added_by)
SELECT 'ops@example.invalid', c, '', true, 'demo', 'seed' FROM unnest(ARRAY['maintenance', 'financial', 'daily', 'reports']) c;
INSERT INTO mail_recipient (email, enabled, note, added_by) VALUES ('ops@example.invalid', true, 'seed', 'seed');

COMMIT;
