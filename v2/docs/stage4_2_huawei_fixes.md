# Stage 4.2 — Fix Huawei per-MPPT semantics + add phase voltages

Two targeted fixes based on live daylight data observations from May 14.

## What's wrong, what we're fixing

### Fix 1: `mppt_X_cap` is lifetime energy in Wh, not daily

Stage 4.1 wrote `mppt_X_cap` values into the wide schema's `epv{i}_today_kwh`
columns. Live data revealed two problems:

**Symptom**: Overnight runs showed `mppt_X_cap` values barely changing. Daily
counters should reset at midnight. They didn't.

**Actual nature of the field**: `mppt_X_cap` is **lifetime cumulative energy
per MPPT, in Watt-hours**. The numerical values (~56,000 for a moderately-used
MPPT) are inconsistent with kWh (no MPPT generates 56k kWh in a day) but
plausible as Wh.

**Fix**:
- Rename the dataclass field `pv_eday_kwh` → `pv_etotal_kwh`.
- Parser divides each value by 1000 to convert Wh → kWh.
- Row builder routes the values to `epv{i}_total_kwh` columns.
- `epv{i}_today_kwh` columns now stay blank for Huawei (the API doesn't expose
  per-MPPT daily energy at all).

### Fix 2: Phase voltages were left blank

The Stage 4.1 DEBUG log showed Huawei's `getDevRealKpi` exposes BOTH
line-to-line (`ab_u`, `bc_u`, `ca_u`) AND line-to-neutral (`a_u`, `b_u`,
`c_u`). Stage 4.1 only read line-to-line, leaving `vacr_v`, `vacs_v`, `vact_v`
blank.

**Fix**: parse `a_u`, `b_u`, `c_u` into new dataclass fields `a_u_v`, `b_u_v`,
`c_u_v` and map them to `vacr_v`, `vacs_v`, `vact_v` in the wide row builder.

## Verifying the fix matches reality

From the May 14 09:30 daylight run:

```
Inverter 2 (ES2470051826):
  etoday_kwh = 180.81             # whole-inverter day energy (kWh)
  epv1_today_kwh (Stage 4.1) = 56822.68  ← way too high
  
Math: if 1 MPPT did 56k kWh today, but whole inverter did 180 kWh,
the data is broken by ~300x — confirming Wh ≠ kWh and not daily.

Stage 4.2 behavior:
  epv1_today_kwh = blank          # Huawei doesn't expose per-MPPT daily
  epv1_total_kwh = 56.82          # lifetime kWh (56822.68 Wh ÷ 1000)
```

## Files changed

```
v2/argia/vendors/huawei_telemetry.py             # parser + dataclass
v2/argia/telemetry/huawei_row.py                 # row builder mapping
v2/tests/unit/test_huawei_telemetry_parser.py    # 4.2 + 4.1 regression
v2/tests/unit/test_huawei_telemetry_row.py       # 4.2 + 4.1 regression
v2/docs/stage4_2_huawei_fixes.md                 # this file
```

**Files unchanged**: schema (no column adds/renames), Growatt code, sheets
writer, telemetry script.

## Migration

No sheet wipe. No schema migration. Just code:

```bash
cd ~/Documents/argia_solar_monitoring
unzip -o ~/Downloads/argia_mont_v2_stage4_2.zip

cd v2
PYTHONPATH=. python -m pytest \
    tests/unit/test_huawei_telemetry_parser.py \
    tests/unit/test_huawei_telemetry_row.py \
    -v
```

Expect ~50 tests passing.

```bash
git add argia/vendors/huawei_telemetry.py \
        argia/telemetry/huawei_row.py \
        tests/unit/test_huawei_telemetry_parser.py \
        tests/unit/test_huawei_telemetry_row.py \
        docs/stage4_2_huawei_fixes.md

git commit -m "Stage 4.2: fix Huawei per-MPPT energy semantics + phase voltages"
git push
```

CI green → run the workflow live to see the fix in action.

## What you'll see after running live

In `Telemetry_MEX1`:

| Column | Before (Stage 4.1) | After (Stage 4.2) |
|---|---|---|
| `vacr_v` | blank | ~277 V (line-to-neutral) |
| `vacs_v` | blank | ~277 V |
| `vact_v` | blank | ~277 V |
| `epv1_today_kwh` | 56822 (wrong, was lifetime Wh) | blank |
| `epv1_total_kwh` | blank | 56.82 (lifetime kWh) |

In `Telemetry_Argia`: **no change.** The narrow common row doesn't carry
per-MPPT or phase voltage columns.

## What stays broken

- **MPPTs 17-21 still missing** (schema has 16; deferred decision)
- **Per-MPPT daily energy** still unavailable (Huawei API limitation, not ours)
- **`iac_a` single AC current** still blank (Huawei reports per-phase only)
- **Growatt fault_code_1/2 columns** still blank for Huawei (different fault model)
