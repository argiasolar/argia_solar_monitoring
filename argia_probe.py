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

    cli = GrowattMonitoringClient(GrowattAuth(username=username, password=password))

    LOG.info("=== LOGIN START %s ===", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    cli.login()

    ok, js = cli.auth_check()
    LOG.info("Auth check: %s", "OK" if ok else "FAIL")
    LOG.info("Auth check payload (trimmed): %s", json.dumps(js, ensure_ascii=False)[:3000])

    # Probe a few candidate endpoints we care about
    probes = [
        ("/newPlantAPI.do", {"op": "getPlantList"}),
        ("/newInvAPI.do", {"op": "getInvList"}),
        ("/panel/inverter/getInverterData", {"sn": "TEST_SN"}),
        ("/indexbC/inv/getInvData", {"sn": "TEST_SN"}),
    ]

    for path, params in probes:
        try:
            out = cli._get_json(path, params=params, referer_path="/index")
            LOG.info("---- PROBE %s OK ----", path)
            LOG.info("JSON (trimmed): %s", json.dumps(out, ensure_ascii=False)[:2000])
        except Exception as e:
            LOG.error("---- PROBE %s FAIL: %s ----", path, e)

    LOG.info("=== PROBE END ===")


if __name__ == "__main__":
    main()
