# Argia_Mont v2 — operations runbook (go-live edition, 2026-09-07)

One page for whoever runs the fleet monitor: where things live, what runs
when, how to deploy, how to restore, who gets which mail, what to do when
something is red. Everything here is verified against the running system
at v218.1 (`scripts/drift_check.py` proves the deployed copies equal git
every morning).

## 1. Hosts
| Host | Role | Access |
|---|---|---|
| **pio06** — `37.235.105.173`, Debian, hosted at vas-hosting.cz | Everything: collection, PostgreSQL 17 `argia_mont`, portal, mails, backups source | `ssh -i ~/.ssh/argia_pio06 root@37.235.105.173` (key only; password login disabled) |
| **Pi** — `argiapi.local` (Zapopan office), user `zemel` | Off-site vantage point only: portal watchdog (ntfy), outage-mode PPA watch, nightly backup pull, CFE tariff fetcher | `ssh zemel@argiapi.local` |
| **GitHub** `argiasolar/argia_solar_monitoring` (main) | Source of truth; CI `v2-tests` on every push | deploy key on pio06 and the Pi (read-only) |
| **Laptop** (Windows, Git Bash, Python 3.14 venv) | Development, commits via the staged bundle script | — |

Do not touch the hosting panel (VPS Centrum) for anything nginx/domains —
nginx is hand-managed from the repo (`server/bundle/*.conf`).

## 2. What runs when (pio06, systemd timers; MX = America/Mexico_City)
| Timer | When | Does |
|---|---|---|
| argia-telemetry | every 5 min 05–21 MX | 5-minute inverter telemetry, Growatt + Huawei (`telemetry_5m.py --skip-brand SOLAREDGE`) |
| argia-telemetry-se | :03/:23/:43 06–20 MX | SolarEdge telemetry |
| argia-alerts-snap | :00/:30 07–19 MX | acute alerts (engine → `alert_ledger`) + ledger mail |
| argia-mailer | :07/:37 every hour | infrastructure/plant alert mailer (`alert_mailer.py`), 07:07 MX WARNING digest |
| argia-kpi | 06:00 MX | KPI end-of-day (`kpi_eod.py --dense-irradiance`) → `daily_production` |
| argia-recon-close | 1st 06:10 MX | monthly close (`recon_close.py`) — closed months are frozen |
| argia-satcheck | 06:20 MX | site sensor vs satellite irradiance drift |
| argia-alerts-daily | 06:30 MX | daily alert tier (energy vs expected, twins, thermal day peak) |
| argia-drift | 06:30 MX | status-quo harness (git vs deployed, smoke answers, backup age) |
| argia-finreport | 06:50 MX | financial report pages |
| argia-report-am, argia-report-pm | 07:05 / 20:45 MX | daily report pages (yesterday / today) |
| argia-client-pages | :15 07–20 MX | client report pages |
| argia-dash-update | :02/10 06–20 MX | dashboard tabs + HTML |
| argia-portal-gen | every 5 min | portal pages (`/www/hosting/portal.argia.com.mx/www`) |
| argia-invoice | 1st 07:30 MX | monthly invoices for PPA plants |
| argia-cfe-ingest / argia-cfe-push | 09:15 MX / 15:45 UTC | CFE tariff CSV from the Pi → `cfe_tariff`; push to the ARGIA Engine |
| argia-dailyperf | 19:00 MX | daily PPA performance mail |
| argia-strings | 21:15 MX | string-level daily aggregates |
| argia-recon | 23:50 MX | nightly reconciliation vs vendor counters (+14-day retry) |
| argia-finmail-weekly, argia-finmail-monthly | Fri 23:59 / 1st 23:59 MX | financial mail |
| argia-thermal | 01:10 MX | inverter thermal health (`thermal_daily`, `thermal_bins`) |
| argia-archive, argia-archive-month | 03:00 MX / 2nd 03:00 MX | telemetry archive + retention |
| argia-dbdump | 03:30 server time | pg_dump + auth DB + portfolio snapshot → `/root/argia_backups` |
| argia-ags-ingest | Sun 04:20 | Golden Standard page → `knowledge` (Ask ARGIA) |

Services (always on): `argia-auth` 8512 (login/session), `argia-setup` 8511 (admin), `argia-ask` 8513 (Ask ARGIA); nginx in front. Every job runs through `pi/run_job.sh <name> <script>` (venv, secrets, flock, log `/root/argia_logs/<name>.log`).

Pi cron (`pi/crontab.example` is byte-for-byte the live table): `deploy.sh` every 10 min (follows main), `report_watch.sh` every 5 min (portal probe → ntfy `argia-reportwatch-x9k24fq7`), `ppa_watch.sh` every 30 min 08–19 (acts only while the server is down), `pull_backup.sh` 22:00 (dumps + `portfolio.json`), `cfe_daily.sh` 08:10.

## 3. Secrets (never in git, chat or logs)
`/root/.argia_env` (vendor + PG + mail switches), `/root/.argia_mail` (SMTP), `/root/.argia_cfe_push` (Engine token), `/root/.argia_ask` (model key), `/root/.googlecredentials.json`, `/root/.growatt*.json` — all `0600 root`. Auth state in `/opt/argia/auth/` (`users.db` 0600, `sessions.db`, `session.key`). To inspect the env, only ever `grep "^ARGIA_.*SOURCE\|^ARGIA_SHEET\|^ARGIA_KPI_WRITE" /root/.argia_env`. Rotation = edit the file in place, restart the service that reads it.

## 4. Deploy (the only way code reaches the server)
```
cd /root/argia_v2 && git fetch -q && git reset --hard -q origin/main
cp v2/server/bundle/*.py v2/server/bundle/*.sh v2/server/bundle/*.sql /opt/argia/bundle/
cp v2/server/monitoring_gen.py /opt/argia/bundle/
cp v2/server/bundle/argia-*.service v2/server/bundle/argia-*.timer /etc/systemd/system/ && systemctl daemon-reload
# nginx only when a vhost/snippet changed — see server/bundle/README.md for the file map
systemctl restart argia-auth argia-setup argia-ask       # only when those apps changed
/root/argia_v2/v2/pi/run_job.sh drift drift_check.py && tail -3 /root/argia_logs/drift.log   # "status quo intact"
```
Rules: commit scripts use `set -o pipefail` and are idempotent; never hand-edit a deployed file (the drift check will name it the next morning); never `crontab <file>` on the Pi.

## 5. Restore
- **Database**: dumps in `/root/argia_backups/argia_mont_YYYYMMDD.dump` (3 kept) and on the Pi `~/db_backups/daily` (14) + `weekly` (8). `runuser -u postgres -- pg_restore -d argia_mont --clean --if-exists /path/argia_mont_YYYYMMDD.dump`.
- **Accounts**: `users_YYYYMMDD.db` → `/opt/argia/auth/users.db` (chmod 600), then `systemctl restart argia-auth argia-setup`.
- **Server rebuild**: install PostgreSQL 17 + nginx + python3-venv; clone the repo to `/root/argia_v2`; `python3 -m venv v2/.venv && pip install -r v2/requirements.txt`; recreate the secret files; run the deploy block; restore the DB; `systemctl enable --now` every `argia-*.timer` and the three services; certificates via certbot webroot for `portal.argia.com.mx` (the old three domains only 301).

## 6. Who gets which mail (v217 rules, v223 cadence)
| Channel (`/setup/`) | Content | Who |
|---|---|---|
| maintenance | **one morning mail** after the 06:30 daily run: new CRITICAL first, then WARNING, grouped plant → issue → inverters, the still-open reminder, explanations once per issue type; **during the day only CRITICAL** (energy being lost or a unit off: plant at 0 W, inverter fault, inverter off ≥3 h, measured thermal loss, production < 70 %) — a WARNING waits for the next morning. **PPA plants only, CAPEX never**; names first, codes as detail | subscribers, scoped per plant |
| — | infrastructure & monitoring-internal (job failed, disk, PostgreSQL, CFE pipeline, reconciliation FAIL, sensor drift, config drift, smoke) | **administrator only** (`ARGIA_MAIL_ADMIN`, default tomasz.zemelka@argia.com.mx) |
| daily | 19:00 MX PPA performance mail | subscribers |
| financial | Friday weekly + monthly financial mail | subscribers |
| reports | AM/PM report pages | subscribers |
| ntfy | `portal.argia.com.mx is DOWN / BACK UP` (+ HTTP code), backup pull failures, outage-mode plant stalls | the topic on the admin's phone |

If the plant table cannot be read, plant alerts are **held** (fail closed) and infrastructure alerts still go out — nothing is silently sent to a CAPEX customer.

Severity rule (Tomasz, 2026-09-07): **CRITICAL = energy is being lost or a plant/inverter is off**; data gaps, heat without a measured loss and diagnostic flags without a measured loss are WARNING. A fleet-wide telemetry blank (the collector, the vendor cloud, PostgreSQL) never becomes an inverter alert (`silent.collector_windows`). The infra mailer (`alert_mailer.py`, every 30 min) no longer judges plants — the ledger does.

## 7. When something is red
| Symptom | First look | Then |
|---|---|---|
| "job failed: argia-x.service" | `journalctl -u argia-x -n 50`, `/root/argia_logs/<name>.log` | `systemctl reset-failed argia-x && systemctl start argia-x`; transient vendor errors self-heal, the nightly recon fills the day |
| plant dark / stale | vendor portal reachable? `tail /root/argia_logs/telemetry.log` | if the site is really down, log a maintenance event in `/setup/` — it silences the alert without losing the trail |
| reconciliation FAIL | portal → Monitoring → Reconciliation; `reconciliation_daily.note` | `recon_snapshot.py --retry-days 14` runs nightly; never edit `daily_production` by hand |
| config-drift / smoke-fail (digest) | `/root/argia_logs/drift_latest.json` `findings` | deploy from git or commit the server's version — never leave them apart |
| portal DOWN (ntfy) | `curl -sI https://portal.argia.com.mx/login` (401 = healthy), `systemctl status nginx argia-auth` | `nginx -t && systemctl reload nginx`; `systemctl restart argia-auth` |
| PostgreSQL slow / locks | `runuser -u postgres -- psql -c "SELECT pid, now()-query_start, left(query,60) FROM pg_stat_activity WHERE state<>'idle'"` | never run an ad-hoc query on `plant`/`telemetry` without `SET statement_timeout`; `pg_terminate_backend(pid)` by explicit pid |
| backup stale | `ls -l /root/argia_backups`, `journalctl -u argia-dbdump` | `systemctl start argia-dbdump`; on the Pi `tail ~/argia_logs/db_backup_pull.log` |

## 8. Where the numbers are explained
Portal "How the numbers are calculated" (report pages), `docs/AGS_701_VS_MONITORING_2026-09.md` (standard vs implementation), `docs/INVERTER_THERMAL_HEALTH_V216.md`, Ask ARGIA (`/ask/`, read-only SQL + Golden Standard search).
