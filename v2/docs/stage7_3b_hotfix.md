# Stage 7.3b — Hotfix: SheetsClient missing methods + telemetry sparsity flag

## The bug

When you ran `python scripts/infer_plant_specs.py --apply`, this happened:

```
ERROR argia.infer_specs: Failed to update Inverters[MEX1/ES2470051825]:
    'SheetsClient' object has no attribute 'write_cell'
```

My fault. I wrote code that called `sheets.write_cell(...)` and
`sheets.write_row(...)` and `sheets.delete_row(...)` — none of which
existed on the real `SheetsClient`. The unit tests used bare `MagicMock()`,
which auto-invents any attribute access, so my tests "passed" calling
non-existent methods. Classic mock-blind-spot bug.

The bug is latent in:

| Script | Affected path |
|---|---|
| `scripts/infer_plant_specs.py` | `--apply` path: calls `write_cell` |
| `argia/archive/kpi_daily.py` | upsert update path: calls `write_row`. Triggered when re-running `kpi_eod.py` for a date that already has rows. |
| `argia/archive/kpi_daily.py` | prune path: calls `delete_row`. Triggered by `kpi_eod.py --prune-apply`. |

You only hit one of these because your KPI_Daily was empty (no update
path needed) and you hadn't run `--prune-apply` yet.

## The fix

`argia/core/sheets.py` now has three new methods:

- `write_cell(tab, row, col, value, value_input_option="RAW")` — single-cell update
- `write_row(tab, row, values, value_input_option="USER_ENTERED")` — write whole row starting at col A
- `delete_row(tab, row)` — delete one row, shifting subsequent rows up (uses batchUpdate's deleteDimension)

Plus a `_col_to_a1` helper (1→A, 27→AA) and tab-GID caching for delete_row.

**No changes to call sites.** The existing `kpi_daily.py` and
`infer_plant_specs.py` already use these method names — they just
needed the methods to exist.

## What I also added: a regression test against bare MagicMock

`tests/unit/test_sheets_writes.py::TestSpecCatchesMissingMethods` uses
`MagicMock(spec=SheetsClient)` instead of bare `MagicMock()`. With
`spec=`, calling a method that doesn't exist on the real class raises
`AttributeError` — which is what should happen.

I'll roll this discipline forward into future test files. Honest:
this should have been caught in 7.3 not 7.3b.

## Deploy

```bash
cd ~/Documents/argia_solar_monitoring
unzip -o ~/Downloads/argia_mont_v2_stage7_3b.zip
cd v2

# Tests
PYTHONPATH=. python -m pytest tests/unit/test_sheets_writes.py -v
# expect 25 passed

# Then retry the apply
PYTHONPATH=. python scripts/infer_plant_specs.py --apply
```

## SEPARATE OBSERVATION: the telemetry pipeline is writing way too few rows

Worth surfacing — this is not a Stage 7.x bug but it's blocking 7.x from giving meaningful output.

The `infer_plant_specs.py --apply` output showed:

```
Loaded 95 telemetry rows
GTO1   JFM5D8900B   Days=1
GTO1   JFM7DXN00T   Days=1
[... 25 more inverters with Days=1 ...]
SLP1   JNM7DY306D   Days=0  — no telemetry rows for this inverter
```

That's **95 rows / 30 inverters / 7 days = ~0.45 rows per inverter per day.**
A healthy 5-minute cadence over a ~10-hour daylight window would give
**120 rows per inverter per day**. You're getting ~250× less than expected.

This is consistent with the irradiance fallback we saw earlier (only
2-6 ShineMaster samples per day on 7 plants). It's not "the script is
too strict at 2 days minimum" — it's that the upstream telemetry pipeline
hasn't been writing meaningful row counts.

### Likely causes (in order of probability)

1. **Cron only ran a few times in the last 7 days.** Check
   `journalctl -u cron` on the Pi, or whatever scheduler is in use.
2. **Cron is running but the vendor fetch is failing silently.** Check
   the cron log file you're directing stdout/stderr to.
3. **Telemetry IS being fetched but failing to write to the sheet.**
   Check for repeated SheetsClient errors in the logs.
4. **Aggressive dedup/upsert is overwriting older rows instead of
   appending.** Telemetry_Argia should be append-only at 5-min cadence.

### Recommended diagnostic

```bash
# On the Pi (or wherever telemetry cron runs):
# 1. Check the cron schedule
crontab -l | grep -i argia

# 2. Look at recent cron output
tail -200 /var/log/argia/*.log
# or wherever your cron output goes

# 3. Manually run one telemetry cycle
PYTHONPATH=v2 python v2/scripts/telemetry_5m.py
# Watch for errors. After the run, the Telemetry_Argia tab should have
# ~30 new rows (one per active inverter)

# 4. Check Telemetry_Argia row count over time
# In Sheets, sort by timestamp_utc desc and look at the time gaps
```

### Why I'm flagging this separately

Stage 7.4 (alert engine) will need 14+ days of dense telemetry to make
meaningful alert decisions. If we ship 7.4 before fixing this, every
alert will be either "data_stale" (fires constantly) or "PR_LOW" (because
sparse data produces weird PR values).

**My recommendation: fix the upstream telemetry pipeline BEFORE Stage 7.4.**
Otherwise we're building the alert engine on quicksand.

I don't know your telemetry cron code well enough to debug it from here.
If you want, paste the recent cron log + the `telemetry_5m.py` source
and I'll look at it.
