# Stage 2 Runbook — Wire Growatt web client into the facade

Status: complete. 140 unit tests passing (was 106 in Stage 1; +34 facade tests).

## What this stage does

Replaces the fragile parts of `argia/vendors/growatt.py`:

| Before (v1-era code in facade)                          | After (Stage 2)                                       |
|---------------------------------------------------------|-------------------------------------------------------|
| `_fetch_day_kwh_web` scraped HTML for `val_device_plantEToday` | `_fetch_day_kwh_web` calls `GrowattWebClient.get_max_total_data` → `parse_max_total_data` |
| `_fetch_inverters_web` tried 4 endpoint variants × 2 payload variants hoping one returned data | `_fetch_inverters_web` calls `GrowattWebClient.get_max_history` per SN → `extract_latest_row` → `build_inverter_snapshot` |
| Login + cookie handling + HTML/JSON sniffing duplicated inside facade | Web client owns its login (idempotent, three success paths) and JSON envelope handling |
| `_web_get` / `_web_post` with their own safety guards   | Web client has the safety guards; facade no longer needs them |

**Public API of `GrowattClient` is identical.** The orchestrator, factory, and
every other module that consumes `fetch_day_kwh` / `fetch_inverter_snapshots`
sees the same behaviour and the same exception types.

## What's in this patch

```
v2/argia/vendors/growatt.py                # rewritten facade
v2/argia/vendors/growatt_web.py            # Stage 1 web client
v2/argia/vendors/growatt_web_parser.py     # Stage 1 parser
v2/tests/conftest.py                       # unchanged from v2 (variadic load_fixture)
v2/tests/unit/test_growatt.py              # rewritten facade tests
v2/tests/unit/test_growatt_web_client.py   # Stage 1 client tests
v2/tests/unit/test_growatt_web_parser.py   # Stage 1 parser tests
v2/docs/stage2_runbook.md                  # this file
```

If you applied Stage 1 already, the three Stage 1 files just overwrite with
identical content — `git diff` will show no changes for them. Apply atomically
in one shot is fine.

## Apply the patch

From your `argia_solar_monitoring` repo root:

```bash
unzip -o ~/Downloads/argia_mont_v2_stage2.zip
```

## Delete the dead fixtures (they're no longer referenced by any test)

```bash
git rm v2/tests/fixtures/growatt/web_pv_page.html
git rm v2/tests/fixtures/growatt/web_device_list.json
```

These were inputs to `_parse_plant_etoday_html` and `_parse_web_inverter`,
which Stage 2 removes. Keeping them around would be misleading clutter.
If you want to double-check they're really unused before deleting:

```bash
grep -rn "web_pv_page\|web_device_list" v2/
# expected output: nothing
```

## Run the tests locally

```bash
cd v2
PYTHONPATH=. pytest -v
```

Expected: at least 140 tests collected for the Growatt-related files;
**all green**. Your full v2 suite (including Huawei, SolarEdge, orchestrator,
factory, etc.) should still pass — Stage 2 did not touch any of those.

If your repo's total was around 317 tests after the earlier patch, expect
roughly 351 now (+34 from the new facade tests, +106 from Stage 1, minus the
old facade tests that were removed). Numbers are approximate because some
of the deleted tests overlapped with the new ones.

## Commit and push

```bash
git add v2/argia/vendors/growatt.py
git add v2/argia/vendors/growatt_web.py
git add v2/argia/vendors/growatt_web_parser.py
git add v2/tests/unit/test_growatt.py
git add v2/tests/unit/test_growatt_web_client.py
git add v2/tests/unit/test_growatt_web_parser.py
git add v2/docs/stage2_runbook.md
git rm v2/tests/fixtures/growatt/web_pv_page.html
git rm v2/tests/fixtures/growatt/web_device_list.json
git commit -m "Stage 2: rewire Growatt web fallback to use Stage 1 JSON client + parser"
git push
```

This triggers `v2-tests` automatically. Expected: green check.

## Architecture and the honest reasoning

### Why per-inverter `getMAXHistory` instead of one device-list call

The old code tried `/device/getMAXList` (and 3 other variants). For the
TAIGENE plant we know Growatt's `getDevicesByPlant` is buggy — it returns
only one of the four inverters per call. Even when those endpoints worked,
they were returning sparse data.

`getMAXHistory` is the canonical per-inverter endpoint. We call it once per
SN. For TAIGENE that's 4 HTTP calls of ~150 5-min rows each, with a 200 ms
delay between (`PER_INVERTER_DELAY_SEC`). Total wall time ≈ 1.2 seconds.
Acceptable for a 10-minute snapshot cron and far more reliable.

If perf becomes an issue later, the optimisation is to pass `start=N` (a high
sample index) to skip ahead — `getMAXHistory` paginates from the start of
day. Not worth doing yet; simplicity wins.

### Why the web path now refuses non-today dates

`getMAXTotalData` has no date parameter — Growatt always returns plant
eToday in plant local time. The old HTML scraper had the same limitation
(it scraped today's value off the dashboard) but didn't say so out loud.
Stage 2 makes the contract explicit: ask for today, get today; ask for
yesterday, get `None`.

If you ever need historical day totals from the web path, the right
endpoint is `getMAXDayChart` — 288 five-minute slots that can be summed.
The parser already returns this as `List[float]`. Wire it in then.

### Why the Open API path was not touched

It works. The Open API is the preferred path on accounts where it's
licensed (it's faster, cleaner, and rate-limited rather than scraped).
Stage 2 is only about the fallback. Open API tests are identical to before.

### Errors: two namespaces, one orchestrator contract

`growatt_web.py` has its own `GrowattAuthError` and `GrowattAPIError` — they
fire when the web client fails. The facade catches them and either falls
back (during Open API → web transition) or returns `None` / `[]`.

The facade ALSO has `GrowattAuthError` and `GrowattAPIError` of the same
name — they fire from the Open API path. They are different classes (same
name, different module). The orchestrator-facing contract is unchanged:
`fetch_day_kwh` never raises, returns `Optional[float]`. The exception
plumbing is internal.

If this two-namespaces-same-name thing irritates you, the cleanup is one
search-and-replace at a calmer moment. It's a code-style preference, not a
correctness issue.

### Lazy web client init

`GrowattWebClient` is built lazily on first web call. Pure Open API
accounts never instantiate it. Web-only accounts build it once and reuse
across plants. The `login()` method inside the web client is idempotent —
a no-op after the first success — so the facade can call it on every fetch
without thinking about whether we're already logged in.

Test `test_web_client_cached_across_calls` pins this behaviour: two
sequential `fetch_day_kwh` calls produce exactly one `GrowattWebClient`
instantiation.

## Honest non-goals — what Stage 2 deliberately does NOT do

- **Live integration test in CI.** Still no real Growatt creds in CI. The
  140 unit tests are the contract; if Growatt changes their wire format,
  Stage 0's capture script (`v2/scripts/growatt_capture_fixtures.py`) is
  the regrounding tool.
- **Replace the Open API path.** That works. Don't touch it.
- **Plant-level alerts / weather / irradiance.** The web client has
  `get_alert_plant_event`, `get_weather_by_plant_id`, and the parser
  handles them. Wiring them into the orchestrator is Stage 3+ work.
- **Per-MPPT and per-string history surfaced to Sheets.** The parser
  exposes them via `per_mppt_voltages`, `per_string_voltages`, etc. but
  the snapshot dataclass only carries plant_key, sn, ts, status, power_w,
  etoday_kwh. If you want richer telemetry in Sheets, that's a sheets-side
  schema change first.

## How the contract breaks (regression strategy)

If Growatt changes wire format, the failure order is approximately:

1. **Stage 1 envelope/parser tests** trip first — `TestEnvelopeUnwrap`,
   `TestParseMaxHistory::test_raw_dict_preserves_all_fields`. Failure here
   means "Growatt changed the envelope or stripped fields".
2. **Stage 2 web-fallback tests** trip next — `TestWebDayKwh::test_returns_etoday_from_fixture`
   uses a real fixture, so if `parse_max_total_data` starts returning `None`,
   the facade test fails before the orchestrator ever runs.
3. **Stage 2 lazy-init test** trips if `GrowattWebClient.__init__` signature
   changes. That's a one-line fix in the facade.

If a Growatt API change requires re-capturing fixtures: re-run
`v2/scripts/growatt_capture_fixtures.py`, commit the new fixtures, run
`pytest`. Failing tests point at the field that moved.

## What's left for Stage 3 (if/when you go there)

Suggested order, smallest-risk first:

1. Wire `parse_alert_plant_event` into a new orchestrator pre-flight check —
   surfaces active Growatt alerts in HealthLog before they cascade into
   missing data.
2. Wire `parse_max_day_chart` for historical day totals on the web path
   (drop the "today-only" limitation).
3. Decide whether per-MPPT / per-string data goes into the
   `InverterSnapshot10m` tab or a new wide-format tab. Sheets-side design
   decision; not a code change first.
