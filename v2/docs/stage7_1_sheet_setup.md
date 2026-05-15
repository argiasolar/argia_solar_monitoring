# Sheet setup — Stage 7.1

Stage 7.1 introduces 8 new columns on Plants, 3 new columns on Inverters,
and two new tabs: Thresholds + Alerts.

**This stage adds NO new business logic.** No KPIs computed, no alerts
fired, no PDFs generated. It's pure plumbing so Stage 7.2 onwards has a
schema to read from.

## Why this order

If we added the alert engine and the schema in one go, you'd be debugging
30 files at once when a threshold value looks wrong. Splitting the schema
out first lets you:

1. Populate the new columns from your real plant docs (DC kWp, panel count,
   tilt/azimuth, etc.)
2. Decide on alert thresholds at your own pace, with sensible defaults
   pre-populated
3. See the data in the sheet *before* any code reacts to it

## 1. New columns on Plants tab

Add these 8 columns after `active` (the current last column):

| Column | Type | Meaning | Example |
|---|---|---|---|
| `module_count` | int | Total panels in the plant | 1110 |
| `module_wp` | float | Watts per panel nameplate | 540 |
| `string_count` | int | Total strings across all inverters | 60 |
| `tilt_deg` | float | Panel tilt from horizontal | 15 |
| `azimuth_deg` | float | Panel azimuth (180=south) | 180 |
| `system_losses_pct` | float | Real-world derate % | 14 |
| `commissioning_date` | ISO date | When the plant went live | 2024-03-15 |
| `notes` | text | Free text ops context | "Shading 4-5pm SW corner" |

**All fields are optional.** Empty cells become None at load time, and
checks that depend on a missing field will silently skip for that plant.

Cross-check while you fill these in:

```
kwp_dc ≈ (module_count × module_wp) / 1000
```

If they disagree, one of them is wrong. The loader doesn't enforce this in
7.1, but Stage 7.2's KPI computation will flag mismatches.

## 2. New columns on Inverters tab

Add these 3 columns after `active`:

| Column | Type | Meaning | Example |
|---|---|---|---|
| `mppt_count` | int | Independent MPPT trackers on this inverter | 6 |
| `strings_per_mppt` | int | Strings wired into each MPPT | 2 |
| `rated_kw_dc` | float | DC-side rating (often differs from AC rated_kw) | 110 |

Why `mppt_count` matters: parsers extract `vpv1..vpv16`. If an inverter
only has 6 MPPTs, the parser is padding entries 7-16 with blanks or
default values. Stage 7.2 will use `mppt_count` to validate parser output
per plant.

Why `rated_kw_dc` matters: DC/AC ratio (often called the "DC overbuild")
shows you when clipping is normal vs. when it indicates a fault. A 110kW
DC / 100kW AC inverter clipping at 100kW on a sunny day is correct
behavior; the same clipping at 80kW AC isn't.

## 3. New Thresholds tab

Defines what conditions trigger alerts. One row per (plant, metric,
severity). Columns:

| Column | Meaning |
|---|---|
| `plant_key` | `ALL` for portfolio-wide or a specific key like `QRO1` |
| `metric` | Machine name; see KNOWN_METRICS below |
| `severity` | `INFO`, `WARNING`, or `CRITICAL` |
| `condition` | `below`, `above`, `equals`, or `duration` |
| `value` | Numeric threshold |
| `duration_min` | Minutes the condition must persist (only used when `condition=duration`) |
| `enabled` | `TRUE` / `FALSE` — disable a check without deleting the row |
| `channels` | Comma-separated: `sheet`, `email`, `slack` |
| `notes` | Free text |

### Known metrics (Stage 7.1)

- `inverter_offline` — individual inverter dark
- `inverter_relative` — inverter producing < X of peer mean (X is a ratio 0-1)
- `inverter_temp_high` — inverter temperature above threshold
- `plant_offline` — whole plant dark
- `pr_daily` — end-of-day Performance Ratio
- `energy_daily_pct` — end-of-day kWh vs expected, as ratio 0-1
- `data_stale` — no telemetry rows in N minutes during daylight

Adding a new metric is a code change — that's intentional. The alert
engine needs to know HOW to compute each metric; just dropping a name in
the sheet wouldn't do anything.

### Plant-specific overrides

If a row has `plant_key=QRO1` for the same (metric, severity) as an
`ALL` row, the QRO1 row wins for QRO1. Other plants still get the `ALL`
row. Use this for plants with known quirks — e.g. an older plant where
0.75 PR is genuinely the expected ceiling.

## 4. New Alerts tab

Append-only state store. The alert engine reads + writes it. You
shouldn't edit it manually except to silence a noisy alert (change
`state` from `OPEN` to `SILENCED`).

Schema:

| Column | Meaning |
|---|---|
| `alert_id` | `ALT-YYYYMMDD-NNN` |
| `alert_key` | Deterministic, used for dedup |
| `plant_key`, `inverter_sn`, `metric`, `severity` | Identifies what tripped |
| `state` | `OPEN`, `RESOLVED`, `SILENCED` |
| `opened_utc`, `last_seen_utc`, `resolved_utc` | Timestamps |
| `value` | Observed metric value at most recent check |
| `threshold` | Threshold value that was breached |
| `message` | Human-readable, for the email body |
| `channels_sent` | Comma-separated record of channels notified |

### How the state machine fires emails (preview of 7.4)

```
NEW: condition true, no current OPEN record
  → add OPEN row
  → email if severity matches firing strategy (CRITICAL: immediate)

STILL TRUE: OPEN row already exists
  → update last_seen_utc
  → DO NOT re-email

CLEARED: OPEN row exists, condition now false
  → transition to RESOLVED
  → email resolution if we emailed the open

SILENCED: ops set state=SILENCED in the sheet
  → engine still tracks the condition but does not fire
  → on clear, transitions silently to RESOLVED
```

## 5. Bootstrap

```bash
cd v2
PYTHONPATH=. python -c "
from argia.core.sheets import SheetsClient
from argia.core.thresholds import create_thresholds_tab_if_missing
from argia.core.alerts_state import create_alerts_tab_if_missing
import os

sheets = SheetsClient(sheet_id=os.environ['GOOGLE_SHEET_ID_V2'])
print('Thresholds:', create_thresholds_tab_if_missing(sheets))
print('Alerts:', create_alerts_tab_if_missing(sheets))
"
```

This:
- Creates the Thresholds tab if missing, populates the header + 9 default
  rows (conservative — none fire without data flowing)
- Creates the Alerts tab if missing (header only, no rows)
- Idempotent: re-running does nothing

The **Plants and Inverters new columns must be added manually** — the
bootstrap script doesn't touch existing tabs to avoid clobbering. Add the
columns, then re-run any pipeline; existing 18-column rows continue to
work, the new columns just become None until you fill them.

## 6. Validation

After populating the new columns:

```bash
cd v2
PYTHONPATH=. python -c "
from argia.core.sheets import SheetsClient
from argia.core.config import load_portfolio
from argia.core.thresholds import load_thresholds
from argia.core.alerts_state import load_alerts_ledger
import os

sheets = SheetsClient(sheet_id=os.environ['GOOGLE_SHEET_ID_V2'])
portfolio = load_portfolio(sheets)
ts = load_thresholds(sheets)
ledger = load_alerts_ledger(sheets)

print(f'Plants: {len(portfolio.plants)} ({len(portfolio.active_plants())} active)')
for p in portfolio.active_plants():
    missing = []
    if p.module_count is None: missing.append('module_count')
    if p.module_wp is None: missing.append('module_wp')
    if p.string_count is None: missing.append('string_count')
    if p.tilt_deg is None: missing.append('tilt_deg')
    if p.azimuth_deg is None: missing.append('azimuth_deg')
    print(f'  {p.plant_key:10s} kwp_dc={p.kwp_dc:.1f}  missing: {missing or \"none\"}')
print(f'Thresholds: {len(ts.all_thresholds)} ({len(ts.all_metrics_configured())} metrics configured)')
print(f'Alerts ledger: {len(ledger.records)} records ({len(ledger.all_open())} currently OPEN)')
"
```

You should see your plants enumerated with their missing optional fields.
Fill in as much as you have, ship as you go.

## 7. What's next (Stage 7.2 onwards)

| Stage | Adds |
|---|---|
| 7.2 | KPI computation — daily PR, expected vs actual energy, inverter ranking. Pure math against archived rows. |
| 7.3 | EOD archive + daily PDF (one ops, one customer) generated from a Jinja2 template. |
| 7.4 | Alert engine — actually evaluates thresholds, manages the state machine, sends emails. |
| 7.5 | Static HTML dashboard regenerated daily. |
