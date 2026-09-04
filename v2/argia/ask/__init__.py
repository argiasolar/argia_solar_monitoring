"""Ask ARGIA — a tool-calling assistant over the monitoring database.

Phase 0 (2026-09-04). Read-only. The model never sees SQL: it picks one
of the functions in ``argia.ask.tools``, the backend runs a fixed query
against PostgreSQL, and the model writes the narrative around numbers it
was handed. Every answer is logged with the tools that produced it.
"""
