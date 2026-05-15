# Stage 7.2 — KPI computation

Pure math on a day's archived telemetry. No I/O outside reading
``Telemetry_Argia``. No writes. No alerts.

## What's here

| Module | Purpose |
|---|---|
| `argia/kpi/reader.py` | Read one day's rows from `Telemetry_Argia` into typed `InverterRow` objects |
| `argia/kpi/energy.py` | End-of-day energy per inverter; handles reboot, rollover, vendor differences |
| `argia/kpi/irradiance.py` | Trapezoidal integration of W/m² → kWh/m², with cloud-cover fallback |
| `argia/kpi/performance.py` | Performance Ratio, capacity factor, per-inverter peer ranking |

## What's NOT here (yet)

- ❌ Writing results back to a `KPI_Daily` tab — Stage 7.3
- ❌ Reading from archive tabs (>1 day old) — Stage 7.3
- ❌ Multi-day trending / comparisons — Stage 7.3
- ❌ Alerts based on KPI values — Stage 7.4

## Quick start

```python
from argia.core.sheets import SheetsClient
from argia.core.config import load_portfolio
from argia.kpi import (
    read_day_bundle, compute_plant_energy, compute_plant_pr,
    compute_inverter_peer_ranking,
)
from argia.kpi.irradiance import daily_irradiance_for_plant
import os

sheets = SheetsClient(sheet_id=os.environ["GOOGLE_SHEET_ID_V2"])
portfolio = load_portfolio(sheets)

# Pull one day's data
bundle = read_day_bundle(sheets, "2026-05-13")

for plant in portfolio.active_plants():
    rows = bundle.rows_for_plant(plant.plant_key)
    if not rows:
        print(f"{plant.plant_key}: no telemetry")
        continue

    # Per-inverter energy
    energy_by_inv = compute_plant_energy(rows)

    # Day's irradiance — ShineMaster first, cloud-cover fallback
    irr = daily_irradiance_for_plant(
        rows, lat=plant.lat, date_iso=bundle.date_iso,
    )

    # Plant PR + capacity factor
    perf = compute_plant_pr(
        plant_key=plant.plant_key,
        date_iso=bundle.date_iso,
        kwp_dc=plant.kwp_dc, kwp_ac=plant.kwp_ac,
        energy_per_inverter=energy_by_inv,
        irradiance=irr,
        inverter_count_expected=len(portfolio.inverters_for(plant.plant_key)),
    )

    print(
        f"{plant.plant_key:8s} "
        f"E={perf.energy_kwh:7.1f}kWh "
        f"H={perf.irradiance_kwh_m2 or 0:.2f}kWh/m2 "
        f"PR={perf.pr or 0:.3f} ({perf.pr_confidence.value}) "
        f"CF={perf.capacity_factor or 0:.3f} "
        f"src={perf.irradiance_source.value}"
    )

    # Peer ranking
    inv_meta = {
        inv.inverter_sn: {
            "rated_kw": inv.rated_kw,
            "inverter_label": inv.inverter_label,
        }
        for inv in portfolio.inverters_for(plant.plant_key)
    }
    ranks = compute_inverter_peer_ranking(
        plant.plant_key, energy_by_inv, inv_meta,
    )
    for r in ranks:
        marker = ""
        if r.relative_to_peer is not None and r.relative_to_peer < 0.85:
            marker = " ⚠ underperforming"
        print(
            f"    {r.inverter_label:15s} "
            f"{r.specific_yield_kwh_per_kwp or 0:5.2f} kWh/kWp "
            f"({(r.relative_to_peer or 0) * 100:5.1f}% of peers){marker}"
        )
```

Save that as `scripts/kpi_demo.py` and run it to see what KPI numbers look
like for your portfolio. **The output is read-only** — perfect for
validating thresholds before Stage 7.4 starts firing alerts.

## Design notes (worth reading before tuning)

### The `etoday_kwh` cumulative trap

`etoday_kwh` is *supposed to be* a monotonically-increasing day total.
In practice:

- **Inverter reboot mid-day**: resets to 0. Sequence becomes
  `[..., 150.0, 0.0, 30.0, ...]`. `max()` gives you the pre-reboot peak;
  `last()` gives you the post-reboot tail. Neither equals true day total.
- **Midnight rollover noise**: some vendors briefly report 0 just past
  midnight for the previous day.
- **SolarEdge derived noise**: SolarEdge computes etoday by diffing
  `totalEnergy`, which can dip by 0.001 kWh between samples (harmless).

We detect reboots (drop > 0.5 kWh between adjacent samples) and switch
energy_kwh from `last()` to `max()` when one is detected. Stage 7.3 will
add proper segmented integration; until then, reboots are flagged via
`EnergyDay.detected_reboot` so the alert engine can degrade confidence.

### PR confidence flags

Not all PR values are equally trustworthy. The KPI structure tells you
how it was computed:

- **HIGH** — ShineMaster with ≥60 samples (5-min cadence covers 5+ hours)
- **MEDIUM** — ShineMaster with 10-59 samples (partial day)
- **LOW** — Cloud-cover fallback (Open-Meteo-based; ±10-15% accuracy)
- **NONE** — No usable irradiance data

When you wire alerts in Stage 7.4, you'll want to require at least MEDIUM
confidence before firing a "PR below target" alert. Otherwise a sensor
outage looks like a performance problem.

### What if my plant has no irradiance sensor?

You're using the cloud-cover fallback. To improve it:
1. Fill in `lat` and `lon` on the Plants tab (Stage 7.1 added these).
2. Optionally: point `weather_plant_id` to a Growatt plant nearby that
   DOES have a sensor — the 5-min pipeline already pulls irradiance from
   that plant's ShineMaster and stamps it into the rows.

The hybrid logic in `daily_irradiance_for_plant()` automatically picks
the best source available.

### Peer ranking caveat

The peer mean is computed across only the inverters in the plant *for
that day*. If 3 of 4 inverters are dark, the 4th will show
`relative_to_peer = 1.0` (perfect peer ranking)
because it has no peers. That's not a bug — it's the right behavior for
a metric whose only meaning is "how does this inverter compare to its
neighbors." The alert engine should check inverter_offline first, peer
ranking second.

## Sample output (what to expect with real data)

A reasonable Mexican plant on a sunny day:

```
QRO1     E= 2840.0kWh H=6.42kWh/m2 PR=0.853 (HIGH) CF=0.296 src=shinemaster
    Inverter 1      6.85 kWh/kWp (104.7% of peers)
    Inverter 2      6.71 kWh/kWp (102.5% of peers)
    Inverter 3      6.49 kWh/kWp ( 99.1% of peers)
    Inverter 4      5.13 kWh/kWp ( 78.4% of peers) ⚠ underperforming
```

That last line is the kind of insight you'd never spot eyeballing 5-min
rows: inverter 4 is 21% below its peers despite the same conditions.
Worth a maintenance ticket.

## Sanity-check your numbers

Once you run the demo, sanity-check these:

| Metric | Reasonable range (Mexico) | Out of range likely means |
|---|---|---|
| PR | 0.70–0.88 | <0.65: dirty panels, hot day, or PR confidence is LOW; >0.95: kwp_dc wrong in sheet |
| Capacity factor | 0.16–0.28 | <0.10: mostly cloudy; >0.30: kwp_ac wrong in sheet |
| Irradiance kWh/m² | 5–7 (sunny) | <3: heavy overcast or sensor stuck; >8: sensor mis-calibrated |
| Specific yield kWh/kWp | 4–6.5 (sunny) | Compare across inverters — outliers tell the story |

If a plant's PR comes back >0.95, the most common explanation in my
experience is **kwp_dc is set to the AC rating in the Plants tab**, not
the DC nameplate. The DC nameplate is usually 1.10-1.25× the AC.
