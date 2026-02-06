import os
import json
import logging
from datetime import datetime, timezone

from argia_growatt_monitoring import GrowattMonitoringClient, GrowattAuth


def setup_logging() -> None:
    level = logging.DEBUG if os.getenv("ARGIA_MONITORING_DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main() -> None:
    setup_logging()
    LOG = logging.getLogger("argia.probe")

    username = os.getenv("GROWATT_USERNAME", "")
    password = os.getenv("GROWATT_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("Missing GROWATT_USERNAME / GROWATT_PASSWORD")

    client = GrowattMonitoringClient(GrowattAuth(username=username, password=password))
    client.login()

    LOG.info("=== PROBE START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    results = client.probe_endpoints()

    for method, url, payload in results:
        ok = payload.get("ok")
        LOG.info("---- %s %s (ok=%s) ----", method, url, ok)
        if ok:
            js = payload.get("json")
            txt = json.dumps(js, ensure_ascii=False)[:6000]
            LOG.info("JSON (trimmed): %s", txt)
        else:
            LOG.error("Error: %s", payload.get("error"))

    LOG.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
