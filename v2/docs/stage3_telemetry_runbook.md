# Stage 3 — Growatt 5-min telemetry pipeline

What's new and how to verify it works against your real plants.

## What this stage delivers

A new script + workflow that, every time it runs:

1. Reads your portfolio from `Plants` + `Inverters` tabs in `Argia_Mont_v2`.
2. Filters to Growatt plants only.
3. For each active Growatt plant, for each active inverter:
   - Fetches the latest 5-min sample via Growatt web UI (the new Stage 1
     client + parser).
   - Joins plant-level weather (irradiance W/m², 5-min kWh/m², cloud cover %).
   - Builds a wide row (~135 columns).
4. Upserts the rows into `Telemetry_<KEY>` (one tab per plant) AND
   `Telemetry_Argia` (aggregated across plants).
5. Tabs auto-create + header auto-writes on first run. No manual sheet setup.

**Not in scope for Stage 3:**
- No cron schedule yet. Workflow is **manual trigger only**. Once verified
  across 2–3 days, cron flips on in a tiny follow-up commit.
- No daily archive + clear. The tab grows during the day and stays. Stage 5
  will add the end-of-day report + reset.
- `ambient_temp_c` column is currently always blank — the existing
  `GrowattIrradianceClient` doesn't expose env temperature yet. The column
  exists in the schema so we don't have to rewrite the header later; we'll
  populate it in a Stage 3.x patch when we add the method.

## Files added

```
v2/argia/telemetry/__init__.py
v2/argia/telemetry/schema.py            ~ column definitions, ~135 cols/row
v2/argia/telemetry/growatt_row.py       ~ pure row builders
v2/argia/telemetry/sheets_writer.py     ~ ensure_tab + ensure_header + upsert
v2/scripts/growatt_telemetry_5m.py      ~ the cron-runnable script
v2/tests/unit/test_growatt_telemetry_schema.py
v2/tests/unit/test_growatt_telemetry_row.py    ~ uses real TAIGENE fixture
v2/tests/unit/test_growatt_telemetry_writer.py
.github/workflows/v2-growatt-telemetry-5m.yml  ~ manual trigger
v2/docs/stage3_telemetry_runbook.md     ~ this file
```

## Before you run anything: 30-second sanity check

Open Argia_Mont_v2 sheet → `Inverters` tab. Every Growatt inverter SN you
care about must be listed there, with `active=TRUE` and `plant_key` matching
the Plants tab.

The script silently skips plants with no active inverter rows. The most
likely reason for "nothing happened" on a real run is "no inverters
configured" rather than "code broke."

Right now we know TAIGENE (GTO1) has 4 SNs:
- `JFM7DXN00T`
- `JFM7DXN00U`
- `JFM5D8900B`
- `JFMCE9D014`

For the other Growatt plants (SLP1, SLP2, NL1, NL2, MEX3, OECHSLER), check
that their inverter SNs are in the Inverters tab. If not, add them before
running, or the script will skip those plants.

## Verification path

### Step 1 — Local tests

```bash
cd v2
PYTHONPATH=. python -m pytest \
    tests/unit/test_growatt_telemetry_schema.py \
    tests/unit/test_growatt_telemetry_row.py \
    tests/unit/test_growatt_telemetry_writer.py \
    -v
```

Expect: all passing, no failures. The row-builder tests use the real
TAIGENE fixture so they're exercising actual Growatt JSON shapes.

### Step 2 — Push and let CI verify

```bash
git add v2/argia/telemetry v2/scripts/growatt_telemetry_5m.py \
        v2/tests/unit/test_growatt_telemetry_*.py \
        v2/docs/stage3_telemetry_runbook.md \
        .github/workflows/v2-growatt-telemetry-5m.yml
git commit -m "Stage 3: Growatt 5-min telemetry pipeline (manual workflow only)"
git push
```

CI runs the full v2 test suite across Python 3.10/3.11/3.12. Should be
green.

### Step 3 — First dry-run for TAIGENE only

GitHub → Actions → **v2 Growatt Telemetry 5m** → Run workflow:
- dry_run: ✓ (true)
- plant_key: `GTO1`
- log_level: `INFO`

Run. Watch the log. You should see:

```
... INFO Loaded portfolio: N plants (M active), K inverter rows
... INFO Processing 1 Growatt plant(s): ['GTO1']
... INFO [GTO1] 4 active inverter(s): ['JFM7DXN00T', 'JFM7DXN00U', ...]
... INFO [GTO1] Telemetry_GTO1: DRY RUN {'inserted': 0, 'updated': 0, 'unchanged': 0, 'dry_run': 4}
... INFO [ARGIA] Telemetry_Argia: DRY RUN {'dry_run': 4, ...}
... INFO DONE: plants_processed=1 plants_skipped=0 rows_collected=4 errors=0 dry_run=True
```

Exit code 0. No sheet writes happened.

If any line says `fetch/parse failed`, paste it back and we'll debug. Most
common cause: a credential mismatch or Growatt rate limit on rapid retries.

### Step 4 — First live run for TAIGENE

Same workflow, but uncheck `dry_run`. Run.

Expected log:
```
... INFO [GTO1] Telemetry_GTO1: {'inserted': 4, 'updated': 0, 'unchanged': 0}
... INFO [ARGIA] Telemetry_Argia: {'inserted': 4, 'updated': 0, 'unchanged': 0}
```

Then open the v2 sheet. New tabs appeared:
- `Telemetry_GTO1` — 1 header row + 4 data rows (one per inverter).
- `Telemetry_Argia` — 1 header row + 4 data rows.

Eyeball each tab:
- Headers match the schema (`timestamp_utc`, `timestamp_mx`, `inverter_sn`,
  `inverter_label`, then `status`, `power_w`, `etoday_kwh`, `pac_w`, etc.).
- Data rows have real numbers — `pf=1.0`, `fac_hz≈60`, per-MPPT voltages,
  per-string voltages, fault codes typically `0`.
- Weather columns: `irradiance_wm2` populated if it's daylight, blank
  overnight. `cloud_cover_pct` should always be there if Open-Meteo is
  reachable.

### Step 5 — Idempotency check

Run the same live workflow again right away (same inputs). Wait for it to
finish.

Expected:
- `Telemetry_GTO1` row count UNCHANGED (still 5 rows total).
- Log shows something like `{'inserted': 0, 'updated': 0, 'unchanged': 4}`
  OR `{'inserted': 0, 'updated': 4, 'unchanged': 0}` depending on whether
  the inverter produced a new sample between runs.

If row count GREW, upsert is broken. Stop and let me know.

### Step 6 — Wait a few minutes, run again

Wait ~5 minutes. Run the live workflow once more. Each Growatt inverter
should now have a NEW row with a more recent `timestamp_utc`. Row count
should be 5 header + (4 SNs × 2 timestamps) = 13.

If you see the new sample appended cleanly alongside the old one, the
pipeline is doing exactly what it should.

### Step 7 — All Growatt plants

Once GTO1 looks good, run the workflow with `plant_key` left empty and
`dry_run` still checked. This exercises every active Growatt plant.

Log will show one block per plant. For each plant, either:
- ✓ `[<KEY>] N active inverter(s): [...]` followed by a dry-run row count
- ✗ `[<KEY>] no active inverters in Inverters tab — skipping` ← means you
  need to add SNs to the Inverters tab for that plant
- ✗ `fetch/parse failed: ...` ← actual problem; capture the message

When dry-run passes for all plants, uncheck `dry_run` and run live. You'll
see new `Telemetry_<KEY>` tabs appear for each plant. Aggregated tab
`Telemetry_Argia` has one row per (plant, inverter) pair.

## After verification: turn on cron (small follow-up commit)

Edit `.github/workflows/v2-growatt-telemetry-5m.yml`, add a schedule block:

```yaml
on:
  workflow_dispatch:
    inputs: ...  # keep existing block
  schedule:
    # Every 5 minutes from 12:00 UTC (06:00 MX) to 02:00 UTC next day (20:00 MX)
    # Two cron blocks to avoid the cross-midnight UTC range.
    - cron: "*/5 12-23 * * *"  # 12:00-23:55 UTC
    - cron: "*/5 0-2 * * *"    # 00:00-02:55 UTC
```

That's roughly 14 hours × 12 runs/hour = ~168 runs/day. Each run does
roughly: N HTTP calls per inverter × ~17 inverters across all Growatt
plants. Growatt has been fine at the existing 10-min cron rate; the new
5-min rate is double that — keep an eye on rate-limit errors in the first
24 hours.

If you see rate limit errors, the fix is one of:
- Add a sleep between plants (currently we only sleep between inverters
  within a plant).
- Stagger plant processing so not all plants hit Growatt simultaneously.
- Move some plants to a separate workflow with offset cron.

Don't pre-optimize. Run it. See what breaks.

## Honest limitations / known gotchas

**Parser is MAX-only.** The Stage 1 parser handles Growatt MAX-series
inverters. If any of your plants has TLX, MIX, or MOD inverters, the
`get_max_history` endpoint will either return empty data or fail, and that
plant's inverters will be silently skipped. If the log says "no history
rows for <SN>" repeatedly for a plant, the inverter type may be wrong.
We extend the parser in a future stage.

**Today-only.** `get_max_history` is called with `date_iso = today MX
local`. There's no backfill capability in this script — that's by design,
because the goal here is live 5-min data, not historical replay. Historical
backfill is a different tool.

**Weather is best-effort.** If irradiance fetch fails, those cells go blank
for that 5-min sample. The inverter data still lands. Weather doesn't block
the inverter write path.

**No alerts yet.** This stage only collects data. The alerting layer
consumes this data in a later stage. The wide schema is designed to support
alerts on per-string voltage dropouts, fault codes, temperature, etc.,
without re-fetching.

**Inverter labels come from the Inverters tab.** If you want a human-readable
name in the row (e.g. "Inverter 1" instead of "JFM7DXN00T"), set
`inverter_label` in the Inverters tab. Otherwise the SN is used as the
label.

## Rollback

If anything goes wrong:
- The script never modifies existing tabs (DailyProduction, InverterSnapshot10m,
  SyncRuns). Those are untouched.
- Existing cron jobs (v1, or v2 daily/snapshot) are not affected.
- To "uninstall" Stage 3: delete the new `Telemetry_*` tabs in the sheet
  and revert the commit. No state to clean up elsewhere.
