"""
Time utilities — DST-correct.

v1 used ``datetime.utcnow() + timedelta(hours=-6)`` which:
  - breaks during DST (Mexico observes DST in some regions)
  - uses naive datetimes (deprecated since Python 3.12)
  - hardcodes the offset in multiple places

v2 uses ``zoneinfo.ZoneInfo`` and timezone-aware datetimes everywhere.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo

# Mexico City — covers all Argia plant locations.
# (CDMX, GTO, NL, OAX, SLP all use America/Mexico_City)
MX_TZ = ZoneInfo("America/Mexico_City")
UTC = dt.timezone.utc


def now_utc() -> dt.datetime:
    """Current UTC time, second precision, timezone-aware."""
    return dt.datetime.now(UTC).replace(microsecond=0)


def now_mx() -> dt.datetime:
    """Current Mexico City time, second precision, timezone-aware."""
    return now_utc().astimezone(MX_TZ)


def now_utc_iso() -> str:
    """Current UTC time as ISO 8601 string. ``2026-05-08T14:23:00+00:00``."""
    return now_utc().isoformat()


def utc_to_mx(when: dt.datetime) -> dt.datetime:
    """
    Convert any datetime to Mexico City time.

    Naive datetimes are assumed to be UTC (defensive default).
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(MX_TZ)


def fmt_sheets_datetime(when: dt.datetime) -> str:
    """
    Format a datetime as ``M/D/YYYY H:MM:SS`` so Google Sheets parses it
    as a real datetime when written with ``valueInputOption=USER_ENTERED``.

    Always rendered in Mexico City local time.

    Examples:
        >>> from datetime import datetime, timezone
        >>> fmt_sheets_datetime(datetime(2026, 4, 15, 18, 30, 0, tzinfo=timezone.utc))
        '4/15/2026 12:30:00'
    """
    mx = utc_to_mx(when)
    return f"{mx.month}/{mx.day}/{mx.year} {mx.hour}:{mx.minute:02d}:{mx.second:02d}"


def fmt_sheets_date(when: dt.datetime) -> str:
    """
    Format the date portion only, using Sheets-compatible ``M/D/YYYY``.
    Mexico City local date.

    Examples:
        >>> from datetime import datetime, timezone
        >>> fmt_sheets_date(datetime(2026, 4, 15, 18, 30, tzinfo=timezone.utc))
        '4/15/2026'
    """
    mx = utc_to_mx(when)
    return f"{mx.month}/{mx.day}/{mx.year}"


def parse_provider_datetime(value: Any) -> Optional[dt.datetime]:
    """
    Parse the various datetime formats inverter APIs return.

    Returns a UTC-aware datetime or None if unparseable.

    Supports:
      - epoch seconds (10-digit integer)
      - epoch milliseconds (13-digit integer)
      - ``"YYYY-MM-DD HH:MM:SS"`` (assumed UTC — defensive)
      - ``"YYYY-MM-DDTHH:MM:SS"`` (ISO, with or without tz)
      - ``"YYYY/MM/DD HH:MM:SS"``

    Examples:
        >>> parse_provider_datetime(1700000000).isoformat()
        '2023-11-14T22:13:20+00:00'
        >>> parse_provider_datetime("2026-04-15 12:30:00").year
        2026
        >>> parse_provider_datetime("garbage") is None
        True
    """
    if value is None:
        return None

    # numeric epoch — detect ms vs s by digit count
    if isinstance(value, (int, float)):
        n = int(value)
        # 13 digits ≈ ms (year 2001 onwards in ms is 13 digits)
        # 10 digits ≈ seconds (year 2001 onwards in seconds is 10 digits)
        if n > 10**12:
            return _from_epoch(n // 1000)
        return _from_epoch(n)

    s = str(value).strip()
    if not s:
        return None

    if re.fullmatch(r"\d{10}", s):
        return _from_epoch(int(s))
    if re.fullmatch(r"\d{13}", s):
        return _from_epoch(int(s) // 1000)

    # ISO with TZ — let fromisoformat handle it (Python 3.11+ supports trailing Z)
    iso_candidate = s.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(iso_candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        pass

    # Common provider formats — assume UTC (caller can re-anchor if needed)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            naive = dt.datetime.strptime(s, fmt)
            return naive.replace(tzinfo=UTC)
        except ValueError:
            continue

    return None


def _from_epoch(epoch_seconds: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch_seconds, tz=UTC)


def parse_growatt_calendar(cal: dict) -> Optional[dt.datetime]:
    """
    Growatt's ``calendar`` object uses 0-based months (Java Calendar legacy).

    Keys: ``year``, ``month`` (0-11), ``dayOfMonth``, ``hourOfDay``,
    ``minute``, ``second``.

    Treats the resulting time as Mexico City local (Growatt servers
    use the plant's local timezone for the calendar object).

    Examples:
        >>> cal = {"year": 2026, "month": 3, "dayOfMonth": 15,
        ...        "hourOfDay": 12, "minute": 30, "second": 0}
        >>> dt_obj = parse_growatt_calendar(cal)
        >>> dt_obj.month  # March because Java Calendar months are 0-based
        4
        >>> dt_obj.tzinfo is not None
        True
    """
    if not isinstance(cal, dict):
        return None
    try:
        year = int(cal["year"])
        month_zero_based = int(cal["month"])
        day = int(cal.get("dayOfMonth") or cal.get("day"))
        hour = int(cal.get("hourOfDay", 0))
        minute = int(cal.get("minute", 0))
        second = int(cal.get("second", 0))
        return dt.datetime(
            year,
            month_zero_based + 1,
            day,
            hour,
            minute,
            second,
            tzinfo=MX_TZ,
        )
    except (KeyError, ValueError, TypeError):
        return None
