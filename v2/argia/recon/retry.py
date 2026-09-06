"""Reconciliation retries (v212) — pure selection and SQL builders.

Tomasz 2026-09-05: "keep trying to reconcile whenever there is such
possibility, once the inverter is online maybe there is a way to
reconcile the past days, I would like to avoid situation when we drag
the issue till end of month".

The vendors keep per-day history (Growatt getMAXHistory, Huawei
getKpiStationDay, SolarEdge /site/energy), so a day that reconciled
badly because OUR side had a gap — the Pi down, a poll that failed, an
inverter that reported nothing to us while the vendor still has its
day — can be re-fetched later. The nightly recon run therefore goes
back over a window of open days (``RETRY_DAYS``), picks the plant-days
that still need a retry, fetches the vendor's day for exactly those,
and re-reconciles them. Rules that never bend:

* never lower — a fetched vendor day only fills or raises the stored
  snapshot (``build_retry_snapshot_sql``), and ``daily_production`` is
  touched only through the reconciliation's own fill-or-raise;
* a CLOSED plant-month is frozen — never selected, never healed;
* a PASS day is done — no vendor calls for it;
* a REVIEW day whose note says the VENDOR is the one missing uploads
  (inverter counters above the vendor) is not our gap — no retry.

What a retry cannot do: recover energy a Growatt datalogger never
uploaded (the inverter's own eTotal register would be needed for that;
not captured today) — those days stay REVIEW with the note saying so.
"""
from __future__ import annotations

import datetime as dt
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from argia.recon import engine as E

RETRY_DAYS = 14
"""How far back the nightly run looks for plant-days worth a retry."""

RETRY_NOTE = "history-retry"

# note fragments (from engine.daily_recon) that mean OUR side had the gap
_OUR_GAP = ("no inverter counters", "collection gap", "no vendor daily counter",
            "counter anomaly", "undercount expected")
_THEIR_GAP = ("vendor upload gap",)


def needs_retry(status: Optional[str], note: Optional[str],
                completeness_pct: Optional[float]) -> bool:
    """Is this reconciled plant-day worth another look? Pure."""
    st = (status or "").strip().upper()
    n = (note or "").lower()
    if st == "":
        return True                       # never reconciled at all
    if st == E.STATUS_PASS:
        return False
    if st in (E.STATUS_FAIL, E.STATUS_NO_DATA):
        return True
    # REVIEW: our gap → retry; the vendor's gap → nothing to fetch
    if any(k in n for k in _THEIR_GAP) and not any(k in n for k in _OUR_GAP):
        return False
    if completeness_pct is not None and completeness_pct < E.COMPLETENESS_MIN_PCT:
        return True
    return any(k in n for k in _OUR_GAP)


def window_dates(today: dt.date, days: int = RETRY_DAYS) -> List[str]:
    """Yesterday back ``days`` days, oldest first. Today is never in the
    window — its day is not over."""
    return [(today - dt.timedelta(days=b)).isoformat()
            for b in range(days, 0, -1)]


def select_retry(plants: Iterable[str], dates: Sequence[str],
                 recon_rows: Iterable[Sequence],
                 closed: Iterable[Tuple[str, str]] = (),
                 ) -> List[Tuple[str, str]]:
    """The (plant_key, date) pairs to retry, ordered by plant then date.
    ``recon_rows`` = (plant_key, prod_date, status, note,
    completeness_pct) from reconciliation_daily; ``closed`` =
    (plant_key, 'YYYY-MM') plant-months with a closed monthly close.
    A plant-day without a recon row is a retry too. Pure."""
    frozen: Set[Tuple[str, str]] = {(str(p).upper(), str(m)[:7]) for p, m in closed}
    by_key: Dict[Tuple[str, str], Tuple[str, str, Optional[float]]] = {}
    for r in recon_rows:
        if len(r) < 3:
            continue
        pk, d = str(r[0]).strip().upper(), str(r[1])[:10]
        comp = None
        if len(r) >= 5 and r[4] not in (None, ""):
            try:
                comp = float(r[4])
            except (TypeError, ValueError):
                comp = None
        by_key[(pk, d)] = (str(r[2] or ""), str(r[3] or "") if len(r) >= 4 else "", comp)
    out: List[Tuple[str, str]] = []
    for pk in sorted({str(p).strip().upper() for p in plants}):
        for d in dates:
            if (pk, d[:7]) in frozen:
                continue
            st, note, comp = by_key.get((pk, d), ("", "", None))
            if needs_retry(st, note, comp):
                out.append((pk, d))
    return out


def _txt(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def build_retry_snapshot_sql(rows: Iterable[Tuple[str, str, str, Optional[float]]]
                             ) -> Optional[str]:
    """UPSERT the re-fetched vendor days into vendor_counter_snapshot —
    fill a missing daily_kwh or RAISE a lower one, never lower it, and
    never touch the nightly monthly/lifetime counters. rows =
    (plant_key, vendor, date_iso, daily_kwh); None values are skipped.
    None for an empty batch."""
    vals = []
    for pk, vendor, d, kwh in rows:
        if kwh is None:
            continue
        vals.append(f"({_txt(str(pk).strip().upper())},{_txt(str(vendor).upper())},"
                    f"DATE '{d}',{float(kwh):.3f},{_txt(RETRY_NOTE)})")
    if not vals:
        return None
    return (
        "INSERT INTO vendor_counter_snapshot (plant_key, vendor, snap_date,"
        " daily_kwh, note) VALUES\n" + ",\n".join(vals) +
        "\nON CONFLICT (plant_key, snap_date) DO UPDATE SET"
        " daily_kwh = EXCLUDED.daily_kwh,"
        f" note = {_txt(RETRY_NOTE)}, captured_at = now()"
        " WHERE vendor_counter_snapshot.daily_kwh IS NULL"
        " OR vendor_counter_snapshot.daily_kwh < EXCLUDED.daily_kwh;"
    )


def group_dates(pairs: Iterable[Tuple[str, str]]) -> Dict[str, List[str]]:
    """{plant_key: [dates ascending]} — one vendor session per plant."""
    out: Dict[str, List[str]] = {}
    for pk, d in pairs:
        out.setdefault(pk, []).append(d)
    for pk in out:
        out[pk] = sorted(set(out[pk]))
    return out
