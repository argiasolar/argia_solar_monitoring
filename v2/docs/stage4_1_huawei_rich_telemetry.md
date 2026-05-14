# Stage 4.1 — Rich Huawei telemetry from getDevRealKpi

What Stage 4 left blank, Stage 4.1 fills in — by extracting every available
field from the same ``getDevRealKpi`` response we were already calling.

## What changed

The Stage 4 Huawei parser only read 5 fields per inverter (status, power,
eToday, timestamp, SN). The `dataItemMap` Huawei returns actually has many
more fields: **temperature, frequency, power factor, per-MPPT voltages,
per-MPPT day energy, three-phase AC voltages, inverter state codes, total
energy, MPPT total energy**. Stage 4.1 reads all of them.

**No new API endpoint, no new HTTP call, no new rate-limit budget.** Same
single batched call per Huawei plant per 5-min cycle. Just a richer parser.

## What the wide `Telemetry_MEX1` row looks like now

| Column family | Stage 4 | Stage 4.1 |
|---|---|---|
| identity (timestamp, SN, label) | ✓ | ✓ |
| status, power_w, etoday_kwh, pac_w | ✓ | ✓ |
| temperature_c | blank | **populated** |
| pf, fac_hz | blank | **populated** |
| vac_rs/st/tr_v (line-to-line) | blank | **populated** |
| ppv_w, epv_total_kwh | blank | **populated** |
| vpv1..vpv16 (per-MPPT voltage) | blank | **populated where Huawei reports** |
| epv1..15_today (per-MPPT day energy) | blank | **populated where Huawei reports** |
| vacr/s/t_v (phase-to-N) | blank | still blank (Huawei doesn't expose) |
| iac_a (single AC current) | blank | still blank (Huawei reports per-phase) |
| vstring*, istring* (per-string) | blank | still blank (not exposed) |
| Growatt fault_code_1/2/type | blank | still blank (Huawei has different fault model) |

The narrow `Telemetry_Argia` row's `temperature_c` is now real for Huawei
plants too, and `fault_code` carries useful diagnostic info derived from
`devStatus` + `inverter_state` + `run_state`.

## Defensive parsing

Every field is read with `safe_float` and tries multiple candidate key names
(e.g. `active_power` OR `activePower` OR `pac`). If your specific inverter
model uses a different naming convention than expected, the column stays
blank instead of crashing.

**Use DEBUG mode to verify what your hardware returns:**

```
GitHub Actions → v2 Telemetry 5m → Run workflow:
  dry_run: ✓
  plant_key: MEX1
  log_level: DEBUG
```

The log will print, once per inverter:

```
DEBUG huawei dataItemMap for ES2470051825 has 27 fields: ['ab_u', 'active_power',
      'bc_u', 'ca_u', 'day_cap', 'efficiency', 'elec_freq', 'inverter_state', ...]
```

If you see fields in that list that the parser isn't reading — tell me and
we'll add them.

## Files added/changed

```
v2/argia/vendors/huawei_telemetry.py             ~ NEW
v2/argia/telemetry/huawei_row.py                 ~ REWRITTEN (drives from HuaweiTelemetryRow)
v2/scripts/telemetry_5m.py                       ~ updated to call fetch_inverter_telemetry
v2/tests/unit/test_huawei_telemetry_row.py       ~ REWRITTEN for rich rows
v2/tests/unit/test_huawei_telemetry_parser.py    ~ NEW
v2/docs/stage4_1_huawei_rich_telemetry.md        ~ this file
```

**Files unchanged (deliberately):**
- `v2/argia/vendors/huawei.py` — the existing `HuaweiClient` is untouched.
  The new `fetch_inverter_telemetry` lives in `huawei_telemetry.py` and calls
  the client's existing `_post_json` / `_ensure_logged_in`. This avoids any
  risk of breaking the daily/snapshot10m paths that still use
  `fetch_inverter_snapshots`.
- `v2/argia/telemetry/schema.py` — schemas unchanged. Same 142-col wide and
  15-col narrow.
- All Growatt files — unchanged.

## Migration

Easier than Stage 4. No schema change, no sheet-tab wipe.

```bash
cd ~/Documents/argia_solar_monitoring/v2
# unzip the Stage 4.1 delivery on top
unzip -o ~/Downloads/argia_mont_v2_stage4_1.zip
# nothing to git rm — pure additions and one rewrite
```

Stage and verify:

```bash
git add argia/vendors/huawei_telemetry.py \
        argia/telemetry/huawei_row.py \
        scripts/telemetry_5m.py \
        tests/unit/test_huawei_telemetry_row.py \
        tests/unit/test_huawei_telemetry_parser.py \
        docs/stage4_1_huawei_rich_telemetry.md
git status
```

Run tests locally:

```bash
PYTHONPATH=. python -m pytest \
    tests/unit/test_huawei_telemetry_row.py \
    tests/unit/test_huawei_telemetry_parser.py \
    -v
```

Expect: ~50 tests passing, 0 failing.

Then commit + push:

```bash
git commit -m "Stage 4.1: rich Huawei telemetry from getDevRealKpi dataItemMap"
git push
```

CI green → ready to verify live.

## Verification path

### Step 1 — Dry-run MEX1 with DEBUG logging

GitHub → Actions → **v2 Telemetry 5m (all vendors)** → Run workflow:
- dry_run: ✓ checked
- plant_key: `MEX1`
- log_level: **DEBUG**

The log will print every inverter's raw dataItemMap keys. Save this output —
it's the ground truth for what Huawei exposes for your hardware.

### Step 2 — Live MEX1

Same workflow:
- dry_run: ☐ unchecked
- plant_key: `MEX1`

Then open `Telemetry_MEX1` in the sheet. The wide row should now show:
- `temperature_c`: a real number (~30-60°C for an operating inverter)
- `fac_hz`: ~60.0 (Mexico grid frequency)
- `pf`: 0.95-1.0 (power factor)
- `vac_rs_v`, `vac_st_v`, `vac_tr_v`: ~480 V (line-to-line, 277V line-to-neutral)
- `vpv1_v`, `vpv2_v`, ...: per-MPPT DC voltages (typically 600-800 V)
- `epv1_today_kwh`, ...: per-MPPT day energy

`Telemetry_Argia` row for MEX1 should show:
- `temperature_c`: now populated (was blank in Stage 4)
- `fault_code`: now reflects inverter_state/run_state combo, not just "0"

### Step 3 — Live all vendors

dry_run unchecked, plant_key empty. Same expectations + Growatt rows continue
to look as they did in Stage 3/4.

## Honest limitations

**1. We're parsing against API DOCS, not a captured fixture.** Same risk that
bit us in Stage 2 with the Growatt truncation bug. The parser is defensive
(multiple key variants, blanks on miss), but if Huawei returns field names
we haven't anticipated, those cells stay blank. The DEBUG mode is the
mitigation — first live run, you can SEE exactly what's there.

**2. Some fields stay structurally blank.** The wide schema was built for
Growatt's data shape (per-string voltages and currents, two-channel fault
codes). Huawei doesn't expose those, so those columns stay blank. Not a bug
— a vendor-shape mismatch we documented in Stage 4.

**3. `efficiency_pct` and `reactive_power_var` are parsed but not yet
written to the wide row.** They're on `HuaweiTelemetryRow`, available for
future use, but the wide schema doesn't have columns for them. If you want
them surfaced, we can add columns to the schema (bump COLUMN_VERSION, wipe
the tabs, redeploy — same drill as Stage 3→4).

**4. `huawei.py` not modified.** The new `fetch_inverter_telemetry` lives in
`huawei_telemetry.py` and reaches into the client's `_post_json` directly.
Pragmatic (same package, controlled access) but technically using a private
method. If we ever need a third Huawei call path, refactor.
