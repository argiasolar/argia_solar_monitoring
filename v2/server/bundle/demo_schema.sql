-- demo.argia.com.mx (v279): the "demo" schema - read-only views over the
-- production tables that the demo generator reads INSTEAD of public.*
-- (demo_gen.py runs psql with PGOPTIONS='-c search_path=demo,public').
--
-- Same data as the portal, three differences:
--   * every customer is "ARGIA SOLAR <n> (<location>)" - real names never
--     reach the demo pages (demo_gen also refuses to publish a page that
--     still contains one);
--   * every plant is PPA - the four CAPEX plants are priced at the fleet's
--     kWh-weighted PPA tariff of each month, their contracted kWh is the
--     PPA fleet's contracted kWh per kWp of that month (unless they have
--     their own contract row);
--   * free text that may carry a name or a secret name (plant notes,
--     secret_* names, client channel) is blanked.
-- Nothing here writes; every table not shadowed below is read from public.
-- Idempotent: re-running replaces the views.  Apply:
--   runuser -u postgres -- psql -d argia_mont -v ON_ERROR_STOP=1 -f demo_schema.sql
BEGIN;
CREATE SCHEMA IF NOT EXISTS demo;
DROP VIEW IF EXISTS demo.loss_daily, demo.contract_monthly, demo.plant,
                    demo.ppa_tariff_month, demo.name_map CASCADE;

-- plant code -> demo number.  Fixed, so a prospect who saw ARGIA SOLAR 3
-- last week sees the same plant as ARGIA SOLAR 3 today.  A plant missing
-- here becomes "ARGIA SOLAR <code>" - never its customer name.
CREATE VIEW demo.name_map AS
SELECT v.plant_key, v.n FROM (VALUES
    ('GTO1', 1), ('GTO2', 2), ('MEX1', 3), ('MEX2', 4), ('MEX3', 5),
    ('NL1', 6), ('NL2', 7), ('QRO1', 8), ('SLP1', 9), ('SLP2', 10), ('TAM1', 11)
) AS v(plant_key, n);

-- per month: the PPA tariff weighted by the PPA plants' measured kWh
-- (plain average when no energy is recorded yet), and the PPA fleet's
-- contracted / design kWh per kWp
CREATE VIEW demo.ppa_tariff_month AS
WITH t AS (
    SELECT c.plant_key, c.year, c.month, c.tariff_mxn, c.contract_kwh, c.design_kwh, p.kwp_dc
    FROM public.contract_monthly c
    JOIN public.plant p ON p.plant_key = c.plant_key
    WHERE p.portfolio = 'PPA' AND coalesce(c.tariff_mxn, 0) > 0
), e AS (
    SELECT plant_key, extract(year FROM prod_date)::int AS y,
           extract(month FROM prod_date)::int AS m, sum(energy_kwh) AS kwh
    FROM public.daily_production
    GROUP BY 1, 2, 3
)
SELECT t.year, t.month,
       round(coalesce(sum(t.tariff_mxn * e.kwh) / nullif(sum(e.kwh) FILTER (WHERE e.kwh > 0), 0),
                      avg(t.tariff_mxn)), 4) AS tariff_mxn,
       sum(t.contract_kwh) / nullif(sum(t.kwp_dc) FILTER (WHERE t.contract_kwh IS NOT NULL), 0) AS contract_kwh_per_kwp,
       sum(t.design_kwh) / nullif(sum(t.kwp_dc) FILTER (WHERE t.design_kwh IS NOT NULL), 0) AS design_kwh_per_kwp
FROM t
LEFT JOIN e ON e.plant_key = t.plant_key AND e.y = t.year AND e.m = t.month AND e.kwh > 0
GROUP BY t.year, t.month;

CREATE VIEW demo.plant AS
WITH ppa AS (
    SELECT round(sum(tariff_mxn_per_kwh * kwp_dc) / nullif(sum(kwp_dc), 0), 4) AS tariff,
           avg(sla_target) AS sla
    FROM public.plant
    WHERE portfolio = 'PPA' AND coalesce(tariff_mxn_per_kwh, 0) > 0
)
SELECT p.plant_key,
       'ARGIA SOLAR ' || coalesce(nm.n::text, p.plant_key)
           || coalesce(' (' || substring(p.customer FROM '\(([^)]*)\)\s*$') || ')', '') AS customer,
       p.brand, p.site_id, p.kwp_dc, p.kwp_ac, p.lat, p.lon,
       'PPA'::text AS portfolio,
       CASE WHEN p.portfolio = 'PPA' THEN p.tariff_mxn_per_kwh ELSE ppa.tariff END AS tariff_mxn_per_kwh,
       p.pr_baseline, p.contracted_kwh, p.active, p.om_cost_monthly_mxn, p.investment_mxn,
       CASE WHEN p.portfolio = 'PPA' THEN p.sla_target ELSE coalesce(p.sla_target, ppa.sla) END AS sla_target,
       p.expected_factor, p.pr_target, p.installation_date,
       NULL::text AS secret_api_name, NULL::text AS secret_user_name, NULL::text AS secret_pass_name,
       p.weather_plant_id, p.datalogger_sn, p.datalogger_addr, p.module_count, p.module_wp,
       p.string_count, p.tilt_deg, p.azimuth_deg,
       NULL::text AS notes,
       p.kwp_dc_override, p.kwp_dc_check, p.pr_stc_model, p.gamma_pmax, p.monitoring_class,
       p.p90_annual_kwh, p.date_interconnection,
       CASE WHEN p.portfolio = 'PPA' THEN p.billing_scheme ELSE coalesce(p.billing_scheme, 'measured') END AS billing_scheme,
       p.module_model, p.show_dashboard, p.show_daily_report,
       true AS show_financial,
       NULL::text AS client_channel
FROM public.plant p
LEFT JOIN demo.name_map nm ON nm.plant_key = p.plant_key
CROSS JOIN ppa;

CREATE VIEW demo.contract_monthly AS
SELECT c.plant_key, c.year, c.month, c.design_kwh, c.contract_kwh, c.tariff_mxn, c.fixed_income_ccy, c.ccy
FROM public.contract_monthly c
JOIN public.plant p ON p.plant_key = c.plant_key
WHERE p.portfolio = 'PPA'
UNION ALL
SELECT p.plant_key, m.year, m.month,
       coalesce(c.design_kwh, round(p.kwp_dc * m.design_kwh_per_kwp, 3)),
       coalesce(c.contract_kwh, round(p.kwp_dc * m.contract_kwh_per_kwp, 3)),
       m.tariff_mxn, NULL::numeric(12,2), 'MXN'::text
FROM public.plant p
CROSS JOIN demo.ppa_tariff_month m
LEFT JOIN public.contract_monthly c ON c.plant_key = p.plant_key AND c.year = m.year AND c.month = m.month
WHERE coalesce(p.portfolio, '') <> 'PPA';

CREATE VIEW demo.loss_daily AS
SELECT l.plant_key, l.prod_date, l.kwp_dc, l.expected_weather_kwh, l.expected_peers_kwh, l.expected_kwh,
       l.expected_basis, l.peers, l.actual_kwh, l.lost_kwh, l.unavailability_kwh, l.overheating_kwh,
       l.underperformance_kwh, l.excused_kwh,
       CASE WHEN p.portfolio = 'PPA' THEN l.tariff_mxn ELSE m.tariff_mxn END AS tariff_mxn,
       CASE WHEN p.portfolio = 'PPA' THEN l.lost_mxn
            ELSE round(l.lost_kwh * m.tariff_mxn, 2) END AS lost_mxn,
       l.computed_at, l.counter_kwh, l.catchup_kwh, l.peer_ratio, l.weather_ratio, l.tolerance_kwh
FROM public.loss_daily l
JOIN public.plant p ON p.plant_key = l.plant_key
LEFT JOIN demo.ppa_tariff_month m
       ON m.year = extract(year FROM l.prod_date)::int AND m.month = extract(month FROM l.prod_date)::int;
COMMIT;
