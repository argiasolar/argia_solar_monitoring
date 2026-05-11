# Argia_Mont v2.0

Solar plant monitoring system for the Argia portfolio (Mexico).

Replaces the v1 `argia_solar_monitoring` codebase with a tested, modular architecture
that supports Growatt, Huawei, SolarEdge, and SMA inverters.

## Why v2?

v1 worked but had:
- 5 near-duplicate Growatt clients
- Two scripts (`argia_snap.py`, `argia_sync.py`) with identical purpose
- No tests anywhere
- Naive timezone handling (`utcnow() + timedelta(hours=-6)` breaks DST)
- Append-only writes with no idempotency (re-running = duplicate rows)
- Inverter SNs stored as columns with typos (`INVERTER2`, `IVERTER2`)
- Dead code paths (SMA scaffold never finished)

v2 fixes all of the above and adds SolarEdge.

## Project layout

```
argia/
├── core/        # sheets client, time utils, normalization, config loader
├── vendors/     # one file per vendor: growatt, huawei, solaredge, sma
└── meteo/       # irradiance + cloud cover

tests/
├── unit/        # pure functions, fast, no network
├── fixtures/    # captured real API responses (anonymized)
└── regression/  # schema lock + idempotency

scripts/         # CLI entry points the Pi cron calls
pi/              # crontab.example for Raspberry Pi
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run the test suite
pytest

# See coverage
pytest --cov=argia
```

## Required environment variables

Stored in `~/.argia_mont.env` on the Pi (chmod 600). NEVER commit.

```
GOOGLE_SHEET_ID=...
GOOGLE_CREDENTIALS=...     # service account JSON, single line

GROWATT_USERNAME=...
GROWATT_PASSWORD=...
GROWATT_API_TOKEN=...      # Open API token (preferred over web scraping)

HUAWEI_USERNAME=...
HUAWEI_PASSWORD=...

SOLAREDGE_API_KEY=...

SMA_CLIENT_ID=...          # sandbox/prod
SMA_CLIENT_SECRET=...
SMA_LOGIN_HINT=...         # email used during back-channel consent
SMA_ENVIRONMENT=sandbox    # sandbox or production
```

## Sheet schema (v2)

See `MIGRATION.md` for the new tab layout and how to migrate from v1.

## Running

The Raspberry Pi cron is the scheduler. See `pi/crontab.example`.

```bash
# Daily aggregate (one row per plant per day, idempotent)
python scripts/argia_mont_daily.py

# 10-minute snapshot during daylight hours
python scripts/argia_mont_10min.py
```
