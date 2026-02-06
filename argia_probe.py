import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

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


def trim_text(t: str, n: int = 300) -> str:
    t = (t or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[:n] + "...(trimmed)"


def extract_data_urls(html: str) -> List[str]:
    if not html:
        return []
    found = re.findall(r'data-url\s*=\s*"([^"]+)"', html)
    out: List[str] = []
    seen = set()
    for u in found:
        if u.startswith("/device") and u not in seen:
            out.append(u)
            seen.add(u)
    return out


def extract_any_device_paths(html: str) -> List[str]:
    if not html:
        return []
    found = re.findall(r"(/device/[A-Za-z0-9_\-\/\.\?\=\&]+)", html)
    out: List[str] = []
    seen = set()
    for p in found:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def main() -> None:
    setup_logging()
    LOG = logging.getLogger("argia.probe")
    ensure_dir(OUT_DIR)

    username = os.getenv("GROWATT_USERNAME")
    password = os.getenv("GROWATT_PASSWORD")
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        raise RuntimeError("Missing GOOGLE_SHEET_ID")

    LOG.info("=== PROBE START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    snap = read_snap_config(sheet_id)
    siteids: List[str] = []
    for r in snap:
        sid = str(r.get("SITEID", "")).strip()
        if sid and sid not in siteids:
            siteids.append(sid)
    LOG.info("Loaded %d SITEIDs from SNAP: %s", len(siteids), ", ".join(siteids))

    cli = GrowattMonitoringClient(GrowattAuth(username=username, password=password))
    cli.login()

    device_html = cli.get_device_page_html()
    write_text(os.path.join(OUT_DIR, "device.html"), device_html)

    urls = extract_data_urls(device_html)
    paths = extract_any_device_paths(device_html)

    LOG.info("Discovered %d data-url /device* entries", len(urls))
    if urls:
        LOG.info("data-url sample: %s", ", ".join(urls[:25]))

    LOG.info("Discovered %d /device* paths via regex", len(paths))
    if paths:
        LOG.info("path sample: %s", ", ".join(paths[:25]))

    for plant_id in siteids:
        if not plant_id.isdigit():
            LOG.warning("Skipping non-numeric SITEID: %s", plant_id)
            continue

        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        env_html = cli.get_env_page_html(plant_id)
        write_text(os.path.join(OUT_DIR, f"{plant_id}__envpage.html"), env_html)
        LOG.info("Env page endpoints: %s", ", ".join(extract_any_device_paths(env_html)))

        js = cli.post_get_env_list(plant_id, 1, "")
        if isinstance(js, dict):
            LOG.info("POST /device/getEnvList OK keys=%s", list(js.keys()))
        else:
            LOG.info("POST /device/getEnvList OK type=%s", type(js).__name__)

        pv_html = cli.get_pv_page_html(plant_id)
        write_text(os.path.join(OUT_DIR, f"{plant_id}__pvpage.html"), pv_html)
        LOG.info("PV page /device/photovoltaic body='%s'", trim_text(pv_html, 300))

    LOG.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
