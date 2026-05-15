# Stage 6 — SMA telemetry pipeline (sandbox)

Adds SMA Sunny Portal / ennexOS Monitoring API support to the 5-min telemetry
pipeline alongside Growatt, Huawei, SolarEdge.

## What ships in Stage 6

| File | Purpose |
|---|---|
| `argia/vendors/sma.py` | OAuth client_credentials + backchannel consent + Monitoring API transport |
| `argia/vendors/sma_telemetry.py` | Rich parser (defensive against unknown field names) |
| `argia/telemetry/sma_row.py` | Wide + narrow row builders |
| `argia/vendors/factory.py` | Adds SMA branch to vendor factory |
| `scripts/sma_discover_plants.py` | One-shot: list plants + devices |
| `scripts/sma_capture.py` | Saves real sandbox responses as fixtures |
| `scripts/telemetry_5m.py` | Updated: adds `_run_sma` pipeline |
| `tests/unit/test_sma_client.py` | OAuth + endpoint + error tests |
| `tests/unit/test_sma_telemetry_parser.py` | Parser defensive-behavior tests |
| `tests/unit/test_sma_row.py` | Row builder tests |

## ⚠️ Stage 6.1 risk acknowledged

You chose "build full Stage 6 now" instead of capture-first. **Expect a Stage
6.1 hotfix** once we run `sma_capture.py` against the sandbox — specifically
the parser's field-name guesses for `pvGeneration.set` will likely need to
match what SMA actually returns.

The parser is built with this in mind:
- Multiple key variants for each field (e.g. `power`, `pac`, `activePower`,
  `totalActivePower`)
- DEBUG-level log lines that print raw response keys when LOG_LEVEL=DEBUG
- Missing fields stay None, never crash

## Required env vars (already in repo README)

```
SMA_CLIENT_ID=argiamexico_api_sbx
SMA_CLIENT_SECRET=a0TGLSqAs6NU5uH5eFvgSSDjXNhli9eX
SMA_LOGIN_HINT=apiTestUser@apiSandbox.com   # sandbox shared user
SMA_ENVIRONMENT=sandbox                      # or 'production' after contract signing
```

For GitHub Actions:
- Add all 4 as repo secrets
- Add to `.github/workflows/v2-telemetry-5m.yml` env block:
  ```yaml
  SMA_CLIENT_ID: ${{ secrets.SMA_CLIENT_ID }}
  SMA_CLIENT_SECRET: ${{ secrets.SMA_CLIENT_SECRET }}
  SMA_LOGIN_HINT: ${{ secrets.SMA_LOGIN_HINT }}
  SMA_ENVIRONMENT: sandbox
  ```

## Deployment sequence

### 1. Unzip and run tests

```bash
cd ~/Documents/argia_solar_monitoring
unzip -o ~/Downloads/argia_mont_v2_stage6.zip

cd v2
PYTHONPATH=. python -m pytest \
    tests/unit/test_sma_client.py \
    tests/unit/test_sma_telemetry_parser.py \
    tests/unit/test_sma_row.py \
    -v
```

Expect ~80 tests passing. If green, proceed.

### 2. Local discovery (one-shot, sandbox)

Set env vars locally:
```bash
export SMA_CLIENT_ID="argiamexico_api_sbx"
export SMA_CLIENT_SECRET="a0TGLSqAs6NU5uH5eFvgSSDjXNhli9eX"
export SMA_LOGIN_HINT="apiTestUser@apiSandbox.com"
export SMA_ENVIRONMENT="sandbox"
```

Run discovery:
```bash
PYTHONPATH=. python scripts/sma_discover_plants.py
```

Output:
- Plant OIDs from sandbox (probably 1-3 virtual plants)
- Devices per plant (probably 1-3 inverters per plant)
- Suggested Plants + Inverters tab rows ready for paste

### 3. Live capture (one-shot)

```bash
PYTHONPATH=. python scripts/sma_capture.py
```

Writes `tests/fixtures/sma/live_*.json`:
- `live_plants_list.json`
- `live_plant_{id}.json` per plant
- `live_devices_{id}.json` per plant
- `live_plant_sets_{id}.json` (available measurement sets)
- `live_device_sets_{id}.json` (per first device)
- `live_pvgeneration_{id}.json` (the actual telemetry shape)

**Critical**: paste the log here so I can see what field names appear in
`pvGeneration.set`. That tells us whether the parser needs a Stage 6.1
patch or works as-is.

### 4. Add SMA_SANDBOX to the sheet

After discovery, you'll see the actual plant OIDs. Add a Plants row:
```
plant_key:        SMA_SANDBOX
customer:         SMA Sandbox (dev only)
brand:            SMA
site_id:          <plant OID from discovery>
kwp_dc:           0
secret_api_name:  SMA_CLIENT_ID
secret_user_name: SMA_CLIENT_SECRET
secret_pass_name: SMA_LOGIN_HINT
active:           TRUE
```

And Inverters rows (one per device the discovery found).

### 5. Commit + push + CI

```bash
git add argia/vendors/sma.py argia/vendors/sma_telemetry.py \
        argia/vendors/factory.py \
        argia/telemetry/sma_row.py \
        scripts/sma_discover_plants.py scripts/sma_capture.py \
        scripts/telemetry_5m.py \
        tests/unit/test_sma_client.py \
        tests/unit/test_sma_telemetry_parser.py \
        tests/unit/test_sma_row.py \
        tests/fixtures/sma/live_*.json \
        docs/stage6_sma_sandbox.md

git commit -m "Stage 6: SMA pipeline (sandbox-ready) with OAuth + Monitoring API"
git push
```

### 6. Add GitHub secrets + workflow env

Settings → Secrets and variables → Actions:
- `SMA_CLIENT_ID`
- `SMA_CLIENT_SECRET`
- `SMA_LOGIN_HINT`

Edit `.github/workflows/v2-telemetry-5m.yml` env block (and any other workflow that runs `telemetry_5m.py`).

### 7. First CI dry-run

```
Actions → v2 Telemetry 5m → Run workflow
  dry_run: ✓
  plant_key: SMA_SANDBOX
  log_level: DEBUG
```

Expected log:
```
INFO Processing 1 SMA plant(s) [env=sandbox]: ['SMA_SANDBOX']
INFO [SMA_SANDBOX] N active inverter(s): [...]
DEBUG sma pvGeneration 'set' keys for DEV1: [...]   ← THIS IS WHAT WE NEED
INFO [SMA_SANDBOX] fetched N rows from N inverter(s)
INFO [SMA_SANDBOX] Telemetry_SMA_SANDBOX: DRY RUN ...
```

**Paste those DEBUG lines.** They tell us if the parser's field names match
sandbox reality or need a Stage 6.1 patch.

## What stays blank in the SMA wide row by design

SMA aggregates measurement at the inverter level (per-panel monitoring on
SMA happens via Sunny Portal optimizers separately, not through the
Monitoring API). So:

| Column group | Status | Why |
|---|---|---|
| Per-MPPT voltage/power (vpv2..16, ppv1..9) | Blank | SMA aggregates DC at inverter |
| Per-string (vstring*, istring*) | Blank | Same reason |
| Per-MPPT energy (epv*_today/total) | Blank | Same reason |
| Per-phase AC voltage/power | Blank | SMA Monitoring API doesn't expose per-phase |
| Growatt-style fault codes | Blank | SMA uses operationalState strings; surface via `fault_code` in narrow tab |

For sandbox specifically:
| Field | Likely status |
|---|---|
| power_w, etoday, etotal, temperature | Should populate |
| status from operationalState | Should populate |
| DC voltage / DC current / DC power | May or may not populate |
| AC frequency, current, power factor | Probably blank in sandbox |

## Stage 6 → Stage 6.1 expected flow

1. Run discovery → see actual SNs
2. Run capture → see actual `pvGeneration.set` field names
3. Compare against parser's `pick()` lists in `sma_telemetry.py`
4. If field names match → ship as-is, mark Stage 6 done
5. If names differ → Stage 6.1: a small parser patch with the right names,
   tests updated, deploy

Estimated Stage 6.1 size: 10-20 lines if needed.

## Honest disclaimers

1. **Sandbox is not real data.** SMA simulates ennexOS plants with 15-min
   resolution. At 5-min cadence you'll see the same data 3 times until
   it ticks over. Idempotent writes mean this is harmless but the row
   counts in `Telemetry_SMA_SANDBOX` will tick up slowly.

2. **Production switch requires contract signing.** SMA's reply email says
   you need to sign their API contract to get production credentials.
   Until then, only sandbox works.

3. **Backchannel consent in sandbox is auto-accepted.** In production, the
   plant owner has to click a link in an email. The first production run
   after onboarding a new plant will block for up to 10 minutes waiting
   for the click — plan for this.

4. **No live verification possible from this environment.** I built and
   syntax-checked everything, but I can't test against SMA's sandbox
   from my sandbox. First live run is the real test.
