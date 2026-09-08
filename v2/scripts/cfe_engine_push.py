"""Push the CFE tariff overlay to the ARGIA Engine (pio06 only).

Contract: ARGIA_PC_KIT/patch_outbox/reports/CFE_ENGINE_INGEST_CONTRACT.md
(2026-09-03). The engine consumes live CFE rates as a runtime overlay on
its seed: we POST the overlay JSON to
POST {CFE_PUSH_URL} with header X-ARGIA-CFE-KEY.

What one run does:
  1. pulls the engine's six tariff codes for the target year from
     cfe_tariff (scrape-preferred, master_db_10 fallback — the ORDER BY
     puts cfe_scrape rows last so the fold lets them win),
  2. builds the overlay {year, sourceThrough, generated, data} and
     validates it LOCALLY against the same gates the engine enforces
     (ranges per unit, >= 60 values, month format) — an invalid overlay
     never leaves this host,
  3. POSTs it. 200 -> log applied stats; anything else -> exit 1, which
     fails the systemd unit and rides the maintenance alert channel.

Config lives in a root-only file (default /root/.argia_cfe_push;
KEY=VALUE: CFE_PUSH_URL, CFE_PUSH_KEY) — never in the repo, env exports,
or logs. Missing config -> log-and-skip (exit 0): a plant server must
never crash because the engine link is not set up.

Triggers: argia-cfe-push.timer daily at 15:45 UTC (30 min after the CFE
ingest) AND event-driven — cfe_ingest.py starts this unit immediately
after loading a new CSV. Re-pushes are idempotent on the engine side.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from argia.store import pg_mirror

LOG = logging.getLogger("argia.cfe_engine_push")

CONFIG_PATH = "/root/.argia_cfe_push"

ENGINE_CODES = ("GDMTH", "GDMTO", "PDBT", "RAMT", "GDBT", "DIST")
# the ten charges the engine models, grouped by validation unit
CHARGES_ENERGY = ("ENERGIA BASE", "ENERGIA INTERMEDIA", "ENERGIA PUNTA",
                  "TRANSMISION", "CENACE", "SERVICIOS CONEXOS NO MEM")
CHARGES_DEMAND = ("CAPACIDAD", "DISTRIBUCION")
CHARGE_SUMINISTRO = "SUMINISTRO BASICO"
CHARGE_FACTOR = "FACTOR DE CARGA"
ENGINE_CHARGES = frozenset(CHARGES_ENERGY) | frozenset(CHARGES_DEMAND) | {
    CHARGE_SUMINISTRO, CHARGE_FACTOR}
MIN_VALUES = 60
_YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def load_config(path: str = CONFIG_PATH) -> Optional[Dict[str, str]]:
    """KEY=VALUE file -> dict; None unless both URL and KEY present."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    cfg: Dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        cfg[k.strip()] = v.strip()
    if cfg.get("CFE_PUSH_URL") and cfg.get("CFE_PUSH_KEY"):
        return cfg
    return None


# v239 — energy basis. app.cfe.mx publishes INTEGRATED energy prices
# (generación + transmisión + CENACE + servicios conexos; PDBT also +
# distribución + capacidad, which are per-kWh in that tariff), while
# master_db_10 rows are DECOMPOSED (generación only) and the engine adds
# the adders itself. Pushing scraped cells as-is double-counted
# 0.1946 MXN/kWh on every scraped energy cell (engine memo 2026-09-08,
# CFE_UPSTREAM_DEFECTS_FOR_MONITORING). The overlay now carries every
# energy cell on the decomposed basis and says so.
ENERGY_PRICES = ("ENERGIA BASE", "ENERGIA INTERMEDIA", "ENERGIA PUNTA")
ADDERS = ("TRANSMISION", "CENACE", "SERVICIOS CONEXOS NO MEM")
ADDERS_PDBT = ADDERS + ("DISTRIBUCION", "CAPACIDAD")
ENERGY_BASIS = "decomposed"


def adders_for(code: str) -> Tuple[str, ...]:
    return ADDERS_PDBT if code == "PDBT" else ADDERS


def decompose(integrated: float, adders: Sequence[Optional[float]]) -> Optional[float]:
    """Integrated price minus the adders, 4 dp; None when an adder is
    missing or the result is not a positive price (a decomposed cell
    that slipped into a scraped block would go negative here). Pure."""
    if any(a is None for a in adders):
        return None
    v = round(integrated - sum(float(a) for a in adders), 4)
    return v if v > 0 else None


def build_overlay(rows: Sequence[Tuple[str, str, str, str, str, str]],
                  year: int,
                  now: Optional[dt.datetime] = None) -> dict:
    """Fold (code, region, charge, ym, value, source) rows into the
    engine overlay. Later rows win per key — feed rows ordered with
    cfe_scrape LAST (the contract SQL does) so scrape beats master.
    Unknown codes/charges (SEMIPUNTA included) and bad months/values
    are dropped, mirroring the reference generator.

    v239: a scraped ENERGIA cell is integrated, so it is decomposed
    with the adders of the same tariff/region/month before it goes out;
    when the adders are missing or the arithmetic gives no positive
    price the master (decomposed) value is kept instead, or the cell is
    dropped when there is none. ``basis`` in the result counts what
    happened. Pure."""
    data: dict = {}
    master: dict = {}
    origin: dict = {}
    scrape_months = set()
    for code, region, charge, ym, value, source in rows:
        if code not in ENGINE_CODES or charge not in ENGINE_CHARGES:
            continue
        if not _YM.match(ym or ""):
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v != v or v in (float("inf"), float("-inf")):
            continue
        data.setdefault(code, {}).setdefault(region, {}) \
            .setdefault(charge, {})[ym] = v
        origin[(code, region, charge, ym)] = source
        if source == "master_db_10":
            master[(code, region, charge, ym)] = v
        if source == "cfe_scrape":
            scrape_months.add(ym)
    stats = {"decomposed": 0, "kept_master": 0, "dropped": 0}
    for code, regions in data.items():
        for region, charges in regions.items():
            for charge in ENERGY_PRICES:
                months = charges.get(charge) or {}
                for ym in list(months):
                    if origin.get((code, region, charge, ym)) != "cfe_scrape":
                        continue
                    adders = [charges.get(a, {}).get(ym) for a in adders_for(code)]
                    dv = decompose(months[ym], adders)
                    if dv is not None:
                        months[ym] = dv
                        stats["decomposed"] += 1
                    elif (code, region, charge, ym) in master:
                        months[ym] = master[(code, region, charge, ym)]
                        stats["kept_master"] += 1
                    else:
                        del months[ym]
                        stats["dropped"] += 1
                if not months and charge in charges:
                    del charges[charge]
    now = now or dt.datetime.now(dt.timezone.utc)
    return {
        "year": int(year),
        "sourceThrough": max(scrape_months) if scrape_months else None,
        "generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "energyBasis": ENERGY_BASIS,
        "data": data,
        "basis": stats,
    }


def _range_for(charge: str) -> Tuple[float, float]:
    if charge == CHARGE_FACTOR:
        return 0.0, 1.0
    if charge == CHARGE_SUMINISTRO:
        return 0.0, 200000.0
    if charge in CHARGES_DEMAND:
        return 0.0, 5000.0
    return 0.0, 50.0                      # energy MXN/kWh


def validate_overlay(ov: dict) -> List[str]:
    """The engine's own gates, run locally BEFORE the push — one
    out-of-range value rejects the whole overlay (same discipline as
    our CFE ingest). Returns [] when the payload should pass. Pure."""
    errors: List[str] = []
    year = ov.get("year")
    if not isinstance(year, int) or not 2020 <= year <= 2100:
        errors.append(f"year out of range: {year!r}")
    st = ov.get("sourceThrough")
    if st is not None and not _YM.match(str(st)):
        errors.append(f"sourceThrough not YYYY-MM: {st!r}")
    if ov.get("energyBasis") != ENERGY_BASIS:
        errors.append(f"energyBasis must be {ENERGY_BASIS!r}: {ov.get('energyBasis')!r}")
    n = 0
    for code, regions in (ov.get("data") or {}).items():
        if code not in ENGINE_CODES:
            errors.append(f"unknown tariff code: {code}")
            continue
        for region, charges in regions.items():
            for charge, months in charges.items():
                if charge not in ENGINE_CHARGES:
                    errors.append(f"unknown charge: {code}/{charge}")
                    continue
                lo, hi = _range_for(charge)
                for ym, v in months.items():
                    if not _YM.match(ym):
                        errors.append(f"bad month key: {ym}")
                        continue
                    if not isinstance(v, (int, float)) or v != v \
                            or not lo <= float(v) <= hi:
                        errors.append(
                            f"out of range: {code}/{region}/{charge}/"
                            f"{ym} = {v!r} (allowed {lo}-{hi})")
                    n += 1
    if n < MIN_VALUES:
        errors.append(f"coverage floor: {n} values < {MIN_VALUES}")
    return errors


def overlay_stats(ov: dict) -> Tuple[int, int, int]:
    """(tariffs, tariff*region combos, values). Pure."""
    data = ov.get("data") or {}
    combos = sum(len(r) for r in data.values())
    values = sum(len(m) for r in data.values() for c in r.values()
                 for m in c.values())
    return len(data), combos, values


def gather_rows(year: int) -> List[Tuple[str, str, str, str, str, str]]:
    """The contract SQL, verbatim semantics: scrape-preferred via
    ORDER BY (source='cfe_scrape') so scrape rows fold last."""
    from argia.store.pgq import psql_rows
    codes = "','".join(ENGINE_CODES)
    return [tuple(r) for r in psql_rows(
        "SELECT tariff_code, region, charge_type,"
        " to_char(month,'YYYY-MM'), value_mxn, source FROM cfe_tariff"
        f" WHERE tariff_code IN ('{codes}')"
        f" AND to_char(month,'YYYY') = '{int(year)}'"
        " AND source IN ('cfe_scrape','master_db_10')"
        " AND charge_type <> 'ENERGIA SEMIPUNTA'"
        " ORDER BY tariff_code, region, charge_type,"
        " to_char(month,'YYYY-MM'), (source='cfe_scrape');")
        if len(r) >= 6]


def push(cfg: Dict[str, str], overlay: dict, timeout: int = 60
         ) -> Tuple[int, dict]:
    """POST the overlay. Returns (status, parsed body). The secret is
    sent as a header and never logged."""
    body = json.dumps({k: v for k, v in overlay.items() if k != "basis"},
                      separators=(",", ":")).encode()
    req = urllib.request.Request(
        cfg["CFE_PUSH_URL"], data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-ARGIA-CFE-KEY": cfg["CFE_PUSH_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:                                 # noqa: BLE001
            return e.code, {}
    except Exception as e:                                # noqa: BLE001
        LOG.error("push transport error: %s", type(e).__name__)
        return 0, {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="CFE overlay push")
    parser.add_argument("--year", type=int,
                        default=dt.date.today().year)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", help="also write the overlay JSON here")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: "
                               "%(message)s")
    if not pg_mirror.enabled():
        LOG.info("ARGIA_PG_MIRROR not enabled — nothing to do here")
        return 0
    cfg = load_config()
    if not cfg and not args.dry_run:
        LOG.info("no %s — engine push not configured, skipping",
                 CONFIG_PATH)
        return 0

    overlay = build_overlay(gather_rows(args.year), args.year)
    tariffs, combos, values = overlay_stats(overlay)
    LOG.info("overlay %d: tariffs=%d tariff*regions=%d values=%d"
             " through=%s", args.year, tariffs, combos, values,
             overlay["sourceThrough"])
    b = overlay.get("basis") or {}
    LOG.info("energy basis %s: %d scraped cells decomposed, %d kept the"
             " master value (adders missing / non-positive), %d dropped",
             overlay.get("energyBasis"), b.get("decomposed", 0),
             b.get("kept_master", 0), b.get("dropped", 0))
    errors = validate_overlay(overlay)
    if errors:
        for e in errors[:10]:
            LOG.error("validation: %s", e)
        LOG.error("overlay INVALID (%d error(s)) — not pushed",
                  len(errors))
        return 1
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in overlay.items() if k != "basis"}, fh, separators=(",", ":"))
        LOG.info("wrote %s", args.out)
    if args.dry_run:
        LOG.info("dry-run: valid, nothing pushed")
        return 0

    status, resp = push(cfg, overlay)
    if status == 200 and resp.get("ok"):
        LOG.info("engine accepted: applied=%s loaded_at=%s",
                 resp.get("applied"), resp.get("loaded_at"))
        return 0
    LOG.error("engine push FAILED: HTTP %s %s", status,
              json.dumps(resp)[:300])
    return 1


if __name__ == "__main__":
    sys.exit(main())
