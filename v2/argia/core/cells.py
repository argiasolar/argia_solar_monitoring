"""Sheet-cell coercion — the ONE place that knows what the Sheets API
returns.

With valueRenderOption=UNFORMATTED_VALUE, datetime cells arrive as SERIAL
NUMBERS (days since 1899-12-30, e.g. 46199.5625), not datetimes and not
ISO strings. xlsx exports hand you real datetimes, which is exactly how a
consumer tested only against exports ships a parser that fails against the
live API (watchdog false-alarm incident, 2026-07-05: "no parseable
timestamps at all" on a healthy sheet).

Every script reading date/time cells must use these helpers — never a
private parser.
"""

from __future__ import annotations

import datetime as dt

GOOGLE_EPOCH = dt.datetime(1899, 12, 30)


def coerce_ts(v) -> dt.datetime | None:
    """Sheet cell -> naive local datetime (as stored in the sheet)."""
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return GOOGLE_EPOCH + dt.timedelta(days=float(v))
    if isinstance(v, str):
        s = v.strip().replace("T", " ")
        s = s.split("+")[0].strip()
        if s.endswith("Z"):
            s = s[:-1].strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y-%m-%d %H:%M:%S.%f", "%d/%m/%Y %H:%M:%S",
                    "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                pass
    return None


def coerce_date(v) -> dt.date | None:
    ts = coerce_ts(v)
    if ts is not None:
        return ts.date()
    if isinstance(v, str) and len(v.strip()) >= 10:
        try:
            return dt.date.fromisoformat(v.strip()[:10])
        except ValueError:
            pass
    return None
