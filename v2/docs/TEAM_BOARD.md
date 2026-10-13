# Solar team board - /board/ (v325)

A Kanban board for the Solar Department's daily stand-up, on
portal.argia.com.mx. Private while we try it: only the owners see the
landing card and the page.

## How the board works

| Column | Means | Limit / colour |
|---|---|---|
| Backlog | everything we may do, not committed | - |
| This week | committed at the Monday review | amber after 7 days, red after 14 |
| In progress | someone works on it now | **3 cards per person**; a card of a person above the limit gets an amber outline; amber after 5 days, red after 10 |
| Review | done by the owner, waiting for a second pair of eyes or the client | amber after 3 days, red after 7 |
| Done | finished and accepted | leaves the board after 14 days (kept in the database) |

A blocked card stays in its column with a red edge and a written reason
(the board refuses "blocked" without one). Each card has an owner, a
process from the process map (P1-P9, M1, E1-E5), a priority, a due date,
watchers, and a timeline (every change and comment, with name and time).

**Stand-up view** (toolbar): one row per person, the same five columns.
Walk it right to left: Done, Review, In progress, then blockers. Drag a
card into another person's row to hand it over.

The chips **Blocked / Overdue / Stuck** filter the board to what the
stand-up should talk about first. The board refreshes every 30 seconds
(never in the middle of a drag or an unsaved edit).

## Attachments (v326)

On an open card: **Attach files**, drag files onto the comment box, or
**paste a screenshot (Ctrl+V)**. Files post together with the comment
(or alone). Images and PDFs open in the browser; other files download.

- Allowed: PNG, JPG, GIF, WEBP, HEIC, PDF, TXT, CSV, XLSX, DOCX, PPTX, ZIP, KMZ, DWG, MP4.
  Not allowed: SVG/HTML/scripts, and any executable whatever its name (same magic-byte check as v324).
- 15 MB per file, 10 files per post. Viewers may attach too (attaching is commenting).
- Stored in `/opt/argia/board_files/<card id>/` (0700 / 0600), listed in table `board_file`.
- Not in the nightly backup yet (same as the maintenance ticket attachments).

## Who sees it

| Role | How | May |
|---|---|---|
| owner | `ARGIA_BOARD_EMAILS` in `argia-board.service`, or a line in `/opt/argia/auth/board_allow.txt` (read live) | everything, Members tab, starter backlog |
| editor | added by an owner on `/board/members/` | create, edit, move, archive, comment, watch |
| viewer | added by an owner | read, comment, watch |

Disabled and customer accounts are never let in, whatever the members list says.

## Code

- `argia/board/core.py` - rules (columns, WIP, aging, validation, roles, starter backlog)
- `argia/board/store.py` - `PgStore` (psql, like every server job) and `MemStore` (tests)
- `argia/board/service.py` - operations; one timeline event per change
- `server/bundle/board_app.py` - Flask app on 127.0.0.1:8516, JSON API + the page
- Tests: `tests/unit/test_board_core.py`, `test_board_service.py`, `test_board_app.py`,
  `tests/portal/test_board_pg.py` (the same scenarios on a real PostgreSQL)

API (JSON; every POST needs the header `X-Argia-Board: 1`):
`GET /board/me`, `GET /board/api/state`, `GET /board/api/cards/<id>`,
`POST /board/api/cards`, `POST /board/api/cards/<id>`, `.../move`
(`{col, order:[ids top to bottom], assignee?}`), `.../comment`, `.../watch`,
`.../archive`, `POST /board/api/members`, `POST /board/api/seed`.

## Not in v325 (on purpose)

- No e-mail notifications yet (only owners use it now; add when the team joins).
- No sprints or burndown: the cadence is weekly (This week column) plus the daily stand-up.
