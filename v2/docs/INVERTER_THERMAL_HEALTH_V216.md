# Inverter thermal health — from "65 °C = critical" to a measured diagnosis (v216, 2026-09-06)

Tomasz: "growatt inverters which have temperatures over 65C the monitoring show it as high and notifies as critical error, but there are no data supporting this". True on both counts — and the fix is to *measure* it on ARGIA's own fleet rather than to look for a manufacturer number that does not exist.

## 1. What the manufacturers give us (and what they don't)
From the material brought into the session (to be pinned to the exact manuals per model — see §6):
- Growatt MAX 100–150KTL3-X: ambient operating range −30…+60 °C, smart air cooling; the MAX 124KTL3-X MV nameplate says **power derating above 45 °C ambient**.
- Growatt MAX manual: internal temperature drives fan operation and, if it stays high, **output derating**; causes listed: blocked/dirty fans, damaged fans, poor ventilation. Warning 407 "Over Temperature", Error 408.
- Growatt warranty terms exclude damage from insufficient ventilation, installation in direct sunlight, failure to meet operating-temperature requirements.
- Huawei SUN2000 and SolarEdge inverters publish the same kind of statement (derating above a stated ambient, ventilation clearances) — the ARGIA rule is vendor-neutral for that reason.

What no manual says: "internal 65 °C = X % lost". The monitored value is an internal/heat-sink temperature, not the datasheet's ambient. So the alarm cannot be justified by a manufacturer limit; it can be justified by ARGIA's own measurement. That is what v216 does.

## 2. The ARGIA model (`argia/analytics/thermal.py`, pure, tested)
Per 5-minute interval, per inverter, using the telemetry the fleet already stores (power_w, temperature_c, ambient_temp_c from the weather feed, rated_kw from the inverter table):

| Signal | Definition | Why |
|---|---|---|
| **Band** | normal <50, watch 50–60, warning 60–65, high 65–70, critical ≥70 (immediate ≥75) °C — *ARGIA operational thresholds, labelled as such everywhere, never "warranty limits"* | the old flat 65/75 rule, graded |
| **ΔT ambient** (thermal stress index) | internal − site ambient | 66 °C at 43 °C ambient is a hot day; 66 °C at 32 °C is a cooling problem |
| **ΔT peers** | internal − median of the plant's other inverters at the same minute | the strongest single signal: ambient, irradiance and loading are shared, only the cooling differs |
| **Expected output** | median specific power (kW per rated kW) of the peers that are ≥5 °C cooler and running ≥20 % of rated, × this unit's rated kW × its own **cool baseline** (its usual ratio to peers when nobody is hot, median of the last 30 days) | the baseline removes the "smaller DC field looks like a loss" bias Tomasz raised |
| **Suspected derating** | interval ≥65 °C, ≥5 °C hotter than the cooler peers, actual < 97 % of expected | three conditions, so a hot-but-producing unit is not "derating" |
| **Lost kWh** | Σ (expected − actual) × 5/60 over derating intervals | the number for the O&M case; valued at the month's PPA tariff on the report page |
| **Events, hours ≥65/≥70** | contiguous hot runs (one missing tick tolerated), minutes summed | duration matters as much as peak |
| **Cooling health** | POOR when ΔT peers ≥10 °C or ≥30 min of derating with loss; WATCH ≥5 °C; GOOD; n/a without peers | the one word for the O&M list |
| **Derating curve** | actual/expected ratio binned by 2.5 °C, merged over 30 days; **knee** = first bin (≥20 samples) from which the ratio stays below 0.97 | the measured "temperature at which this unit starts losing" |

Cases the model refuses to over-read: no cooler peer (whole plant hot, single-inverter plant) → temperature statistics only, no derating claim; dawn/dusk (peers below 20 % of rated) → no reference; unconfigured serials ignored.

## 3. Where it lives
- **Nightly job** `scripts/thermal_daily.py` (`argia-thermal.timer`, 01:10 MX) → tables `thermal_daily` (per inverter-day) and `thermal_bins` (curve). `--days-back N` backfills oldest-first so the baseline settles; `--report GTO1` prints a plant's 30-day curve and knee.
- **Live plant page** (portal `/monitoring/<slug>/`): "Thermal health" card — peak (band pill), h ≥65, events, ΔT peers, ΔT ambient, derating h, lost kWh, cooling health, plant total; shows the last evaluated day on the live page.
- **Report plant page** (`/report/<slug>/`): "Thermal health — last 30 days" — same per inverter plus **MXN** (lost kWh × PPA tariff), TOTAL row, ⓘ with the method.
- **Alerts** (`argia/analytics/acute.py`, every 30 min): ≥65 WARNING; ≥70 CRITICAL when ≥5 °C hotter than the plant peers or alone; ≥70 with the whole plant equally hot = WARNING "plant-wide heat"; ≥75 always CRITICAL. The message carries the peer deviation and, when cooler peers make more per rated kW, the measured shortfall ("producing 15 % below cooler peers") — evidence in the alert itself.
- **Ask ARGIA**: `get_thermal_health(plant?, date_from, date_to)` — per-inverter summary with totals and, for one plant, the derating curve with its knee; tables `thermal_daily`/`thermal_bins` also open to `query_database`.

## 4. First real numbers
Filled in after the 90-day backfill on pio06 (see the deploy log / next session note). Fleet temperature data: every plant reports internal temperature; ambient is on every row (weather feed); peaks in the last 30 days: NL1 82 °C, MEX1 76, TAM1 76, SLP1 75, GTO1 71 — NL1 (Plastic Omnium, four identical 124 kW MAX units) is the natural first case.

## 4b. The vendor's own word (v222, 2026-09-07)
Growatt MAX inverters publish a **DeratingMode** register (Modbus 104: 0 no derate, 1 PV, 3 Vac, 4 Fac, **5 Tboost, 6 Tinv**, 7 Control, 9 OverBackByTime) which the detail mirror stores as `telemetry_detail.derating_mode` (since 2026-09-05). A fleet-wide sweep found no W407/E408 anywhere, but **mode 6 (Tinv) on Plastic Omnium inverters 1 and 4 on 2026-09-05 — 95 and 60 minutes at 71–82 °C**, the two units the peer-counterfactual analysis had already singled out; no other inverter has ever reported a thermal mode. Since v222:

- `thermal_daily.vendor_derating_minutes` — minutes per inverter-day in Tinv/Tboost, computed by the nightly job from a LEFT JOIN on `telemetry_detail` (NULL/0 for Huawei and SolarEdge, which publish no such register, and for days before the mirror existed). ≥30 min makes cooling health POOR on its own.
- Thermal cards: the daily card gains a **Vendor derating** column, the 30-day report card **Vendor h**; Ask ARGIA `get_thermal_health` returns `vendor_derating_hours`.
- Alerts: the acute rule reads the tail of `telemetry_detail`; when a unit's newest fresh sample is in a thermal mode the message quotes it first ("Growatt reports Tinv derating (45 min); …") and a ≥70 °C unit is CRITICAL on the device's word alone — the inverter confirming that it limits power is abnormal production by definition. The daily day-peak rule does the same with ≥60 vendor minutes. A unit that has already left the mode carries no word.

The measured loss (peer counterfactual) and the vendor minutes are kept as **separate columns** on purpose: one is ARGIA's estimate of kWh, the other is the manufacturer's confirmation that derating happened — quote both to the installer.

## 5. How to use it in O&M
1. Sort the report's thermal card by MXN: that is the cooling-work priority list, with its payback.
2. A unit with POOR cooling health and a knee ≥65 °C is a cooling-system defect (heat sink, fan, clearance, sun exposure) — not the weather; that is the case to put to the installer / for warranty conditions.
3. Whole-plant heat (all units hot, GOOD/WATCH health, no derating) is a site problem (ventilation of the inverter room, shading) — different fix, different budget.
4. The bands stay ARGIA's; when quoting the manufacturer, quote the manual's own words (ventilation, ambient derating, Warning 407) and put ARGIA's measured loss next to them.

## 6. To do
- Pin the manufacturer statements to the exact model manuals in the fleet (Growatt MAX 60/75/124 KTL3-X LV, Huawei SUN2000-150KTL, SolarEdge SE100K) with page references; add the Huawei/SolarEdge over-temperature codes to `fault_catalog`.
- After 30 days of thermal_daily: review the knees per inverter and decide whether 65 °C stays the "high" band or moves per model.
- Fan/cooling maintenance events logged in `maintenance_event` should show on the thermal card (before/after) — the proof that cooling work paid.
