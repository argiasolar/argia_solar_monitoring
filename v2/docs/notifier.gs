/**
 * ARGIA notifier — dumb mail courier for the Argia_Mont_v2 sheet.
 *
 * Runs on a TIME-DRIVEN trigger every 5 minutes (NOT onEdit/onChange —
 * those do not fire reliably for rows written by a service account via
 * the API). Two queues:
 *
 *   1. Alerts tab (engine-owned, may be rewritten by the alert engine):
 *      any OPEN alert whose alert_id is not yet in the Alert_Notifications
 *      ledger gets ONE e-mail; the ledger row makes it idempotent forever.
 *      The ledger is a SEPARATE tab precisely because the engine rewrites
 *      Alerts — a stamp written into Alerts itself would be wiped.
 *
 *   2. Report_Outbox tab (append-only, written by report_daily.py):
 *      rows with an empty notified_at get the PDF mailed as an attachment
 *      and notified_at stamped in place (safe: nothing rewrites this tab).
 *
 * ALL decision logic lives in the tested Python pipeline. This script only
 * ships rows. Keep it dumb; if you feel like adding logic here, add it to
 * the Python side instead where it can be unit-tested.
 *
 * Install: see v2/docs/NOTIFIER_SETUP.md
 */

// ---- configuration ---------------------------------------------------------
var RECIPIENTS = 'om-team@argia.cz';       // comma-separated list
var MAX_EMAILS_PER_RUN = 20;               // safety valve
var SUBJECT_PREFIX = '[ARGIA]';

// ---- entry point (attach the time trigger to this) --------------------------
function notify() {
  var sent = 0;
  sent += notifyAlerts_(MAX_EMAILS_PER_RUN - sent);
  sent += notifyReports_(MAX_EMAILS_PER_RUN - sent);
  if (sent > 0) Logger.log('notifier: sent ' + sent + ' email(s)');
}

// ---- alerts ------------------------------------------------------------------
function notifyAlerts_(budget) {
  if (budget <= 0) return 0;
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

    MailApp.sendEmail(RECIPIENTS, subject, body);
    ledger.appendRow([id, new Date().toISOString()]);
    sent++;
  }
  return sent;
}

// ---- reports -------------------------------------------------------------------
function notifyReports_(budget) {
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

    var dateIso = String(row[col['date_iso']] || '');
    var kind = String(row[col['kind']] || '');
    var pdfId = String(row[col['pdf_file_id']] || '');
    var label = (kind === 'morning_yesterday')
      ? 'Daily performance report'
      : 'Evening production report';

    var attachments = [];
    if (pdfId) {
      try {
        attachments.push(DriveApp.getFileById(pdfId).getAs('application/pdf'));
      } catch (e) {
        Logger.log('PDF fetch failed for ' + pdfId + ': ' + e);
      }
    }
    MailApp.sendEmail({
      to: RECIPIENTS,
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
