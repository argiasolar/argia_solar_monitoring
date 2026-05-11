# Argia_Mont v2 — Migration Guide

v2 lives **alongside v1 in the same repo**, in a `v2/` subfolder. v1 is
unchanged and keeps running. v2 workflows only trigger on `v2/**` changes,
so editing v1 files never accidentally runs v2.

This guide takes you from "I have an existing v1 repo" to "I trust v2 to
run on the Pi" in about an hour.

## Prerequisites

- [ ] You have the `Argia_Mont_v2` Google Sheet open (the one with 9 plants
      and 21 inverters in the correct schema — Plants tab has 18 columns).
- [ ] You have the v1 service account JSON somewhere on disk (or you can
      retrieve it from your Pi).
- [ ] You have admin access to the existing `argia_solar_monitoring` repo.

---

## Step 1 — Drop v2 files into the repo

Download `argia_mont_v2.zip`. Open a terminal at the **root** of your
`argia_solar_monitoring` repo (the folder that contains `argia.py`,
`argia_growatt.py`, etc.), then:

```bash
# On your laptop, in the repo root:
unzip ~/Downloads/argia_mont_v2.zip
```

The zip is structured so that:
- `v2/...` lands as a new `v2/` folder in the repo
- `.github/workflows/v2-*.yml` lands inside your existing `.github/workflows/`

Your existing v1 files at the root are untouched.

Verify the structure:
```bash
ls -la v2/              # argia/  scripts/  tests/  pi/  README.md  MIGRATION.md  ...
ls .github/workflows/   # your v1 yml files PLUS 4 new v2-*.yml files
```

Commit and push:
```bash
git add v2/ .github/workflows/v2-*.yml
git commit -m "Add Argia_Mont v2 (alongside v1)"
git push
```

Open the repo in your browser → **Actions** tab. You should see a new
workflow run for `v2-tests` start. **Expected: green checkmark, 316 tests
passed across Python 3.10/3.11/3.12.**

If it's red, stop and tell me what the failure says.

---

## Step 2 — Check existing GitHub Secrets

Go to your repo → **Settings** → **Secrets and variables** → **Actions**.
You can see **names** of existing secrets but not their values.

v2 workflows expect these secret names. Cross-reference against what's
already there:

| Secret name | Required? | Likely already there from v1? |
|---|---|---|
| `GOOGLE_CREDENTIALS` | yes | probably yes |
| `GROWATT_API_TOKEN` | maybe | check |
| `GROWATT_USERNAME` | yes (web UI for irradiance) | probably yes |
| `GROWATT_PASSWORD` | yes (web UI for irradiance) | probably yes |
| `HUAWEI_USERNAME` | yes | probably yes |
| `HUAWEI_PASSWORD` | yes | probably yes |
| `SOLAREDGE_API_KEY` | only if QRE active | likely no |
| `GOOGLE_SHEET_ID_V2` | **yes — NEW** | **no — you must add this** |

For any secret v1 already uses, **v2 reuses it automatically**. You do NOT
need to know the value or re-enter it.

If v1 uses different secret NAMES (e.g. `GS_CREDS` instead of
`GOOGLE_CREDENTIALS`), tell me and I'll adjust the workflow files.

---

## Step 3 — Add the one NEW secret

Settings → Secrets → **New repository secret**:
- Name: `GOOGLE_SHEET_ID_V2`
- Value: the sheet ID from your Argia_Mont_v2 URL
  (`docs.google.com/spreadsheets/d/<THIS_PART>/edit`)

Click **Add secret**.

---

## Step 4 — Share the sheet with the service account

Find the service account email — it's the `client_email` inside the JSON
stored in `GOOGLE_CREDENTIALS`. You can't read the secret value, but you
can find the email in Google Cloud Console (IAM & Admin → Service Accounts)
or in the original JSON file if you kept it.

In the Argia_Mont_v2 sheet → **Share** → paste the email → Editor →
uncheck "Notify people" → Share.

---

## Step 5 — Run preflight (read-only)

Actions tab → **v2-preflight (manual, read-only)** in the left sidebar →
**Run workflow** → select `main` → **Run workflow**.

This connects to the sheet and every vendor API but **writes nothing**.

**Expected:** every line green OK.

Common failures:

| Failure | Likely cause | Fix |
|---|---|---|
| `GOOGLE_CREDENTIALS is empty` | secret not named that way | rename the secret or adjust the YAML |
| `Could not read Plants tab` | sheet not shared with service account | redo step 4 |
| `MEX1 HUAWEI: env var 'HUAWEI_PASSWORD' is unset` | secret not set | add in Settings → Secrets |
| `SLP1 GROWATT: login failed: 401` | wrong API token | refresh in Growatt portal |
| `QRE SOLAREDGE: 403 Forbidden` | invalid key for site_id | deactivate QRE OR fix key |

Fix and re-run until all green.

---

## Step 6 — Daily dry-run

Actions → **v2-daily-run (manual)** → Run workflow → leave defaults
(dry_run=true, date empty=yesterday).

This will:
1. Read portfolio from sheet
2. Fetch yesterday's kWh per plant
3. Fetch cloud cover + irradiance
4. Compute PR%
5. **Print** what would be written (but NOT write)

Log lines to look for:
```
[SLP1] real_kwh=189.5
[GTO1] real_kwh=4421.0
[DRY RUN] would write 6 rows to DailyProduction
[DRY RUN]   ['2026-05-10', 'SLP1', 'GROWATT', 189.5, ...]
```

**Sanity check:**
- Row count = active plant count
- Each `real_kwh` in the right ballpark (compare to v1 sheet)
- No plant errored

If something looks off, re-run with **plant_key=SLP1** to isolate.

---

## Step 7 — Daily LIVE run

Same workflow, but **uncheck dry_run**. First time data touches the sheet.

Watch DailyProduction fill in. Verify:
- One row per active plant
- Values match dry-run
- SyncRuns shows `status=OK`

**Run it again immediately.** Idempotent upsert means **row count must NOT
grow** — existing rows update in place.

---

## Step 8 — 10-min snapshot dry-run + live

Same drill: dry-run, then live. Writes to InverterSnapshot10m. Expect ~17
rows per run (one per active inverter).

Verify: all inverter SNs appear, `status` is 1 or 3, `power_w` is sensible.

---

## Step 9 — Decide whether to schedule

Before Pi deploy or scheduled Actions:

- [ ] Preflight passes
- [ ] Daily dry-run values match v1 within tolerance
- [ ] Live daily wrote expected rows
- [ ] Re-run daily did NOT duplicate rows
- [ ] 10-min produced one row per inverter
- [ ] 2-3 manual runs clean

Yes to all → safe to deploy. Pi cron entries come in a separate doc once
runs are clean.

---

## Troubleshooting

- **Always start with preflight.** Catches 90% of issues.
- **Check SyncRuns tab.** Every run logs status and errors.
- **Use plant_key=X + dry_run=true** to isolate one plant safely.
- **Edit YAML to add `--log-level DEBUG`** for verbose logs.

---

## What v2 does NOT do (yet)

- **Anomaly alerts** (e.g. VITALMEX -51% drop) — v2 writes the row but
  doesn't alert. Stage 7.
- **Email/PDF reports** like v1.
- **Pi crontab examples.**
- **Scheduled GitHub Actions.** Manual only for now. Easy flip later.
