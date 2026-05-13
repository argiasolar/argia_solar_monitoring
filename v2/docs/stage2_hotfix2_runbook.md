# Stage 2 Hotfix 2 — Re-capture corrupt MAXHistory fixtures

## Root cause

The original Stage 0 capture script (`v2/scripts/growatt_capture_fixtures.py`)
has a bug in `safe_parse`: it only attempts `resp.json()` when the response's
`Content-Type` header contains "json". Growatt returns valid JSON with
`Content-Type: text/html` for many endpoints (their bug, not ours), so the
JSON branch is skipped, the body is stored as `_raw_text`, and **anything
over 100 KB gets silently truncated mid-stream**.

The 4 `GTO1_getMAXHistory_*.json` fixtures captured at `bf988ba` are ~340 KB
each (150 rows × 155 fields). They got chopped at byte 100,000. CI failed
with:

```
JSONDecodeError: Expecting ':' delimiter: line 2 column 1 (char 100001)
```

The "char 100001" is the truncation marker injected by the buggy `safe_parse`.

The 4 other smaller fixtures (`getMAXTotalData`, `getPlantData`, `getDayChart`,
`getDevicesByPlant`, `alertPlantEvent`, `getWeatherByPlantId`, `listDevice`)
are all well under 100 KB and are unaffected.

## The fix

A standalone one-shot script `v2/scripts/recapture_maxhistory.py` that:

- Uses a FIXED `safe_parse` (always tries `resp.json()` first, regardless of
  `Content-Type`; 5 MB fallback cap)
- Hits `/device/getMAXHistory` for the 4 known TAIGENE SNs
- Overwrites the corrupt fixtures in place using `--date 2026-05-11`
- Post-validates each fixture (refuses to leave a truncated file on disk)

The main capture script is **not** touched in this hotfix. The recapture
script's docstring documents the one-line patch you should eventually port
into the main script so future captures don't silently truncate again.

## Apply

```bash
# 1. Drop the script into the repo
unzip -o ~/Downloads/argia_mont_v2_stage2_hotfix2.zip

# 2. Run it (Growatt creds same as Stage 0 capture)
cd v2
export GROWATT_USERNAME=<your-user>
export GROWATT_PASSWORD=<your-pass>
python scripts/recapture_maxhistory.py

# 3. Verify locally before pushing
PYTHONPATH=. pytest tests/unit/test_growatt_web_parser.py tests/unit/test_growatt.py -v
# expect: ~140 passed (was 405 passed / 13 failed / 16 errors)

# 4. Commit and push
git add v2/scripts/recapture_maxhistory.py v2/tests/fixtures/growatt_web/
git commit -m "Re-capture MAXHistory fixtures (fix Stage 0 truncation bug)"
git push
```

Expected CI result on push: **green, ~434 tests passing** (the full collected
suite, with the 13 failures and 16 errors gone).

## If the recapture itself fails

The script exits with:
- `2` — login failed. Check `GROWATT_USERNAME`/`GROWATT_PASSWORD` env vars.
- `3` — config error (creds not set).
- `1` — partial: some SNs succeeded, others failed. Logs show which.

Most likely cause if it fails: Growatt no longer has data for date
`2026-05-11` (they keep ~30 days of history; today is `2026-05-13`, so this
should still work for a few weeks). If that ever happens, pass a more recent
date:

```bash
python scripts/recapture_maxhistory.py --date 2026-05-12
```

…then `git mv` the new fixtures to the expected `_2026-05-11.json` names, OR
update the date references in `test_growatt_web_parser.py` and
`test_growatt.py`. Easier path: keep the date stable.

## Eventually port the fix to the main capture script

When you have a quiet moment, edit `v2/scripts/growatt_capture_fixtures.py`:

```python
# OLD safe_parse:
def safe_parse(resp: requests.Response) -> Any:
    """Try JSON; fall back to text snippet."""
    ct = resp.headers.get("Content-Type", "")
    if "json" in ct.lower():
        try:
            return resp.json()
        except ValueError:
            pass
    text = resp.text
    if len(text) > 100_000:
        text = text[:100_000] + f"\n...[truncated, total {len(text)} chars]"
    return {"_raw_text": text}

# NEW safe_parse:
def safe_parse(resp: requests.Response) -> Any:
    """Try JSON first regardless of Content-Type (Growatt lies)."""
    try:
        return resp.json()
    except ValueError:
        pass
    text = resp.text
    if len(text) > 5_000_000:
        text = text[:5_000_000] + f"\n...[truncated, total {len(text)} chars]"
    return {"_raw_text": text}
```

Not urgent. The recapture script handles the immediate problem; the main
script only matters when you re-run a full capture (rare).
