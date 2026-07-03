# v1 → v2 Cutover Plan (IMPORTRANGE-only)

*Last updated: 2026-07-02. Supersedes the dual-write approach in the old
Phase-1 build plan. This is the CUTOVER strategy; `v2/MIGRATION.md` is the
separate "how to install v2" onboarding guide.*

---

## The decision, in one screen

**v2 never writes to ARGIA_Solar.** The collector writes one place —
`Argia_Mont_v2` — and ARGIA_Solar pulls thin daily aggregates via
**IMPORTRANGE**. No dual-write.

Why this beats dual-write:

- The Pi/collector can't corrupt the sacred financial sheet because it has
  **no write access to it**. Safety by construction, not by care.
- No obligation to reproduce ARGIA_Solar's exact legacy column layout forever.
  A future schema tweak on either sheet doesn't break a cross-writer.
- One collector, one write target. Simpler to reason about and to test.

The cost this moves onto us: the "repoint ARGIA_Solar to read v2" step is no
longer optional — **it *is* the cutover** (Stage 5). And live IMPORTRANGE over
2.3 years of invoicing history is a real risk that needs the freeze-and-append
guard (see "Protecting financial history").

---

## Two switches, deliberately decoupled

Migration is two independent switches. Keeping them separate is what protects
the money — you can prove and reverse each on its own.

| Switch | What flips | When |
|---|---|---|
| **Collection** | who polls the inverters + writes the daily feed (v1-on-Pi → v2) | Stage 4 |
| **Consumption** | what ARGIA_Solar's financial formulas *read* (`DailyData` → `DailyData_v2` via IMPORTRANGE) | Stage 5 |

**Order is load-bearing: consumption switches before collection retires.**
Never stop v1 writing `DailyData` until the financials no longer read it.

---

## The staged path

Each stage has a gate. Don't advance until the gate is met. Everything before
Stage 5 is reversible.

### Stage 0 — Parallel (current baseline)
v1 on the Pi feeds ARGIA_Solar (`DailyData`, `IU10m_hour`) — the live financial
source of truth. v2 on GitHub Actions feeds `Argia_Mont_v2` (`KPI_Daily` etc.)
— maturing, feeds nothing financial. Two independent collectors.
**Status: current.**

### Stage 1 — Prove collection fidelity  ✅ SHIPPED (unproven until overlap)
Read-only `reconcile_daily` compares v2 `KPI_Daily.energy_kwh` vs v1
`DailyData.Real_kWh`, per plant/day, full days only. Energy is the gate; PR is
shown alongside for diagnosis (config/irradiance divergence is expected, not a
failure). Code is on `main` (commit `e9383fb`), 1314 tests green.
**Gate:** ~2 weeks of complete overlapping days — including a cloudy day and a
fault day — land in OK / PR-DIVERGENCE on energy; every ENERGY-MISMATCH has a
named cause.
**Blocked on:** (a) service account needs Viewer on ARGIA_Solar to read
`DailyData`; (b) overlap must accumulate — v1 ended 2026-06-29, v2 started
2026-06-30, so early runs correctly return exit 2 ("nothing proven yet").

### Stage 2 — Fix v2 config truths
Resolve what the reconcile surfaces: **GTO1 kwp (605.9 in v1 vs 818.33 in v2)**,
any other kWp/tariff gaps, and `pr_baseline` (unset — needs ~30 days
observation). This is where v2 *surpasses* v1 rather than just differing.
Scope note: **PR > 1.0 on the ShineMaster plants (SLP1/SLP2/NL1/GTO1) is NOT a
config bug** and must not be chased with config here — it is the sparse-irradiance
cadence artifact fixed at **Stage 4** (see there). Stage 2 owns kwp/tariff truth
on plants whose irradiance is already trustworthy (the cloud-model plants, or any
plant once it is on the Pi).
**Gate:** config-driven PR flags resolved (kwp/tariff), every config value
verified against the physical plant and guarded by a regression test; a residual
ShineMaster PR > 1.0 is *expected* until Stage 4 and does not block this gate.

### Stage 3 — Make v2 autonomous on GitHub
Schedule the remaining rollups (10-min snapshot + daily production — both are
**manual-only today**) and add telemetry pruning (14-day rolling raw, prune by
completeness not age, dry-run default). Still feeds nothing financial.
**Gate:** rollups run unattended and idempotent (re-run adds no duplicate rows);
`KPI_Daily` / `Alerts` never pruned; raw trims and the file stays light.

### Stage 4 — Move v2 to the Pi as the single collector
v2 becomes the one poller. **This is where the token-collision risk permanently
ends** — one login, one poll, no two collectors sharing a credential. v2 keeps
writing only `Argia_Mont_v2`; it still does **not** write ARGIA_Solar.

**This stage also fixes ShineMaster irradiance (root cause verified 2026-07-02).**
v2's daily irradiance is a trapezoidal integral of one instantaneous
`getWeatherByPlantId` reading captured per poll. On GitHub (~1 poll/hr, drops
runs) that is ~7 snapshots/day with the first landing mid-morning — so the ramp
before the first sample is missing and the sparse midday points integrate low and
*unstable* (SLP1 1-Jul: v2 3.49 vs v1 4.75 kWh/m²; GTO1 produced 1.33 **and** 3.61
on two re-runs of the *same* day). The integrator math is correct — it is starved
of samples. v1 already proves the Pi resolves this: same account, same feed, but
polled ~every 10 min → ~78 samples/day → an accurate integral. The cloud-model
plants (MEX1/MEX2) already match v1 within ~4%, which is the control that isolates
the cause. Energy is immune because `EToday` is cumulative; irradiance is not.
**Requirement (no irradiance code change):** on the Pi, poll the weather feed at
~10-min cadence from dawn — do **not** inherit GitHub's hourly schedule for
weather. Density is purely a function of poll frequency.

**Gate:** v2-on-Pi runs clean for several days; SyncRuns shows no auth/401 storms;
`KPI_Daily` keeps filling; **and** on clear, full-coverage overlap days, ShineMaster
daily irradiance for SLP1/SLP2/NL1/GTO1 lands within ~5% of v1's `DailyData`
(no PR > 1.0). **Verify** by re-running the reconcile / the v1-vs-v2 daily-irradiance
comparison over post-cutover overlap — `PR-DIVERGENCE` on the ShineMaster plants
should collapse toward `OK`.
**Reversible:** misbehaves → re-enable v1 collector, disable v2, back to Stage 0.

### Stage 5 — Cut consumption over (the actual cutover)
Confirm `DailyData_v2` (bare IMPORTRANGE from `KPI_Daily`) fills reliably and
reconciles against v1's `DailyData`. Then repoint ARGIA_Solar's financial
formulas from `DailyData` → `DailyData_v2`. **Freeze old history as static
values, append the new feed forward from a cut-over date, one overlap day to
confirm the seam.** Non-destructive — never delete-and-rename.
**Gate:** live reports keep working off the new feed; 2.3 yr history intact;
overlap day matches.

### Stage 6 — Retire the v1 writer
Only after Stage 5 is boring, and because the financials no longer read v1's
`DailyData`, comment out the v1 write path on the Pi. Pure v2.
**Gate:** one collector writing; a week of clean operation; financials unbroken.
This is the point of no easy return — it comes last, after everything above is
dull.

### Stage 7 — (optional, business decision) Upgrade financial formulas
Move ARGIA_Solar's expected-kWh / income math onto v2's *corrected* plant sizes.
**Honest flag:** if contracts/invoicing were built on v1's plant sizes, this
changes numbers mid-contract. That may be exactly right or may break agreed
continuity — **Tomasz's call, not a code decision.** Corrected sizes are safe to
*report on* in the v2 sheet long before deciding whether they should drive
invoicing.

---

## Protecting financial history (do not skip)

IMPORTRANGE is volatile — it can transiently show `Loading…` or `#REF!` on
Google's refresh cadence. An invoicing calc that reads at the wrong moment gets
a blank. Guard against it:

- **Historical rows stay frozen static values.** v1's `DailyData` already *is*
  static values — leave it frozen. Only the recent tail is live IMPORTRANGE.
- **Periodically freeze the settled prior-month tail to values** (manual monthly
  or an Apps Script inside ARGIA_Solar). This keeps the permanent record immune
  to IMPORTRANGE hiccups — and still honors "the Pi never writes ARGIA_Solar,"
  because the freeze is an in-sheet action.

---

## Known gotchas

- **Bare IMPORTRANGE, never QUERY-wrapped.** QUERY stringifies the dates and
  breaks date matching. Bare IMPORTRANGE preserves datetime types.
- **Non-destructive repoint only.** Editing the sacred sheet's formulas is
  freeze-and-append, never delete-and-rename. One overlap day is the proof.
- **Reconcile retires at cutover.** Once ARGIA_Solar reads `DailyData_v2` (which
  *is* `KPI_Daily` via IMPORTRANGE), reconciling the two becomes tautological.
  Stage 1's value is spent during the parallel window — that's why it's now.

---

## Open items that are decisions, not code

- **GTO1 physical capacity: 605.9 or 818.33 kWp today?** The reconcile will flag
  GTO1 PR-DIVERGENCE every overlapping day until this is settled against the
  plant. Blocks Stage 2, not Stage 1.
- **Stage 7 invoicing** — whether corrected sizes should drive contracts.

---

## Explicitly out of scope here

- **Alert engine.** Thresholds tab + Alerts state machine exist, but there is
  **no evaluator** — nothing computes a metric and opens an alert (verified:
  `thresholds.py` is loader-only, `alerts_state.py` is store-only, no script or
  workflow wires them). So a dead-but-online inverter (e.g. MEX1 Inverter 2,
  0 kWh while siblings produced, 2026-07-02) fires **no alert today**. The rule
  set *would* catch it once built — via `inverter_relative` (0 / ~79 kW = 0.0 <
  0.70 → CRITICAL), not `inverter_offline` (inv2 reports ONLINE). Design note
  for when built: judge inverter health by **production during daylight, not the
  vendor's online flag.** That real breach is the ideal first test fixture.
  This is Phase 2, deliberately after the feed is proven.
- Email/PDF reporting, Pi crontab specifics, dashboard.

---

## Current state snapshot (verified 2026-07-02)

- 6 active plants: SLP1, SLP2, GTO1, MEX1, NL1, MEX2. Inactive: MEX3, NL2, QRO1,
  GTO2 (absent from `KPI_Daily` by design).
- `kpi_eod` scheduled daily 06:00 MX (writes yesterday). `telemetry_5m` every
  5 min in daylight (~1 effective sample/hr — GitHub drops scheduled runs under
  load; harmless).
- 10-min snapshot + daily rollup: **manual-only** (Stage 3 work).
- Stage 1 reconcile: on `main`, CI green, waiting on ARGIA_Solar read grant +
  overlap days.
