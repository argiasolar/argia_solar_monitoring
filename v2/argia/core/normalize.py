"""Pure normalization helpers — no I/O, no side effects, easy to test."""

from __future__ import annotations

import math
import re
from typing import Any, Iterable, List, Optional, TypeVar

T = TypeVar("T")


def safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """
    Coerce a value to float, returning ``default`` on any failure.

    Handles:
      - None → default
      - empty string → default
      - "1,234.5" → 1234.5 (strips commas; common in Sheets exports)
      - NaN / inf → default

    Examples:
        >>> safe_float("3.14")
        3.14
        >>> safe_float("1,234.5")
        1234.5
        >>> safe_float(None, default=0.0)
        0.0
        >>> safe_float("not a number") is None
        True
    """
    if value is None:
        return default
    try:
        if isinstance(value, str):
            s = value.strip().replace(",", "")
            if s == "":
                return default
            value = s
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def normalize_text(value: Any) -> str:
    """
    Convert any value to a stripped string. None becomes "".

    Examples:
        >>> normalize_text(None)
        ''
        >>> normalize_text("  hello  ")
        'hello'
        >>> normalize_text(42)
        '42'
    """
    if value is None:
        return ""
    return str(value).strip()


def normalize_sn(value: Any) -> str:
    """
    Normalize an inverter serial number: strip whitespace, uppercase.

    Same SN coming from different APIs should produce the same string.

    Examples:
        >>> normalize_sn("  abc123  ")
        'ABC123'
        >>> normalize_sn("ES24 70051825")
        'ES2470051825'
        >>> normalize_sn(None)
        ''
    """
    if value is None:
        return ""
    return re.sub(r"\s+", "", str(value)).upper()


def pick(d: dict, keys: List[str]) -> Optional[Any]:
    """
    Return the first non-empty value in ``d`` matching any of ``keys``.

    Useful when an API uses different field names across versions
    (e.g. ``eToday`` vs ``EToday`` vs ``todayEnergy``).

    Empty/None/null values are skipped.

    Examples:
        >>> pick({"a": "", "b": "hi", "c": "x"}, ["a", "b"])
        'hi'
        >>> pick({"a": None, "b": 0}, ["a", "b"])
        0
        >>> pick({}, ["a"]) is None
        True
    """
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def chunked(items: List[T], size: int) -> List[List[T]]:
    """
    Split a list into chunks of ``size``. Final chunk may be shorter.

    Useful for batching API calls (Huawei accepts up to 50 SNs per request).

    Examples:
        >>> chunked([1, 2, 3, 4, 5], 2)
        [[1, 2], [3, 4], [5]]
        >>> chunked([], 3)
        []
        >>> chunked([1, 2, 3], 10)
        [[1, 2, 3]]
    """
    if size <= 0:
        raise ValueError(f"chunk size must be positive, got {size}")
    return [items[i : i + size] for i in range(0, len(items), size)]


def looks_like_growatt_site_id(value: Any) -> bool:
    """
    Growatt plant IDs are 6-12 digits, e.g. ``9275498`` or ``10069072``.

    Examples:
        >>> looks_like_growatt_site_id("10069072")
        True
        >>> looks_like_growatt_site_id("NE=35314736")
        False
        >>> looks_like_growatt_site_id("")
        False
    """
    return bool(re.fullmatch(r"\d{6,12}", normalize_text(value)))


def looks_like_huawei_station_code(value: Any) -> bool:
    """
    Huawei FusionSolar station codes start with ``NE=``.

    Examples:
        >>> looks_like_huawei_station_code("NE=35314736")
        True
        >>> looks_like_huawei_station_code("9275498")
        False
    """
    return normalize_text(value).startswith("NE=")


def looks_like_solaredge_site_id(value: Any) -> bool:
    """
    SolarEdge site IDs are positive integers, typically 6-8 digits.

    Examples:
        >>> looks_like_solaredge_site_id("123456")
        True
        >>> looks_like_solaredge_site_id("12")
        False
        >>> looks_like_solaredge_site_id("NE=123")
        False
    """
    s = normalize_text(value)
    return bool(re.fullmatch(r"\d{4,10}", s))
