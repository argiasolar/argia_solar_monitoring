"""ARGIA platform for Prologis - prologis.argia.com.mx (v292).

A separate, co-branded customer platform: live metering, map, maintenance
tickets on the Prologis MSA response-time classes, projects, documents,
its own login with TOTP MFA, roles, audit log and data export.

Customer data (site list, addresses, documents) never lives in this
public repository: the registry is a server-only JSON file, the
database and files sit under /opt/argia/prologis. Tests use a synthetic
registry (tests/fixtures/prologis/).
"""
