# SMA — how ARGIA reaches Sunny Portal / ennexOS

Status **2026-09-11**: the Stage 6 scaffold exists and its auth flow is right,
but **no real SMA measurement has ever passed through it**. Nothing here is
running in production. This document records what was verified, what is still a
guess, and what has to happen before MD Elektronik and Oechsler Solar can be
monitored like the Growatt, Huawei and SolarEdge plants.

## ⚠️ A credential was published in this repository

Earlier revisions of this file contained an SMA **sandbox** `client_secret` in
plain text. The repository is public, so that value must be treated as
disclosed. It is removed from the working tree, but **removing it here does not
remove it from git history** — anyone can still read it in commit `7130390`.

* Blast radius is small: it is a sandbox credential against SMA's shared mock
  environment (`apiTestUser@apiSandbox.com`), it reaches no real plant and no
  ARGIA data.
* It must still be **rotated with SMA API Developer Support** when we ask for
  production credentials, and it must not be reused.
* No SMA credential belongs in this repository or in chat. Production values go
  in `/root/.argia_sma` on pio06, `chmod 600`, written by Tomasz — the same
  pattern as `/root/.argia_savio`.

`tests/unit/test_sma_access.py::TestNoCredentialIsCommitted` fails the build if
a secret-shaped literal comes back.

## What SMA actually sells (verified 2026-09-11)

From the SMA Developer Portal:

| | |
|---|---|
| Sandbox | **Free.** "Cost-free trial on SMA Sandbox environment", mock ennexOS plants only — it will never return MD Elektronik or Oechsler data. |
| Production Monitoring API | **Paid, usage-based, no standing fee.** Priced per plant by AC size. |
| Contract | One contract, not one per plant: it "includes the set-up of client credentials… you will be able to monitor any system to which you have access". |
| Setup fee | None. |

Monitoring API price bands (SMA's own example: a 50 kWac system = €16.50):

| Plant size | Price |
|---|---|
| 1–19 kWac | €12 base |
| 20–99 kWac | €12 + €0.09/kWac |
| 100–499 kWac | €15 + €0.06/kWac |
| 500–999 kWac | €25 + €0.04/kWac |
| > 1,000 kWac | €65 base |

**Unresolved:** the price page is read as per-year in one place and "monthly
charges" in another. Confirm the period with SMA before signing. Even at the
monthly reading this is a small number for two plants.

Do **not** use the SMA **Live-API** for this: it starts at €100/month and SMA
states it is "not allowed for constant monitoring". The Monitoring API is the
correct product.

## Why the API and not a headless browser

The ennexOS front end is an Angular app that talks to a private backend
(`uiapi.sunnyportal.com`, auth via Keycloak at `login.sma.energy/auth/realms/SMA`,
public client `SPpbeOS`). Scraping it is technically possible and costs nothing
in licence fees, but it means storing a human's portal password, tracking an
undocumented API that changes without notice, and operating outside SMA's
terms. Against roughly the price of a coffee per plant per month, that is a bad
trade for a fleet we are paid to keep running. The API is the route; the portal
is only a fallback if SMA refuses us a contract.

## Where the ARGIA telemetry cadence lands

ennexOS plants (Data Manager) upload **every 5 minutes** and SMA processes them
"on the fly", so `scripts/telemetry_5m.py` runs at native resolution — the same
as the other three brands. (Classic Sunny Portal systems are 15-minute with up
to 2 hours of processing delay; ours are ennexOS.)

## Consent: how ARGIA gets access to its own plants

Custom Flow / backchannel, which `argia/vendors/sma.py` already implements:

1. `client_credentials` → client token.
2. `POST bc-authorize {loginHint: arturo.gonzalez@argia.solar}`.
3. SMA emails Arturo; he approves once.
4. Consent "remain[s] valid until actively revoked" on the portal.

Nobody types a password into the pipeline. Verified hosts:

| | Sandbox | Production |
|---|---|---|
| Token | `sandbox-auth.smaapis.de/oauth2/token` | `auth.smaapis.de/oauth2/token` |
| Consent | `sandbox.smaapis.de/oauth2/v2/bc-authorize` | `async-auth.smaapis.de/oauth2/v2/bc-authorize` |
| Data | `sandbox.smaapis.de/monitoring/v1` | **not published by SMA — ask them** |

The production *data* host is the one thing SMA does not document publicly. The
code ships a placeholder and reads `SMA_API_BASE` as an override, so confirming
it with SMA is a config change, not a release.

## What is still wrong in the scaffold

Fixed in v252:

* production data host was pointed at the *consent* host — now overridable and
  labelled unverified;
* the device measurement set was hard-coded to `pvGeneration`, which is SMA's
  word for the data *category*, not a set name the API accepts. The one real
  capture we hold (`tests/fixtures/sma/live_inverter_sets_16.json`) lists
  `Sensor, EnergyAndPowerPv, PowerDc, PowerAc` — so **every telemetry call the
  scaffold would have made was a 404**. The client now asks the device what it
  offers and picks from that;
* `site_timezone` was accepted and discarded (both branches returned Mexico).

Still open, and **only real data can close them**:

* `fetch_day_kwh` asks for the plant set `EnergyMix`. Unverified — same class of
  guess as `pvGeneration` was.
* `argia/vendors/sma_telemetry.py` guesses field names inside the response
  (`power`, `pac`, `activePower`, …). Not one has been seen in a real payload.
* `OFFLINE_DEVICE_STATES` is invented; SMA's real status vocabulary is unknown.

## The order of work (capture first — the mistake Stage 6 made)

The original Stage 6 doc predicted its own "Stage 6.1 hotfix" and shipped a full
parser before capturing anything. It was right, and the cost was a whole vendor
integration that has never returned a number. Do not repeat it.

1. Tomasz asks SMA API Developer Support for a **production Monitoring API**
   contract for the two plants, and for the production data host. Rotate the
   disclosed sandbox secret in the same message.
2. Arturo approves the consent email.
3. Credentials into `/root/.argia_sma` on pio06 (0600), never in the repo.
4. Run `scripts/sma_discover_plants.py`, then `scripts/sma_capture.py` — **one
   read, no parsing decisions** — and commit the captured payloads as fixtures
   (they are ARGIA production data: synthesise or redact before committing, per
   the repo's public-repo rule).
5. Only then finish the parser against those fixtures, and add the two plants as
   CAPEX with the usual unit and regression tests.

Until step 4 produces a real payload, any further parser work is guessing.
