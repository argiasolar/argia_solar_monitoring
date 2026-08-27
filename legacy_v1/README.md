# legacy_v1 — the retired v1 system (frozen 2026-08-26)

The original Sheets-based monitoring scripts that ran on the Raspberry
Pi and GitHub Actions from 2024 until the pio06 migration. Moved here
from the repo root during the 2026-08-27 due-diligence cleanup
(roadmap P1-10). Nothing imports or schedules them anymore:

- Pi crons: all commented out 2026-08-26 (#MIGRATED-TO-PIO06#),
  backup in the Pi's crontab.backup.decommission.*
- GitHub workflows for v1: moved to legacy_v1/workflows/ (their crons
  were already disabled; moving them out of .github/ prevents even
  manual dispatch against relocated paths)

Everything these scripts did lives in v2/ now — telemetry, KPI,
reconciliation, reports, alerting — running under systemd on pio06.
Keep for reference; delete whenever comfortable.
