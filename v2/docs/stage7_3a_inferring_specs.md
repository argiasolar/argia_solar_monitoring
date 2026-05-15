# Stage 7.3a — Inferring plant specs from telemetry

**Status**: ONBOARDING-ONLY. Replace inferred values with real installer
data before this system is customer-facing.

## The problem this solves

Stage 7.3 surfaced 32 warnings — mostly `rated_kw=0` on inverters and
missing `kwp_ac`/`kwp_dc` on a few plants. Without these, peer ranking
returns `--` and PR confidence stays LOW. Contacting installers takes
days; this script gets you placeholder values in 5 minutes so the rest
of the pipeline can start producing meaningful output.

## What the script DOES infer

| Field | Source | Confidence |
|---|---|---|
| `Inverters.rated_kw` | Observed peak `power_w` over N days → snap UP to standard size (3/5/6/10/15/25/50/100/etc. kW AC) | **Medium-high** |
| `Plants.kwp_ac` | Sum of inferred + existing `rated_kw` across plant's inverters | **Medium-high** (derived) |
| `Plants.kwp_dc` | `kwp_ac × DC_AC_RATIO` (default 1.20) | **Medium** (assumption) |

## What the script DOES NOT touch

| Field | Why |
|---|---|
| `module_count`, `module_wp` | Cannot be inferred from electrical telemetry. Faking would poison soiling math. |
| `tilt_deg`, `azimuth_deg` | Cannot be inferred. KPI math has defaults when missing. |
| `system_losses_pct` | Plant-specific. KPI math degrades gracefully. |
| Any non-zero existing value | Never overwritten, period. |

## Safety guards (built in)

1. **Dry-run by default.** Nothing writes without `--apply`.
2. **Never overwrites a non-zero existing value.** Re-running as you
   backfill real data is safe.
3. **Refuses inference with <2 days of data.** A single cloudy day
   doesn't tell us inverter size.
4. **Refuses inference when peak < 1 kW.** Means inverter was offline
   or only saw nighttime data.
5. **Warns when peak < 60% of inferred size.** Likely under-rating;
   real nameplate may be larger.

## How to run

```bash
# Preview (dry-run) — recommended first
PYTHONPATH=. python scripts/infer_plant_specs.py

# Limit to one plant for debugging
PYTHONPATH=. python scripts/infer_plant_specs.py --plant-key QRO1

# Use 14 days of telemetry instead of default 7
PYTHONPATH=. python scripts/infer_plant_specs.py --days 14

# Different DC/AC overbuild for a plant you know is higher
PYTHONPATH=. python scripts/infer_plant_specs.py --dc-ac-ratio 1.30

# When the preview looks right, actually write to sheet
PYTHONPATH=. python scripts/infer_plant_specs.py --apply
```

## Output anatomy

```
=== Inverter inferences (last 7 days) ===

Plant      SN                     Days Peak kW  Existing  Inferred  Action  Note
QRO1       7E0571B7-AB              5    9.82      --        10     UPDATE  observed peak 9.82 kW over 5 days → snapped UP to 10 kW
QRO1       7E05721B-10              5    9.74      --        10     UPDATE  observed peak 9.74 kW over 5 days → snapped UP to 10 kW
GTO1       JFM7DXN00T               5   95.30      --       100     UPDATE  observed peak 95.30 kW over 5 days → snapped UP to 100 kW
SLP1       JNMDEXH011               1    3.21      --         --    skip    only 1 day(s) of data; need 2+ ...
```

The `Note` column tells you exactly why each row was chosen or skipped.

## The formulas (for future manual use)

If you later want to do this by hand (e.g. in a spreadsheet helper column):

### Inverter rated_kw

```
1. observed_peak_kw = max(power_w over N days) / 1000
2. If observed_peak_kw < 1.0: SKIP (insufficient signal)
3. If only 1 day of data: SKIP (single-day max could be cloudy)
4. inferred_rated_kw = ceiling(observed_peak_kw to next standard size)

Standard sizes (kW AC):
  3, 4, 5, 6, 7, 8, 10, 12,
  15, 17, 20, 25, 30, 33,
  40, 50, 60, 75, 80, 100, 110,
  125, 150, 175, 200, 250
```

In Google Sheets, given a column of observed peaks in B2:B40:

```
=IFS(
  B2 < 1.0, "SKIP",
  B2 <= 3, 3,
  B2 <= 4, 4,
  B2 <= 5, 5,
  B2 <= 6, 6,
  B2 <= 7, 7,
  B2 <= 8, 8,
  B2 <= 10, 10,
  B2 <= 12, 12,
  B2 <= 15, 15,
  B2 <= 17, 17,
  B2 <= 20, 20,
  B2 <= 25, 25,
  B2 <= 30, 30,
  B2 <= 33, 33,
  B2 <= 40, 40,
  B2 <= 50, 50,
  B2 <= 60, 60,
  B2 <= 75, 75,
  B2 <= 80, 80,
  B2 <= 100, 100,
  B2 <= 110, 110,
  B2 <= 125, 125,
  B2 <= 150, 150,
  B2 <= 175, 175,
  B2 <= 200, 200,
  TRUE, 250
)
```

### Plant kwp_ac

```
kwp_ac = sum of rated_kw across all inverters in the plant
```

In Sheets: `=SUMIF(Inverters!A:A, "QRO1", Inverters!D:D)`

### Plant kwp_dc

```
kwp_dc = kwp_ac × DC_AC_RATIO
```

Where `DC_AC_RATIO` is:
- **1.10** for older Mexican plants (conservative)
- **1.20** typical commercial (DEFAULT)
- **1.25–1.30** modern utility-scale or hot-climate plants
- Look at the panel × module count math when you finally have it:
  `kwp_dc = (module_count × module_wp) / 1000`

When the real installer data shows up, that derived formula becomes
the ground truth. Replace the placeholder.

## Replacing placeholders later

When you get real installer specs:

1. Update the Inverters and Plants tabs by hand with the real values.
2. Re-run `python scripts/infer_plant_specs.py` (dry-run). Confirm the
   script reports "skip — not overwriting" for the rows you fixed.
3. The script will fill in only the still-missing rows.

There's no need to clear out the placeholder values first. The script's
"existing > 0 → skip" rule handles the transition cleanly.

## Tests

`tests/unit/test_infer_specs.py` — 23 tests covering:
- Snap function across the full size range
- Inference safety (single-day skip, low-peak skip, no-data skip)
- Idempotency (existing values preserved)
- Plant aggregation (sum, DC/AC ratio, incomplete handling)

Run:
```bash
PYTHONPATH=. python -m pytest tests/unit/test_infer_specs.py -v
```

## After running this — fix the upstream irradiance issue

Even with all `rated_kw` filled, the demo will still show LOW
confidence PR for 7 plants because the upstream irradiance pipeline is
only capturing 2-6 samples per plant per day. That's a Stage 3/4 issue
to investigate separately — not blocked on this script.
