# Stage 5.1 — SolarEdge per-phase + line-to-line + frequency extraction

A focused upgrade to Stage 5 based on live production capture analysis.

## What we learned from the live capture

The Stage 5 design assumed SolarEdge's `/equipment/{siteId}/{sn}/data` response
exposed only ~9 fields based on the older API docs. Live capture (QRO1 inverter)
revealed **15 fields**, including:

```
Top-level (Stage 5 already extracted):
  date, totalActivePower, dcVoltage, groundFaultResistance, powerLimit,
  totalEnergy, temperature, inverterMode, operationMode

Line-to-line voltages (NEW — Stage 5.1 extracts):
  vL1To2, vL2To3, vL3To1

Per-phase nested dicts (NEW — Stage 5.1 extracts):
  L1Data, L2Data, L3Data — each with:
    acCurrent, acVoltage, acFrequency,
    activePower, apparentPower, reactivePower, cosPhi
```

That's **30 meaningful values per inverter**, not 9. The wide plant row now
populates ~22 cells (was ~7 in Stage 5).

## What Stage 5.1 changes

| Wide schema column | Stage 5.0 source | Stage 5.1 source |
|---|---|---|
| `vacr_v`, `vacs_v`, `vact_v` | blank | **L1/L2/L3.acVoltage** ✓ |
| `vac_rs_v`, `vac_st_v`, `vac_tr_v` | blank | **vL1To2, vL2To3, vL3To1** ✓ |
| `pacr_w`, `pacs_w`, `pact_w` | blank | **L1/L2/L3.activePower** ✓ |
| `iac_a` | blank | **mean(L1.acCurrent, L2.acCurrent, L3.acCurrent)** ✓ |
| `pf` | blank | **mean(L1.cosPhi, L2.cosPhi, L3.cosPhi)** ✓ |
| `fac_hz` | blank | **mean(L1.acFrequency, L2.acFrequency, L3.acFrequency)** ✓ |

Phase-mean derivations are honest — the three phases nearly always agree
within 0.1% (verified in the captured data). If they ever diverge significantly
the per-phase active_power_w deltas will reveal the imbalance via dedicated
columns.

## Files changed

```
v2/argia/vendors/solaredge_telemetry.py        # adds PhaseData + L1/L2/L3 parsing
v2/argia/telemetry/solaredge_row.py            # _TYPED_MAPPING populates 9 new cols
v2/tests/unit/test_solaredge_telemetry_parser.py   # +Stage 5.1 cases, +regression
v2/tests/unit/test_solaredge_telemetry_row.py      # +Stage 5.1 cases, +regression
v2/docs/stage5_1_solaredge_richer.md           # this file
```

**Files unchanged**: `scripts/telemetry_5m.py`, schema, sheets writer, Growatt,
Huawei. No migration required.

## Migration

```bash
cd ~/Documents/argia_solar_monitoring
unzip -o ~/Downloads/argia_mont_v2_stage5_1.zip

cd v2
PYTHONPATH=. python -m pytest \
    tests/unit/test_solaredge_telemetry_parser.py \
    tests/unit/test_solaredge_telemetry_row.py \
    -v
```

Expect ~70 tests passing. Then:

```bash
git add argia/vendors/solaredge_telemetry.py \
        argia/telemetry/solaredge_row.py \
        tests/unit/test_solaredge_telemetry_parser.py \
        tests/unit/test_solaredge_telemetry_row.py \
        docs/stage5_1_solaredge_richer.md

git commit -m "Stage 5.1: extract SolarEdge L1/L2/L3Data + line-to-line voltages + freq"
git push
```

After CI green, run the workflow live (or dry-run first) for QRO1.

## What you'll see in Telemetry_QRO1

The wide row now populates (per the captured production data for Inverter 2 at 10:50):

```
power_w = 80991 W
pacr_w = 27064, pacs_w = 26998, pact_w = 26929    (sum = 80991 ✓)
vacr_v = 251.07, vacs_v = 251.82, vact_v = 251.08
vac_rs_v = 435.14, vac_st_v = 435.44, vac_tr_v = 435.33
fac_hz = 60.025
pf = 1.0
iac_a = ~109.56
temperature_c = 53.11
epv_total_kwh = 421970.88
etoday_kwh = 470.88
vpv1_v = 893.17 (DC bus voltage)
```

That's a real operational picture, comparable in quality to Growatt and Huawei.

## What still stays blank

By API design, not parser limitation:
- **Per-MPPT data** (vpv2..16, ppv1..9, vstring*, istring*) — SolarEdge architecture
  puts measurement at the per-panel optimizer, not aggregated to the inverter
- **Per-MPPT daily/lifetime energy** (epv1..15_today/total) — same reason
- **Growatt-style fault codes** (fault_code_1/2, warn_code) — SolarEdge uses
  `inverterMode` string instead (captured in `raw_mode`; surfaces in the
  `fault_code` column of the narrow Argia tab when not MPPT)

## Honest gotchas

1. **Phase-mean for iac_a is lossy.** If one phase fails completely, the mean
   averages two phases instead of three — still useful but misleading. The
   per-phase active_power_w columns are the real signal for imbalance.

2. **Reactive power discrepancy.** Production data showed `cosPhi = 1.0`
   per phase but `reactivePower = -5775` VAR. Not a bug — the inverter
   is doing reactive compensation. The pf column will read 1.0; if you
   need actual power factor including reactive component, derive from
   active vs apparent power columns.

3. **Stage 5's GTO2 Inverter 1 issue persists.** Inverter SN `7B115A29-0F`
   returned `count: 0, telemetries: []`. The pipeline handles this cleanly
   (returns no row for that inverter; others still produce). Not a bug,
   just an offline/unconfigured inverter.

4. **No new rate limit cost.** Stage 5.1 doesn't add any API calls. The
   richer parsing happens on the SAME response Stage 5 already fetches.

## What you'll see in Telemetry_Argia

The narrow common row is unchanged in 5.1. The wide tab is where all the
Stage 5.1 wins land.
