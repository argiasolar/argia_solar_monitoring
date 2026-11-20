"""v385: collect the plants' grid meters into ``meter_sample`` (argia.telemetry.meter).

Every 15 minutes, day and night (consumption does not stop at sunset):
today's 5-minute meter history from the vendor, upserted, so a missed run
heals itself on the next one. Each run also refetches up to HEAL_PER_RUN
recent days that hold less than a full day of samples (server down, vendor
gap). ``--backfill N`` fills the last N days once (slow, paced).

Which plants have a meter is asked of the vendors once a day (Huawei
getDevList devTypeId 17, Growatt getMeterList) and kept in
/var/lib/argia/meter/meters.json - no device ids in the public repo.

Read-only towards the vendors. ``--dry-run`` writes nothing anywhere and
prints, per meter and day, the vendor registers next to the integrated
power (the validation view).

Usage: run_job.sh meter meter_collect.py [--dry-run] [--backfill N] [--plants MEX1,SLP1] [--rediscover]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from argia.telemetry import meter as M  # noqa: E402

LOG = logging.getLogger("argia.meter_collect")
MX = ZoneInfo("America/Mexico_City")
STATE_DIR = os.environ.get("ARGIA_METER_DIR", "/var/lib/argia/meter")
DISCOVER_EVERY_H = 24
HEAL_DAYS = 7
HEAL_PER_RUN = 2
HUAWEI_PAUSE_S = 2.0
GROWATT_PAUSE_S = 0.6
GROWATT_MAX_PAGES = 40


class RateLimited(Exception):
    pass


# ------------------------------------------------------------ vendor answers (pure)

def growatt_json(envelope) -> dict:
    """The GrowattWebClient envelope's body as a dict (Growatt serves JSON as text/html)."""
    body = (envelope or {}).get("response")
    if isinstance(body, dict) and "_raw_text" in body:
        try:
            return json.loads(body["_raw_text"])
        except ValueError:
            return {}
    return body if isinstance(body, dict) else {}


def growatt_meters(answer: dict) -> List[dict]:
    """getMeterList answer -> [{'datalog_sn', 'addr'}] (every dict in it carrying both)."""
    found, stack = [], [answer]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            sn, addr = x.get("datalogSn"), x.get("addr")
            if sn and addr not in (None, ""):
                found.append({"datalog_sn": str(sn), "addr": str(addr)})
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    uniq = {(m["datalog_sn"], m["addr"]): m for m in found}
    return [uniq[k] for k in sorted(uniq)]


def huawei_meters(answer: dict) -> List[str]:
    return sorted(str(d["id"]) for d in (answer or {}).get("data") or [] if d.get("devTypeId") == 17 and d.get("id"))


def huawei_check(answer: dict) -> dict:
    if not answer.get("success"):
        if str(answer.get("failCode")) == "407":
            raise RateLimited("Huawei failCode 407 (access frequency)")
        if str(answer.get("failCode")) == "305":
            # one session per account: another job logged in after us. Logging in again would
            # end THEIR session, so this run leaves Huawei alone (the self-heal fetches it later).
            raise RateLimited("Huawei failCode 305 (session taken by another job)")
        raise RuntimeError(f"Huawei failCode={answer.get('failCode')} {str(answer.get('message'))[:120]}")
    return answer


def days_to_fetch(today: dt.date, counts: Dict[str, int], backfill: int = 0, hour: int = 12) -> List[str]:
    """Today; yesterday too in the first hour after midnight (its last samples arrive late);
    up to HEAL_PER_RUN short days of the last HEAL_DAYS; with ``backfill`` every short day of that range."""
    out = [today.isoformat()]
    if hour < 1:
        out.append((today - dt.timedelta(days=1)).isoformat())
    span = max(HEAL_DAYS, backfill)
    past = [(today - dt.timedelta(days=k)).isoformat() for k in range(1, span + 1)]
    short = [d for d in M.short_days(counts, past) if d not in out]
    out += short if backfill else short[:HEAL_PER_RUN]
    return out


# ------------------------------------------------------------ I/O

def _portfolio():
    from argia.core.config import portfolio_from_records
    from argia.core.config_pg import read_inverters_records, read_plants_records
    return portfolio_from_records(read_plants_records(), read_inverters_records())


def _growatt_web():
    from argia.vendors.growatt_web import GrowattWebClient
    w = GrowattWebClient(username=os.environ["GROWATT_USERNAME"], password=os.environ["GROWATT_PASSWORD"])
    w.login()
    return w


class Sessions:
    """One login per vendor account per run, like telemetry_5m: Huawei keeps one session per
    account and refuses logins that come too often (seen 2026-11-20: 'no XSRF-TOKEN')."""

    def __init__(self):
        self._huawei = None
        self._huawei_err = ""
        self._web = None

    def huawei(self):
        """The logged-in client; RateLimited when the one login of this run failed (never retried)."""
        if self._huawei is None:
            if self._huawei_err:
                raise RateLimited(self._huawei_err)
            from argia.vendors.huawei import HuaweiClient
            try:
                c = HuaweiClient(username=os.environ.get("HUAWEI_USERNAME", ""), password=os.environ.get("HUAWEI_PASSWORD", ""))
                c.login()
            except Exception as e:  # noqa: BLE001
                self._huawei_err = f"Huawei login failed: {type(e).__name__} {str(e)[:120]}"
                raise RateLimited(self._huawei_err) from e
            self._huawei = c
        return self._huawei

    def growatt(self):
        if self._web is None:
            self._web = _growatt_web()
        return self._web


def discover(pf, keys: Optional[List[str]], ses: "Sessions", failed: Optional[set] = None) -> List[dict]:
    """The meters the vendors list; plants whose question failed are added to ``failed``."""
    from argia.core.normalize import growatt_site_ids
    meters = []
    failed = set() if failed is None else failed
    for key, p in sorted(pf.plants.items()):
        if not p.active or (keys and key not in keys):
            continue
        brand = (p.brand or "").upper()
        try:
            if brand == "HUAWEI":
                for mid in huawei_meters(huawei_check(ses.huawei()._post_json("/getDevList", {"stationCodes": p.site_id}))):
                    meters.append({"plant_key": key, "brand": brand, "meter_id": mid})
                time.sleep(HUAWEI_PAUSE_S)
            elif brand == "GROWATT":
                web = ses.growatt()
                for sid in growatt_site_ids(p.site_id):
                    web._session.cookies.set("selectedPlantId", str(sid))
                    ans = growatt_json(web._post("/device/getMeterList", {"plantId": str(sid), "currPage": "1"}))
                    for m in growatt_meters(ans):
                        meters.append({"plant_key": key, "brand": brand, "site_id": str(sid),
                                       "meter_id": f"{m['datalog_sn']}:{m['addr']}", **m})
                    time.sleep(GROWATT_PAUSE_S)
        except Exception as e:  # noqa: BLE001 - one plant never stops the others
            failed.add(key)
            LOG.warning("discovery %s failed: %s %s", key, type(e).__name__, str(e)[:200])
    return meters


def merge_discovery(fresh: List[dict], failed: set, previous: List[dict]) -> List[dict]:
    """A plant whose question failed keeps the meters it had (a failed Huawei login must not
    make SAG and Vitalmex disappear for a day). Pure."""
    return fresh + [m for m in previous if m["plant_key"] in failed]


def load_meters(pf, keys, rediscover: bool, dry_run: bool, ses: "Sessions") -> List[dict]:
    path = os.path.join(STATE_DIR, "meters.json")
    previous: List[dict] = []
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        previous = st["meters"]
        age_h = (time.time() - st["fetched"]) / 3600.0
        if not rediscover and age_h < DISCOVER_EVERY_H:
            return [m for m in previous if not keys or m["plant_key"] in keys]
    except (OSError, ValueError, KeyError):
        pass
    failed: set = set()
    meters = merge_discovery(discover(pf, keys, ses, failed), failed, previous)
    if failed:
        LOG.warning("discovery incomplete (%s) - asked again next run", ", ".join(sorted(failed)))
    elif not dry_run and sorted(keys or []) == sorted(M.VALIDATED_PLANTS) and meters:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"fetched": time.time(), "meters": meters}, fh, indent=1)
        os.replace(tmp, path)
    return meters


def day_counts() -> Dict[tuple, Dict[str, int]]:
    """{(plant_key, meter_id): {local day: samples}} for the last weeks; {} before the table exists."""
    from argia.store import pgq
    try:
        rows = pgq.psql_rows("SELECT plant_key, meter_id, (ts_utc AT TIME ZONE 'America/Mexico_City')::date::text,"
                             " count(*) FROM meter_sample WHERE ts_utc > now() - interval '120 days' GROUP BY 1, 2, 3;")
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[tuple, Dict[str, int]] = {}
    for r in rows:
        if len(r) >= 4:
            out.setdefault((r[0], r[1]), {})[r[2]] = int(r[3])
    return out


def fetch_huawei(client, m: dict, day: str) -> List[M.MeterSample]:
    d0 = dt.datetime.fromisoformat(day).replace(tzinfo=MX)
    ans = huawei_check(client._post_json("/getDevFiveMinutes", {"devIds": m["meter_id"], "devTypeId": "17",
                                                                "collectTime": int(d0.timestamp() * 1000)}))
    return [s for s in M.parse_huawei_five_minutes(m["plant_key"], ans) if s.meter_id == m["meter_id"]]


def fetch_growatt(web, m: dict, day: str) -> List[M.MeterSample]:
    web._session.cookies.set("selectedPlantId", m["site_id"])
    rows, start = [], 0
    for _ in range(GROWATT_MAX_PAGES):
        o = growatt_json(web._post("/device/getMeterHistory", {
            "datalogSn": m["datalog_sn"], "addr": m["addr"], "startDate": day, "endDate": day,
            "start": str(start)})).get("obj") or {}
        rows += o.get("datas") or []
        if not o.get("haveNext"):
            break
        nxt = o.get("start")
        start = int(nxt) if nxt not in (None, "") and int(nxt) > start else start + 1
        time.sleep(GROWATT_PAUSE_S)
    return [s for s in M.parse_growatt_meter_history(m["plant_key"], m["meter_id"], rows, MX)
            if s.ts_utc.astimezone(MX).date().isoformat() == day]


def report(m: dict, day: str, samples: List[M.MeterSample]) -> str:
    e = M.day_energy(samples, day, MX)
    if not e:
        return f"{m['plant_key']} {day}: no samples"
    f1 = lambda v: "n/a" if v is None else f"{v:,.1f}"
    gw = [s.grid_w for s in samples if s.grid_w is not None]
    return (f"{m['plant_key']} {day}: n={e['n']} basis={e['basis']} import={f1(e['imp'])} export={f1(e['exp'])}"
            f" | power-integrated import={f1(e['imp_power'])} export={f1(e['exp_power'])}"
            f" | grid_w min={f1(min(gw) if gw else None)} max={f1(max(gw) if gw else None)}")


VENDOR_UNITS = ("argia-telemetry.service", "argia-recon.service")   # the other jobs logging in to the same accounts


def others_active() -> bool:
    import subprocess
    try:
        return any(subprocess.run(["systemctl", "is-active", "-q", u], capture_output=True).returncode == 0
                   for u in VENDOR_UNITS)
    except OSError:                                    # no systemd (tests, laptop)
        return False


def slot_ok(now: dt.datetime) -> bool:
    """Huawei keeps ONE session per account: a login ends the previous one. The telemetry run
    starts on every 5-minute mark (systemd may start it up to a minute late), so vendor calls are
    made only between 1:30 and 4:30 into each 5-minute slot. Pure."""
    s = (now.minute % 5) * 60 + now.second
    return 90 <= s <= 270


def may_call(now: Optional[dt.datetime] = None, active=None) -> bool:
    return slot_ok(now or dt.datetime.now(MX)) and not (active or others_active)()


def gate(patient: bool = False, max_wait_s: int = 600) -> bool:
    """May this run call a vendor now? A backfill waits for the next safe slot, a normal run does not."""
    waited = 0
    while not may_call():
        if not patient or waited >= max_wait_s:
            return False
        time.sleep(10)
        waited += 10
    return True


def wait_for_telemetry(max_s: int = 300, is_active=None) -> bool:
    """Never talk to the vendors while another job does (same accounts).
    True when they are idle, False after ``max_s`` of waiting."""
    is_active = is_active or others_active
    waited = 0
    while waited < max_s:
        if not is_active():
            return True
        time.sleep(5)
        waited += 5
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill", type=int, default=0)
    ap.add_argument("--plants", default="")
    ap.add_argument("--rediscover", action="store_true")
    ap.add_argument("--days", default="", help="explicit local days (comma-separated), dry-run validation")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    keys = [k.strip().upper() for k in a.plants.split(",") if k.strip()] or None
    if keys and not a.dry_run and set(keys) - set(M.VALIDATED_PLANTS):
        LOG.error("only validated plants are stored (%s); use --dry-run to look at others", ", ".join(M.VALIDATED_PLANTS))
        return 2
    now = dt.datetime.now(MX)
    if not wait_for_telemetry():
        LOG.warning("telemetry still running after 5 min - this run skipped")
        return 0
    pf = _portfolio()
    ses = Sessions()
    meters = load_meters(pf, keys or list(M.VALIDATED_PLANTS), a.rediscover, a.dry_run, ses)
    LOG.info("meters: %s", ", ".join(f"{m['plant_key']}({m['brand']})" for m in meters) or "none")
    counts = {} if a.days else day_counts()
    written, huawei_off = 0, False
    for m in sorted(meters, key=lambda m: m["brand"] != "HUAWEI"):    # Huawei first: early in the slot
        days = [d.strip() for d in a.days.split(",") if d.strip()] or \
            days_to_fetch(now.date(), counts.get((m["plant_key"], m["meter_id"]), {}), a.backfill, now.hour)
        for day in days:
            if not a.days and not gate(patient=a.backfill > 0):
                LOG.info("%s: outside the safe slot or another vendor job running - the rest waits for the next run",
                         m["plant_key"])
                break
            try:
                if m["brand"] == "HUAWEI":
                    if huawei_off:
                        break
                    got = fetch_huawei(ses.huawei(), m, day)
                    time.sleep(HUAWEI_PAUSE_S)
                else:
                    got = fetch_growatt(ses.growatt(), m, day)
            except RateLimited as e:
                LOG.warning("%s: %s - Huawei left for the next run", m["plant_key"], e)
                huawei_off = True
                break
            except Exception as e:  # noqa: BLE001 - one meter-day never stops the rest
                LOG.warning("%s %s failed: %s %s", m["plant_key"], day, type(e).__name__, str(e)[:200])
                continue
            if a.dry_run:
                print(report(m, day, got), flush=True)
            written += M.store(got, dry_run=a.dry_run, log=LOG)
    LOG.info("meter samples %s: %d", "that would be written" if a.dry_run else "written", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
