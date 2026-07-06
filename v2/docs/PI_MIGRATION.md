# GitHub Actions -> Raspberry Pi migration (v2 schedulers)

Why: the 2026-07-05/06 GitHub scheduler outages (multi-hour cron delays)
are the platform risk this removes. Prerequisite met: the dead-man's
switch is EXTERNAL — watchdog (GitHub) + notifier nag (Google infra) both
live-fire-proven this week — so a dead Pi is detected within 90 min /
3 h respectively.

Deploy model: the Pi PULLS. Laptop workflow unchanged (edit -> pytest ->
push). A 10-min cron on the Pi fast-forwards to origin/main and installs
requirements when they change. Nothing pushes INTO the Pi; no inbound
ports. Your pytest-before-push discipline is the deploy gate.

## Phase 0 — gates (do not skip)
1. [x] `crontab -l` recorded 2026-07-06 — the THREE v1 lines to protect
       during appends and to comment out at cutover:
         */10 6-18 * * * /bin/bash /home/zemel/run_sync.sh
         0 19 * * *      /bin/bash /home/zemel/run_sync.sh
         0 20 * * *      /bin/bash /home/zemel/run_night.sh
       Everything below APPENDS via `crontab -e`. NEVER `crontab <file>`
       (it replaces the whole table).
2. [ ] Pi health honesty check: if "the server problems" were the Pi
       itself (SD wear, power), fix that FIRST — moving onto a flaky box
       trades GitHub's flakiness for worse.
3. [ ] `timedatectl` — timezone America/Mexico_City, NTP active.
4. [x] Python 3.13.5 on Debian 13 (trixie) — measured 2026-07-06.
       Newer than CI's 3.11: the on-Pi pytest run below is the arbiter.
       Trixie may need Chromium libs for Playwright; if
       `playwright install chromium` complains, run:
       `sudo ./.venv/bin/playwright install-deps chromium`

Phase 0 measured 2026-07-06: Pi 4B 4GB, 113d uptime, throttled=0x0,
37 degC, 20G free, NTP+MX tz OK, GitHub 73ms. Verdict: GREEN — the
"server problems" were GitHub's scheduler, not this box.

## Phase 1 discovery (2026-07-06) — READ FIRST
`~/argia_solar_monitoring` on the Pi is v1's LIVE HOME: a February clone
with local, unpushed edits to argia.py and argia_sync.py (backed up to
`~/v1_local_backup/`). No git command ever runs there again. v2 lives in
its own fresh clone at `~/argia_v2`; deploy.sh refuses to reset any dirty
tree as a structural guard. `~/.argia_env` also already exists (v1
sources it): APPEND v2 variables, never overwrite.
CUTOVER TODO: diff the two rescued files against their last commit and
preserve the changes (commit to a v1-final branch) BEFORE retiring v1.

## Phase 1 — prepare (changes nothing in production)
    # read-only deploy key first (repo is private):
    #   ssh-keygen -t ed25519 -f ~/.ssh/argia_deploy -N ""
    #   add ~/.ssh/argia_deploy.pub as a GitHub Deploy Key (read-only)
    cd ~ && git clone git@github.com:argiasolar/argia_solar_monitoring.git argia_v2
    cd argia_v2/v2
    python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
    # reports print PDFs with headless Chromium:
    ./.venv/bin/pip install playwright && ./.venv/bin/playwright install chromium
    # secrets (values = the GitHub Actions secrets, 1:1):
    mkdir -p ~/secrets && nano ~/secrets/argia-service-account.json  # paste JSON
    nano ~/.argia_env   # APPEND missing v2 vars from pi/env.example; chmod 600 if not already
    # the environment earns trust by running the whole suite ON the Pi:
    PYTHONPATH=. ./.venv/bin/python -m pytest tests -q     # expect all green
    # then one real dry-run per job, eyeballed:
    ~/argia_solar_monitoring/v2/pi/run_job.sh smoke telemetry_5m.py --dry-run
    tail -20 ~/argia_logs/smoke.log

Enable the deploy cron now (safe — it only follows main):
    crontab -e   ->  add the */10 deploy.sh line from pi/crontab.example

## Phase 2 — cut over ONE JOB AT A TIME (never dual-write)
Two writers on one tab = duplicate rows + vendor session fights. For each
job, in the SAME hour: (a) uncomment its line in `crontab -e` on the Pi,
(b) delete the `schedule:` block from its workflow yml, commit, push
(keep `workflow_dispatch:` — that is the manual fallback).

Order and soak time:
  1. telemetry        -> watch 24 h (dashboard fresh, watchdog ALL OK)
  2. dashboard(+pub)  -> watch 24 h
  3. alerts-snapshot  -> watch 24 h (an alert e-mail proves the chain)
  4. kpi + alerts-daily + both reports -> watch one full morning

ROLLBACK (any step, ~2 min): comment the Pi cron line; restore the
workflow schedule block (git revert of one commit). The two runtimes are
interchangeable by design — same scripts, same env names.

## Phase 3 — steady state
- Actions keeps: v2-watchdog (both crons) + all workflow_dispatch entries
  + v2-irr-compare. The watchdog's telemetry check now guards the Pi.
- Logs: ~/argia_logs/<job>.log, size-capped by the 03:17 cron line.
- Whole-Pi sanity any time:  tail -n3 ~/argia_logs/*.log

## Known trade accepted
One machine instead of GitHub's fleet: hardware is now the risk. Detection
exists (watchdog + nag); prevention is on you — decent SD card or SSD
boot, and a UPS if the site's power is moody. The later SQLite/dense-data
phase lands on this same setup with zero rework.
