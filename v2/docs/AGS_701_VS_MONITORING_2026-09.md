# ARGIA Golden Standard vs the operating monitor — gap register (2026-09-07)

Tomasz: "make sure our Argia Golden Standard is 100 % corresponding with the
reality of monitoring … change AGS when it is wrong, our current monitoring
should be the source of truth."

The AGS is the design/engineering standard (chapters AGS-000 … 902, 364
slides × en/es/cz). Monitoring lives in **AGS-701 O&M & Performance
Monitoring** (slides 278–287); AGS-702 (O&M contracts), AGS-901 (data
traceability), AGS-504 (met station) and AGS-603 (capacity test) touch it.
Everything below was checked against the code of Argia_Mont v2 at v218.

## Method
1. Fetched the live page (`knowledge.AGS_URL`), parsed with `argia.ask.knowledge.parse_ags` (1092 slides).
2. Read every monitoring-related clause; for each: what the AGS says, what the code does (file + constant), verdict.
3. Rewrote AGS-701 slides 281–287 (en/es/cz) and quiz question 2 so the chapter states the operating reality — thresholds, cadence, what is measured and what is *not yet* — with `tools/ags_701_rev1_patch.py` (idempotent, applied to the live HTML; 21 slides changed, 1071 untouched, rendered headless without errors).
4. The result is `AGS_rev1_2026-09-07.html` (delivered) — it replaces the file on sprinkler.agency; the weekly `argia-ags-ingest` refreshes Ask ARGIA from the live page, so until the upload the server's `knowledge` table was loaded from the file by hand.

## Register — AGS-701 (monitoring)
| Rule | AGS Rev 0 said | Reality in Argia_Mont v2 | Verdict → action |
|---|---|---|---|
| R1 monitoring class | IEC 61724-1 Class A (heated pyranometer) for bankable/larger, Class B otherwise | 5-min inverter telemetry from the vendor platforms (`scripts/telemetry_5m.py`); site irradiance sensor where installed (`IrradianceSource.SHINEMASTER`), cloud-cover/satellite model otherwise; satellite drift check REVIEW at 10 % vs a 40-day baseline (`argia/kpi/satellite.py DRIFT_REVIEW_PCT`); completeness ≥ 95 % for a reconciled day (`argia/recon/engine.py COMPLETENESS_MIN_PCT`) | Rule kept (it is a requirement); **AGS now states the operating basis** — no Class-A (heated, calibrated) sensor is registered for any fleet plant in the monitor's configuration (the site sensors are vendor weather stations) → the fleet is Class B until one is. |
| R2 PR_STC vs model 0.87 + commissioning baseline, ≥ 1 yr | PR and PR_STC daily (`argia/kpi/performance.py`; γ = −0.35 %/°C placeholder, `GAMMA_PMAX_DEFAULT`), expected energy from irradiance × kWp × design PR; monthly close vs contracted kWh (`contract_monthly`, `reconciliation_monthly`) | **AGS corrected**: the comparison is against the plant's expected energy and the PPA contract, not a fleet constant. **Tool gaps**: per-plant γ not configured; no stored AGS-601 commissioning baseline per plant. |
| R3 trigger below ~95 % of P90 "by more than the reconciliation threshold" | Alerts: energy vs expected 85 % WARN / 70 % CRIT (`perf_indicators.EXPECTED_*`), twin plant 85/70 (`TWIN_*`), inverter vs peers 85/70 (`inverter_health.DEFAULT_*`), new string flags (`vendor_flags`), thermal graded 65/70/75 °C with peer ΔT ≥ 5 (`analytics/acute.py`), plant dark / stale 45 min (`alerts/monitor.py PLANT_STALE_MIN`), silent inverter, data gaps > 6 h (`data_health`) | **AGS corrected** to the implemented thresholds; quiz Q2 updated. |
| R4 cleaning vs PB-04, thermography, re-tests | Soiling model with cleaning NOT_DUE/APPROACHING/DUE/OVERDUE (`analytics/soiling.py`, ≥ 7 days); thermography / electrical / torque / vegetation only as logged `maintenance_event`s | **AGS corrected**: says which items the monitor measures and which are logged site work. |
| R5 MTTR, availability ≈ 99 % | Availability daily per inverter = share of daylight slots reporting (`kpi_eod.compute_availability`), plant `sla_target`; acute alerts every 30 min 07:00–19:30 MX + 07:07 digest (`argia-alerts-snap`, `argia-mailer`) | **AGS corrected** (cadence + definition). **Tool gap**: MTTR not computed from `maintenance_event` yet. |
| R6 degradation ≤ 0.4 %/yr | Not implemented — needs ≥ 2 years of monthly PR_STC | **AGS now says so** ("not yet a fleet KPI"). **Tool gap** (planned, data-bound). |
| R7 BESS | No BESS in the fleet | AGS says n/a until installed. |
| R8 warranty claims | Evidence: fault codes, string flags, silent history, thermal health (kWh, MXN) — v216 | **AGS corrected** to name the evidence the monitor produces. |
| R9 reporting KPI set | Daily performance mail (PPA), portal report pages, monthly close + invoices from reconciled counters, financial mail; CO₂ | **AGS corrected**; degradation listed as pending. |
| Methods slide (loop) | monitor → evaluate → compare → investigate → track → report | + **RECONCILE** nightly vs vendor counters (PASS ≤ 1 % / REVIEW ≤ 3 % / FAIL daily; 0.5 / 1.5 % monthly; 14-day retry — `argia/recon/engine.py`, `recon/retry.py`); closed months frozen; maintenance windows silence known outages | **AGS corrected** — reconciliation was missing from the standard entirely. |
| PASS/REVIEW/FAIL, common errors, review | generic | + reconciliation states, satellite drift, soiling DUE, "portal without reconciliation", "flat temperature limit" | **AGS corrected**. |

## Register — neighbouring chapters (no AGS change needed)
| Clause | Reality | Verdict |
|---|---|---|
| AGS-702 R2 availability inverter-level annual ≈ 99 %, weather-normalized PR, response SLA | availability per inverter daily → monthly; `sla_target` per plant; alerts within 30 min | consistent (annual roll-up is a query, not a page — future) |
| AGS-702 R3 LD = lost generation × tariff | thermal card values lost kWh at the month's PPA tariff (`report_gen.thermal_card`) | consistent |
| AGS-901 R2 documented satellite source with uncertainty | Open-Meteo documented in code; uncertainty not stated numerically | consistent; state ±% once measured against a Class-A site |
| AGS-901 R6 bad/missing data flagged, gap-fill documented | `status_note`, `reference_basis`, recon `note` 'history-retry', completeness %, `NO_DATA` | consistent |
| AGS-504 one met station per site | not true for the operating fleet (vendor sensors only) | design requirement for new builds — unchanged; the fleet's basis is stated in 701 R1 |
| AGS-603 capacity test IEC 61724-2 | not a monitor function | unchanged |

## Tool gaps this audit opens (for the roadmap, not blocking go-live)
1. Per-plant module γ (temperature coefficient) in `plant` config → PR_STC exact per datasheet.
2. Commissioning baseline per plant (first 30 clean days after COD) stored and shown next to expected.
3. MTTR from `maintenance_event` (start/end) on the plant report.
4. Degradation KPI from monthly PR_STC once two operating years exist (job + card).
5. Annual availability roll-up per inverter (AGS-702 wording) on the report page.
