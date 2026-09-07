# Go-live checklist — Argia_Mont v2 (status 2026-09-07, v218.1)

Verified = checked on the running system today, with the command or test that proves it. Open = Tomasz's decision or action.

## Code and configuration
| # | Item | Status | Proof |
|---|---|---|---|
| 1 | GitHub `main` = server checkout = deployed copies | **verified** | `drift_check`: 84/84 deployed files match git, 0 extras, checkout at origin/main, clean |
| 2 | GitHub `main` = laptop | **verified** | every commit is made on the laptop by the staged script; `git status` clean after push |
| 3 | GitHub `main` = Pi | **verified** (v217.1 by hand; self-updating again every 10 min) | `~/argia_logs/deploy_cron.log` on the Pi |
| 4 | No hand-edited files on any host | **verified + guarded** | `argia-drift.timer` daily → admin WARNING on drift |
| 5 | Test suite green | **verified** | 3,457 passed / 11 skipped (sandbox 3.11), laptop 3.14, CI `v2-tests` green on every commit since v215.4 |
| 6 | Every deployed file type has a deploy rule | **verified** | `test_drift_check.py::test_every_real_bundle_file_has_a_deploy_rule_or_is_declared` |
| 7 | Every systemd job that uses `run_job.sh` sets HOME | **verified** | `test_v217_mail_hygiene.py::TestV217_1PiFollowUp` |

## Data and jobs
| # | Item | Status | Proof |
|---|---|---|---|
| 8 | All 28 timers enabled and active, no failed units | **verified** | `systemctl list-timers 'argia-*'`, `systemctl --failed` empty |
| 9 | Nightly backup + off-site copy | **verified** | dump 03:30 on pio06, pulled 22:00 MX to the Pi (log `pull OK` nightly); `portfolio_latest.json` now produced (v217.1) |
| 10 | Restore procedure written | **written, not rehearsed** | `docs/OPERATIONS.md §5` — **open: one restore rehearsal into a scratch database** |
| 11 | Reconciliation retries past days when counters return | **verified** | v212 `recon/retry.py`, 14-day window, tests |
| 12 | Closed months frozen; `daily_production` never edited by hand | **verified** | `closed_plant_months()` in the recon path; rule in OPERATIONS.md |

## Communication
| # | Item | Status | Proof |
|---|---|---|---|
| 13 | CAPEX plants never mailed, fail closed | **verified** | v217 `subscriptions.NEVER_MAILED_PORTFOLIOS`, `HOLD_ALL`; dry run `portfolio filter: 1 alert(s) not mailed (TAM1)` |
| 14 | Names first, codes as detail, in every mail and push | **verified** | v217 `naming.py`; server render of Budenheim / Plastic Omnium alerts |
| 15 | Infrastructure & internal alerts only to the administrator | **verified** | `ARGIA_MAIL_ADMIN` routing, tests |
| 16 | Pi watchdog probes the portal, names it, carries the HTTP code | **verified** | `report_watch.log`: `OK (HTTP 401)` every 5 min; `BACK UP` push received |
| 17 | Recipients = portal accounts only, managed in `/setup/` | **verified** | `only_portal()` at send time |

## Security (audit 2026-09-07)
| # | Item | Status | Proof / note |
|---|---|---|---|
| 18 | ssh: key only, root without-password, MaxAuthTries 3, fail2ban (sshd jails) | **verified** | `sshd -T` |
| 19 | Secrets 0600, none in git history | **verified** | perms listed; history scan (patterns) clean; no `.env`/key files ever committed |
| 20 | `users.db` 0600; portal web root 750 root:www-data; old web root 700 | **fixed today** | was 644 / 755 / 755 on a shared host |
| 21 | Portal security headers (HSTS, X-Frame-Options, Referrer-Policy, nosniff, `always`) | **fixed today** | `curl -sI https://portal.argia.com.mx/login` |
| 22 | Login throttling | **verified** | auth_app "Too many attempts" back-off; nginx zones present |
| 23 | PostgreSQL bound to localhost only | **verified** | `listen_addresses = localhost` |
| 24 | Unattended security upgrades, 0 pending | **verified** | `unattended-upgrades 2.12` |
| 25 | Stray data files out of `/opt/argia/bundle` (migration CSVs, old bundles) | **fixed today** | moved to `/root/argia_attic/` (0700) |
| 26 | GitHub repository is PUBLIC | **open — make it private** | Settings → Danger zone → Change visibility (code names hosts, customers, structure) |
| 27 | GitHub Actions repository secrets from the Sheets era | **open — delete** | Settings → Secrets; the workflows no longer use them |
| 28 | CFE Engine push secret that once transited chat | **open — rotate** | edit `/root/.argia_cfe_push` and the Engine side, file to file |
| 29 | Seven ssh keys on root: 3 × vas-hosting staff, tomasz, `petrk`, two restricted service keys (cfe-pi rrsync, backup-pull sftp) | **open — confirm `petrk` and the hosting keys are wanted** | `/root/.ssh/authorized_keys` |
| 30 | PostgreSQL: superuser login roles `root` and `admin`; `pg_hba` carries `hostssl all all 0.0.0.0/0` (inert while bound to localhost) | **open — confirm `admin` is yours; remove the hostssl line** | `SELECT rolname FROM pg_roles WHERE rolsuper` |
| 31 | Hosting-managed services exposed on the same VPS: proftpd :21, rpcbind :111, munin :4949, apache :8080/:8443, radicale :5232, mail stack | **open — hosting decision** | iptables is panel-managed (vpsc chains); not touched |
| 32 | `portfolio.argia.com.mx` has no DNS record; vhost + certificate exist | **open — drop the vhost or create the record** | `getent hosts` fails |

## Standard
| # | Item | Status | Proof |
|---|---|---|---|
| 33 | AGS-701 states the operating monitor (thresholds, cadence, reconciliation, gaps) | **rewritten (en/es/cz)**, loaded into Ask ARGIA | `docs/AGS_701_VS_MONITORING_2026-09.md`; `knowledge` rows 281–287 |
| 34 | Corrected page published on sprinkler.agency | **open — upload `AGS_rev1_2026-09-07.html`** | until then the Sunday ingest reverts Ask ARGIA to Rev 0 |

## Known tool gaps (not blocking)
Per-plant module temperature coefficient; stored commissioning baseline; MTTR from maintenance events; degradation KPI (needs 2 years); annual availability roll-up; old web root deletion after a clean month; vendor-counter tile on the portal.
