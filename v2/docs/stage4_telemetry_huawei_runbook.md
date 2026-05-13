# Stage 4 — Huawei telemetry + cross-vendor unified pipeline

What's new and how to deploy it safely.

## What changed from Stage 3

| Thing | Stage 3 | Stage 4 |
|---|---|---|
| Vendors supported | Growatt only | Growatt + Huawei |
| `Telemetry_<KEY>` schema | 142-col wide, Growatt-only | 142-col wide, vendor-agnostic (Huawei rows mostly empty) |
| `Telemetry_Argia` schema | **143-col wide** | **15-col narrow common (BREAKING CHANGE)** |
| Script | `growatt_telemetry_5m.py` | `telemetry_5m.py` |
| Workflow | `v2-growatt-telemetry-5m.yml` | `v2-telemetry-5m.yml` |
| Sheets writer | Header preserved if mismatched | **Refuses with clear error if mismatched** |

The narrow common schema:

```
timestamp_utc, timestamp_mx, vendor, plant_key, inverter_sn, inverter_label,
status, power_w, etoday_kwh, temperature_c, fault_code,
irradiance_wm2, irradiance_kwh_m2_5m, cloud_cover_pct, ambient_temp_c
```

Natural key: (timestamp_utc, plant_key, inverter_sn). Across vendors.

## Files added/changed

```
v2/argia/telemetry/schema.py            ~ updated: ARGIA_SCHEMA narrowed
v2/argia/telemetry/growatt_row.py       ~ updated: build_argia_row → build_common_row
v2/argia/telemetry/huawei_row.py        ~ NEW: build_plant_row + build_common_row
v2/argia/telemetry/sheets_writer.py     ~ updated: SchemaMismatchError
v2/scripts/telemetry_5m.py              ~ NEW: unified Growatt + Huawei
v2/tests/unit/test_growatt_telemetry_schema.py  ~ updated for narrow ARGIA
v2/tests/unit/test_growatt_telemetry_row.py     ~ updated, build_common_row tests
v2/tests/unit/test_growatt_telemetry_writer.py  ~ updated, SchemaMismatchError tests
v2/tests/unit/test_huawei_telemetry_row.py      ~ NEW
v2/docs/stage4_telemetry_huawei_runbook.md      ~ this file
.github/workflows/v2-telemetry-5m.yml           ~ NEW: unified workflow
```

Files **removed** (cleaner than leaving them):

```
v2/scripts/growatt_telemetry_5m.py
.github/workflows/v2-growatt-telemetry-5m.yml
v2/docs/stage3_telemetry_runbook.md  (superseded by this doc)
```

## Migration — DO THIS IN ORDER

### Step 1 — Wipe the old Telemetry_Argia tab

The new `Telemetry_Argia` schema is incompatible with the Stage 3 wide version.

1. Open `Argia_Mont_v2` sheet.
2. Right-click the `Telemetry_Argia` tab → **Delete**.
3. Confirm.

That's it for Sheets. The per-plant `Telemetry_<KEY>` tabs stay — same schema.

If you skip this step, the script will refuse to write to the tab and tell you why:

```
SchemaMismatchError: Tab 'Telemetry_Argia' exists but its header doesn't match
the 'argia' schema. Expected 15 columns (first='timestamp_utc',
last='ambient_temp_c'); found 143 columns. To fix: delete the tab
'Telemetry_Argia' in the Sheets UI, then re-run.
```

Loud and clear, no silent data corruption.

### Step 2 — Apply the Stage 4 update

```bash
cd ~/Documents/argia_solar_monitoring
unzip -o ~/Downloads/argia_mont_v2_stage4.zip

cd v2
git rm scripts/growatt_telemetry_5m.py
git rm docs/stage3_telemetry_runbook.md
git rm ../.github/workflows/v2-growatt-telemetry-5m.yml
```

The unzip drops in the new files; the `git rm` lines remove the old ones that are being superseded.

### Step 3 — Verify locally

```bash
PYTHONPATH=. python -m pytest \
    tests/unit/test_growatt_telemetry_schema.py \
    tests/unit/test_growatt_telemetry_row.py \
    tests/unit/test_growatt_telemetry_writer.py \
    tests/unit/test_huawei_telemetry_row.py \
    -v
```

Expect: all pass. If you see anything like "no module named 'google'", that's the same local-only google-auth gap from before — just push and let CI run.

### Step 4 — Commit and push

```bash
git add argia/telemetry/ scripts/telemetry_5m.py \
        tests/unit/test_growatt_telemetry_*.py \
        tests/unit/test_huawei_telemetry_row.py \
        docs/stage4_telemetry_huawei_runbook.md \
        ../.github/workflows/v2-telemetry-5m.yml
git commit -m "Stage 4: Huawei telemetry + cross-vendor common Argia schema"
git push
```

Wait for CI to come back green. ~30 seconds.

## Verification path (after CI is green)

### Step A — Dry-run, single Huawei plant

GitHub → Actions → **v2 Telemetry 5m (all vendors)** → Run workflow:
- dry_run: ✓
- plant_key: (one of your Huawei plant_keys, e.g. `MEX1`)
- log_level: `INFO`

Expected log shape:
```
INFO Processing 1 Huawei plant(s): ['MEX1']
INFO [MEX1] N active inverter(s): ['ES...', 'GR...', ...]
INFO [MEX1] Telemetry_MEX1: DRY RUN {'dry_run': N, ...}
INFO DONE: plants_processed=1 plants_skipped=0 rows_collected=N
```

If any inverter is offline or didn't return data, you'll see:
```
WARNING [MEX1/SOME_SN] Huawei API did not return data for this SN
```

That's informational, not an error — same idea as Growatt's "no history rows for today" case.

### Step B — Dry-run, all vendors

Same workflow, but leave `plant_key` empty. Expected log will show **both** Growatt and Huawei pipelines running. Confirm:
- Growatt plants process as before (same SNs, same row counts)
- Huawei plants get processed (1 row per inverter that the API returns)
- The aggregated rows write to a fresh `Telemetry_Argia` tab

### Step C — Live, all vendors

Uncheck `dry_run`, leave `plant_key` empty. Run.

In the v2 sheet:
- `Telemetry_Argia` should appear with the new 15-column schema.
- Each per-plant `Telemetry_<KEY>` tab still has the 142-column wide schema (unchanged from Stage 3 for Growatt; mostly-empty rows for new Huawei tabs).
- Argia tab row count = (Growatt inverters returning data) + (Huawei inverters returning data).

For your portfolio (10 plants, 30 inverters: 6 Growatt + ~4 Huawei plants), expect roughly 15-20 rows per run.

### Step D — Idempotency check

Run live again immediately. Argia tab row count must NOT grow. Per-inverter rows either stay (same timestamp) or update in place (new timestamp from same SN+plant).

## Honest limitations

**Huawei rows are skinny.** The current Huawei parser only extracts status,
power_w, etoday_kwh, timestamp from each `getDevRealKpi` response. The API
exposes more (`temperature` in `dataItemMap`, mppt-level data via other
endpoints, fault info via `getAlarmList`), but the parser doesn't surface
those yet. Stage 4.x will extend.

**SolarEdge and SMA plants are skipped.** The script logs them as "not yet
built" and moves on. Stage 5 = SolarEdge, Stage 6 = SMA.

**ambient_temp_c stays blank.** Same gap as Stage 3 — the env-station temp
reader isn't wired yet.

**Huawei rate limits.** Per the Huawei docs, `getDevRealKpi` is capped at 5
calls/min/token. The unified script makes 1 batched call per Huawei plant,
so a 5-min cron with ~4 Huawei plants = ~4 calls/run = well under the limit.

## When to flip cron on

Same advice as Stage 3:
1. Run the workflow manually a few times across 24 hours.
2. Confirm `Telemetry_Argia` stays clean (no duplicates, no schema errors).
3. Confirm Huawei plants behave sensibly (status flips on/off at appropriate
   times, no constant errors).
4. Then add the schedule block to `.github/workflows/v2-telemetry-5m.yml`:

```yaml
on:
  workflow_dispatch:
    inputs: ...
  schedule:
    - cron: "*/5 12-23 * * *"  # every 5 min, 12:00-23:55 UTC
    - cron: "*/5 0-2 * * *"    # every 5 min, 00:00-02:55 UTC
```

## Rollback

If anything goes wrong:
- The new tabs (`Telemetry_<KEY>`, `Telemetry_Argia`) can be safely deleted.
- Existing daily/snapshot tabs (DailyProduction, InverterSnapshot10m, SyncRuns)
  are untouched.
- The v1 cron is unaffected.
- To revert: `git revert` the Stage 4 commit and re-deploy.
