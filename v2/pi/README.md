# v2/pi — historic name, SERVER ops kit

This directory kept its name from the Raspberry Pi era, but since the
2026-08-26 migration it is the **pio06 server ops kit**: `run_job.sh`
here is the wrapper referenced by ExecStart in all 19 argia-* systemd
units on the server (venv + secrets + flock + logging).

Renaming to `v2/ops/` is deliberately DEFERRED (roadmap P2): the rename
must land as one coordinated change — git mv, sed over
/etc/systemd/system/argia-*.service, daemon-reload, test_pi_kit.py
path updates, docs — in a quiet window, not during month-close week.

`crontab.example`, `deploy.sh`, `env.example` are the Pi-era files,
kept for the cold-standby rollback path documented in
V3_SERVER_MIGRATION_PLAN_2026-08-26.md.
