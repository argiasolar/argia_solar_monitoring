# Stage 5 — SolarEdge telemetry pipeline

Adds SolarEdge to the unified 5-min telemetry script alongside Growatt and Huawei.

## Three pieces ship in this stage

1. **Inverter discovery** — `scripts/solaredge_discover_inverters.py`
   - Calls `/equipment/{siteId}/list` for each SolarEdge plant
   - Prints inverter SNs ready for paste into the Inverters tab
   - Burns 1 API call per site (~2 total) — well within quota

2. **Live capture** — `scripts/solaredge_capture.py`
   - Saves real production responses to `tests/fixtures/solaredge/live_*.json`
   - 2 calls per plant (site_list + equipment_data for first inverter)
   - These fixtures replace the synthetic ones we've been testing against

3. **Telemetry integration**
   - New `argia/vendors/solaredge_telemetry.py` — rich parser + fetch fn
   - New `argia/telemetry/solaredge_row.py` — row builders for wide + narrow tabs
   - Updated `scripts/telemetry_5m.py` — adds SolarEdge alongside Growatt + Huawei
   - 429 rate-limit handling: catches and skips remaining SE plants for that run

## ⚠️ Rate limit reality check

**SolarEdge enforces 300 API calls/day per site/api_key combo.** At 5-min cadence
with 4-5 inverters per site:

```
6 calls/run × 12 runs/hr × 14 hrs daylight = 1,008 calls/day
```

That's ~3.4× over the per-site quota. The script will hit HTTP 429 around
mid-morning. Once it does:

- The SolarEdge pipeline catches the 429, logs a warning, skips remaining
  SolarEdge plants for that run
- Growatt and Huawei pipelines continue normally
- Quota resets at midnight UTC

This is the "deal with it when it hits" approach you chose. You'll see real
data for a few hours each morning, then nothing until midnight.

**To increase quota long-term**: contact SolarEdge sales or
`monitoringAPI@solaredge.com` and ask about the aggregator program
(installer-class accounts get higher limits). Or drop SolarEdge to 15-min
cadence (one workflow change).

## Migration steps

### Step 1 — Discover inverter SNs

```bash
cd ~/Documents/argia_solar_monitoring/v2
PYTHONPATH=. python scripts/solaredge_discover_inverters.py
```

The script prints tab-separated rows ready to paste into the Inverters tab.
**Add the rows manually** — the script doesn't write to the sheet itself.

Expected format:
```
plant_key  inverter_sn  inverter_label  capacity_kwp_dc  active
QRO1       7E1A2B3C-FF   Inverter 1                       TRUE
...
```

The `capacity_kwp_dc` column needs to be filled in manually (the SolarEdge
equipment list endpoint doesn't expose it).

### Step 2 — Capture live fixtures

```bash
PYTHONPATH=. python scripts/solaredge_capture.py
```

Writes `tests/fixtures/solaredge/live_site_list_QRO1.json` etc. Verify the
files exist and look reasonable. Add them to git so we have a baseline.

### Step 3 — Apply Stage 5

```bash
unzip -o ~/Downloads/argia_mont_v2_stage5.zip

cd v2
PYTHONPATH=. python -m pytest \
    tests/unit/test_solaredge_telemetry_parser.py \
    tests/unit/test_solaredge_telemetry_row.py \
    -v
```

Expect ~50 tests passing. Commit + push:

```bash
git add argia/vendors/solaredge_telemetry.py \
        argia/telemetry/solaredge_row.py \
        scripts/telemetry_5m.py \
        scripts/solaredge_discover_inverters.py \
        scripts/solaredge_capture.py \
        tests/unit/test_solaredge_telemetry_parser.py \
        tests/unit/test_solaredge_telemetry_row.py \
        docs/stage5_solaredge.md

# Add the captured fixtures if you ran solaredge_capture.py
git add tests/fixtures/solaredge/live_*.json

git commit -m "Stage 5: SolarEdge telemetry pipeline + rich parser + rate-limit handling"
git push
```

### Step 4 — Verify live

GitHub → Actions → **v2 Telemetry 5m (all vendors)** → Run workflow:
- dry_run: ✓ (start safe)
- plant_key: `QRO1`
- log_level: `DEBUG`

Expected log:
```
INFO Processing 1 SolarEdge plant(s): ['QRO1']
INFO [QRO1] 5 active inverter(s): ['SN1', 'SN2', ...]
DEBUG solaredge telemetry latest entry keys for SN1: ['date', 'dcVoltage',
       'groundFaultResistance', 'inverterMode', 'operationMode',
       'powerLimit', 'temperature', 'totalActivePower', 'totalEnergy']
INFO [QRO1] fetched 5 telemetry rows from 5 inverter(s)
INFO [QRO1] Telemetry_QRO1: DRY RUN {...}
```

Then live (uncheck dry_run). New tab `Telemetry_QRO1` will be created with
~7 populated columns per row (status, power, etoday, pac, etotal, temp, vpv1)
plus weather, and the narrow Argia tab gets SolarEdge-vendor rows.

## What the wide row looks like

| Column | Filled | Notes |
|---|---|---|
| identity (timestamp, SN, label) | ✓ | |
| status, power_w, etoday_kwh, pac_w | ✓ | |
| epv_total_kwh, temperature_c | ✓ | |
| vpv1_v | ✓ | DC bus voltage (single value) |
| Everything else (~130 cols) | blank | SolarEdge doesn't expose them |

The narrow Argia row gets temperature populated and `fault_code` reflects
the inverter mode (e.g. `MODE=FAULT`, `MODE=SLEEPING`).

## Honest disclaimers

1. **Captured fixtures don't exist yet.** We tested against synthetic data
   matching the SolarEdge docs. The first live capture might reveal that
   your inverters expose slightly different fields. The DEBUG mode prints
   what's actually there.

2. **The rate limit WILL bite.** Expect "rate-limited" warnings in the logs
   every day after ~mid-morning until quota resets. Other vendors keep working.

3. **`vpv1_v` is the DC bus voltage, not MPPT 1.** SolarEdge inverters
   aggregate DC at the inverter level (per-panel optimization happens
   upstream). Don't read vpv1 as Growatt's "MPPT 1 input voltage" — it's
   the combined DC bus.

4. **`etoday_kwh` derived from totalEnergy diff** — slight inaccuracy at
   the very start of the day (before the first telemetry entry of the day).
   The existing SolarEdgeClient does the same; we're consistent.
