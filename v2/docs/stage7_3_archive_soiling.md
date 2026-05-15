# Stage 7.3 — Archive + Soiling + Cleaning Costs

Three things at once. Honest order: if you only read part of this doc,
read the **honest disclaimers** section first.

## What's here

| File | Purpose |
|---|---|
| `argia/archive/kpi_daily.py` | KPI_Daily tab: schema, upsert (idempotent), 14-day prune |
| `argia/analytics/soiling.py` | Rolling PR vs baseline, cleaning cost decision |
| `argia/core/config.py` | Plants tab gains `pr_baseline` + `tariff_mxn_per_kwh`. Sanity warnings at load time. |
| `scripts/kpi_eod.py` | Daily cron: read yesterday's telemetry → upsert KPI_Daily |
| `scripts/soiling_check.py` | Read-only CLI: rolling-PR vs baseline assessment |
| `tests/unit/test_kpi_daily.py` | 32 tests covering serialization, upsert, prune |
| `tests/unit/test_soiling.py` | 24 tests covering decision logic + missing inputs |
| `tests/unit/test_config_v73.py` | 14 tests covering new fields + sanity warnings |

## Honest disclaimers (read these first)

1. **Soiling math is a framework, not a truth.** The thresholds —
   "DUE when projected monthly loss ≥ 100% of cleaning cost" — are
   educated guesses. Real-world tuning happens after 30+ days of
   archived KPIs per plant. For now, expect false positives if
   `pr_baseline` was set too high.

2. **`pr_baseline` needs human input.** No plant in your portfolio
   has one yet. Until you set baselines, every soiling assessment
   returns `INSUFFICIENT_DATA`. The cron will run, the tab will fill,
   but the alert engine in 7.4 won't fire soiling alerts until baselines
   exist.

3. **14-day pruning is destructive.** I gated it behind `--prune-apply`
   (not the default). The first time you run with `--prune` (no apply),
   you'll see how many rows it WOULD delete. Once you trust the count,
   add `--prune-apply` to the cron line.

4. **Sanity warnings reveal what we already saw in the demo.**
   When you next run anything that calls `load_portfolio()`, you'll
   see WARNING logs like:
   - `[GTO1] kwp_dc=100.0 disagrees with module_count×module_wp=599.4 by >15%.`
   - `[QRO1/SN1] rated_kw is 0 on Inverters tab — peer ranking will not work for this inverter`
   These are HELPFUL — they pinpoint exactly which cells you need to fix.

5. **Soiling alerts haven't been wired to email yet.** That's Stage 7.4.
   In 7.3, you run `soiling_check.py` manually and see the output. The
   results are NOT persisted to a sheet either.

## Step 1: Add 2 new columns to Plants tab

After your existing Stage 7.1 columns:

| Column | Type | Meaning |
|---|---|---|
| `pr_baseline` | float (0-1) | Clean-state PR for this plant. Leave empty until you have one. |
| `tariff_mxn_per_kwh` | float | Energy price for soiling cost-benefit math. |

## Step 2: Bootstrap the new tabs

```bash
cd v2
PYTHONPATH=. python -c "
from argia.core.sheets import SheetsClient
from argia.core.config import load_portfolio
from argia.archive.kpi_daily import create_kpi_daily_tab_if_missing
from argia.analytics.soiling import create_cleaning_costs_tab_if_missing
import os

sheets = SheetsClient(sheet_id=os.environ['GOOGLE_SHEET_ID_V2'])
portfolio = load_portfolio(sheets)

print('KPI_Daily:', create_kpi_daily_tab_if_missing(sheets))
print('Cleaning_Costs:', create_cleaning_costs_tab_if_missing(
    sheets, plant_keys=sorted(p.plant_key for p in portfolio.active_plants())
))
"
```

After this:
- **KPI_Daily** exists with header, no rows yet.
- **Cleaning_Costs** exists with header + one empty row per active plant.
  Fill in `cost_mxn` and `last_cleaned_date` per plant.

## Step 3: First EOD run (dry-run)

```bash
PYTHONPATH=. python scripts/kpi_eod.py --dry-run
```

Expected output:
- Per-plant log line with energy, PR, CF, confidence
- "KPI_Daily upsert: {'inserted': N, 'updated': 0, 'unchanged': 0}" — N being your plant count
- Lots of sanity warnings (kwp_dc, rated_kw, module math) — these are your sheet TODO list

If the numbers look right, drop `--dry-run`:

```bash
PYTHONPATH=. python scripts/kpi_eod.py
```

This writes yesterday's KPIs to `KPI_Daily`. Re-running is idempotent
(re-writes the same date's row, doesn't append).

## Step 4: Cron wiring

Add to your existing cron (probably `pi/crontab.example`):

```cron
# EOD KPI: 01:30 MX every day. Prune previewed but not applied.
30 1 * * * cd /home/pi/argia && PYTHONPATH=v2 python v2/scripts/kpi_eod.py --prune >> /var/log/argia/kpi_eod.log 2>&1

# Once a week, actually prune old KPI rows (Sunday 02:00 MX)
0 2 * * 0 cd /home/pi/argia && PYTHONPATH=v2 python v2/scripts/kpi_eod.py --prune-apply >> /var/log/argia/kpi_prune.log 2>&1
```

Why split prune from main run: if pruning ever malfunctions, daily KPI
upserts keep working while you investigate.

## Step 5: Set up baselines (multi-day workflow)

This is the part you have to do by hand. Recipe:

1. Pick a plant that you know was recently cleaned.
2. Run `kpi_eod.py` daily for 14 days. The KPI_Daily tab fills with
   that plant's PRs.
3. Pull the 14-day median PR (you can compute it yourself or look at
   the values).
4. Enter that median as `pr_baseline` on the Plants tab.
5. Repeat for each plant.

There's no automation for this in 7.3 — and shouldn't be. Setting a
baseline is a deliberate "this is what clean looks like for THIS
plant" decision that needs your eyes on it.

## Step 6: Soiling assessment (manual for now)

```bash
PYTHONPATH=. python scripts/soiling_check.py
```

Expected output (a few weeks in):

```
=== Soiling assessment as of 2026-06-14 ===

Plant      Decision          PR(roll)  PR(base)   Loss%  Loss$/mo    Cost$  Notes
----------------------------------------------------------------------------------
QRO1       NOT_DUE              0.815     0.820    0.6%       250     8500
GTO1       APPROACHING          0.788     0.820    3.9%      4720     5000  
MEX1       DUE ⚠                0.756     0.820    7.8%      9200     5000  
NL1        INSUFFICIENT_DATA      --       --        --        --     5500  pr_baseline missing
SLP1       INSUFFICIENT_DATA      --     0.810      --        --       --   cleaning cost not configured
```

Run weekly. The ones marked DUE or OVERDUE are your maintenance priority
list. APPROACHING is a heads-up.

## Architecture: how the pieces fit

```
                    ┌──────────────────────────┐
   5-min cron  ──►  │     Telemetry_Argia      │  (already exists, 7.2 reads from here)
                    └──────────────────────────┘
                                  │
                                  │ kpi_eod.py reads 1 day
                                  ▼
                    ┌──────────────────────────┐
       EOD cron  ──►│        KPI_Daily         │  (14-day rolling, append+update)
                    └──────────────────────────┘
                          │              │
                          │              │ soiling_check.py reads 14 days
                          ▼              ▼
                  ┌───────────────┐  ┌────────────────────┐
                  │   Plants      │  │  Cleaning_Costs    │
                  │ (pr_baseline, │  │  (cost_mxn,         │
                  │  tariff)      │  │   last_cleaned)    │
                  └───────────────┘  └────────────────────┘
                                  │
                                  ▼
                          SoilingAssessment
                          (printed, not persisted in 7.3)
```

Stage 7.4 will read both `KPI_Daily` (for performance alerts) and the
soiling assessment (for cleaning alerts) and write to the existing
`Alerts` state machine + send the email digest.

## What's next

| Stage | Adds |
|---|---|
| 7.4 | Alert engine: reads Thresholds, evaluates KPI_Daily + soiling, writes to Alerts tab, sends email digest |
| 7.5 | Once 30 days of KPI history exists per plant: automated `pr_baseline` derivation suggestion (still requires human approval) |
| 7.6 | Daily PDF report (ops + customer variants) |
| 7.7 | HTML dashboard regenerated nightly |
