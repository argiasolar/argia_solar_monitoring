import os
import json
import logging
from datetime import datetime

from argia_growatt_monitoring import GrowattMonitoringClient, GrowattAuth

def setup_logging():
    level = logging.DEBUG if os.getenv("ARGIA_MONITORING_DEBUG", "0") == "1" else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

def main():
    setup_logging()
    LOG = logging.getLogger("argia.probe")

    user = os.getenv("GROWATT_USER", "")
    pw = os.getenv("GROWATT_PASS", "")
    if not user or not pw:
        raise RuntimeError("Missing GROWATT_USER / GROWATT_PASS")

    client = GrowattMonitoringClient(GrowattAuth(user, pw))
    client.login()

    LOG.info("=== PROBE START %s ===", datetime.utcnow().isoformat() + "Z")
    results = client.probe_endpoints()

    for method, url, payload in results:
        ok = payload.get("ok")
        LOG.info("---- %s %s (ok=%s) ----", method, url, ok)
        if ok:
            js = payload.get("json")
            # Print trimmed JSON
            txt = json.dumps(js, ensure_ascii=False)[:4000]
            LOG.info("JSON (trimmed): %s", txt)
        else:
            LOG.error("Error: %s", payload.get("error"))

    LOG.info("=== PROBE END ===")

if __name__ == "__main__":
    main()
