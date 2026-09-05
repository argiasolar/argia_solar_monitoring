"""Per-channel mail subscriptions with per-plant scoping (v176).

Tomasz, 2026-09-02: separate mailing lists for (1) maintenance —
live metering / live issues, (2) financial reports, (3) daily
performance; admin adds/removes people in /setup/; ONLY portal users
can receive mail; a maintenance subscriber can be limited to specific
plants so he never hears about a plant outside his access.

One PG table, ``mail_subscription``, keyed (email, channel). The
``plants`` column is a comma-separated list of plant keys; empty means
ALL plants — and only an all-plants maintenance subscriber receives
infrastructure alerts (server disk, failed jobs, PG down): a client
scoped to his own plant must never be paged about our server room.

Everything here except ``recipients_for``/``portal_emails`` is pure
and unit-tested. The setup UI (server/bundle/setup_app.py) duplicates
ENSURE_SQL inline because the bundle cannot import the argia package —
a source-level test keeps the two copies identical.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple

CHANNELS = ("maintenance", "financial", "daily", "reports")

# v196: 'reports' = the morning/evening PDF reports the Apps Script
# notifier used to mail from Report_Outbox. The CHECK is re-created so an
# existing table (created with three channels) accepts the fourth.
ENSURE_SQL = """CREATE TABLE IF NOT EXISTS mail_subscription (
    email    text NOT NULL,
    channel  text NOT NULL
             CHECK (channel IN ('maintenance','financial','daily','reports')),
    plants   text NOT NULL DEFAULT '',
    enabled  boolean NOT NULL DEFAULT true,
    username text NOT NULL DEFAULT '',
    added_by text,
    added_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, channel)
);
ALTER TABLE mail_subscription DROP CONSTRAINT IF EXISTS mail_subscription_channel_check;
ALTER TABLE mail_subscription ADD CONSTRAINT mail_subscription_channel_check
    CHECK (channel IN ('maintenance','financial','daily','reports'));"""

# Alert-key prefixes whose second ':'-segment is a plant key. Everything
# else (unit-failed, disk-full, postgres-down, cfe-*) is infrastructure.
_PLANT_PREFIXES = ("plant-dark", "plant-stale", "inverter-silent",
                   "recon-fail", "satellite-drift",
                   # v203: engine (ledger) metrics rendered as issues in the
                   # 19:00 mail — "<metric>:<plant>:<sn>"
                   "inverter_temp_high", "inverter_fault", "inverter_relative",
                   "inverter_silent", "string_fault", "energy_daily_pct",
                   "plant_offline", "plant_twin_yield", "data_stale")


def parse_plants(text: Optional[str]) -> Optional[FrozenSet[str]]:
    """'GTO1, mex1' -> frozenset({'GTO1','MEX1'}); ''/None -> None (=all)."""
    if not text or not text.strip():
        return None
    keys = {p.strip().upper() for p in text.split(",") if p.strip()}
    return frozenset(keys) or None


def plants_field(scope: Optional[Iterable[str]]) -> str:
    """Inverse of parse_plants — canonical DB text for a scope."""
    if not scope:
        return ""
    return ",".join(sorted({p.strip().upper() for p in scope if p.strip()}))


def alert_plant(key: str) -> Optional[str]:
    """Plant key an alert concerns, or None for infrastructure alerts."""
    head, _, rest = key.partition(":")
    if head in _PLANT_PREFIXES and rest:
        return rest.split(":", 1)[0]
    return None


def covers(scope: Optional[FrozenSet[str]], plant: Optional[str]) -> bool:
    """Does a subscriber scope receive an alert about ``plant``?
    scope None = all plants AND infrastructure; a limited scope gets
    only its own plants and NO infrastructure noise."""
    if scope is None:
        return True
    return plant is not None and plant in scope


def filter_alerts(alerts: Sequence, scope: Optional[FrozenSet[str]]) -> list:
    """Alerts (objects with .key) visible to a scope, order preserved."""
    return [a for a in alerts if covers(scope, alert_plant(a.key))]


def filter_keys(keys: Sequence[str],
                scope: Optional[FrozenSet[str]]) -> List[str]:
    """Recovered-key strings visible to a scope, order preserved."""
    return [k for k in keys if covers(scope, alert_plant(k))]


def group_recipients(alerts: Sequence, recovered: Sequence[str],
                     recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]]
                     ) -> List[Tuple[List[str], list, List[str]]]:
    """Group (email, scope) pairs by the identical filtered view so each
    distinct mail is built and sent once: [(emails, alerts, recovered)].
    Recipients whose view is empty are dropped — no content, no mail."""
    views: Dict[tuple, List[str]] = {}
    payload: Dict[tuple, Tuple[list, List[str]]] = {}
    for email, scope in recipients:
        al = filter_alerts(alerts, scope)
        rc = filter_keys(recovered, scope)
        if not al and not rc:
            continue
        sig = (tuple(a.key for a in al), tuple(rc))
        views.setdefault(sig, []).append(email)
        payload[sig] = (al, rc)
    return [(sorted(views[sig]), payload[sig][0], payload[sig][1])
            for sig in sorted(views)]


def recipients_for(channel: str
                   ) -> List[Tuple[str, Optional[FrozenSet[str]]]]:
    """Enabled (email, scope) subscriptions for a channel, from PG."""
    if channel not in CHANNELS:
        raise ValueError("unknown channel: %r" % channel)
    from argia.store.pgq import psql_rows
    out: List[Tuple[str, Optional[FrozenSet[str]]]] = []
    for r in psql_rows(
            "SELECT email, coalesce(plants,'') FROM mail_subscription"
            f" WHERE enabled AND channel = '{channel}' ORDER BY 1;"):
        if r and r[0]:
            out.append((r[0], parse_plants(r[1] if len(r) > 1 else "")))
    return out


def portal_emails(db_path: str = "/opt/argia/auth/users.db"
                  ) -> Optional[FrozenSet[str]]:
    """Emails of enabled portal accounts (lowercased), or None when the
    portal DB is not readable here (dev box, tests) — callers then skip
    the portal check rather than silencing everyone."""
    import os
    import sqlite3
    if not os.path.exists(db_path):
        return None
    try:
        c = sqlite3.connect(db_path)
        try:
            rows = c.execute(
                "SELECT email FROM users WHERE disabled=0"
                " AND email != ''").fetchall()
        finally:
            c.close()
        return frozenset(r[0].strip().lower() for r in rows
                         if r and r[0] and r[0].strip())
    except sqlite3.Error:
        return None


def only_portal(recipients: Sequence[Tuple[str, Optional[FrozenSet[str]]]],
                portal: Optional[FrozenSet[str]]
                ) -> List[Tuple[str, Optional[FrozenSet[str]]]]:
    """Enforce 'only portal users receive email'. portal None = check
    unavailable -> unchanged (the admin UI already restricts adds)."""
    if portal is None:
        return list(recipients)
    return [(e, s) for e, s in recipients if e.strip().lower() in portal]
