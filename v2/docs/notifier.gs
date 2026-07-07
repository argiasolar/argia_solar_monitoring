/**
 * ARGIA notifier — dumb mail courier for the Argia_Mont_v2 sheet.
 *
 * Runs on a TIME-DRIVEN trigger every 5 minutes (NOT onEdit/onChange —
 * those do not fire reliably for rows written by a service account via
 * the API). Three queues + an independent staleness nag.
 *
 * RECIPIENTS (2026-07-07): four audiences, driven by the `Recipients`
 * tab in the sheet — data, not code:
 *
 *     channel      | emails                     | notes
 *     om           | om-team@argia.cz           | alerts, watchdog, digest
 *     reporting    | reports@...                | daily PDF reports
 *     shareholders | (fill when monthly built)  | monthly reports
 *     invoicing    | (fill when invoicing built)| invoice reports
 *
 * Routing philosophy — the failure modes are DELIBERATE:
 *   - safety mail (om: alerts, watchdog, nag) FAILS OPEN: channel
 *     missing/empty -> falls back to LEGACY_RECIPIENTS below. A config
 *     gap must never silence an alarm.
 *   - business mail (reporting/shareholders/invoicing) FAILS CLOSED:
 *     channel missing/empty -> row is SKIPPED (stays unsent, retried
 *     next run) and logged. A shareholder report must never leak to the
 *     wrong list.
 *
 * Report_Outbox rows may carry a `channel` column; blank means
 * 'reporting' (today's daily reports). Future monthly/invoicing jobs
 * just append rows with their channel — zero changes needed here.
 *
 * ALL decision logic lives in the tested Python pipeline. This script only
 * ships rows. Keep it dumb; if you feel like adding logic here, add it to
 * the Python side instead where it can be unit-tested.
 *
 * Install: paste over the old script, Save. Then run testChannels() once
 * from the editor: every CONFIGURED channel receives a one-line test
 * mail — that is the routing verified end-to-end.
 */

// ---- configuration ---------------------------------------------------------
var LEGACY_RECIPIENTS = 'om-team@argia.cz';  // om-channel fallback ONLY
var MAX_EMAILS_PER_RUN = 20;                 // safety valve
var SUBJECT_PREFIX = '[ARGIA]';

// ---- recipients ------------------------------------------------------------
function loadRecipients_() {
  var map = {};
  var tab = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('Recipients');
  if (!tab || tab.getLastRow() < 2) return map;
  var data = tab.getDataRange().getValues();
  for (var r = 1; r < data.length; r++) {
    var ch = String(data[r][0] || '').trim().toLowerCase();
    var emails = String(data[r][1] || '').trim();
    if (ch && emails) map[ch] = emails;
  }
  return map;
}

// Pure resolver (node-verified in the repo): '' means DO NOT SEND.
function resolveRecipients_(map, channel, failOpenFallback) {
  var v = String(map[channel] || '').trim();
  if (v) return v;
  if (failOpenFallback) return failOpenFallback;   // safety mail: fail OPEN
  Logger.log('recipients: channel "' + channel +
             '" not configured — row skipped (fail closed)');
  return '';                                       // business mail: fail CLOSED
}

// ---- entry point (attach the time trigger to this) --------------------------
function notify() {
  var rcpt = loadRecipients_();
  var sent = 0;
  sent += notifyAlerts_(MAX_EMAILS_PER_RUN - sent, rcpt);
  sent += notifyReports_(MAX_EMAILS_PER_RUN - sent, rcpt);
  sent += notifyWatchdog_(MAX_EMAILS_PER_RUN - sent, rcpt);
  sent += githubDownNag_(MAX_EMAILS_PER_RUN - sent, rcpt);
  if (sent > 0) Logger.log('notifier: sent ' + sent + ' email(s)');
}

// ---- one-time routing verification -------------------------------------------
function testChannels() {
  var rcpt = loadRecipients_();
  var channels = ['om', 'reporting', 'shareholders', 'invoicing'];
  channels.forEach(function (ch) {
    var to = resolveRecipients_(rcpt, ch, ch === 'om' ? LEGACY_RECIPIENTS : '');
    if (!to) { Logger.log('testChannels: "' + ch + '" unconfigured — skipped'); return; }
    MailApp.sendEmail(to,
      SUBJECT_PREFIX + ' channel test — ' + ch,
      'If you received this, you are on the "' + ch + '" list of the ' +
      'ARGIA notifier. No action needed.');
    Logger.log('testChannels: "' + ch + '" -> ' + to);
  });
}

// ---- watchdog rows (written by scripts/watchdog.py on failure) --------------
function notifyWatchdog_(budget, rcpt) {
  if (budget <= 0) return 0;
  var to = resolveRecipients_(rcpt, 'om', LEGACY_RECIPIENTS);
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var tab = ss.getSheetByName('Watchdog_Alerts');
  if (!tab || tab.getLastRow() < 2) return 0;
  var data = tab.getDataRange().getValues();
  var col = {};
  data[0].forEach(function (h, idx) { col[h] = idx; });
  var sent = 0;
  for (var r = 1; r < data.length && sent < budget; r++) {
    if (String(data[r][col['notified_at']] || '') !== '') continue;
    MailApp.sendEmail(to,
      SUBJECT_PREFIX + ' WATCHDOG ' + data[r][col['severity']] + ' — ' +
        data[r][col['check']],
      'Detected (UTC): ' + data[r][col['detected_utc']] + '\n\n' +
      data[r][col['detail']] + '\n\n' +
      'Check the GitHub Actions runs and the Pi job logs (~/argia_logs).');
    tab.getRange(r + 1, col['notified_at'] + 1)
       .setValue(new Date().toISOString());
    sent++;
  }
  return sent;
}

// ---- independent staleness nag ------------------------------------------------
// The Python watchdog runs in GitHub Actions and cannot detect "GitHub is
// down". This runs on GOOGLE infra: if v2 telemetry goes very stale during
// the day, nag once per gap. Threshold deliberately loose (3h) so the
// Python watchdog (90 min) always fires first when GitHub is healthy.
var GH_NAG_STALE_MIN = 180;
function githubDownNag_(budget, rcpt) {
  if (budget <= 0) return 0;
  var to = resolveRecipients_(rcpt, 'om', LEGACY_RECIPIENTS);
  var now = new Date();
  var mxHour = parseInt(Utilities.formatDate(now,
    'America/Mexico_City', 'H'), 10);
  if (mxHour < 8 || mxHour >= 21) return 0;   // daylight only
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var tele = ss.getSheetByName('Telemetry_Argia');
  if (!tele || tele.getLastRow() < 2) return 0;
  // timestamp_mx is column A (header row 1); newest is near the bottom
  var lastRows = tele.getRange(Math.max(2, tele.getLastRow() - 50), 1,
                               Math.min(51, tele.getLastRow() - 1), 1)
                     .getValues();
  var newest = null;
  lastRows.forEach(function (r) {
    var v = r[0];
    if (v instanceof Date && (!newest || v > newest)) newest = v;
  });
  if (!newest) return 0;
  var ageMin = (now - newest) / 60000 -
    (now.getTimezoneOffset() === 0 ? 360 : 0); // sheet stores MX wall time
  if (ageMin <= GH_NAG_STALE_MIN) return 0;
  var props = PropertiesService.getScriptProperties();
  var lastNag = props.getProperty('gh_nag_newest');
  if (lastNag === String(newest.getTime())) return 0;  // once per gap
  MailApp.sendEmail(to,
    SUBJECT_PREFIX + ' WATCHDOG CRITICAL — telemetry silent',
    'v2 telemetry has written nothing for ~' + Math.round(ageMin) +
    ' minutes (newest row ' + newest + ' MX).\n\n' +
    'This nag runs on Google infra, independent of GitHub AND of the ' +
    'Pi — it may be the only alarm you get if the Pi dies.');
  props.setProperty('gh_nag_newest', String(newest.getTime()));
  return 1;
}

// ---- alerts ------------------------------------------------------------------
function notifyAlerts_(budget, rcpt) {
  if (budget <= 0) return 0;
  var to = resolveRecipients_(rcpt, 'om', LEGACY_RECIPIENTS);
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var alerts = ss.getSheetByName('Alerts');
  if (!alerts) return 0;
  var ledger = ss.getSheetByName('Alert_Notifications') ||
               ss.insertSheet('Alert_Notifications');
  if (ledger.getLastRow() === 0) {
    ledger.appendRow(['alert_id', 'notified_utc']);
  }

  var known = {};
  var lv = ledger.getDataRange().getValues();
  for (var i = 1; i < lv.length; i++) known[String(lv[i][0])] = true;

  var data = alerts.getDataRange().getValues();
  if (data.length < 2) return 0;
  var col = {};
  data[0].forEach(function (h, idx) { col[h] = idx; });

  var sent = 0;
  for (var r = 1; r < data.length && sent < budget; r++) {
    var row = data[r];
    var id = String(row[col['alert_id']] || '');
    var state = String(row[col['state']] || '');
    if (!id || state !== 'OPEN' || known[id]) continue;

    var subject = SUBJECT_PREFIX + ' ' +
      String(row[col['severity']] || 'ALERT') + ' — ' +
      String(row[col['plant_key']] || '') + ' ' +
      String(row[col['metric']] || '');
    var body =
      'Alert:      ' + id + '\n' +
      'Plant:      ' + row[col['plant_key']] +
        (row[col['inverter_sn']] ? ('  inverter ' + row[col['inverter_sn']]) : '') + '\n' +
      'Metric:     ' + row[col['metric']] + '\n' +
      'Severity:   ' + row[col['severity']] + '\n' +
      'Opened UTC: ' + row[col['opened_utc']] + '\n' +
      'Value:      ' + row[col['value']] + ' (threshold ' + row[col['threshold']] + ')\n\n' +
      String(row[col['message']] || '') + '\n\n' +
      String(row[col['explanation']] || '');

    MailApp.sendEmail(to, subject, body);
    ledger.appendRow([id, new Date().toISOString()]);
    sent++;
  }
  return sent;
}

// ---- reports -------------------------------------------------------------------
function notifyReports_(budget, rcpt) {
  if (budget <= 0) return 0;
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var outbox = ss.getSheetByName('Report_Outbox');
  if (!outbox || outbox.getLastRow() < 2) return 0;

  var data = outbox.getDataRange().getValues();
  var col = {};
  data[0].forEach(function (h, idx) { col[h] = idx; });
  var notifiedCol = col['notified_at'];

  var sent = 0;
  for (var r = 1; r < data.length && sent < budget; r++) {
    var row = data[r];
    if (String(row[notifiedCol] || '') !== '') continue;

    // channel column optional; blank/legacy rows are daily reports
    var channel = (col['channel'] !== undefined)
      ? (String(row[col['channel']] || '').trim().toLowerCase() || 'reporting')
      : 'reporting';
    var to = resolveRecipients_(rcpt, channel, '');   // business: fail CLOSED
    if (!to) continue;                                // stays queued, retried

    var dateIso = String(row[col['date_iso']] || '');
    var kind = String(row[col['kind']] || '');
    var pdfId = String(row[col['pdf_file_id']] || '');
    var label = (kind === 'morning_yesterday')
      ? 'Daily performance report'
      : (kind === 'evening_today')
      ? 'Evening production report'
      : 'ARGIA report (' + kind + ')';

    var attachments = [];
    if (pdfId) {
      try {
        attachments.push(DriveApp.getFileById(pdfId).getAs('application/pdf'));
      } catch (e) {
        Logger.log('PDF fetch failed for ' + pdfId + ': ' + e);
      }
    }
    MailApp.sendEmail({
      to: to,
      subject: SUBJECT_PREFIX + ' ' + label + ' — ' + dateIso,
      body: label + ' for ' + dateIso + ' attached.\n\n' +
            (attachments.length ? '' :
             '(PDF attachment unavailable — see the Reports folder in the ' +
             'ARGIA archive Shared Drive.)'),
      attachments: attachments
    });
    // stamp IN PLACE (getDataRange row r -> sheet row r+1; 1-based col)
    outbox.getRange(r + 1, notifiedCol + 1)
          .setValue(new Date().toISOString());
    sent++;
  }
  return sent;
}
