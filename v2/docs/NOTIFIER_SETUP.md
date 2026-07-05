# E-mail notifier — one-time setup (~10 minutes)

Result: the OM team gets an e-mail for every NEW open alert (within ~5 min),
both daily report PDFs (morning performance report for yesterday, evening
production report for today) as attachments, and watchdog failures
(KPI missing / v2 telemetry stale / Pi feed stale). The script also carries
an independent staleness nag that fires even if GitHub Actions itself is
down — it runs on Google infra.

Design in one sentence: all decisions stay in the tested Python pipeline;
the Apps Script is a dumb courier that ships rows and stamps them.

## 1. Open the script editor on the LIVE sheet
Open **Argia_Mont_v2** in Google Sheets -> Extensions -> Apps Script.

## 2. Paste the script
Delete the default code, paste the full contents of `v2/docs/notifier.gs`,
and edit the top constant:

    var RECIPIENTS = 'person1@argia.cz,person2@argia.cz';

Save (Ctrl+S).

## 3. Install the time trigger
Left sidebar -> Triggers (clock icon) -> Add Trigger:
- Function: **notify**
- Event source: **Time-driven**
- Type: **Minutes timer** -> **Every 5 minutes**
Save. Google will ask you to authorize (Sheets + Drive + Mail) — approve
with YOUR account; mails are sent from it.

## 4. Test, safely
1. In the sheet, add a row to **Report_Outbox** by hand:
   date_iso `2026-07-05`, kind `test`, pdf_file_id EMPTY, html EMPTY,
   created_utc anything, notified_at EMPTY.
2. In the script editor run `notify` once manually.
3. You should receive "[ARGIA] Evening production report — 2026-07-05"
   (no attachment) and the row's notified_at gets stamped. Delete the row.
4. Alerts: next time the engine opens a real alert, mail arrives within
   5 minutes and Alert_Notifications gains a ledger row.
5. Watchdog: Actions -> "v2 Watchdog" -> Run workflow with dry_run FALSE
   on a healthy day prints "ALL OK" and sends nothing. To see a real
   mail, add a Watchdog_Alerts row by hand with empty notified_at.

## Facts worth knowing
- **Quota**: consumer Gmail 100 recipients/day, Workspace 1,500/day —
  MAX_EMAILS_PER_RUN=20 keeps a runaway impossible.
- **Idempotency**: alerts are deduplicated by alert_id in the
  Alert_Notifications ledger (separate tab because the engine REWRITES the
  Alerts tab); reports are stamped in place (Report_Outbox is append-only).
- **Honest limitation**: this .gs file lives outside the pytest safety net.
  That is why it contains no decisions — if you ever want smarter behavior
  (digests, severity filters, quiet hours), it belongs in the Python side.
