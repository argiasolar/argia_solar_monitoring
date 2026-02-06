import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from argia_growatt_monitoring import GrowattMonitoringClient, GrowattAuth
from argia_sheets_monitoring import read_snap_config


OUT_DIR = "out_probe"


def setup_logging() -> None:
    level = logging.DEBUG if os.getenv("ARGIA_MONITORING_DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def _trim_text(t: str, n: int = 500) -> str:
    t = (t or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[:n] + "...(trimmed)"


def _trim_json(x: Any, n: int = 1500) -> str:
    try:
        s = json.dumps(x, ensure_ascii=False)
    except Exception:
        s = str(x)
    return s if len(s) <= n else s[:n] + "...(trimmed)"


def extract_paths(html: str, prefix: str = "/device/") -> List[str]:
    if not html:
        return []
    # capture /device/... (stop at quotes/spaces)
    found = re.findall(rf"({re.escape(prefix)}[A-Za-z0-9_\-\/]+)", html)
    out: List[str] = []
    seen = set()
    for p in found:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def extract_ajax_endpoints(html: str) -> List[str]:
    # keep only endpoints containing /device/get...
    eps = extract_paths(html, "/device/")
    out: List[str] = []
    for e in eps:
        if "get" in e.lower():
            out.append(e)
    # dedupe stable
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def try_get_page(cli: GrowattMonitoringClient, path: str, referer_path: str = "/index") -> Tuple[int, str]:
    url = f"{cli.base}{path}"
    r = cli.s.get(
        url,
        headers={"Referer": f"{cli.base}{referer_path}", "Accept": "text/html, */*"},
        timeout=45,
        allow_redirects=True,
    )
    return r.status_code, (r.text or "")


def try_post_endpoint(cli: GrowattMonitoringClient, ep: str, plant_id: str, referer_path: str) -> Tuple[bool, Any]:
    # Generic payload that works for getEnvList and often for other list endpoints
    payload = {"plantId": plant_id, "currPage": "1", "alias": ""}
    try:
        js = cli._post_json(ep, payload, referer_path=referer_path)
        return True, js
    except Exception as e:
        return False, str(e)


def main() -> None:
    setup_logging()
    LOG = logging.getLogger("argia.probe")
    ensure_dir(OUT_DIR)

    username = os.getenv("GROWATT_USERNAME", "")
    password = os.getenv("GROWATT_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    LOG.info("=== PROBE START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    snap = read_snap_config(sheet_id)
    siteids: List[str] = []
    for rec in snap:
        sid = (rec.get("SITEID") or "").strip()
        if sid and sid not in siteids:
            siteids.append(sid)

    LOG.info("Loaded %s SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    cli = GrowattMonitoringClient(GrowattAuth(username=username, password=password))
    cli.login()

    # 0) Fetch /index and extract device paths (this is the main “UI discovery” trick)
    st_index, index_html = try_get_page(cli, "/index", referer_path="/login")
    LOG.info("GET /index -> %s len=%s", st_index, len(index_html))
    write_text(os.path.join(OUT_DIR, "index.html"), index_html)

    index_device_paths = extract_paths(index_html, "/device/")
    LOG.info("Discovered %s /device/* paths in /index", len(index_device_paths))
    if index_device_paths:
        LOG.info("Sample paths: %s", ", ".join(index_device_paths[:25]))

    # Also consider some common landing pages that may appear in newer UI layouts
    candidate_pages = list(index_device_paths)
    candidate_pages.extend([
        "/device",                    # sometimes exists
        "/device/plant",              # sometimes exists
        "/device/getEnvPage",         # known-good
    ])

    # De-dupe
    seen = set()
    cand_pages = []
    for p in candidate_pages:
        if p not in seen:
            cand_pages.append(p)
            seen.add(p)

    # 1) For each plant, probe known-good env + then iterate candidate device pages from /index
    for plant_id in siteids:
        if not plant_id.isdigit():
            LOG.warning("Skipping non-numeric SITEID: %s", plant_id)
            continue

        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        cli.seed_plant_context(plant_id)

        # 1a) Known good env page
        env_html = cli.get_env_page_html(plant_id)
        write_text(os.path.join(OUT_DIR, f"{plant_id}__envpage.html"), env_html)

        eps = extract_ajax_endpoints(env_html)
        LOG.info("Env page AJAX endpoints: %s", ", ".join(eps) if eps else "(none)")

        # Confirm env list works
        try:
            js = cli.post_get_env_list(plant_id, curr_page=1, alias="")
            LOG.info("POST /device/getEnvList OK keys=%s", list(js.keys()) if isinstance(js, dict) else type(js))
        except Exception as e:
            LOG.error("POST /device/getEnvList FAIL: %s", e)

        # 1b) Now: try candidate pages discovered from /index
        # Goal: find the *real* PV/inverter page for this tenant (returns 200 HTML)
        good_pages: List[str] = []
        for path in cand_pages:
            st, html = try_get_page(cli, path, referer_path="/index")
            if st in (200, 302, 500, 404):
                LOG.info("GET %s -> %s len=%s body='%s'", path, st, len(html), _trim_text(html, 220))
            else:
                LOG.info("GET %s -> %s len=%s", path, st, len(html))

            # Save the first time we see a page for this plant (helps debug 500)
            safe_name = path.strip("/").replace("/", "_") or "root"
            write_text(os.path.join(OUT_DIR, f"{plant_id}__{safe_name}.html"), html)

            if st == 200 and len(html) > 500:
                good_pages.append(path)

        # 1c) For each good page, extract /device/get* endpoints and attempt POST
        for page
