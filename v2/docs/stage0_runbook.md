# Stage 0 — Growatt Fixture Capture Runbook

Step-by-step. ~10 minutes.

## What this does

Logs into your Growatt account once, hits 6 endpoints, saves their JSON
responses to disk as test fixtures. Once captured, the v2 Growatt parser
can be developed and tested entirely offline.

## Prerequisites

- You're on your laptop with the `argia_solar_monitoring` repo cloned.
- You're in the `v2/` directory (NOT the repo root).
- Python venv activated, `requests` installed (it's already in
  `requirements.txt`).
- Your `GROWATT_USERNAME` and `GROWATT_PASSWORD` env vars are set.

## Steps

### 1. cd to v2 and activate venv

```bash
cd v2
# if you have a venv already:
source .venv/bin/activate
# if not:
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Set credentials (in your shell, not committed anywhere)

```bash
export GROWATT_USERNAME="<your-growatt-username>"
export GROWATT_PASSWORD="<your-growatt-password>"
```

> Don't paste these into chat. Don't commit them. The script reads them from
> env and never writes them to disk.

### 3. Run the script

```bash
python scripts/growatt_capture_fixtures.py
```

You should see output like:

```
14:55:01 INFO: Output: /Users/you/.../v2/tests/fixtures/growatt_web
14:55:01 INFO: Logging in as your-user...
14:55:02 INFO:   login OK (302 → /index)
14:55:02 INFO: Capturing listDevice for account...
14:55:03 INFO:   saved listDevice.json (12543 bytes)
14:55:03 INFO: Capturing getDevicesByPlant for plant GTO1 (9309575)...
14:55:04 INFO:   saved GTO1_getDevicesByPlant.json (8821 bytes)
14:55:04 INFO:   parsed 4 devices from response
14:55:04 INFO: Capturing getPlantData for plant GTO1...
14:55:05 INFO:   saved GTO1_getPlantData.json (4231 bytes)
14:55:05 INFO: Capturing alertPlantEvent for plant GTO1...
14:55:06 INFO:   saved GTO1_alertPlantEvent.json (??? bytes)
14:55:06 INFO: Capturing getWeatherByPlantId for plant GTO1...
14:55:07 INFO:   saved GTO1_getWeatherByPlantId.json (892 bytes)
14:55:07 INFO: Capturing getInvHisData for JFM7DXN00T on 2026-05-10...
14:55:09 INFO:   matched endpoint: /device/getInverterHistory.do
14:55:09 INFO:   saved GTO1_getInvHisData_JFM7DXN00T_2026-05-10.json (487231 bytes)
14:55:09 INFO: Capturing getInvHisData for JFM7DXN00U on 2026-05-10...
14:55:11 INFO:   saved GTO1_getInvHisData_JFM7DXN00U_2026-05-10.json (489102 bytes)
14:55:11 INFO: DONE: all fixtures captured
```

### 4. Verify the fixtures look right

```bash
ls -lh tests/fixtures/growatt_web/
```

Expected files:
- `listDevice.json` — all devices in your account
- `GTO1_getDevicesByPlant.json` — the 4 TAIGENE inverters
- `GTO1_getPlantData.json` — plant aggregate
- `GTO1_alertPlantEvent.json` — **alert feed (NEW — exciting one)**
- `GTO1_getWeatherByPlantId.json` — Growatt's weather
- `GTO1_getInvHisData_<sn>_<date>.json` × 2 — the 155-column history files

Inspect one to confirm no credentials leaked:

```bash
grep -i -E "password|token|cookie|session" tests/fixtures/growatt_web/*.json
```

Expected output: nothing, OR only `***REDACTED***` markers. If you see actual
credentials, **stop and tell me**.

### 5. Commit the fixtures

```bash
git add tests/fixtures/growatt_web/
git status   # double-check what's being added
git commit -m "Stage 0: capture Growatt web UI fixtures (TAIGENE)"
git push
```

The fixtures contain real plant data (inverter SNs, kWh values, etc.) but
NOT credentials. They're safe to commit to your private repo.

## Troubleshooting

### "could not log in"
- Double-check `GROWATT_USERNAME` / `GROWATT_PASSWORD` are correct.
- Try logging in via the web UI manually first to confirm credentials work.
- Growatt sometimes locks accounts after too many failed logins. Wait 15 min.

### "could not find working history endpoint, saved failed attempt"
- The script tries 3 known endpoint variants. If all fail, the
  `_FAILED.json` file will be saved for diagnosis.
- Paste the failed file contents to chat and I'll figure out the right URL.

### Empty `alertPlantEvent.json`
- That's actually meaningful — means TAIGENE has no active alerts (good!).
- Try another plant if you want to see what an alert looks like:
  `python scripts/growatt_capture_fixtures.py --plant-id 9275498 --plant-key SLP1`

### "parsed 0 devices from response"
- The script's parser for getDevicesByPlant didn't find the expected JSON
  structure. The response was still saved, just couldn't auto-extract SNs.
- We'll fix the parser in Stage 1 based on what the real response looks like.

## After this is done

Tell me when fixtures are captured and pushed. I'll write the v2 Growatt
web client (Stage 1) using these fixtures as the test foundation.
