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
    found = re.findall(rf"({re.escape(prefix)}[A-Za-z0-9_\-\/]+)", html)
    out: List[str] = []
    seen = set()
    for p in found:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


def extract_ajax_endpoints(html: str) -> List[str]:
    eps = extract_paths(html, "/device/")
    out: List[str] = []
    for e in eps:
        if "get" in e.lower():
            out.append(e)
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


def try_post_endpoint(cli: GrowattMonitoringClient, ep: str, plant_id: str, referer_path: str):
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

    # ---- STEP 1: FETCH /index ----
    st_index, index_html = try_get_page(cli, "/index", referer_path="/login")
    LOG.info("GET /index -> %s len=%s", st_index, len(index_html))
    write_text(os.path.join(OUT_DIR, "index.html"), index_html)

    device_paths = extract_paths(index_html, "/device/")
    LOG.info("Discovered %d /device/* paths in /index", len(device_paths))
    if device_paths:
        LOG.info("Sample paths: %s", ", ".join(device_paths[:20]))

    candidate_pages = list(dict.fromkeys(
        device_paths + [
            "/device",
            "/device/getEnvPage",
        ]
    ))

    # ---- STEP 2: PER PLANT ----
    for plant_id in siteids:
        if not plant_id.isdigit():
            LOG.warning("Skipping non-numeric SITEID: %s", plant_id)
            continue

        LOG.info("==============================================")
        LOG.info("🏭 PlantId=%s", plant_id)

        cli.seed_plant_context(plant_id)

        # ENV PAGE (known good)
        env_html = cli.get_env_page_html(plant_id)
        write_text(os.path.join(OUT_DIR, f"{plant_id}__envpage.html"), env_html)

        ajax_env = extract_ajax_endpoints(env_html)
        LOG.info("Env page AJAX endpoints: %s", ", ".join(ajax_env))

        try:
            js = cli.post_get_env_list(plant_id, curr_page=1, alias="")
            LOG.info("POST /device/getEnvList OK keys=%s", list(js.keys()))
        except Exception as e:
            LOG.error("POST /device/getEnvList FAIL: %s", e)

        # ---- STEP 3: TRY ALL CANDIDATE DEVICE PAGES ----
        good_pages: List[str] = []

        for path in candidate_pages:
            st, html = try_get_page(cli, path)
            LOG.info("GET %s -> %s len=%s body='%s'", path, st, len(html), _trim_text(html, 200))
            safe = path.strip("/").replace("/", "_") or "root"
            write_text(os.path.join(OUT_DIR, f"{plant_id}__{safe}.html"), html)
            if st == 200 and len(html) > 500:
                good_pages.append(path)

        # ---- STEP 4: EXTRACT AJAX FROM GOOD PAGES ----
        for page in good_pages:
            _, html = try_get_page(cli, page)
            ajax_eps = extract_ajax_endpoints(html)
            if not ajax_eps:
                continue

            LOG.info("Page %s AJAX endpoints: %s", page, ", ".join(ajax_eps))

            for ep in ajax_eps:
                if "Env" in ep:
                    continue
                ok, out = try_post_endpoint(cli, ep, plant_id, referer_path=page)
                if ok:
                    LOG.info("POST %s OK -> %s", ep, _trim_json(out, 800))

    LOG.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
