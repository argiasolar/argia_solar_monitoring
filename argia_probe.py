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

    LOG.info("=== LOGIN START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    client.login()
    LOG.info("✅ LOGIN VALIDATED")

    LOG.info("=== PROBE START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    # Keep your probe list as before, but now login is validated first
    base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")
    endpoints = [
        ("GET", f"{base}/newPlantAPI.do", {"op": "getPlantList"}),
        ("GET", f"{base}/newInvAPI.do", {"op": "getInvList"}),
    ]

    for method, url, params in endpoints:
        try:
            js = client._get_json(url, params=params, allow_redirects=True)  # intentionally using helper
            LOG.info("---- %s %s OK ----", method, url)
            txt = json.dumps(js, ensure_ascii=False)[:6000]
            LOG.info("JSON (trimmed): %s", txt)
        except Exception as e:
            LOG.error("---- %s %s FAIL: %s ----", method, url, e)

    LOG.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
