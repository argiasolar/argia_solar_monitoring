## Start here {#start}

This page explains every number on the monitoring pages of the ARGIA portal: where it comes from, how it is calculated, which assumptions it makes and where its limits are. It is written for the O&M team, for our customers' engineers and for anyone who looks at a figure and asks "why this value?".

**How to find an answer fast**

- Every card on a plant page has a small **?** next to its title. It opens this page at the section that explains that card.
- The search box on the left filters the contents. The language switch (EN / ES) keeps your place.
- Click any screenshot to see it full size.

**Principles behind every figure**

1. **Measured first, estimated second, and always said which.** A value that is estimated carries the word *estimate*, *expected* or *design*, and the rule that produced it is written here.
2. **Ratios over a period are ratios of sums**, never the average of daily ratios (one exception, the 30-day PR tile, is named in its section).
3. **Missing data is never zero.** A lost connection is "no data", not "no production". Energy that the inverter's own counter proves is never lost.
4. **The vendor's cumulative counter is the billing control.** 5-minute data is for operations and diagnosis; it can never lower an invoice.
5. **One calculation, many pages.** The live page, the daily mails, the reports and the alerts read the same stored figures, so they cannot disagree.

The screenshots come from the ARGIA demo site (real plants with the names removed) and, for the cards that show customer engineering data (string layouts, troubleshooting, consumption), from an invented test plant.

## The portal at a glance {#portal}

![The home page: one tile per area of the portal](home)

The portal has four areas: **Reports** (daily, monthly and financial reports, invoice annexes), **Monitoring** (live plant pages, performance, design, losses, reconciliation), **Map** and **Savings**. The header always has the same three buttons: Home, Ask ARGIA (questions in plain language over the same data) and your account menu, where you change the language and find this documentation.

![Monitoring: one tile per plant, coloured by its live status](fleet)

Each plant tile shows the live power, the energy of the day and a status colour (section [Live status](#status)). Click a tile to open the plant page.

![The top of a plant page: live figures, PR and availability of the last 30 days](plant_top)

A plant page reads from top to bottom: live figures and status, then the cards. Every card can be folded with the arrow on its title; the portal remembers what you folded, in this browser, for every plant.

![Folded cards: the troubleshooting card closed, the others open; ? opens this page at the card](folded)

The date box shows any past day; a past day is a frozen copy of that day as it was computed.

## How the data flows {#architecture}

```formula
inverters, loggers, meters, weather station
  -> manufacturer's cloud platform (API)
  -> ARGIA collectors (every 5 to 15 minutes)
  -> ARGIA database (5-minute samples, daily figures, counters)
  -> calculations (daily, nightly, monthly)
  -> portal pages (rebuilt every 5 minutes), mails, reports
```

ARGIA does not install software on the inverters. Everything is read through the manufacturers' documented platforms, read-only. The table lists what runs when (Mexico City time).

| What | When | What it does |
|---|---|---|
| Inverter telemetry | every 5 min, 05:00 to 21:55 | power, day energy, status, faults, temperatures, string currents and voltages, irradiance |
| Telemetry, hourly platforms | :03 / :23 / :43, 06:00 to 20:59 | the same for platforms that publish 15-minute data |
| Grid meters | :02 / :17 / :32 / :47, all day | import, export, signed power (section [Consumption](#consumption)) |
| Portal pages | every 5 min | every page and card is rebuilt from the database |
| Acute alerts | :00 and :30, 07:00 to 19:59 | conditions one sample can prove (section [Alerts](#alerts)) |
| End-of-day KPIs | 06:00 | yesterday's energy, irradiation, PR, expected energy, availability |
| Daily alerts | 06:30 | full-day checks: energy vs expected, inverter vs peers, thermal |
| Satellite check | 06:20 | site irradiance sensor against satellite irradiance |
| Daily performance mail | 19:00 | the day per PPA plant |
| String statistics | 21:15 | per-string amp-hours of the day |
| Reconciliation | 23:50 | the day against the vendor's own counters, with a 14-day retry |
| Thermal health | 01:10 | yesterday's inverter temperatures, derating and its cost |
| Monthly close | 1st, 06:10 | the month against the vendor's monthly and lifetime counters; a closed month is frozen |

**Time.** Every timestamp is stored in UTC and shown in Mexico City time (UTC-6, no daylight saving). A day is a local calendar day, midnight to midnight.

**Freshness.** A plant whose newest sample is older than 30 minutes during 06:00 to 20:00 is shown as stale. A page shows the age of its data next to live values.

## What is collected and how it is cleaned {#data}

### Per inverter, every 5 minutes

| Field | Use |
|---|---|
| AC power (W) | live power, status, availability, intraday curves |
| Day energy counter (kWh since midnight) | daily energy, hourly bars, proof of production over a gap |
| Lifetime energy counter (kWh) | reconciliation, catch-up after a frozen day counter |
| Status and fault codes | status, alerts (the device's own diagnosis) |
| Internal temperature (°C) | thermal health |
| Per string: current (A); per MPPT: current and voltage | string monitoring, EPI-600 |
| Derating mode (where the vendor publishes it) | vendor-confirmed thermal derating |

### Weather

- **Irradiance (W/m²)** from the plant's own sensor (or the nearest ARGIA sensor when the plant has none), with **panel and ambient temperature** where the station has them.
- **Cloud cover (%)** from a public weather model at the plant's coordinates (drawn on the intraday chart, used as a fallback for irradiation).
- **Satellite irradiance** (daily, model analysis) only to detect a drifting sensor, never in a calculation that reaches a customer figure.

### Cleaning rules (applied before any calculation)

- **Repeated records.** A vendor API that stops updating keeps returning its last record. A sample that repeats the previous one exactly (same power and same day counter, power above 0) is a repeat, not a new measurement, and is ignored. A counter that does not move while the inverter reports at least {{c:gaps.STALE_MIN_W}} W in two consecutive samples is also treated as stale.
- **Midnight carry-over.** Some inverters reset their day counter a few minutes after midnight, so the first samples of a day can still carry yesterday's total. A flat leading segment that then drops is yesterday's counter and is removed.
- **Reboots.** A counter that drops in the middle of the day after growing is a reboot; the day energy is the sum of the segments.
- **Irradiance quality** (IEA PVPS T13-28, after IEC 61724-1/-3): values outside {{c:engine.GHI_MIN}} to {{c:engine.GHI_MAX}} W/m² are invalid; more than {{c:engine.NIGHT_MAX_GHI}} W/m² with the sun below the horizon is invalid; a value frozen within {{c:engine.FROZEN_DELTA}} W/m² for {{c:engine.FROZEN_RUN}} or more consecutive 5-minute samples above {{c:engine.FROZEN_MIN_GHI}} W/m² is invalid from its second sample on. Invalid samples are dropped from every model; they are counted and shown.
- **Collector windows.** When the ARGIA collector itself was down for every plant at once, that window is excluded before any inverter is judged.

### Daily irradiation

```formula
H [kWh/m²] = trapezoid integral of the irradiance samples over the day / 1000
```

The trapezoid rule averages each pair of neighbouring samples (a rectangle sum overestimates). The site sensor is used when it gives at least 10 valid samples spread over the day; otherwise the day's irradiation comes from the cloud-cover model and the day is marked as such. Sun that was not measured is never borrowed from other days or other plants.

## Live status {#status}

### Plant tile and plant page status

The status is decided now, from the newest samples, between 06:00 and 20:00. First match wins:

| Status | Rule |
|---|---|
| no data today (red) | no sample of the plant today |
| stale (red) | no inverter has a sample younger than 30 minutes |
| inverter(s) silent (amber) | fewer inverters reported today than are configured |
| fault (amber) | an inverter reports a fault status |
| 0 W while others produce (amber) | an inverter reports 0 W while a sibling produces |
| inverters stale (amber) | some inverters are older than 30 minutes |
| zero power midday (amber) | every inverter at 0 W between 09:00 and 17:00 |
| critical device alert (amber) | an open critical alert on a device of the plant |
| all reporting (green) | none of the above |
| night (grey) | outside 06:00 to 20:00 |

The map uses the same thresholds as the alert mails, so the map and the mails never disagree.

![The map: every plant with its live status](map)

### Inverter status vocabulary

FAULT (the vendor says so), OFFLINE (sun up but silent or at zero), DERATED (derating flag while producing), UNDERPERFORMING (producing, below its peers per rated kW), IDLE_NIGHT, NO_DATA, ONLINE. One shared classifier feeds every page and mail.

## Plant KPIs: energy, PR, availability {#kpis}

### Energy

```formula
E_day [kWh] = sum over inverters of the day energy counter (reboot and carry-over aware)
specific yield [kWh/kWp] = E_day / kWp_DC
```

"Energy today (interval)" on the live page is the sum of each inverter's latest day counter; "Vendor counter" is the plant counter the vendor itself reports. The vendor counter is the billing reference (section [Reconciliation](#reconciliation)).

### Performance ratio (PR)

```formula
PR = E_day / (kWp_DC x H)                     IEC 61724-1
PR_STC = PR / (1 + gamma x (T_cell,eff - 25 °C))
```

- **H** is the day's irradiation on the plane the sensor measures (kWh/m², see [Data](#data)).
- **gamma** is the module's power temperature coefficient (negative, about -0.0034 /°C); **T_cell,eff** is the irradiance-weighted module temperature of the day, so the hot noon hours that carry the energy also carry the correction. Without a measured module temperature or a gamma, PR_STC is not computed.
- A clean, new plant in Mexico reads about 0.78 to 0.85; heat and soiling move it down.

**The "PR 30d" tile** is the mean of the daily PR values of the last 30 days, needing at least {{c:mg.MIN_PR_DAYS}} days with a value. Days with PR above {{c:mg.PR_MAX}} are left out: such a day had an under-measured sun (sensor shaded, frozen or missing hours), not a super-plant. This tile is the one figure in the portal that averages daily ratios.

### Availability

Availability follows the IEC 61724-3 idea: **time without information is not downtime**.

```formula
availability = UP / (UP + DOWN)          weighted by each inverter's rated kW
coverage     = (UP + DOWN) / judged time
```

- The day is cut into {{c:availability.STEP_MIN}}-minute steps. Only steps with the sun at least {{c:availability.SUN_MIN_ELEV}}° above the horizon are judged (dawn, dusk and a small inverter's late wake-up are not downtime).
- Per inverter and step: **UP** = a reading with power above 0 (whatever the status code says); **DOWN** = readings, all at 0 W, unless the day counter grew through the step (a stale 0 W on the vendor feed); **UNKNOWN** = no reading.
- An UNKNOWN step becomes UP when the inverter's own day counter proves it produced through the gap: its energy per rated kW reaches {{pct:availability.COUNTER_PROOF}} of its peers' median.
- When the whole plant is silent (internet or logger down: the inverters keep producing and the vendor receives the data later), the silent time is UP if the plant's day energy reaches {{pct:availability.PLANT_PROOF}} of what it normally makes on such a day.
- An inverter that went silent after a stop (last reading 0 W or a fault) stays DOWN while its peers report.
- UNKNOWN steps that nothing proves are left out and reported as coverage. A dark plant is "no data", never 0 % and never 100 %.

**30 days:** the daily availabilities weighted by their coverage; a day with no row adds owed time without known time.

```formula
availability_30d = sum(availability_d x coverage_d) / sum(coverage_d)
coverage_30d     = sum(coverage_d) / days owed
```

![Performance: PR, PR_STC, availability and coverage per plant](performance)

## Expected energy: the models {#expected}

"Expected" always means: what the plant should have made for the sun that actually came. ARGIA uses four models; each has one job.

| Model | Formula | Used for |
|---|---|---|
| Contract factor | E_exp = kWp_DC x H x expected_factor | the customer invoice annex ("teorica"); never changed by the others |
| HelioScope design | Grid kWh_month / H_design,month x H_measured x ageing | internal performance (EPI, design page, intraday line) |
| EPI-600 string model | per string and 5-minute step, from irradiance on each plane and cell temperature | troubleshooting card, EPI-600 / Availability-150 |
| Peers | neighbours' kWh/kWp x this plant's kWp x its usual ratio to them | losses (section [Losses](#losses)) |

### HelioScope design baseline (internal)

Each PPA plant has its HelioScope report as built (modules, strings, tilt, azimuth, soiling profile, losses). Per month the report gives the grid energy and the irradiation on the sensor's plane.

```formula
design kWh (month)   = Grid kWh_month x ageing(mid-month)
design-expected kWh  = Grid kWh_month / H_design,month x H_measured x ageing
ageing               = (1 - {{pct1:design.DEFAULT_DEGRADATION}} per year) from the interconnection date
EPI (energy)         = actual / design-expected        in line = within {{pct:design.IN_LINE}}
```

### EPI-600 model (5-minute, per string)

This is the model of the ARGIA performance specification v0.7, with its audit changes (November 2026).

```formula
1  G_POA,s   = irradiance on string s's plane (Erbs split + Perez 1990 transposition)
2  T_cell    = T_panel + {{c:model.T_CELL_OFFSET_K}} x min(G_POA / 1000, 1.2)
3  PR0       = b0 + b1 x T_cell + b2 x g + b3 x g²          g = G_POA / 1000
4  D_s       = 1 - degradation(module, years since the string's energization)
5  E_s [W]   = kWp_s x 1000 x D_s x g x PR0 x (1 - S_month) x B_month
6  cap       = min(1, (AC limit / {{c:model.INVERTER_EFFICIENCY}}) / sum of E_s of the inverter)
7  expected  = E_s x cap
```

- **b0..b3** come from the plant model (Annex B); **S_month** is the design soiling of the month (Annex C).
- **B_month** (monthly bias) is computed from the design year so that every plant scores exactly 100 % on its own design weather; it is never typed by hand.
- **Degradation** per module type: first-year loss, then a yearly rate (for example 1.0 % then 0.40 %/year).
- **The inverter limit** clips the DC expectation at the inverter's measured or datasheet AC limit divided by the inverter efficiency.
- Sun position: NOAA/Meeus; transposition and air mass as in pvlib (equivalence tested within 0.02° and 1 %).

### The intraday theoretical line

```formula
theoretical_h [kWh] = mean irradiance of the hour [W/m²] / 1000 x kWp_DC x PR_baseline
```

PR_baseline is the plant's clean-plant PR (0.80 when it is not set).

With a HelioScope design the design factor of the day replaces kWp x PR_baseline.

## Intraday production {#intraday}

![Intraday production: 60-minute bars per inverter, the theoretical line and cloud cover](intraday)

- **Bars:** energy of each hour per inverter, from the day counter: the hour's highest counter minus the previous hour's highest. A first hour before 07:00 counts from midnight; a collection that starts mid-day draws no catch-up bar.
- **Dashed dark line:** the theoretical energy of each hour for the measured irradiance (section [Expected](#expected)).
- **Grey dashed line:** cloud cover, %, right axis.

## Inverter table {#inverters}

![The inverter table: power, energy, kWh/kWp, strings, EPI-600, comparison with peers, temperature](inverters)

```formula
vs peers = (E_i / P_rated,i) / median over producing peers j != i of (E_j / P_rated,j)
```

- **vs peers** compares energy per rated kW, so a smaller inverter is not falsely flagged. The median is leave-one-out; a two-inverter plant compares with its sibling. Amber below {{pct:health.DEFAULT_WARN_BELOW}}, red below {{pct:health.DEFAULT_CRIT_BELOW}}.
- **°C** latest sample / day peak; amber from 65 °C, red from 75 °C.
- **kWh/kWp** day energy over the inverter's kWp (Annex A); **Strings** strings with output today / strings; **EPI-600** day / month to date (section [Troubleshooting](#troubleshooting)).

## Troubleshooting: plant, inverters, strings {#troubleshooting}

One card, six views, all on the same 5-minute data. The yellow area behind the curves is the station irradiance (right axis), so you see at once how sunny the moment was.

The arrows and the date in the card's top right move to the previous or next day (or any day) and land back on this card; the chosen view and inverter stay the same. **Today** returns to the live day.

### Plant

![Plant view: AC generation, design and site consumption](epi_plant)

The plant's AC power (blue), the design for the measured weather (dashed) and, where a grid meter is validated, the site consumption (orange, section [Consumption](#consumption)). Hover for the values of the moment.

![Hover: the values of one 5-minute step](epi_tooltip)

### Inverters

![Inverters view: W per kWp per inverter, its design, and the dips against the peers](epi_inverters)

```formula
specific power [W/kWp] = sum of the strings' DC (I x V) / their kWp
                         (no string sample: AC / {{c:model.INVERTER_EFFICIENCY}}, marked "from AC")
gap: actual/design < {{pct:views.GAP_SHARE}} x median(actual/design of the plant's inverters)
     while design >= {{c:views.GAP_MIN_WKWP}} W/kWp, for {{c:views.GAP_MIN_STEPS}} steps or more
```

Judging against the other inverters (not the design alone) keeps clouds over the station and low-sun shading of the whole plant from flagging every inverter every afternoon. A flagged inverter gets an **Open ticket** button with the dip prefilled.

### Strings

![Strings view: every string of one inverter, the band of its peers and the strings outside it](epi_strings)

```formula
index_s   = (W/kWp of string s) / (its own design W/kWp)       (fair across roof planes)
band      = {{pct:views.BAND_LO}} to {{pct:views.BAND_HI}} x median index of the inverter, per step
outlier   = outside the band for {{c:views.OUT_MIN_STEPS}} steps or more with G_POA >= {{c:views.OUT_MIN_GPOA}} W/m²
no output = mean index < {{pct:views.ZERO_SHARE}} of the median with G_POA >= {{c:views.ZERO_GPOA}} W/m²
```

Strings with no output are left out of the median and the band (one dead string of four would move the median by about 15 %). Each outlier carries its median deviation while outside, its worst moment and, when it is low or dead, a prefilled ticket button.

### Week and month

![Month view: daily EPI-600 per inverter over the target bands, insolation bars and the strings by day](epi_month)

Daily EPI-600 of the plant and each inverter over the bands (green 97 % or more, amber 93 to 97 %, red below 93 %), the day's insolation as yellow bars (kWh/m², right axis), and a strings x days map. The week and 30-day figures are ratios of sums of the days shown. A whole column in red is the whole plant that day (clouds over the station, a sensor); a row that stays red is the string.

### Consumption

![Consumption view: 30 days of solar used on site, grid import and export, and the day by hour](epi_consumption)

Shown where a grid meter is validated; section [Consumption](#consumption) explains every figure.

### EPI-600 by string, month to date

![EPI-600 by string and inverter, month to date: one dot per string over the target bands](epi_bystring)

## EPI-600 and Availability-150 {#epi}

```formula
EPI-600 = sum(real Wh) / sum(expected Wh)
          over exactly the same valid 5-minute samples with G_POA >= 600 W/m²
EPI-150 = the same with G_POA >= 150 W/m²
Availability-150 = UP / (UP + DOWN) per inverter over the steps with station GHI > 150 W/m²
plant availability = kWp-weighted mean of the inverters
```

**Valid sample** = station irradiance, panel and ambient temperature present and passing the quality checks ([Data](#data)); the string's current and voltage present; the string energized (strings before their energization date do not exist); the inverter producing, i.e. its DC at least {{pct:model.PRODUCING_SHARE}} of its expected DC. A non-producing inverter is excluded from EPI and counts DOWN for availability. No inverter data in a step = MISSING: neither UP nor DOWN.

**Bands:** green ≥ {{pct:model.GREEN}}, amber ≥ {{pct:model.AMBER}}, red below; above {{pct:model.SUSPECT}} = suspect (the sensor or the model, not the plant); fewer than {{c:model.MIN_VALID_SAMPLES}} valid samples = insufficient data.

**Month to date** = the tallies (real Wh, expected Wh, samples) of every day of the month added, then divided once. A closed day is computed once, {{c:live.SETTLE_H}} hours after it ends (late uploads land in that time), and kept; changing the configuration or the rules recomputes it.

### Communication gaps

Energy is never invented. Over a gap the inverter's day counter gives the exact energy, E = C(after) - C(before); that energy is spread over the gap's 5-minute slots in proportion to the expected power for the measured irradiance (or a clear-sky shape where the station is invalid). Every slot is flagged measured, reconstructed or unknown. After the last sample of a day nothing is known unless the vendor's daily total is given.

```formula
counter rose >= 50 % of the expected energy over the gap  -> UP
counter rose <=  5 %                                      -> DOWN (proven)
otherwise                                                 -> UNKNOWN
```

## Strings on the plant layout {#strings}

![The layout card: each numbered dot is one string, coloured by its current against its neighbours](layout)

```formula
ratio_s(t) = I_s(t) / median current of its peers at t     (peers: same module, same roof plane)
day index  = sum of I_s over the judged steps / sum of the peer median over the same steps
```

- Current, not power: strings on one plane see the same light, so their currents match whatever the module count.
- A step is judged only when the peer median is at least {{c:layout.DIM_A}} A (dawn, dusk, heavy cloud are not judged). A data gap on one inverter cannot pull its strings down.
- Classes: ok ≥ {{pct:layout.OK_RATIO}}; low {{pct:layout.LOW_RATIO}} to {{pct:layout.OK_RATIO}}; very low below {{pct:layout.LOW_RATIO}}; no current = below {{c:layout.ZERO_A}} A while the peers carry {{c:layout.DIM_A}} A.
- A string's own normal is the median of its day index over the last {{c:layout.NORMAL_DAYS}} days, so a string that always reads a little lower (a shorter string, a partly shaded row) is not flagged; only a change is. A string whose normal is below {{pct:layout.CHRONIC}} is never shown as ok.
- A plane with fewer than {{c:layout.MIN_GROUP}} strings borrows the plant's strings of the same module.
- A red or dark dot has a ticket button; if an open ticket already covers that input, the button shows the ticket number instead.

## Consumption and solar coverage {#consumption}

Shown only for plants whose grid meter has been checked (what it measures, its sign, and the energy balance against the inverters).

```formula
grid power        > 0 = drawn from the grid (import), < 0 = sent to the grid (export)
consumption       = solar + grid import - grid export
solar used on site = solar - export
solar share       = solar used on site / consumption
several days      = sum(solar used on site) / sum(consumption)
```

- **Import and export of a day** come from the best register the meter gives: the meter's own day registers; else the lifetime counters (last reading of the day minus the last of the day before); else the integrated 5-minute power (each sample held until the next; gaps over 15 minutes are not integrated).
- **By hour:** the lifetime-counter difference between consecutive readings when both have counters (exact at any reading interval up to 3 hours), else the integrated power. The energy is booked to the hour of the earlier reading.
- **Live curve:** the meter's power on the card's 5-minute axis, linear between readings at most 20 minutes apart; consumption = AC power of the plant + grid power.
- **A day with more export than solar** (beyond 2 % or 2 kWh) cannot be one site behind one meter: it is shown greyed, without a share, and left out of the sums.
- **When a vendor refuses the 5-minute history**, one real-time reading per run (every 15 minutes) keeps today current; the history is fetched later and fills the gaps.
- **Validation** (20 November 2026, live data): counters against integrated power within about 1 % a day; day registers against integrated power within 1 to 4 %; every day of the month balanced. The CFE bill remains the billing reference; this view is for operations.

## Thermal health {#thermal}

![Thermal health: peak temperature, time hot, deviation from the peers and ambient, derating and its cost](thermal)

- **Bands** on the internal temperature (ARGIA operational thresholds, not warranty limits): normal < 50, watch 50 to 60, warning 60 to 65, high 65 to 70, critical ≥ 70 °C (immediate inspection ≥ 75 °C).
- **ΔT peers** = this inverter minus the median of the plant's other inverters: the strongest sign of a cooling problem (heat sink, fan, clearance), because ambient, irradiance and load are shared.
- **ΔT ambient** = internal minus site ambient.
- **Suspected derating** in a 5-minute interval: the inverter is at least {{c:thermal.T_HOT}} °C, hotter than its cooler peers by at least {{c:thermal.DT_PEER_MIN}} °C, and produces at least {{c:thermal.LOSS_MIN_PCT}} % less per rated kW than they do (corrected by its own cool-weather ratio); the peers must run at {{pct:thermal.REF_MIN_SP}} of rated or more. **Lost kWh** = that shortfall summed.
- **Vendor derating** = minutes the inverter itself reported a temperature derating mode (where the manufacturer publishes it).
- An alert on temperature is CRITICAL only with evidence of lost production; otherwise it is a warning or an information flag.

## Losses and their cost {#losses}

![Last 7 days: energy, expected, lost kWh and MXN, and the main cause](last7)

Expected energy for a fully available plant, two independent ways:

```formula
peers:   E_exp = median over healthy neighbours within {{c:losses.PEER_MAX_KM}} km of (kWh/kWp)
                 x kWp of this plant x this plant's usual ratio to them
weather: E_exp = kWp_DC x H x expected_factor  (scaled by the plant's usual actual/model ratio)
```

A neighbour counts on a day only if it made at least {{pct:losses.HEALTHY_FRAC}} of its own weather expectation. The peer figure is used when it exists; the usual ratio needs {{c:losses.MIN_CALIB_DAYS}} shared healthy days and must lie within {{c:losses.CALIB_BOUNDS}}.

```formula
lost = max(0, expected - actual), split in this order, each capped at what is left:
  1 unavailability   expected spread over the day's 5-minute slots by irradiance;
                     slots at 0 W lose their share (silent slots judged by the counter)
  2 overheating      the thermal job's measured derating loss
  3 underperformance the rest
```

- **Catch-up:** a day counter that froze during a data gap is corrected by the next night's lifetime counter step before the day is judged.
- **Tolerance:** the estimated part (underperformance and silent slots) counts only from {{pct:losses.UNDERPERF_TOL}} of expected (twice the normal day-to-day scatter); 0 W and heat are measured and always count.
- **Excused:** approved customer maintenance windows are billed as deemed energy, so they are not a loss.
- **MXN:** the PPA tariff of the month; CAPEX plants have no per-kWh tariff and show kWh only.
- Hover any figure on the page to see how that day was calculated.

![Losses: the month per plant, in kWh and MXN](losses_page)

## Reconciliation and billing control {#reconciliation}

![Daily reconciliation: our 5-minute total against the vendor's counter](recon)

| Check | Compares |
|---|---|
| 1 | sum of our 5-minute energy vs sum of the vendor's daily counters |
| 2 | sum of the vendor's daily counters vs the vendor's monthly counter |
| 3 | lifetime-counter difference over the month (our own nightly snapshots) |
| 4 | lifetime-counter difference vs the vendor's monthly counter |

**Billing basis**, most tamper-proof first: lifetime-counter difference > vendor monthly counter > sum of vendor daily counters > sum of 5-minute data. A connection gap between 13:00 and 17:00 can never shrink an invoice: the counter still captured that energy.

| Status | Day | Month |
|---|---|---|
| PASS | within {{c:recon.DAILY_PASS_PCT}} % | within {{c:recon.MONTHLY_PASS_PCT}} % |
| REVIEW | within {{c:recon.DAILY_REVIEW_PCT}} % | within {{c:recon.MONTHLY_REVIEW_PCT}} % |
| FAIL | beyond | beyond |

- A day is complete when at least {{c:recon.COMPLETENESS_MIN_PCT}} % of its 5-minute data arrived; a less complete day says so on its page.
- **Never lower:** a correction from the vendor only fills or raises a stored day; a value above the vendor is flagged for a person, never silently reduced.
- **Retries:** the nightly run goes back 14 days and re-fetches the vendor's day for every day that did not pass because of a gap on our side.
- **Closed months are frozen** after the monthly close; no energy moves between months.

![Reconciliation: each plant-month with its basis and status](recon_month)

## Alerts, mails and tickets {#alerts}

**Two tiers.** The **acute** tier runs every 30 minutes in daylight and only uses conditions one sample can prove (a fault code, a whole plant at 0 W while its counters stand still, a very hot inverter, an inverter at 0 W for 30 minutes or more next to a producing sibling with its own counter flat, data older than the freshness limit). The **daily** tier runs at 06:30 on full-day figures (energy vs expected, inverter vs peers per rated kW, regional twins, thermal) and is the only one that closes alerts.

**Severity**

| Severity | Meaning | Mailed |
|---|---|---|
| CRITICAL | energy is being lost now and the data proves it | yes |
| WARNING | a measured loss or a device's own warning | yes, once |
| INFO | a flag without proven loss (possible problem, missing data) | no, shown on the portal |

- An alert is mailed once; ongoing issues are listed in the morning mail; an alert covered by an open maintenance ticket is not repeated.
- A temperature alert is CRITICAL only with evidence of lost production; a string flag is a warning only when the day's data shows a loss.
- Data missing for several days in a row becomes one warning mail; a missing day is a possible loss, never a measured one.
- Thresholds (for example inverter below 85 % of its peers = warning, below 70 % = critical) are configurable per plant by an administrator.

![Open alerts on a plant page](alerts)

**Tickets.** From the troubleshooting and layout cards a prefilled maintenance ticket opens with plant, inverter serial, input, the measured deviation and the day. An open ticket is matched by plant, inverter serial and the input named in its title ("input 4", "MPPT 2"); a ticket without an input covers the whole inverter. Open tickets are mailed once a week and when their status changes.

## How we aggregate, average and estimate {#rules}

| Figure | Over time | Over devices |
|---|---|---|
| Energy | sum of day counters | sum |
| PR (day) | ratio E / (kWp x H) | plant level |
| PR 30d tile | mean of daily PR (days > {{c:mg.PR_MAX}} left out) | plant level |
| Availability | UP / (UP + DOWN), coverage-weighted over days | rated-kW weighted |
| EPI-600 / EPI-150 | ratio of sums of Wh over the same samples | ratio of sums |
| String index, layout | ratio of sums of current over judged steps | median of peers |
| Inverter vs peers | day energy per rated kW | leave-one-out median |
| Solar share | ratio of sums of kWh | plant |
| Losses | sum of daily losses | plant |
| Irradiation | trapezoid integral | one sensor per plant |

**Medians vs means.** Comparisons between devices use the median (one broken device does not move the reference). Totals use sums. Ratios over a period are ratios of sums.

**Rounding.** Calculations run on unrounded values; pages round for display (kWh to 0 or 1 decimal, ratios to 0.1 %).

**Missing data.** Not zero, not interpolated from other days or plants. Counters prove energy across gaps; what nothing proves is shown as unknown and its share is reported (coverage, completeness).

**Estimates are labelled.** Expected, design, theoretical, reconstructed and deemed values are named as such wherever they appear.

**Known limits**

- Station irradiance sensors are not yet calibrated against a reference; EPI bands are provisional. Comparisons inside a plant (inverter with inverter, string with string) use the same sensor and are reliable.
- A plant without its own sensor uses the nearest ARGIA sensor; local clouds can differ.
- The 30-day PR tile averages daily ratios (see [KPIs](#kpis)).

## Connecting a new plant: API requirements {#integration}

The ARGIA tool works with any inverter, logger or meter brand that offers a documented data interface. Below is what we need, in order of importance.

### Required

| Data | Resolution | Notes |
|---|---|---|
| Inverter AC power | 5 min (15 min accepted) | per inverter, with the timestamp of the measurement and its time zone |
| Inverter day energy counter | same samples | kWh since local midnight |
| Inverter lifetime energy counter | daily at least | kWh, never resets |
| Inverter status and fault/alarm codes | same samples | with the code list or documentation |
| Plant daily, monthly and lifetime energy | daily | the platform's own counters (billing control) |
| History | 5-minute history of at least 7 days (30+ preferred) | to heal gaps after an outage |
| Device list | on request | serials, model, rated power |

### Strongly recommended

| Data | Resolution | For |
|---|---|---|
| Per-string current (A) and per-MPPT current and voltage | 5 min | string monitoring, EPI-600 |
| Irradiance on the module plane or horizontal (W/m²), module and ambient temperature | 5 min | PR, EPI-600, expected energy |
| Inverter internal temperature and derating mode | 5 min | thermal health |
| Alarm list with start and end time | 15 min | vendor alarms |

### For consumption and coverage

| Data | Resolution |
|---|---|
| Grid meter at the connection point: signed active power, or import and export powers | 5 min |
| Lifetime import and export energy counters (or day registers) | same samples |
| History | at least 7 days |

### Interface

- **Accepted:** a manufacturer cloud API (REST/JSON, documented), or a site logger reachable over Modbus TCP / SunSpec through a gateway ARGIA can read.
- **Authentication:** a read-only API key or account. We never need write access; ARGIA only reads.
- **Rate limits:** enough for one call per plant every 5 minutes plus a daily history call; the limits must be published (ARGIA paces its calls and never logs in more than needed).
- **Timestamps:** measurement time, not upload time; time zone stated.
- **Stability:** versioned endpoints; a deprecation notice before changes.

### Proven integrations

Growatt (ShineServer web and API), Huawei FusionSolar (Northbound API), SolarEdge (Monitoring API), SMA (data via the manufacturer's API). Other platforms are added through the same pipeline: a collector, a parser tested on captured answers, and the same calculations for every brand.

### Onboarding steps

1. Read-only access to the plant on the manufacturer's platform.
2. Plant register: kWp DC and AC, coordinates, inverters with rated power, strings per input (Annex A), module type, tilt and azimuth.
3. Design report (HelioScope or PVsyst) for the expected energy.
4. First week: ARGIA checks every device against the register, the signs and the counters, and the plant goes live on the portal.

## Glossary and questions {#glossary}

| Term | Meaning |
|---|---|
| kWp DC | installed module power at standard test conditions |
| H, irradiation | sun energy per m² in a day, kWh/m² |
| G_POA | irradiance on the plane of the modules, W/m² |
| PR | performance ratio, energy / (kWp x H) |
| EPI | energy performance index, real / expected |
| Availability | share of judged daylight time an inverter was producing |
| Coverage | share of judged time for which we know the state |
| Day counter, lifetime counter | the inverter's or meter's own energy registers |
| Deemed energy | energy billed for an approved customer outage |
| MPPT | the inverter's input that tracks the maximum power point of its strings |
| Peers | devices compared with each other: same plant, or same module and plane |

**Why does a plant show "no data" and not 0 %?** Because we do not know. A lost connection is not an outage until a counter proves it.

**Why can today's energy on the live page differ from the vendor's app?** The live figure is the sum of the inverters' latest day counters; the vendor's plant figure can lag or lead by one upload. The reconciliation compares both every night.

**Why is a day's PR missing in the 30-day tile?** A PR above {{c:mg.PR_MAX}} means the sun was under-measured; that day is left out instead of inflating the average.

**Why does a string show amber when the inverter looks fine?** The string is compared with its neighbours on the same plane and with its own normal. A 10 to 25 % drop on one string barely moves the inverter total but is exactly what the O&M team should look at.

**Who do I ask?** Use Ask ARGIA in the header for questions about the data, or contact your ARGIA O&M team.
