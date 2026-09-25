# Test coverage gaps - what is not yet protected

v263, 2026-09-25. Measured with `pytest --cov=argia --cov=scripts --cov=server --cov-branch`
on the full suite (4,278 tests). How the harness works: `docs/TEST_HARNESS.md`.

## Where we stand

| | Before v263 | After v263 |
|---|---|---|
| Total coverage (lines + branches) | 68.3% | **77.4%** |
| Library code `argia/` | 91.8% | 92.8% |
| Job scripts `scripts/` | 30.6% | 46.4% |
| Portal and apps `server/` | 58.1%, and the portal generators (3,100 lines) never ran at all | **83.7%**, and the whole portal is generated in every test run |
| Portal pages checked end to end | 0 | 73 page types (150 files), every internal link |
| Scheduled jobs (31 timers on pio06) run for real in tests | none | 16 job scripts run end to end, the portal generator has its own test, 14 scripts are listed with the reason they cannot run in a test |
| Web URLs exercised (the five portal apps) | 43 of 81 | 74 of 81 |
| Finance pipeline on a real database | skipped everywhere | runs on CI |

Found and fixed while building this:

- **Ask ARGIA slide links were broken.** In the page template, `\b` was a Python backspace character instead of a JavaScript word boundary, so "slide 12" in an answer never became a link. Fixed, and a regression test fails on the old code.
- The icon table had `print` defined twice; the first definition was silently discarded.
- Invalid `\` escapes in two page templates were warnings today and would become errors in a future Python.
- `tests/unit/test_fin_pipeline_pg.py` (6 tests) had never run on CI or on the laptop. It now runs.
- Dead code in `savio_recon.py` and `monitoring_gen.py`.

## Gaps, most important first

### P1 - production jobs with no end-to-end test

| Feature | Code | Coverage | Why it matters | How to close it |
|---|---|---|---|---|
| Telemetry collection, every 5 min, all vendors | `scripts/telemetry_5m.py` | 27% | Everything starts here. Vendor *parsers* are tested against captured fixtures; the job around them (per-brand loop, retries, gaps, database write) is not. | Fake vendor clients built on the existing fixtures, then run `main()` against the private database and check the `telemetry` rows. |
| Satellite irradiance check | `scripts/satellite_check.py` | 0% | **It has been failing on pio06 since 16 Sep**, which is why the Mexico City plants cannot be priced in the outage-cost figure. | Mock the HTTP call with `responses`, run against the database, reproduce the production failure first. |
| Weekly and monthly financial mail | `scripts/financial_mail.py` | 31% | Goes to management. The page path is hardcoded (`WEBROOT`), so the test cannot point it at the generated site. | Make `WEBROOT` an environment override, point it at the site `test_portal_site` generates, render the PDF when Chromium is present. |
| CFE tariff ingest (Pi CSV to database) | `server/bundle/cfe_ingest.py`, `cfe_load.py` | 42%, never run | Feeds the Engine push. The loader path is hardcoded to `/opt/argia/bundle`. | Make the loader path overridable, feed a sample CSV, check the `cfe_tariff` rows and the anomaly gate. |
| String-level daily check | `scripts/string_daily.py` | 34% | Finds dead strings. | Fake Growatt client from fixtures. |
| Telemetry archive to Drive, monthly archive | `scripts/telemetry_archive.py`, `archive_month_pg.py` | 39%, 80% | The retention step: data is deleted after it is archived. | Fake Drive client, then assert the archive file contains exactly the rows that get deleted. |

### P2 - other untested features

| Feature | Code | Coverage | Note |
|---|---|---|---|
| Drive ingest of the accounting books | `scripts/fin_drive_ingest.py` | 23% measured | The pipeline test runs it as a separate process, so coverage does not count it. It *is* tested; switch that test to in-process to measure it. |
| Client report upload (FTP) | `scripts/client_reports_publish.py` | 28% | Needs a fake FTP server. |
| Tickets by email (IMAP) | `scripts/ticket_mail_in.py` | 39% | Needs a fake IMAP server. |
| Alert mailer (infra alerts) | `scripts/alert_mailer.py` | 55% | Runs, but only the dry-run branch. |
| Daily PDF report job | `scripts/report_daily.py` | 58% | The HTML is tested; the PDF and upload are not. |
| Backfills and fixes | `recon_backfill.py` 12%, `inverter_counter_fix.py` 39%, `inverter_registry.py` 34%, `irradiance_proxy_backfill.py` 51% | | Manual repair tools; they write to production when used. Worth a dry-run test each. |
| Setup: 4 URLs left | `setup_app.py` `/finance/fx`, `/finance/extend`, `/settings`, `/account/change` | 66% file | Loan FX and extension, plant settings, own password change. |
| Language switch, finance demo pages | `auth_app` `/session/lang`, `fin_app` `/finance/demo/*`, `/projects/demo/*` | | Small. |
| Office Pi code | `pi/cfe/cfe_scrape.py` (496 lines), `pi/report_watch/ppa_watch.py` | never run | The CFE scraper feeds all tariffs. Test it against a saved CFE page. |

### P3 - decide: keep and test, or delete

These run by hand, not on a schedule, and have no tests. They are safe to leave only if nobody relies on them:

`scripts/growatt_capture_fixtures.py`, `recapture_maxhistory.py`, `solaredge_capture.py`, `solaredge_discover_inverters.py`, `sma_capture.py`, `sma_discover_plants.py`, `irr_compare.py`, `fin_cost_centers.py`, `report_finance.py`, `report_client_daily.py`, `ags_ingest.py` (on a Sunday timer, downloads from the web), `argia/ask/__main__.py`, and `server/bundle/load_data.py` / `load_telemetry.py` (legacy loaders, not used by any job).

## Design findings (not coverage, but they make changes risky)

1. **The fleet is hardcoded in three places**: `report_gen.PPA/CAPEX`, `setup_app.PLANTS` and `portal_chrome.SLUGS`. A plant missing from the database also crashes the portal generator. A test now makes the three lists agree; the real fix is one list read from the `plant` table.
2. **`kpi_eod.py` has no `--dry-run`**, so it writes every time it runs, including by hand.
3. **Hardcoded paths** (`financial_mail.WEBROOT`, `cfe_ingest.LOAD`, `setup_app.REPORT_GEN`) block end-to-end tests. Each should accept an environment override, as `ARGIA_WEBROOT` already does in `setup_app`.
4. **About 47 test files check the source text** rather than behaviour (194 `... in src` assertions). They pass as long as the text is there, even when the feature is broken. Now that the portal and jobs run for real, the important ones should move to checks on the rendered page or the job output.
5. **Style backlog** (low): 89 unused imports, 19 `zip()` calls without `strict=`, 6 `global` statements.
