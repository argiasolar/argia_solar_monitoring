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
    "tariff_mxn_per_kwh",
    "total_kwh", "theoretical_kwh", "cloud_cover_pct",
    "inverters_total", "inverters_reporting", "inverters_faulted",
]
INVERTER_FIELDS = [
    "date_mx", "hour_label", "plant_key", "inverter_sn", "inverter_label",
    "energy_kwh", "temperature_c", "status", "status_reason",
    "est_loss_kwh",
]

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
    return _TEMPLATE.replace("__DATA__", _embed_json(payload))


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
  <header>
    <div>
      <h1>ARGIA SOLAR — plant dashboard</h1>
      <div class="sub" id="genat"></div>
    </div>
    <div style="display:flex; gap:8px;">
      <select id="plantSel" aria-label="Plant"></select>
      <select id="daySel" aria-label="Day"></select>
    </div>
  </header>

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
  // On the LIVE day only, compare pace-vs-pace: keep buckets strictly before
  // the current MX hour so future/forecast rows can never inflate expected.
  // Completed days keep the full-day comparison (truncating them would hide
  // an afternoon outage).
  function cutLive(rows, day) {
    if (day !== mxTodayIso()) return rows;
    var h = mxNow().getHours();
    return rows.filter(function (r) {
      return parseInt(r.hour_label, 10) < h; });
  }
  function expLabel(day) {
    document.getElementById('cExpLbl').textContent =
      (day === mxTodayIso())
        ? 'Expected \u00b7 to ' + ('0' + mxNow().getHours()).slice(-2) + ':00'
        : 'Expected';
  }

  document.getElementById('genat').textContent =
    'generated ' + DATA.generated_at + ' (America/Mexico_City)';

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
    var m = /(\d+)\s*$/.exec(label || '');
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

  function setCards(prod, theo, pct, faulted, ntot) {
    document.getElementById('cProd').innerHTML =
      fmt(prod) + ' <small>kWh</small>';
    document.getElementById('cExp').innerHTML =
      fmt(theo) + ' <small>kWh</small>';
    document.getElementById('cPct').textContent =
      pct === null ? '\u2013' : fmt(pct) + '%';
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

  function aggInverters(irows, producingHours) {
    var agg = {};
    irows.forEach(function (r) {
      var a = agg[r.inverter_sn] || (agg[r.inverter_sn] = {
        sn: r.inverter_sn, label: r.inverter_label || r.inverter_sn,
        kwh: 0, temp: null, status: 'NO_DATA', reason: '', rank: -1,
        loss: 0, availOk: 0, availN: 0 });
      a.kwh += r.energy_kwh || 0;
      a.loss += r.est_loss_kwh || 0;
      if (producingHours && producingHours[r.hour_label]) {
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
    setCards(prod, theo, pct, issues, ntot);

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
      var avCol = av === null ? '#6b6a64'
        : av < 90 ? '#a32d2d' : av < 98 ? '#854f0b' : '#0f6e56';
      var lossCell = a.loss < 0.5 ? '\u2013'
        : (tariff ? '$' + fmt(a.loss * tariff) : fmt(a.loss) + ' kWh');
      tr.innerHTML = '<td>' + a.label + '</td>' +
        '<td class="num">' + fmt(a.kwh) + '</td>' +
        '<td class="num" style="color:' + avCol + '">' +
        (av === null ? '\u2013' : av + '%') + '</td>' +
        '<td class="num" style="color:' + (a.loss >= 0.5 ? '#a32d2d' : '#6b6a64') + '">' +
        lossCell + '</td>' +
        '<td class="num">' + (a.temp === null ? '\u2013' : fmt(a.temp, 0)) + '</td>' +
        '<td><span class="badge" style="background:' + c[0] + ';color:' +
        c[1] + '">' + a.status + '</span></td>' +
        '<td style="color:#6b6a64">' + a.reason + '</td>';
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
        if (producing[r.hour_label]) {
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
               theo: theo, pct: theo > 0 ? prod / theo * 100 : null,
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
    var pct = theo > 0 ? prod / theo * 100 : null;
    setCards(prod, theo, pct, issues, ntot);
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
        (p.pct === null ? '\u2013' : fmt(p.pct) + '%') + '</td>' +
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
        labels: perPlant.map(function (p) { return p.pk; }),
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
        scales: { x: { grid: { display: false } },
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
