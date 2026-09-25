# Test harness - how we know a change did not lose a feature

v263, 2026-09-25. Run everything with:

```bash
cd v2 && PYTHONPATH=. pytest
```

## The layers

| Layer | Where | What it proves | Runs on |
|---|---|---|---|
| Unit tests | `tests/unit/`, `tests/*.py` | Pure logic: KPIs, alerts, money, parsers against captured vendor fixtures, finance rules | laptop, CI |
| Static checks | `tests/unit/test_static_checks.py` | Every source file compiles; no pyflakes defect (undefined name, duplicate dict key, ...); no invalid `\` escape or hidden control character in a string | laptop, CI |
| No em dash | `tests/unit/test_no_em_dash.py` | No em dash in any file of the tree | laptop, CI |
| Inventory contracts | `tests/contracts/` | Every URL the apps answer (`routes.txt`), every server job and its schedule (`jobs.txt`), every Pi cron job (`pi_jobs.txt`), the fleet list agrees in the three places it is hardcoded, and every server job is run by a test or says why not | laptop, CI |
| Portal end to end | `tests/portal/test_portal_site.py` | `portal_gen.py` runs for real; every page in `expected_pages.txt` is generated, complete, bilingual, free of None/nan/em dash; every internal link resolves; the seeded plant states show up where they belong | CI, sandbox (needs PostgreSQL) |
| Jobs end to end | `tests/portal/test_jobs_smoke.py` | 16 scheduled jobs run their `main()` in safe mode against the database and print the line that proves they worked; dry runs write nothing | CI, sandbox |
| Setup app | `tests/portal/test_setup_app_routes.py` | 31 of 35 Setup URLs: people, passwords, mail subscriptions, maintenance windows, finance edits - each checked in the database afterwards | CI, sandbox |
| Finance pipeline | `tests/portal/test_fin_pipeline_pg.py` | schema, seed, ingest twice, decisions, the finance app's own queries | CI, sandbox |
| Coverage floor | `.github/workflows/v2-tests.yml` | total line+branch coverage may not drop below the floor (76% on 2026-09-25, measured 77.4%) | CI (Python 3.12 leg) |
| Production drift | `scripts/drift_check.py` (06:30 MX daily) | what runs on pio06 equals git, and the test schema copy equals the production schema | pio06 |

## The private database (tests/portal/conftest.py)

The generators and jobs read PostgreSQL through
`runuser -u postgres -- psql -d argia_mont`. The fixture:

1. starts a throwaway PostgreSQL cluster in a temp dir (unix socket, no TCP port);
2. loads `tests/fixtures/pg/schema.sql` - the production DDL (no rows);
3. loads `tests/fixtures/pg/seed.sql` - a synthetic 11-plant fleet; dates are
   relative to today in Mexico, so the pages always see a live fleet;
4. puts a test-double `runuser` first on PATH, so the code runs exactly the
   command it runs on pio06 - no production code knows it is being tested.

On the Windows laptop PostgreSQL is not installed, so these tests skip. CI
sets `ARGIA_REQUIRE_PG=1`, which turns that skip into a failure.

## When you change something on purpose

| You... | Also do |
|---|---|
| add or remove a portal page | edit `tests/portal/expected_pages.txt` |
| add or remove a URL, a job, a Pi cron line | `ARGIA_UPDATE_CONTRACTS=1 PYTHONPATH=. pytest tests/contracts`, then read the git diff of `tests/contracts/*.txt` - that diff is the review |
| add a scheduled job | add it to `JOBS` in `tests/portal/test_jobs_smoke.py`, or to `NOT_RUNNABLE_HERE` with the reason |
| add a plant | add it to `report_gen.PPA/CAPEX`, `setup_app.PLANTS`, `portal_chrome.SLUGS` and `seed.sql` |
| change a table on pio06 | refresh the schema copy (below); the morning drift mail says when it is stale |
| close a gap from `TEST_COVERAGE_GAPS.md` | raise `--cov-fail-under` in the workflow to just under the new total |

## Refreshing the schema copy

On pio06 (read-only, no rows):

```bash
runuser -u postgres -- pg_dump --schema-only --no-owner --no-privileges --no-comments argia_mont \
  | grep -v '^SET \|^SELECT pg_catalog.set_config\|^--\|^\\restrict\|^\\unrestrict' | cat -s \
  > /tmp/schema.sql
```

Put the header comment of the old file on top, replace
`v2/tests/fixtures/pg/schema.sql`, run the tests, commit.
