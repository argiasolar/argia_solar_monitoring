"""Money arithmetic: Decimal, 2 places, half-up. Floats never touch an
invoice (0.1 + 0.2 is not 0.3 and a peso is a peso)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Union

Number = Union[str, int, float, Decimal]
CENT = Decimal("0.01")
TOL = Decimal("0.01")


def D(v: Number) -> Decimal:
    """Any number-ish -> Decimal at 2 places. '' / None -> 0.00."""
    if v is None or v == "":
        return Decimal("0.00")
    if isinstance(v, float):
        v = repr(v)
    try:
        return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as e:
        raise ValueError(f"not a money amount: {v!r}") from e


def q(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def same(a: Number, b: Number, tol: Decimal = TOL) -> bool:
    return abs(D(a) - D(b)) <= tol


def pct(part: Number, whole: Number) -> Decimal | None:
    """part / whole * 100 at 2 places; None when whole is 0."""
    w = D(whole)
    if w == 0:
        return None
    return q(D(part) / w * 100)
