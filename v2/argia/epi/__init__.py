"""EPI-600 / Availability-150 engine (v377).

Spec: ARGIA PPA performance calculation spec v0.7 (Arturo, Nov 2026), with the
changes proven in the project doc EPI600_PARAMETER_AUDIT_2026-11-19.md.

- ``sky``   : sun position, Erbs decomposition, Perez transposition (pure Python,
              same equations as pvlib 0.16; equivalence is a unit test).
- ``model`` : cell temperature, PR0, degradation, expected string power,
              inverter limit, ratio of sums, monthly bias from the design year.

No plant data lives in this package: string registers, coefficients and limits
are server-side configuration (the repository is public).
"""
