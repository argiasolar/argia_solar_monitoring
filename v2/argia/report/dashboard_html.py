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
    "est_loss_kwh",
]

# Argia Solar logotype (transparent PNG, 851x96, ~18 KiB) embedded so the
# page stays a single self-contained file.
LOGO_B64 = "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"

STATUS_COLORS = {
    "ONLINE": ("#E1F5EE", "#085041"),
    "UNDERPERFORMING": ("#FAEEDA", "#633806"),
    "FAULT": ("#FCEBEB", "#791F1F"),
    "DERATED": ("#FAEEDA", "#633806"),
    "OFFLINE": ("#F1EFE8", "#444441"),
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
<title>Argia Solar — plant dashboard</title>
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
      <img src="data:image/png;base64,__LOGO__" alt="ARGIA SOLAR"
           style="height:28px; display:block;">
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
  var ISSUE_STATUSES = { FAULT: 1, OFFLINE: 1, DERATED: 1,
                         UNDERPERFORMING: 1 };
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

  function aggInverters(irows, producingHours) {
    var agg = {};
    irows.forEach(function (r) {
      var a = agg[r.inverter_sn] || (agg[r.inverter_sn] = {
        sn: r.inverter_sn, label: r.inverter_label || r.inverter_sn,
        kwh: 0, temp: null, status: 'NO_DATA', reason: '', rank: -1,
        loss: 0, availOk: 0, availN: 0 });
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
    });
    var list = Object.keys(agg).map(function (k) { return agg[k]; });
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
    var irows = cutLive(DATA.inverter_rows.filter(function (r) {
      return r.plant_key === pk && r.date_mx === day; }), day);

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
      var worst = {};
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
