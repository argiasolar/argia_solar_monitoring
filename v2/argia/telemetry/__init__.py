"""5-minute live telemetry pipeline.

Stage 3 of the v2 rewrite: capture the wide-format Growatt inverter data
(per-MPPT, per-string, fault codes, etc.) plus weather, and append rows to
Sheets every 5 minutes during daylight.

Two tabs per Growatt plant + one aggregated Argia tab. The tabs are reset
daily by a separate end-of-day workflow (Stage 5).
"""
