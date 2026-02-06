import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from argia_growatt_monitoring import GrowattMonitoringClient, GrowattAuth
from argia_sheets_monitoring import read_snap_config


def setup_logging() -> None:
    level = logging.DEBUG if os.getenv("ARGIA_MONITORING_DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _trim(x: Any, n: int = 2500) -> str:
    try:
        s = json.dumps(x, ensure_ascii=False)
    except Exception:
        s = str(x)
    return s if len(s) <= n else s[:n] + "...(trimmed)"


def extract_device_ajax_endpoints(html: str) -> List[str]:
    """
    Extract '/device/...' endpoints referenced in HTML/JS.
    We keep it simple: regex on strings that look like /device/xxxx
    """
    if not html:
        return []
    found = re.findall(r"(/device/[A-Za-z0-9_/-]+)", html)
    # keep only likely ajax endpoints (get*List / get*Data / history)
    keep = []
    for f in found:
        if "get" in f.lower():
            keep.append(f)
    # dedupe, stable order
    out = []
    seen = set()
    for x in keep:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out


def main() -> None:
    setup_logging()
    LOG = logging.getLogger("argia.probe")

    username = os.getenv("GROWATT_USERNAME", "")
    password = os.getenv("GROWATT_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    LOG.info("=== PROBE START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    snap = read_snap_config(sheet_id)
    siteids = []
    for rec in snap:
        sid = (rec.get("SITEID") or "").strip()
        if sid and sid not in siteids:
            siteids.append(sid)

    LOG.info("Loaded %s SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    cli = GrowattMonitoringClient(GrowattAuth(username=username, password=password))
    cli.login()

    # We probe only the /device surface that your working script uses.
    # For each plantId, we load pages and discover endpoints.
    for plant_id in siteids:
        if not plant_id.isdigit():
            LOG.warning("Skipping non-numeric SITEID (not a Growatt plantId): %s", plant_id)
            continue

        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        # 1) Seed plant context + fetch env page HTML (known-good)
        cli.seed_plant_context(plant_id)
        env_html = cli.s.get(f"{cli.base}/device/getEnvPage", timeout=45).text or ""
        LOG.info("GET /device/getEnvPage -> len=%s", len(env_html))

        endpoints = extract_device_ajax_endpoints(env_html)
        LOG.info("Endpoints discovered in getEnvPage HTML: %s", ", ".join(endpoints) if endpoints else "(none)")

        # 2) Always test the known-good endpoint: POST /device/getEnvList
        try:
            js = cli._post_json("/device/getEnvList", {"plantId": plant_id, "currPage": "1", "alias": ""}, referer_path="/device/getEnvPage")
            LOG.info("POST /device/getEnvList OK. Keys: %s", list(js.keys()) if isinstance(js, dict) else type(js))
            LOG.info("Sample: %s", _trim(js, 2000))
        except Exception as e:
            LOG.error("POST /device/getEnvList FAIL: %s", e)

        # 3) Try also photovoltaic pages to discover inverter-related endpoints
        pv_paths = [
            "/device/photovoltaic",          # commonly used
            "/device/getPhotovoltaicPage",   # sometimes exists
            "/device/getPlantDevicePage",    # fallback guess
        ]

        for pv_path in pv_paths:
            try:
                r = cli.s.get(f"{cli.base}{pv_path}", headers={"Referer": f"{cli.base}/index"}, timeout=45)
                LOG.info("GET %s -> %s len=%s", pv_path, r.status_code, len(r.text or ""))
                if r.status_code != 200:
                    continue

                pv_html = r.text or ""
                pv_eps = extract_device_ajax_endpoints(pv_html)
                if pv_eps:
                    LOG.info("Endpoints discovered in %s HTML: %s", pv_path, ", ".join(pv_eps))
                else:
                    LOG.info("No /device/get* endpoints found in %s HTML.", pv_path)

                # 4) Try calling discovered endpoints with generic payloads
                # Many Growatt lists accept plantId/currPage/alias; we try those first.
                for ep in pv_eps[:12]:
                    if ep in ("/device/getEnvList", "/device/getEnvHistory"):
                        continue
                    try:
                        js2 = cli._post_json(ep, {"plantId": plant_id, "currPage": "1", "alias": ""}, referer_path=pv_path)
                        LOG.info("POST %s OK. Type=%s", ep, type(js2).__name__)
                        if isinstance(js2, dict):
                            LOG.info("Keys: %s", list(js2.keys())[:40])
                        LOG.info("Sample: %s", _trim(js2, 1500))
                    except Exception as e:
                        # Some endpoints are GET; we’ll see 405 or parse errors; still useful
                        LOG.debug("POST %s failed: %s", ep, e)

            except Exception as e:
                LOG.debug("GET %s failed: %s", pv_path, e)

    LOG.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
