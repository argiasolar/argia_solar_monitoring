# Maintenance tickets — structure, strategy, implementation (v226, 2026-09-07)

Tomasz: "log maintenance tickets, connect them to plants, notify the users on the ticket, track progress, comments and attachments, open → in progress → verification → resolved; send the daily warnings, but when a ticket is already open share its progress instead of repeating the warning; anything else that makes the tool useful and benchmark standard."

## What we already had (and reuse)
| Existing piece | Role in the ticket module |
|---|---|
| `alert_ledger` (`alert_key`, OPEN/RESOLVED, acute + daily engines) | **detection** — a ticket is what ARGIA does about an alert; `ticket_alert` links the two by `alert_key`, so the correlation ("is there already a ticket for this?") is one lookup, no second event pipeline |
| `argia/alerts/naming.py`, `explanations.py` | names first, codes as detail; the ticket title from an alert is "Plastic Omnium · Inverter 1: inverter running hot" |
| `ledger_mail.render_mail` (v223 morning mail) | the "In hand" section: ticket progress lines replace warnings that have an open ticket |
| `maintenance_event` (Setup) | stays what it is: the *invoicing / suppression* window (customer-caused shutdowns, deemed energy). A ticket is the O&M record; a maintenance event is the contractual one. They are not merged on purpose. |
| users.db (portal accounts, level `argia` / admin) | the technicians: assignee and followers are portal accounts; notifications go to their account e-mail |
| `emailer` (service@argia.com.mx) | ticket notifications |
| portal chrome, session login, nginx `X-Remote-User` | the `/maintenance/` app is the fourth Flask service (8514), same pattern as Setup and Ask |

## Options considered
1. **Extend `maintenance_event` into tickets** — rejected: it is an invoicing object (approval flips billable hours); mixing O&M chatter into it would put attachments and comments next to money.
2. **External CMMS (open-source or SaaS)** — rejected for now: another login, no link to `alert_key`, no names layer, no PostgreSQL; the value is exactly the integration with the monitor.
3. **A ticket module inside the portal, on the same stack** — chosen. Pure rules in `argia/maintenance/tickets.py` (tested), a small Flask app in the bundle, PostgreSQL tables, files on disk.

## Structure (Phase 1, live)
```
MAINTENANCE  (/maintenance/)
├── Open        counts (open, P1/P2, in progress, verification, over SLA), open by plant, my tickets, all open
├── New ticket  plant, inverter, title, category, priority, assignee, followers, description — or prefilled from an alert
├── Ticket      header (status, priority, SLA), actions (next status, assign, priority, follow), linked monitoring alerts,
│               "add an update" (text + attachments), the timeline, resolution (root cause, what was done, energy lost)
└── Resolved    last 90 days
```
Lifecycle `NEW → IN_PROGRESS → WAITING → VERIFICATION → RESOLVED → CLOSED` (re-open = back to IN_PROGRESS). Resolved and closed are separate: resolved = the data confirms the fix; closed = paperwork done. Priority P1–P4 with resolve targets 4 h / 24 h / 72 h / planned; a CRITICAL alert opens as P2 (P1 is a human call: plant outage, safety). Numbers `TK-NL1-0007` carry the plant. Categories: inverter (offline, derating/heat, fault, comms), PV array (string, soiling, module), electrical, metering, communication (site, vendor cloud), structural, grid/CFE, preventive, customer request, other. Root causes: equipment, installation, manufacturer/warranty, environmental, grid, customer, communication, design, unknown.

One **timeline** per ticket: created, comment, status, assign, follow, attachment, alert occurrence (actor `monitoring`), priority, resolution. Attachments (photos, PDF, CSV, XLSX, DOCX, ZIP, MP4; 15 MB each) live in `/opt/argia/tickets/<number>/` under random names, served only to internal accounts.

**Notifications**: every change mails the other participants (creator, assignee, followers) with the change, the ticket header and the link. Nobody is mailed about their own action.

**Alert ↔ ticket**: the monitoring page's open-alerts card shows the ticket behind an alert or an "open ticket" link that prefills the form; the ticket page lists the plant's open alerts to link. Once linked: the morning mail reports the ticket's progress line instead of the warning, new occurrences are counted and written to the timeline, and the alert is not re-mailed as "new".

## Tables
`ticket` (number, plant_key, inverter_sn, title, description, category, priority, status, created_by, assigned_to, created/updated/started/verification/resolved/closed_at, root_cause, resolution, lost_kwh), `ticket_follower`, `ticket_event` (ts, actor, kind, body, meta jsonb), `ticket_attachment`, `ticket_alert` (alert_key, first/last_seen, occurrences). Created by `tickets.ENSURE_SQL` on first use.

## Access
Internal accounts only (level `argia` or global admin). Customer accounts get a no-access page. Customer-visible tickets (public vs internal comments) are Phase 3.

## v227 (same day)
- **Participants by e-mail**: any address can follow a ticket (at creation or later); it is notified of every change. Notifications go out as `[TK-NL1-0007] …` with `Reply-To` = the service mailbox; `scripts/ticket_mail_in.py` (timer `argia-ticket-mail`, every 10 min) files a participant's reply as a comment (attachments included) once `IMAP_HOST/IMAP_USER/IMAP_PASS` exist in `/root/.argia_mail` — until then it logs "IMAP not configured".
- **Resolved comes from the data**: a ticket with linked alerts cannot be marked Resolved by a person — the button says "by the data (put it in Verification)". In Verification the nightly run (`alerts_daily._verify_tickets`) marks it Resolved when no linked alert is open or has been seen for 2 days, or sends it back to In progress when one recurs; both as `monitoring` timeline events with a mail. Tickets without alerts are resolved by people.
- **No repeated warnings**: at creation every open alert on the asset is linked; the nightly run attaches any alert opened/touched on the same plant + inverter (or the plant, for plant-level alerts) to the open ticket — so the morning mail and the 19:00 performance mail show "In hand: TK-… · status · last update" instead of the warning.
- **Statistics tab**: open tickets by status over 60 days (stacked SVG), opened/resolved per week, MTTR, open by plant / category / priority, over-SLA count.
- **Tooltips**: every status, priority and button carries its meaning; "How it works" legend on the dashboard; priorities explained on the form.
- **Form**: the inverter list follows the selected plant. Landing tile sits right of the Golden Standard.

## Roadmap
- **Phase 1 (this commit)**: tickets, plant/inverter link, assignment, followers, timeline, comments, attachments, e-mail notifications, dashboard, alert prefill + link, morning-mail "in hand" lines, occurrences on the timeline.
- **Phase 2 — automation**: open a ticket automatically for CRITICAL alerts (energy lost / unit off) after N hours without a human ticket; telemetry snapshot on the ticket (power vs expected, temperature, fault code, last-24 h chart); auto-verification (no alert recurrence for 24 h → propose RESOLVED); SLA breach mails.
- **Phase 3 — operations**: tasks/checklists, preventive-maintenance schedules generating tickets (same engine), maintenance calendar, public vs internal updates and customer visibility per plant, energy/financial impact from the thermal and recon data.
- **Phase 4 — intelligence**: recurring-problem detection (same plant + model + category), problem records grouping tickets, MTTA/MTTR/SLA/repeat-failure KPIs on the report pages, Ask ARGIA tools over tickets, AI summaries.

## Verification
`tests/unit/test_maintenance_tickets.py` (rules, SQL, mail integration), `tests/unit/test_maint_app.py` (the app against an in-memory fake of the tables: access, create, transitions, attachments, notifications, prefill, wiring). Live: `drift_check` probes `/maintenance/` (302 wall), the ticket created on deploy day proves the round trip.
