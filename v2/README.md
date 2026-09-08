# Argia_Mont v2 — fleet monitoring for ARGIA Solar (Mexico)

Operating monitor for the ARGIA solar fleet (11 plants — Growatt, Huawei,
SolarEdge; PPA, CAPEX and LaaS portfolios). It collects inverter telemetry
every 5 minutes, reconciles it nightly against the vendors' own counters,
computes the KPIs (energy vs expected, PR, PR_STC, availability, soiling,
thermal health), alerts by mail and push, closes the month, publishes the
portal and the invoices, and answers questions (Ask ARGIA).

**Live**: https://portal.argia.com.mx (login required). **Runbook**:
`docs/OPERATIONS.md`. **Go-live status**: `docs/GO_LIVE_CHECKLIST.md`.
**Standard vs implementation**: `docs/AGS_701_VS_MONITORING_2026-09.md`.
**Next modules (Project Management + Finance)**: `docs/PM_FINANCE_ARCHITECTURE.md` — decisions, domain model, Savio/Drive/PMO integration, roadmap; the rules live in `argia/fin/` (pure, tested); book inputs `docs/ags/AGS-903_*.md`, `AGS-904_*.md`.

## Layout
```
argia/
├── core/        config (plant + inverter registers), time, normalisation, alert state
├── vendors/     growatt / huawei / solaredge clients + parsers (fixtures under tests/)
├── meteo/       satellite irradiance + weather
├── kpi/         energy, irradiance, performance (PR, PR_STC, availability), reconcile, satellite
├── recon/       nightly reconciliation vs vendor counters, monthly close, 14-day retry
├── analytics/   alert rules: acute, silent, data health, strings, inverter peers, soiling, thermal
├── alerts/      engine (ledger), monitor (infra), subscriptions (who gets what), naming, mail
├── ask/         Ask ARGIA: tools, read-only SQL guard, Golden Standard knowledge
└── store/       PostgreSQL access (psql wrapper), mirror
scripts/         every scheduled job (one file each) — see docs/OPERATIONS.md §2
server/bundle/   what runs on pio06: generators, auth/setup/ask apps, systemd units, nginx — README there is the deploy map
pi/              the off-site Pi: deploy loop, portal watchdog, outage watch, backup pull, CFE fetcher, crontab
tests/           unit (pure functions, source-level invariants), fixtures (captured vendor responses), regression
docs/            runbooks, design notes, the go-live checklist
tools/           one-off, kept for traceability (e.g. the AGS-701 Rev 1 patch)
```

## Principles
- **Git is the only source of truth.** Nothing is patched on a server by hand; `scripts/drift_check.py` compares every deployed copy with git every morning and the administrator is told about any difference.
- **Pure functions, tested.** Decisions (what is an alert, who receives it, what a number means) are pure and unit-tested; I/O is thin. Source-level tests pin the invariants that matter (portal-only recipients, CAPEX never mailed, names before codes, every unit sets HOME…).
- **Vendor counters are the reference.** A gap > 3 % between our interval sum and the vendor's daily counter is a collection fault, not production; closed months are frozen; `daily_production` is never edited by hand.
- **Secrets never travel through chat, git or logs** — file to file, 0600.

## Develop
```
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. python -m pytest -q                   # 3,4xx tests, ~15 s
```
Scripts read their configuration from the environment (`pi/env.example`
lists the names); without `ARGIA_PG_MIRROR=1` every job exits quietly, so
the suite and a dry run never touch production.

## History
v1 (repository root, `legacy_v1/`) was the Google-Sheets era. v2 moved
collection to the Pi (July 2026), then everything to the pio06 server
with PostgreSQL (August 2026), retired the Sheets (v188–v192), the old
report domains (v214) and reached the go-live audit at v218 (September
2026). The dated notes in `docs/` and the project workspace tell the story
version by version.
