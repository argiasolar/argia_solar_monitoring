"""5-minute live telemetry pipeline.

Stage 3 of the v2 rewrite: capture wide-format Growatt inverter data
(per-MPPT, per-string, fault codes, etc.) plus weather, and append rows to
Sheets every 5 minutes during daylight.

Stage 4: extend to Huawei. Per-plant tabs stay wide (vendor-shaped, mostly
empty for vendors with skinnier APIs); aggregated ``Telemetry_Argia`` tab
becomes a narrow cross-vendor common subset.

The tabs reset daily via a separate end-of-day workflow (Stage 5).
"""
