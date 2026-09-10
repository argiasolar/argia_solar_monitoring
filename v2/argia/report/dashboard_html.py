"""Self-contained HTML dashboard renderer.

Takes the SAME rows the Dashboard_Plant / Dashboard_Inverter tabs hold and
renders one standalone HTML file: plant selector, day selector, scorecards,
temperature / production gauges, per-inverter status table, and the intraday
stacked chart with the theoretical overlay.

Design constraints (deliberate):
* ONE file, data embedded as JSON — no fetch(), no CORS/cookie issues on
  authenticated hosts (storage.cloud.google.com), trivially testable.
* Chart.js from the cdnjs CDN is the only external resource.
* Pure rendering — this module does no I/O. The publish script feeds it and
  ships the result, so the renderer is unit-testable end to end.
"""

from __future__ import annotations

import json
from typing import List

# Only these fields are embedded — keeps the payload small and the contract
# explicit. Adding a field to the page starts here.
PLANT_FIELDS = [
    "date_mx", "hour_label", "plant_key", "customer", "kwp_dc",
    "tariff_mxn_per_kwh", "data_start",
    "total_kwh", "theoretical_kwh", "cloud_cover_pct",
    "inverters_total", "inverters_reporting", "inverters_faulted",
]
INVERTER_FIELDS = [
    "date_mx", "hour_label", "plant_key", "inverter_sn", "inverter_label",
    "energy_kwh", "temperature_c", "status", "status_reason",
    "est_loss_kwh", "fault_events",
]

# The ARGIA wordmark — "ARGIA / Smart Energy Solutions" (transparent PNG,
# 764x120, ~4 KiB) embedded so the page stays a single self-contained file.
# Show it at 34px or taller: the three-line tagline blurs below that.
LOGO_B64 = "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"

STATUS_COLORS = {
    "ONLINE": ("#E1F5EE", "#085041"),
    "UNDERPERFORMING": ("#FAEEDA", "#633806"),
    "FAULT": ("#FCEBEB", "#791F1F"),
    "DERATED": ("#FAEEDA", "#633806"),
    "OFFLINE": ("#F1EFE8", "#444441"),
    "RECOVERED": ("#E7EEF7", "#2F5C8F"),
    "IDLE_NIGHT": ("#F1EFE8", "#888780"),
    "NO_DATA": ("#F1EFE8", "#B4B2A9"),
}


def _slim(rows: List[dict], fields: List[str]) -> List[dict]:
    return [{f: r.get(f) for f in fields} for r in rows]


def _embed_json(obj) -> str:
    """JSON safe for inline <script> embedding ('</script>' cannot occur)."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def _template() -> str:
    return _TEMPLATE.replace("__LOGO__", LOGO_B64)


def render(plant_rows: List[dict], inverter_rows: List[dict],
           generated_at: str, active_plants: List[str] | None = None) -> str:
    """Render the dashboard. Rows are the Dashboard tab dicts (or a superset).

    active_plants: plant_keys to include, in display order. Defaults to the
    distinct plants present in plant_rows with any production, sorted.
    """
    if active_plants is None:
        seen = {}
        for r in plant_rows:
            pk = r.get("plant_key")
            if pk and (r.get("total_kwh") or 0) > 0:
                seen[pk] = True
        active_plants = sorted(seen)

    plant_rows = [r for r in plant_rows if r.get("plant_key") in active_plants]
    inverter_rows = [r for r in inverter_rows
                     if r.get("plant_key") in active_plants]

    payload = {
        "generated_at": generated_at,
        "plants": active_plants,
        "customers": {r["plant_key"]: r.get("customer") or r["plant_key"]
                      for r in plant_rows},
        "plant_rows": _slim(plant_rows, PLANT_FIELDS),
        "inverter_rows": _slim(inverter_rows, INVERTER_FIELDS),
        "status_colors": STATUS_COLORS,
    }
    return _template().replace("__DATA__", _embed_json(payload))


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARGIA — plant dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
  body { margin: 0; background: #f4f3ef; color: #1a1a19; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 20px 16px 40px; }
  header { display: flex; justify-content: space-between; align-items: center;
           flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0; letter-spacing: .3px; }
  .sub { font-size: 12px; color: #6b6a64; }
  .sn { display: block; font-size: 10.5px; color: #9a998f; }
  select { font-size: 14px; padding: 7px 10px; border: 1px solid #c9c8c0;
           border-radius: 8px; background: #fff; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 12px; margin-bottom: 14px; }
  .card { background: #fff; border-radius: 10px; padding: 14px 16px;
          border: 1px solid #e4e3dc; }
  .card .lbl { font-size: 12px; color: #6b6a64; }
  .card .val { font-size: 24px; font-weight: 600; margin-top: 2px; }
  .card .val small { font-size: 12px; font-weight: 400; color: #6b6a64; }
  .row { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
         gap: 12px; margin-bottom: 14px; }
  .panel { background: #fff; border-radius: 10px; border: 1px solid #e4e3dc;
           padding: 14px 16px; }
  .panel h2 { font-size: 13px; font-weight: 600; margin: 0 0 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-weight: 400; color: #8a897f; padding: 4px 6px; }
  td { border-top: 1px solid #eceae2; padding: 7px 6px; }
  .badge { padding: 2px 10px; border-radius: 10px; font-size: 12px;
           white-space: nowrap; }
  .chartbox { position: relative; width: 100%; height: 280px; }
  .note { font-size: 12px; color: #9a6a1f; background: #faeeda;
          border-radius: 8px; padding: 8px 12px; margin-bottom: 12px;
          display: none; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div class="wrap">
  <header style="display:block;">
    <div style="display:flex; align-items:center; justify-content:space-between;
                gap:14px; margin-bottom:12px;">
      <span style="font-size:16px; font-weight:600; letter-spacing:3.5px;
                   color:#3c3b37; white-space:nowrap;">PERFORMANCE&nbsp;REPORT</span>
      <img src="data:image/png;base64,__LOGO__" alt="ARGIA — Smart Energy Solutions"
           style="height:34px; display:block;">
    </div>
    <div style="display:flex; align-items:center; justify-content:space-between;
                gap:10px; flex-wrap:wrap;">
      <div style="display:flex; gap:8px;">
        <select id="plantSel" aria-label="Plant"></select>
        <select id="daySel" aria-label="Day"></select>
      </div>
      <div id="genat" style="white-space:nowrap; font-size:14px;
           color:#4a4a45;"></div>
    </div>
  </header>

  <div class="note" id="gapNote" style="background:#fcebeb; color:#791f1f;">
    </div>

  <div class="note" id="todayNote">Selected day is still running: production and
    expected are pro-rated to the last complete hour (Mexico City time); the
    expected value is a live estimate (&plusmn;10%) until the end-of-day KPI is
    stamped tonight.</div>

  <div class="cards">
    <div class="card"><div class="lbl">Production</div>
      <div class="val" id="cProd">–</div></div>
    <div class="card"><div class="lbl" id="cExpLbl">Expected</div>
      <div class="val" id="cExp">–</div></div>
    <div class="card"><div class="lbl">Production vs expected</div>
      <div class="val" id="cPct">–</div></div>
    <div class="card"><div class="lbl">Inverters with issues</div>
      <div class="val" id="cFault">–</div></div>
    <div class="card"><div class="lbl" id="cLossLbl">Est. loss (unavailability)</div>
      <div class="val" id="cLoss">–</div></div>
  </div>

  <div class="row">
    <div class="panel">
      <h2 id="g1Title">Hottest inverter</h2>
      <svg viewBox="0 0 180 100" width="100%" style="max-width:210px;display:block;margin:auto" role="img" aria-label="Gauge">
        <path d="M20 92 A70 70 0 0 1 160 92" fill="none" stroke="#e4e3dc" stroke-width="12" stroke-linecap="round"/>
        <path id="gTempArc" d="" fill="none" stroke="#0ca30c" stroke-width="12" stroke-linecap="round"/>
        <text id="gTempVal" x="90" y="72" text-anchor="middle" font-size="24" font-weight="600" fill="#1a1a19">–</text>
        <text id="g1Legend" x="90" y="94" text-anchor="middle" font-size="10" fill="#8a897f">green &lt;60 &middot; amber 60&ndash;70 &middot; red &gt;70 &deg;C</text>
      </svg>
    </div>
    <div class="panel">
      <h2>Production vs expected</h2>
      <svg viewBox="0 0 180 100" width="100%" style="max-width:210px;display:block;margin:auto" role="img" aria-label="Production gauge">
        <path d="M20 92 A70 70 0 0 1 160 92" fill="none" stroke="#e4e3dc" stroke-width="12" stroke-linecap="round"/>
        <path id="gPctArc" d="" fill="none" stroke="#0ca30c" stroke-width="12" stroke-linecap="round"/>
        <text id="gPctVal" x="90" y="72" text-anchor="middle" font-size="24" font-weight="600" fill="#1a1a19">–</text>
        <text x="90" y="94" text-anchor="middle" font-size="10" fill="#8a897f">red &lt;70 &middot; amber 70&ndash;90 &middot; green &gt;90 %</text>
      </svg>
    </div>
  </div>

  <div class="panel" style="margin-bottom:14px;">
    <h2 id="tblTitle">Inverters — consolidated status</h2>
    <table>
      <thead id="tblHead"></thead>
      <tbody id="tblBody"></tbody>
    </table>
  </div>

  <div class="panel">
    <h2 id="chartTitle">Intraday production &middot; 60-min buckets</h2>
    <div class="chartbox"><canvas id="chart" role="img"
      aria-label="Stacked hourly production per inverter with theoretical line"></canvas></div>
  </div>

  <div class="panel" id="panel2" style="margin-top:14px; display:none">
    <h2 id="chart2Title">Production vs expected &middot; by plant</h2>
    <div class="chartbox"><canvas id="chart2" role="img"
      aria-label="Production versus expected by plant"></canvas></div>
  </div>

  <div class="panel" id="audit" style="margin-top:14px;">
    <details>
      <summary style="font-size:13px; font-weight:600; cursor:pointer;">
        How these numbers are calculated (audit)</summary>
      <dl style="font-size:12.5px; color:#3c3b37; line-height:1.55; margin:10px 0 0;">
        <dt style="font-weight:600;">Production kWh</dt>
        <dd style="margin:0 0 8px;">From each inverter's cumulative daily
        counter (vendor <i>etoday</i>): the increase inside each 60-min
        bucket, summed across inverters. Stale post-midnight carryover
        readings are stripped before counting. Daily totals reconcile with
        the v1 system to &lt;0.1%.</dd>

        <dt style="font-weight:600;">Expected kWh</dt>
        <dd style="margin:0 0 8px;">kWp<sub>DC</sub> &times; measured
        irradiance (kWh/m&sup2;) &times; plant expected-factor. Completed
        days use the audited end-of-day KPI value, distributed across hours
        by the day's irradiance shape. The LIVE day integrates the
        instantaneous W/m&sup2; readings (trapezoidal, gaps capped at 3 h)
        &mdash; a &plusmn;10% estimate until tonight's KPI stamp &mdash; and
        both sides are pro-rated to the last complete hour (Mexico City).
        Irradiance for completed days comes from the plant's
        ShineMaster weather station: its STORED minute-scale history
        (~300 samples/day) is fetched from the logger and integrated
        trapezoidally &mdash; validated July 2026 against an independent
        weather model to &lt;1% on every plant. If that fetch fails, the
        fallback is poll-time snapshots, then a cloud-adjusted clear-sky
        model; the KPI records which source was used each day. Either
        way, Expected already reflects the actual clouds.</dd>

        <dt style="font-weight:600;">Production vs expected (%)</dt>
        <dd style="margin:0 0 8px;">Production &divide; Expected over the
        same hours. Performance vs the actual weather, not vs clear sky.
        Judged on COMPLETED hours only &mdash; the in-flight hour is
        excluded (datalogger upload offsets make it momentarily
        lopsided). On mornings where telemetry started late, the % is
        computed over COVERED HOURS only (marked &ldquo;&middot;covered
        hrs&rdquo; / &ldquo;&middot;c&rdquo;): the roll-in bucket holds
        energy whose sun was never measured, so it is excluded from both
        sides. That evening&rsquo;s KPI carries the corrected full-day
        number from the logger&rsquo;s stored history.</dd>

        <dt style="font-weight:600;">Availability (operational)</dt>
        <dd style="margin:0 0 8px;">Share of inverter-hours in a PRODUCING
        state (ONLINE / UNDERPERFORMING / DERATED), counted only in hours
        where the plant produced. An inverter that reports telemetry while
        producing 0 kWh counts as unavailable. Buckets where the
        inverter was not observed at all (collector gaps, partial polls)
        count as UNKNOWN and are excluded &mdash; not treated as
        downtime. Note: this measures
        operation; the KPI_Daily availability measures data coverage, so
        the two can differ.</dd>

        <dt style="font-weight:600;">Status &amp; Issues</dt>
        <dd style="margin:0 0 8px;">One shared classifier for every view:
        FAULT = vendor fault code/flag; OFFLINE = silent or 0 kWh while
        peers produce; UNDERPERFORMING = below 85% of peers per installed
        kW (leave-one-out median, size-normalized); DERATED = derating flag.
        &ldquo;Issues&rdquo; counts inverters whose worst state of the day
        is any of these. Max &deg;C is the inverter&rsquo;s INTERNAL
        (electronics) temperature; amber &ge;65&thinsp;&deg;C, red
        &ge;75&thinsp;&deg;C &mdash; the same bands the alert engine uses.
        Above ~75&thinsp;&deg;C the unit protects itself by derating, so
        heat becomes lost production.</dd>

        <dt style="font-weight:600;">Est. loss (unavailability)</dt>
        <dd style="margin:0 0 8px;">Only for FAULT/OFFLINE hours: what the
        producing peers achieved per kW &times; the unit's rated kW, minus
        what it actually produced; priced with the plant's
        tariff_mxn_per_kwh where set. Underperformance is deliberately NOT
        included, and a whole-plant outage shows no loss here (no peers to
        estimate from) &mdash; it shows in Production % instead.</dd>
      </dl>
    </details>
  </div>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  var DATA = JSON.parse(document.getElementById('data').textContent);
  var SERIES = ['#0F6E56','#5DCAA5','#3B6D11','#97C459','#085041','#1D9E75',
                '#639922','#9FE1CB'];
  var ALL = '__ALL__';
  // statuses that count as "needing attention" on the cards / Issues column
  // RECOVERED (v96): was OFFLINE/FAULT earlier today, producing now — still
  // listed, because the availability loss it took is real.
  var ISSUE_STATUSES = { FAULT: 1, OFFLINE: 1, DERATED: 1,
                         UNDERPERFORMING: 1, RECOVERED: 1 };
  var plantSel = document.getElementById('plantSel');
  var daySel = document.getElementById('daySel');
  var chart = null;
  var chart2 = null;

  function mxNow() {
    try {
      return new Date(new Date().toLocaleString('en-US',
        { timeZone: 'America/Mexico_City' }));
    } catch (e) { return new Date(); }
  }
  function mxTodayIso() {
    try {
      return new Date().toLocaleDateString('en-CA',
        { timeZone: 'America/Mexico_City' });
    } catch (e) { return new Date().toISOString().slice(0, 10); }
  }
  // On the LIVE day only, drop FUTURE buckets so forecast-timestamped rows
  // can never inflate expected. The CURRENT in-progress hour is kept: its
  // production is real and its trapezoid expected only integrates samples
  // that exist, so both sides are elapsed-matched. (Regression 2026-07-06:
  // cutting the in-progress hour hid the first data after an overnight
  // telemetry gap — tabs had 08:19 data, the page showed zeros.)
  // Completed days keep the full-day comparison.
  function cutLive(rows, day) {
    if (day !== mxTodayIso()) return rows;
    // STRICTLY before the current hour (2026-07-08): the in-flight
    // bucket is mid-birth — datalogger phase offsets mean some inverters
    // trail by one sample at the boundary, and judging that bucket
    // branded two healthy NL1 inverters OFFLINE with phantom loss. The
    // banner has always promised "last complete hour"; now the code
    // agrees.
    var h = mxNow().getHours();
    return rows.filter(function (r) {
      return parseInt(r.hour_label, 10) < h; });
  }
  function expLabel(day) {
    document.getElementById('cExpLbl').textContent =
      (day === mxTodayIso())
        ? 'Expected \u00b7 so far (' + ('0' + mxNow().getHours()).slice(-2) + 'h)'
        : 'Expected';
  }

  document.getElementById('genat').textContent =
    'generated ' + DATA.generated_at;

  var oAll = document.createElement('option');
  oAll.value = ALL; oAll.textContent = 'All plants \u00b7 portfolio';
  plantSel.appendChild(oAll);
  DATA.plants.forEach(function (p) {
    var o = document.createElement('option');
    o.value = p; o.textContent = (DATA.customers[p] || p) + ' \u00b7 ' + p;
    plantSel.appendChild(o);
  });

  var days = Array.from(new Set(DATA.plant_rows.map(function (r) {
    return r.date_mx; }))).sort();
  days.forEach(function (d) {
    var o = document.createElement('option');
    o.value = d; o.textContent = d; daySel.appendChild(o);
  });
  var maxDay = days[days.length - 1];
  // Default to TODAY (MX) when present — this is a live ops view; the
  // banner explains that today's numbers are pro-rated estimates. Falls
  // back to the newest available day (e.g. a stale offline copy).
  var todayIso = mxTodayIso();
  daySel.value = days.indexOf(todayIso) >= 0 ? todayIso : maxDay;

  // A plant whose first sample arrived well after dawn had its early
  // energy rolled into the first sampled bucket, while the gap's sun is
  // unmeasurable — the live % is then OVERSTATED. Warn, never hide.
  var LATE_START_AFTER = '06:45';
  function lateStarts(prowsAll, day) {
    if (day !== mxTodayIso()) return [];
    var seen = {};
    prowsAll.forEach(function (r) {
      if (r.date_mx === day && r.data_start) seen[r.plant_key] = r.data_start;
    });
    var out = [];
    Object.keys(seen).forEach(function (pk) {
      if (seen[pk] > LATE_START_AFTER)
        out.push({ pk: pk, from: seen[pk] });
    });
    return out.sort(function (a, b) { return a.pk < b.pk ? -1 : 1; });
  }
  // Gap mornings: the roll-in bucket holds unmeasured-sun energy, so a
  // full-day live %% is fiction. Over hours strictly AFTER it, production
  // and expected are both measured and hour-aligned — an honest partial
  // window, labeled as such. Tonight's KPI stays the full-day truth.
  function coveredPct(prows, fromHHMM) {
    var startH = parseInt(fromHHMM, 10);
    var prod = 0, theo = 0;
    prows.forEach(function (r) {
      if (parseInt(r.hour_label, 10) > startH) {
        prod += r.total_kwh || 0;
        theo += r.theoretical_kwh || 0;
      }
    });
    return theo > 0 ? prod / theo * 100 : null;
  }
  function lateSetOf(late) {
    var m = {};
    late.forEach(function (l) { m[l.pk] = l.from; });
    return m;
  }
  function setGapNote(late) {
    var el = document.getElementById('gapNote');
    if (!late.length) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    el.textContent = '\u26a0 Telemetry started late today for ' +
      late.map(function (l) { return l.pk + ' (from ' + l.from + ')'; })
          .join(', ') + ' \u2014 energy produced during the gap rolled ' +
      'into the first sampled hour, but the sun for those hours could not ' +
      'be measured. Production is real; the % is overstated until ' +
      'coverage builds. Tonight\u2019s KPI corrects the full-day number.';
  }

  function lossText(kwh, tariff) {
    if (!kwh || kwh < 0.5) return '\u2013';
    if (tariff) return '$' + fmt(kwh * tariff) + ' <small>MXN \u00b7 ' +
      fmt(kwh) + ' kWh</small>';
    return fmt(kwh) + ' <small>kWh (set tariff_mxn_per_kwh for MXN)</small>';
  }

  function fmt(n, dec) {
    if (n === null || n === undefined || isNaN(n)) return '\u2013';
    return Number(n).toLocaleString('en-US',
      { maximumFractionDigits: dec === undefined ? 0 : dec });
  }

  function invSortKey(label, sn) {
    // "Inverter 12" -> 12; unnumbered labels sort after, alphabetically
    var m = /(\\d+)\\s*$/.exec(label || '');
    return [m ? parseInt(m[1], 10) : 1e9, label || sn];
  }

  function arc(el, frac, color) {
    frac = Math.max(0, Math.min(1, frac));
    if (frac < 0.01) { el.setAttribute('d', ''); return; }
    var a = Math.PI * (1 - frac);
    var x = 90 + 70 * Math.cos(a), y = 92 - 70 * Math.sin(a);
    el.setAttribute('d', 'M20 92 A70 70 0 0 1 ' +
      x.toFixed(2) + ' ' + y.toFixed(2));
    el.setAttribute('stroke', color);
  }

  function setCards(prod, theo, pct, faulted, ntot, covered) {
    document.getElementById('cProd').innerHTML =
      fmt(prod) + ' <small>kWh</small>';
    document.getElementById('cExp').innerHTML =
      fmt(theo) + ' <small>kWh</small>';
    document.getElementById('cPct').innerHTML =
      pct === null ? '\u2013'
        : fmt(pct) + '%' + (covered
          ? ' <small>\u00b7 covered hrs</small>' : '');
    document.getElementById('cFault').innerHTML =
      fmt(faulted) + ' <small>of ' + fmt(ntot) + '</small>';
  }

  function setGauges(maxTemp, pct) {
    var tCol = maxTemp === null ? '#c9c8c0'
      : maxTemp > 70 ? '#d03b3b' : maxTemp > 60 ? '#fab219' : '#0ca30c';
    arc(document.getElementById('gTempArc'),
        maxTemp === null ? 0 : maxTemp / 100, tCol);
    document.getElementById('gTempVal').textContent =
      maxTemp === null ? '\u2013' : fmt(maxTemp, 0) + '\u00b0C';
    var pCol = pct === null ? '#c9c8c0'
      : pct < 70 ? '#d03b3b' : pct < 90 ? '#fab219' : '#0ca30c';
    arc(document.getElementById('gPctArc'),
        pct === null ? 0 : Math.min(pct, 120) / 120, pCol);
    document.getElementById('gPctVal').textContent =
      pct === null ? '\u2013' : fmt(pct) + '%';
  }

  function chartDefaults(cfg) {
    cfg.options = cfg.options || {};
    cfg.options.devicePixelRatio =
      Math.max(window.devicePixelRatio || 1, 2);   // crisp on scaled displays
    cfg.options.responsive = true;
    cfg.options.maintainAspectRatio = false;
    return cfg;
  }
  function newChart(cfg) {
    if (chart) chart.destroy();
    chart = new Chart(document.getElementById('chart'), chartDefaults(cfg));
  }
  function newChart2(cfg) {
    if (chart2) chart2.destroy();
    chart2 = new Chart(document.getElementById('chart2'), chartDefaults(cfg));
  }

  var AVAIL_OK_SET = { ONLINE: 1, UNDERPERFORMING: 1, DERATED: 1 };
  // Availability counts only ASSESSABLE buckets. NO_DATA (collector gap,
  // partial poll) is UNKNOWN — counting it as downtime punished plants
  // for the collector's absences: 2026-07-06, MEX1 read 80% availability
  // while producing 130% of expected with zero issues.
  var AVAIL_ASSESS = { ONLINE: 1, UNDERPERFORMING: 1, DERATED: 1,
                       FAULT: 1, OFFLINE: 1 };

  // Fault events for the day, from UNCUT rows: the in-flight-hour rule
  // (cutLive) protects CLASSIFICATION from mid-birth buckets, but a raw
  // vendor fault is a fact, not a judgement — it must show regardless of
  // which hour it happened in (JFM5D8900B FT=302 lesson, 2026-07-09).
  function faultEventsByInv(irowsAllDay) {
    var ev = {};
    irowsAllDay.forEach(function (r) {
      if (r.fault_events) {
        (ev[r.inverter_sn] = ev[r.inverter_sn] || []).push(r.fault_events);
      }
    });
    return ev;
  }

  // v96: consolidated status. worst = worst bucket of the day; last =
  // most recent completed bucket. If worst is hard-down (OFFLINE/FAULT)
  // but the inverter is producing in its latest bucket, it RECOVERED —
  // show that, not a stale OFFLINE. Mirrors argia.analytics.status.
  // display_status (the tested reference).
  var PROD_NOW = { ONLINE: 1, UNDERPERFORMING: 1, DERATED: 1 };
  var HARD_DOWN = { OFFLINE: 1, FAULT: 1 };
  function displayStatus(worst, last) {
    return (HARD_DOWN[worst] && PROD_NOW[last]) ? 'RECOVERED' : worst;
  }

  function aggInverters(irows, producingHours) {
    var agg = {};
    irows.forEach(function (r) {
      var a = agg[r.inverter_sn] || (agg[r.inverter_sn] = {
        sn: r.inverter_sn, label: r.inverter_label ? r.inverter_label + ' (' + r.inverter_sn + ')' : r.inverter_sn,
        kwh: 0, temp: null, status: 'NO_DATA', reason: '', rank: -1,
        loss: 0, availOk: 0, availN: 0,
        lastHour: '', lastStatus: 'NO_DATA' });
      a.kwh += r.energy_kwh || 0;
      a.loss += r.est_loss_kwh || 0;
      if (producingHours && producingHours[r.hour_label]
          && AVAIL_ASSESS[r.status]) {
        a.availN += 1;
        if (AVAIL_OK_SET[r.status]) a.availOk += 1;
      }
      if (r.temperature_c !== null && r.temperature_c !== undefined)
        a.temp = Math.max(a.temp === null ? -1e9 : a.temp, r.temperature_c);
      var rank = { FAULT: 5, OFFLINE: 4, DERATED: 3, UNDERPERFORMING: 2,
                   ONLINE: 1, IDLE_NIGHT: 0, NO_DATA: 0 }[r.status] || 0;
      if (rank > a.rank) { a.rank = rank; a.status = r.status;
                           a.reason = r.status_reason || ''; }
      if ((r.hour_label || '') >= a.lastHour) {
        a.lastHour = r.hour_label || ''; a.lastStatus = r.status;
      }
    });
    var list = Object.keys(agg).map(function (k) { return agg[k]; });
    list.forEach(function (a) {
      var disp = displayStatus(a.status, a.lastStatus);
      if (disp === 'RECOVERED' && a.status !== 'RECOVERED') {
        a.reason = 'recovered \u2014 was ' + a.status.toLowerCase()
                 + ' earlier today';
        a.status = 'RECOVERED';
      }
    });
    list.sort(function (a, b) {
      var ka = invSortKey(a.label, a.sn), kb = invSortKey(b.label, b.sn);
      return ka[0] - kb[0] || (ka[1] < kb[1] ? -1 : 1);
    });
    return list;
  }

  function drawPlant(pk, day) {
    document.getElementById('panel2').style.display = 'none';
    expLabel(day);
    var late = lateStarts(
      DATA.plant_rows.filter(function (r) { return r.plant_key === pk; }),
      day);
    setGapNote(late);
    var gapDay = !!lateSetOf(late)[pk];
    var prows = cutLive(DATA.plant_rows.filter(function (r) {
      return r.plant_key === pk && r.date_mx === day; }), day);
    var irowsAllDay = DATA.inverter_rows.filter(function (r) {
      return r.plant_key === pk && r.date_mx === day; });
    var faultEv = faultEventsByInv(irowsAllDay);
    var irows = cutLive(irowsAllDay, day);

    var prod = 0, theo = 0, faulted = 0, ntot = 0;
    prows.forEach(function (r) {
      prod += r.total_kwh || 0; theo += r.theoretical_kwh || 0;
      faulted = Math.max(faulted, r.inverters_faulted || 0);
      ntot = Math.max(ntot, r.inverters_total || 0);
    });
    var pct = theo > 0 ? prod / theo * 100 : null;
    // Gap morning (2026-07-08): unmeasured sun makes the full-day live
    // % a lie. Compute over covered hours instead (after the roll-in
    // bucket); tonight's KPI carries the corrected full-day number.
    var covered = false;
    if (gapDay) {
      pct = coveredPct(prows, lateSetOf(late)[pk]);
      covered = pct !== null;
    }
    var producingHours = {};
    prows.forEach(function (r) {
      if ((r.total_kwh || 0) > 0) producingHours[r.hour_label] = 1;
    });
    var tariff = null;
    prows.forEach(function (r) {
      if (r.tariff_mxn_per_kwh) tariff = r.tariff_mxn_per_kwh;
    });
    var invs = aggInverters(irows, producingHours);
    var plantLoss = 0;
    invs.forEach(function (a) { plantLoss += a.loss; });
    document.getElementById('cLoss').innerHTML =
      lossText(plantLoss, tariff);
    var issues = invs.filter(function (a) {
      return ISSUE_STATUSES[a.status]; }).length;
    setCards(prod, theo, pct, issues, ntot, covered);

    var maxTemp = null;
    invs.forEach(function (a) {
      if (a.temp !== null)
        maxTemp = Math.max(maxTemp === null ? -1e9 : maxTemp, a.temp);
    });
    document.getElementById('g1Title').textContent = 'Hottest inverter';
    document.getElementById('g1Legend').textContent =
      'green <60 \u00b7 amber 60\u201370 \u00b7 red >70 \u00b0C';
    setGauges(maxTemp, pct);

    document.getElementById('tblTitle').textContent =
      'Inverters \u2014 consolidated status';
    document.getElementById('tblHead').innerHTML =
      '<tr><th>Inverter</th><th class="num">kWh</th>' +
      '<th class="num">Avail</th><th class="num">Loss</th>' +
      '<th class="num">Max \u00b0C</th><th>Status</th><th>Reason</th></tr>';
    var body = document.getElementById('tblBody');
    body.innerHTML = '';
    invs.forEach(function (a) {
      var c = DATA.status_colors[a.status] || ['#eee', '#444'];
      var tr = document.createElement('tr');
      var av = a.availN > 0 ? Math.round(100 * a.availOk / a.availN) : null;
      // Temperature speaks the alert engine's language (amber >=65,
      // red >=75 deg C) and explains itself — a red gauge over a mute
      // table row was unanswerable (user question, 2026-07-07).
      var tCol = a.temp === null ? '#6b6a64'
        : a.temp >= 75 ? '#a32d2d' : a.temp >= 65 ? '#854f0b' : '#1a1a19';
      var tNote = a.temp !== null && a.temp >= 65
        ? ((a.temp >= 75 ? 'hot: derating likely' : 'running hot')
           + ' \u2014 check cooling/heatsink (derates \u226575\u00b0C)')
        : '';
      var reasonTxt = a.reason || '';
      if (tNote) reasonTxt = reasonTxt
        ? reasonTxt + ' \u00b7 ' + tNote : tNote;
      if (faultEv[a.sn]) {
        var evTxt = '\u26a1 fault today: ' + faultEv[a.sn].join('; ');
        reasonTxt = reasonTxt ? reasonTxt + ' \u00b7 ' + evTxt : evTxt;
      }
      var avCol = av === null ? '#6b6a64'
        : av < 90 ? '#a32d2d' : av < 98 ? '#854f0b' : '#0f6e56';
      var lossCell = a.loss < 0.5 ? '\u2013'
        : (tariff ? '$' + fmt(a.loss * tariff) : fmt(a.loss) + ' kWh');
      tr.innerHTML = '<td>' + a.label +
        '<span class="sn">' + a.sn + '</span></td>' +
        '<td class="num">' + fmt(a.kwh) + '</td>' +
        '<td class="num" style="color:' + avCol + '">' +
        (av === null ? '\u2013' : av + '%') + '</td>' +
        '<td class="num" style="color:' + (a.loss >= 0.5 ? '#a32d2d' : '#6b6a64') + '">' +
        lossCell + '</td>' +
        '<td class="num" style="font-weight:600;color:' + tCol + '">' +
        (a.temp === null ? '\u2013' : fmt(a.temp, 0)) + '</td>' +
        '<td><span class="badge" style="background:' + c[0] + ';color:' +
        c[1] + '">' + a.status + '</span></td>' +
        '<td style="color:#6b6a64">' + reasonTxt + '</td>';
      body.appendChild(tr);
    });

    var hours = Array.from(new Set(prows.map(function (r) {
      return r.hour_label; }))).sort();
    var datasets = invs.map(function (a, i) {
      var by = {};
      irows.forEach(function (r) {
        if (r.inverter_sn === a.sn)
          by[r.hour_label] = (by[r.hour_label] || 0) + (r.energy_kwh || 0);
      });
      return { type: 'bar', label: a.label, stack: 'p', order: 2,
               backgroundColor: SERIES[i % SERIES.length], borderRadius: 2,
               data: hours.map(function (h) {
                 return Math.round((by[h] || 0) * 10) / 10; }) };
    });
    var theoBy = {}, cloudBy = {};
    prows.forEach(function (r) {
      theoBy[r.hour_label] = r.theoretical_kwh;
      cloudBy[r.hour_label] = r.cloud_cover_pct;
    });
    datasets.push({ type: 'line', label: 'Theoretical', order: 1,
      data: hours.map(function (h) {
        return Math.round((theoBy[h] || 0) * 10) / 10; }),
      borderColor: '#888780', borderDash: [6, 4], borderWidth: 2,
      pointRadius: 0, tension: 0.35, yAxisID: 'y' });
    datasets.push({ type: 'line', label: 'Cloud cover %', order: 0,
      data: hours.map(function (h) {
        var v = cloudBy[h];
        return (v === null || v === undefined) ? null : Math.round(v); }),
      borderColor: '#b5d4f4', backgroundColor: 'rgba(181,212,244,0.25)',
      borderWidth: 2, pointRadius: 0, tension: 0.3, fill: true,
      spanGaps: true, yAxisID: 'y1' });

    document.getElementById('chartTitle').textContent =
      'Intraday production \u00b7 60-min buckets';
    newChart({
      data: { labels: hours, datasets: datasets },
      options: {
        plugins: { legend: { position: 'bottom',
                             labels: { boxWidth: 10, font: { size: 11 } } },
                   tooltip: { mode: 'index' } },
        scales: {
          x: { stacked: true, grid: { display: false } },
          y: { stacked: true, title: { display: true, text: 'kWh' } },
          y1: { position: 'right', min: 0, max: 100,
                grid: { drawOnChartArea: false },
                title: { display: true, text: 'cloud %' } } } }
    });
  }

  function drawPortfolio(day) {
    document.getElementById('panel2').style.display = '';
    expLabel(day);
    var late = lateStarts(DATA.plant_rows, day);
    setGapNote(late);
    var lateSet = lateSetOf(late);
    var perPlant = DATA.plants.map(function (pk) {
      var prows = cutLive(DATA.plant_rows.filter(function (r) {
        return r.plant_key === pk && r.date_mx === day; }), day);
      var irows = cutLive(DATA.inverter_rows.filter(function (r) {
        return r.plant_key === pk && r.date_mx === day; }), day);
      var prod = 0, theo = 0, faulted = 0, ntot = 0, kwp = 0,
          rep = 0, tot = 0;
      prows.forEach(function (r) {
        prod += r.total_kwh || 0; theo += r.theoretical_kwh || 0;
        faulted = Math.max(faulted, r.inverters_faulted || 0);
        ntot = Math.max(ntot, r.inverters_total || 0);
        kwp = Math.max(kwp, r.kwp_dc || 0);
      });
      // OPERATIONAL availability (2026-07-05 SAG lesson): an inverter that
      // reports telemetry but produces nothing is NOT available. Within
      // buckets where the plant produced, an inverter counts available when
      // its status is a producing state (ONLINE / UNDERPERFORMING /
      // DERATED); FAULT, OFFLINE or silence count unavailable. Dawn and
      // fleet-wide data gaps (no production recorded) stay excluded.
      var producing = {};
      prows.forEach(function (r) {
        if ((r.total_kwh || 0) > 0) producing[r.hour_label] = 1;
      });
      var t = null;
      var worst = {}, lastSt = {}, lastHr = {};
      var AVAIL_OK = { ONLINE: 1, UNDERPERFORMING: 1, DERATED: 1 };
      irows.forEach(function (r) {
        if (producing[r.hour_label] && AVAIL_ASSESS[r.status]) {
          tot += 1;
          if (AVAIL_OK[r.status]) rep += 1;
        }
        if (r.temperature_c !== null && r.temperature_c !== undefined)
          t = Math.max(t === null ? -1e9 : t, r.temperature_c);
        var rank = { FAULT: 5, OFFLINE: 4, DERATED: 3, UNDERPERFORMING: 2,
                     ONLINE: 1, IDLE_NIGHT: 0, NO_DATA: 0 }[r.status] || 0;
        var w = worst[r.inverter_sn];
        if (!w || rank > w.rank) worst[r.inverter_sn] =
          { rank: rank, status: r.status };
        if ((r.hour_label || '') >= (lastHr[r.inverter_sn] || '')) {
          lastHr[r.inverter_sn] = r.hour_label || '';
          lastSt[r.inverter_sn] = r.status;
        }
      });
      // v96: fold recovery in — a hard-down inverter now producing is
      // RECOVERED (still an issue, but not a HARD/red one).
      Object.keys(worst).forEach(function (sn) {
        worst[sn].status = displayStatus(worst[sn].status, lastSt[sn]);
      });
      var issues = 0, hardIssues = 0;
      Object.keys(worst).forEach(function (sn) {
        if (ISSUE_STATUSES[worst[sn].status]) issues += 1;
        if (worst[sn].status === 'FAULT' || worst[sn].status === 'OFFLINE')
          hardIssues += 1;
      });
      var tariff = null, loss = 0;
      prows.forEach(function (r) {
        if (r.tariff_mxn_per_kwh) tariff = r.tariff_mxn_per_kwh;
      });
      irows.forEach(function (r) { loss += r.est_loss_kwh || 0; });
      return { pk: pk, customer: DATA.customers[pk] || pk, prod: prod,
               lossKwh: loss, tariff: tariff,
               theo: theo,
               pct: lateSet[pk] ? coveredPct(prows, lateSet[pk])
                                : (theo > 0 ? prod / theo * 100 : null),
               covered: !!lateSet[pk],
               faulted: faulted, ntot: ntot, temp: t, kwp: kwp,
               issues: issues, hardIssues: hardIssues,
               avail: tot > 0 ? rep / tot * 100 : null,
               availOk: rep, availN: tot,
               prows: prows };
    });

    var prod = 0, theo = 0, issues = 0, ntot = 0;
    perPlant.forEach(function (p) {
      prod += p.prod; theo += p.theo; issues += p.issues; ntot += p.ntot;
    });
    var pct = null;
    if (!late.length) {
      pct = theo > 0 ? prod / theo * 100 : null;
    } else {
      // fleet %% over each plant's own covered window
      var cp = 0, ct = 0;
      perPlant.forEach(function (p) {
        var from = lateSet[p.pk] || '00:00';
        var startH = parseInt(from, 10);
        p.prows.forEach(function (r) {
          if (!lateSet[p.pk] || parseInt(r.hour_label, 10) > startH) {
            cp += r.total_kwh || 0; ct += r.theoretical_kwh || 0;
          }
        });
      });
      pct = ct > 0 ? cp / ct * 100 : null;
    }
    setCards(prod, theo, pct, issues, ntot, !!late.length);
    var lossKwh = 0, lossMxn = 0, allTariffed = true;
    perPlant.forEach(function (p) {
      lossKwh += p.lossKwh;
      if (p.tariff) lossMxn += p.lossKwh * p.tariff;
      else if (p.lossKwh >= 0.5) allTariffed = false;
    });
    document.getElementById('cLoss').innerHTML =
      lossKwh < 0.5 ? '\u2013'
      : allTariffed ? ('$' + fmt(lossMxn) + ' <small>MXN \u00b7 ' +
                       fmt(lossKwh) + ' kWh</small>')
      : (fmt(lossKwh) + ' <small>kWh (tariffs incomplete)</small>');

    // Fleet availability: reporting/expected inverters over DAYLIGHT buckets
    // (bucket-level ratio; KPI_Daily's gap-clustered availability remains
    // the audit-grade number and can differ slightly on gappy days).
    var repSum = 0, totSum = 0;
    perPlant.forEach(function (p) {
      if (p.availN > 0) { repSum += p.availOk; totSum += p.availN; }
    });
    var avail = totSum > 0 ? repSum / totSum * 100 : null;
    document.getElementById('g1Title').textContent = 'Fleet availability';
    document.getElementById('g1Legend').textContent =
      'red <90 \u00b7 amber 90\u201398 \u00b7 green \u226598 %';
    var aCol = avail === null ? '#c9c8c0'
      : avail < 90 ? '#d03b3b' : avail < 98 ? '#fab219' : '#0ca30c';
    arc(document.getElementById('gTempArc'),
        avail === null ? 0 : avail / 100, aCol);
    document.getElementById('gTempVal').textContent =
      avail === null ? '\u2013' : (Math.round(avail * 10) / 10) + '%';

    var pCol = pct === null ? '#c9c8c0'
      : pct < 70 ? '#d03b3b' : pct < 90 ? '#fab219' : '#0ca30c';
    arc(document.getElementById('gPctArc'),
        pct === null ? 0 : Math.min(pct, 120) / 120, pCol);
    document.getElementById('gPctVal').textContent =
      pct === null ? '\u2013' : Math.round(pct) + '%';

    document.getElementById('tblTitle').textContent =
      'Plants \u2014 daily summary';
    document.getElementById('tblHead').innerHTML =
      '<tr><th>Plant</th><th class="num">kWh</th>' +
      '<th class="num">Expected</th><th class="num">%</th>' +
      '<th class="num">Availability</th>' +
      '<th class="num">Issues</th><th class="num">Max \u00b0C</th></tr>';
    var body = document.getElementById('tblBody');
    body.innerHTML = '';
    perPlant.forEach(function (p) {
      var col = p.pct === null ? '#6b6a64'
        : p.pct < 70 ? '#a32d2d' : p.pct < 90 ? '#854f0b' : '#0f6e56';
      var aCol2 = p.avail === null ? '#6b6a64'
        : p.avail < 90 ? '#a32d2d' : p.avail < 98 ? '#854f0b' : '#0f6e56';
      var tr = document.createElement('tr');
      tr.innerHTML = '<td>' + p.customer + ' \u00b7 ' + p.pk +
        ' \u00b7 ' + fmt(p.kwp) + ' kWp DC</td>' +
        '<td class="num">' + fmt(p.prod) + '</td>' +
        '<td class="num">' + fmt(p.theo) + '</td>' +
        '<td class="num" style="color:' + col + ';font-weight:600">' +
        (p.pct === null ? '\u2013'
          : fmt(p.pct) + '%' + (p.covered
            ? ' <small title="over covered hours only \u2014 the '
              + 'late-start roll-in bucket is excluded">\u00b7c</small>'
            : '')) + '</td>' +
        '<td class="num" style="color:' + aCol2 + '">' +
        (p.avail === null ? '\u2013'
          : (Math.round(p.avail * 10) / 10) + '%') + '</td>' +
        '<td class="num" style="font-weight:600;color:' +
        (p.issues === 0 ? '#6b6a64'
          : p.hardIssues > 0 ? '#a32d2d' : '#854f0b') + '">' +
        (p.issues ? p.issues : '\u2013') + '</td>' +
        '<td class="num">' + (p.temp === null ? '\u2013' : fmt(p.temp, 0)) + '</td>';
      body.appendChild(tr);
    });

    // fleet hourly: production vs expected, hour by hour
    var hourAgg = {};
    perPlant.forEach(function (p) {
      p.prows.forEach(function (r) {
        var h = hourAgg[r.hour_label] ||
          (hourAgg[r.hour_label] = { prod: 0, theo: 0 });
        h.prod += r.total_kwh || 0;
        h.theo += r.theoretical_kwh || 0;
      });
    });
    var hrs = Object.keys(hourAgg).sort();
    document.getElementById('chartTitle').textContent =
      'Fleet hourly \u00b7 production vs expected';
    newChart({
      data: { labels: hrs, datasets: [
        { type: 'bar', label: 'Production kWh', order: 2,
          backgroundColor: '#1D9E75', borderRadius: 2,
          data: hrs.map(function (h) {
            return Math.round(hourAgg[h].prod); }) },
        { type: 'line', label: 'Expected kWh', order: 1,
          borderColor: '#888780', borderDash: [6, 4], borderWidth: 2,
          pointRadius: 0, tension: 0.35,
          data: hrs.map(function (h) {
            return Math.round(hourAgg[h].theo); }) }
      ] },
      options: {
        plugins: { legend: { position: 'bottom',
                             labels: { boxWidth: 10, font: { size: 11 } } },
                   tooltip: { mode: 'index' } },
        scales: { x: { grid: { display: false } },
                  y: { title: { display: true, text: 'kWh' } } } }
    });

    document.getElementById('chart2Title').textContent =
      'Production vs expected \u00b7 by plant';
    newChart2({
      data: {
        labels: perPlant.map(function (p) {
          // customer name, trimmed at ' PPA' and at the first comma,
          // so all labels fit horizontally on one row
          return (p.customer || p.pk).split(' PPA')[0].split(',')[0]; }),
        datasets: [
          { type: 'bar', label: 'Production kWh',
            backgroundColor: '#1D9E75', borderRadius: 3,
            data: perPlant.map(function (p) {
              return Math.round(p.prod); }) },
          { type: 'bar', label: 'Expected kWh',
            backgroundColor: '#D3D1C7', borderRadius: 3,
            data: perPlant.map(function (p) {
              return Math.round(p.theo); }) }
        ] },
      options: {
        plugins: { legend: { position: 'bottom',
                             labels: { boxWidth: 10, font: { size: 11 } } },
                   tooltip: { mode: 'index' } },
        scales: { x: { grid: { display: false },
                       ticks: { font: { size: 10 }, maxRotation: 0,
                                autoSkip: false } },
                  y: { title: { display: true, text: 'kWh' } } } }
    });
  }

  function draw() {
    var day = daySel.value;
    document.getElementById('todayNote').style.display =
      (day === maxDay) ? 'block' : 'none';
    if (plantSel.value === ALL) drawPortfolio(day);
    else drawPlant(plantSel.value, day);
  }

  plantSel.addEventListener('change', draw);
  daySel.addEventListener('change', draw);
  draw();
})();
</script>
</body>
</html>
"""
