# Stage 7.3c — Sheet headers + physical-spec placeholders

You asked: "where are we going to add number of panels, and details for
each inverter we discussed before?" Fair question. Two things were missing:

1. The **column headers themselves** never got added to your Plants and
   Inverters tabs. Stage 7.1 shipped the loader code expecting them, but
   said in its runbook "add the columns manually." That step likely got
   skipped.

2. A way to **fill placeholder values** for the physical specs (panel count,
   MPPT count, tilt, azimuth, losses). Stage 7.3a inferred *electrical*
   specs (rated_kw etc.) from telemetry. Physical specs can't be inferred
   from electrical data — they need assumptions.

This stage adds both.

## Step 1 — Add the column headers (one-time manual paste)

Open your Plants tab. Find the `active` column (column R / 18 in v7.0,
or column W if you also have other columns). After that, paste these
**10 cells** into row 1, one per column to the right:

```
module_count	module_wp	string_count	tilt_deg	azimuth_deg	system_losses_pct	commissioning_date	notes	pr_baseline	tariff_mxn_per_kwh
```

(tabs between cells — paste as a single row from clipboard)

For your Inverters tab, after the `active` column, paste these **3 cells**:

```
mppt_count	strings_per_mppt	rated_kw_dc
```

Order matters — the headers above match what the loader expects.

If you've already added some of these manually with slightly different
names ("Module Count" vs "module_count"), rename them to match exactly.
The loader is case-sensitive.

## Step 2 — Seed physical placeholders

After step 1, run:

```bash
PYTHONPATH=. python scripts/seed_plant_physicals.py
```

Dry-run output shows what would be written. Honest preview:

```
=== Plant physical inferences ===

Plant      module_wp  module_count   tilt  azim  losses%  Action
SLP1            →540          →350    →15  →180      →14  module_wp+module_count+tilt_deg+azimuth_deg+system_losses_pct
SLP2            →540          →516    →15  →180      →14  module_wp+module_count+tilt_deg+azimuth_deg+system_losses_pct
GTO1            →540         →1122    →15  →180      →14  module_wp+module_count+tilt_deg+azimuth_deg+system_losses_pct
...

=== Inverter physical inferences ===

Plant      SN                     rated_kw  mppt_count  Action
MEX1       ES2470051825              175.0          →12  UPDATE
MEX1       ES2470051826              150.0          →12  UPDATE
MEX1       GR2489022511              175.0          →12  UPDATE
QRO1       7E0571B7-AB                 --           →2   UPDATE
... (rest with rated_kw=0 → mppt_count default of 2)
```

Looks reasonable? Apply:

```bash
PYTHONPATH=. python scripts/seed_plant_physicals.py --apply
```

## What it fills (and what it doesn't)

| Field | Source of estimate | Confidence |
|---|---|---|
| `module_wp` | 540W if installed 2021+, else 330W | Medium — most Mexican commercial since 2021 uses 540W panels |
| `module_count` | `round(kwp_dc × 1000 / module_wp)` | Derived — only as good as kwp_dc |
| `tilt_deg` | 15° (Mexico latitude rule of thumb) | Low — varies by installer; some sites are 5°, some 30° |
| `azimuth_deg` | 180° (south) | High — nearly universal in Mexico |
| `system_losses_pct` | 14% (NREL PVWatts default) | Medium — industry standard for commercial PV |
| `mppt_count` | Bucketed by rated_kw | Low — vendor variation is huge |

**NOT filled** (need real installer docs):
- `string_count`, `strings_per_mppt`, `rated_kw_dc`, `commissioning_date`
- `notes`, `pr_baseline`, `tariff_mxn_per_kwh`

## The honest caveat — read this

`module_count = kwp_dc / module_wp` means the **Stage 7.3 sanity warning
"kwp_dc disagrees with module_count×module_wp"** will be silent after
you run this script. By design — we made them agree by assumption.

This warning only becomes useful again **after you replace these
placeholders with real installer values**. Until then, the warning
can't help you catch a wrong kwp_dc.

This is the trade-off you accepted when you said "fill in placeholders
during onboarding, fix details later."

## How to "fix details later"

When you get real installer data (a `module_count = 1280` from a
commissioning report, not derived from kwp_dc):

1. Just edit the cell in Plants tab.
2. Re-run `seed_plant_physicals.py` to check no other plants need filling.
   The script's "non-zero → skip" rule preserves your edit.
3. Re-run `kpi_eod.py`. The sanity warning will fire again on plants
   where derived vs real disagree by >15%.

That's the workflow that lets you transition placeholders → real values
incrementally.

## What's in this zip

| File | Purpose |
|---|---|
| `scripts/seed_plant_physicals.py` | Dry-run-by-default placeholder filler |
| `tests/unit/test_seed_physicals.py` | 25 tests covering defaults + idempotency |
| `docs/stage7_3c_paste_headers.md` | This document |

After running seed_plant_physicals.py with --apply, your sheet finally
has values in every column that Stage 7.x code reads from. The first
real `kpi_eod.py` run should show meaningfully better data quality.

## Reminder — the telemetry sparsity issue still blocks meaningful KPIs

This stage fills metadata. It does NOT fix the upstream problem we
identified: only 95 telemetry rows in 7 days across 30 inverters. That's
still the next thing to investigate.
