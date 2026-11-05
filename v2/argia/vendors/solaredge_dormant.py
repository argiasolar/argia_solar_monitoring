"""SolarEdge plants that send no data: one ping a day instead of hourly (v358).

Tomasz 2026-11-05: "drop the tetrapak, leave one ping per day to see if it
works". QRO1 (Tetra Pak, site 4146396) has returned no energy since July
2026; on API v2 every call costs a credit (free plan 2,000 a month), and
QRO1 alone cost about 570 a month for nothing.

A DORMANT plant:
* telemetry: polled once a day, at the PING_HOUR run (12:07 MX), instead of
  every hour;
* nightly counters and the retry pass: not called (3-4 credits a night);
* comes back BY ITSELF: as soon as it has telemetry rows in the last
  ALIVE_DAYS days (the noon ping found data), it is polled and counted like
  any plant again, and the job warns that it is reporting so the entry can
  be removed from DORMANT_PLANTS. If it dies again it falls back to the
  daily ping after ALIVE_DAYS days.

Cost while dormant: 1 credit a day (about 30 a month).
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, List

LOG = logging.getLogger("argia.vendors.solaredge_dormant")

DORMANT_PLANTS = frozenset({"QRO1"})
"""Plant keys polled once a day. QRO1 Tetra Pak: no energy since July 2026."""

PING_HOUR = 12
"""Local (MX) hour of the daily ping: the 12:07 run of argia-telemetry-se."""

ALIVE_DAYS = 3
"""Telemetry rows within this many days = the plant is reporting again."""


def is_dormant(plant_key: str) -> bool:
    return (plant_key or "").upper() in DORMANT_PLANTS


def should_poll(plant_key: str, local_hour: int, alive: bool) -> bool:
    """Telemetry: poll this plant in this run? PURE."""
    if not is_dormant(plant_key) or alive:
        return True
    return local_hour == PING_HOUR


def counter_plants(plants: Iterable, alive_fn: Callable[[str], bool]) -> List:
    """Nightly counters / retry: the plants worth a vendor call. A dormant
    plant only when it is reporting again. PURE given ``alive_fn``."""
    out = []
    for p in plants:
        if is_dormant(p.plant_key) and not alive_fn(p.plant_key):
            LOG.info("solaredge %s: dormant (no data) - no counter calls, one telemetry ping a day",
                     p.plant_key)
            continue
        out.append(p)
    return out


def alive_sql(plant_key: str, days: int = ALIVE_DAYS) -> str:
    """Rows of the plant with real data in the last ``days`` days."""
    pk = (plant_key or "").upper().replace("'", "")
    return ("SELECT count(*) FROM telemetry WHERE plant_key = '%s'"
            " AND ts_utc > now() - interval '%d days'"
            " AND (coalesce(power_w, 0) > 0 OR coalesce(etoday_kwh, 0) > 0);" % (pk, int(days)))


def alive(plant_key: str, days: int = ALIVE_DAYS) -> bool:
    """Has the plant reported real data recently? A database error answers
    False: the plant stays on the daily ping (never more credits by accident)."""
    try:
        from argia.store.pgq import psql_rows
        rows = psql_rows(alive_sql(plant_key, days))
        return bool(rows) and int(float(rows[0][0] or 0)) > 0
    except Exception as e:  # noqa: BLE001
        LOG.warning("solaredge %s: could not read recent telemetry (%s) - treated as dormant",
                    plant_key, str(e)[:120])
        return False
