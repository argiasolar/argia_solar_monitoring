"""Energy reconciliation (v3): interval data vs vendor counters.

Design (2026-08-26, from the external reconciliation advice + AGS-901):
interval telemetry is ANALYTICS; the vendor cumulative counters are the
BILLING CONTROL. A nightly snapshot of the vendor daily/monthly/lifetime
counters builds our own immutable audit trail, so an invoice never
depends on every 5-minute sample having been collected.
"""
