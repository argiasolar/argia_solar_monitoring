# argia_growatt_monitoring.py
# ------------------------------------------------------------
# Growatt (server.growatt.com) minimal monitoring client for:
#  1) Plant daily kWh (from /device/photovoltaic?plantId=...)
#  2) Inverter list per plant (via /device/getInvList or discovered AJAX)
#  3) Inverter daily kWh (via discovered /device/* endpoints; best-effort)
#
# It is designed to be run in GitHub Actions and write debug artifacts
# into ./out with SAFE filenames (no '?' etc).
#
# IMPORTANT:
# - This file does NOT require GOOGLE_SHEET_ID. Your probe can load plantIds
#   from elsewhere; this module only talks to Growatt.
# - Endpoints/params in Growatt change. This code is defensive and tries
#   multiple request shapes + discovers AJAX endpoints from HTML.
#
# Usage (example):
#
#   from argia_growatt_monitoring import GrowattAuth, GrowattMonitoringClient
#   auth = GrowattAuth(user=os.environ["GROWATT_USER"], password=os.environ["GROWATT_PASS"])
#   cli = GrowattMonitoringClient(auth)
#   cli.login()
#   html = cli.get_pv_page_html(plant_id=9275498)
#   kwh = cli.parse_plant_etoday_kwh(html)
#   invs = cli.get_inverter_list(plant_id=9275498)
#   for inv in invs:
#       det = cli.get_inverter_daily(inv, plant_id=9275498)
#
# ------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests

LOG = logging.getLogger("argia.growatt.monitoring")


# ---------- helpers ----------

_INVALID_FILENAME_CHARS = r'["<>:|*?\r\n]'


def safe_filename(name: str, max_len: int = 180) -> str:
    """
    Make a filesystem-safe filename (GitHub artifact + NTFS safe).
    Replaces invalid chars including '?' with '_', trims length.
    """
    name = re.sub(_INVALID_FILENAME_CHARS, "_", name)
    name = name.replace("/", "_").replace("\\", "_").strip("._ ")
    if len(name) > max_len:
        name = name[:max_len]
    if not name:
        name = "file"
    return name


def ensure_out_dir(out_dir: Union[str, Path]) -> Path:
    p = Path(out_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def dump_text(out_dir: Union[str, Path], filename: str, text: str) -> Path:
    out = ensure_out_dir(out_dir) / safe_filename(filename)
    out.write_text(text, encoding="utf-8", errors="ignore")
    return out


def dump_json(out_dir: Union[str, Path], filename: str, obj: Any) -> Path:
    out = ensure_out_dir(out_dir) / safe_filename(filename)
    out.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def try_json(resp: requests.Response) -> Dict[str, Any]:
    """
    Try parse JSON; if not JSON, return {_non_json: true, text: ...}
    """
    try:
        return resp.json()
    except Exception:
        return {"_non_json": True, "text": resp.text}


def regex_find_ajax_endpoints(html: str) -> List[str]:
    """
    Extract /device/... endpoints from HTML & JS.
    """
    # quoted URLs like "/device/getInvList"
    found = re.findall(r'["\'](/device/[a-zA-Z0-9_/.-]+)["\']', html)
    # data-url="/device/photovoltaic"
    found += re.findall(r'data-url\s*=\s*["\'](/device/[a-zA-Z0-9_/.-]+)["\']', html)
    # de-dup, keep order
    seen = set()
    out: List[str] = []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ---------- Auth / Client ----------

@dataclass(frozen=True)
class GrowattAuth:
    user: str
    password: str
    base_url: str = "https://server.growatt.com"


class GrowattMonitoringClient:
    """
    Minimal session-based client.
    """

    def __init__(
        self,
        auth: GrowattAuth,
        out_dir: Union[str, Path] = "out",
        timeout_s: int = 30,
        user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) ArgiaSolarMonitoring/1.0",
    ) -> None:
        self.auth = auth
        self.out_dir = Path(out_dir)
        self.timeout_s = timeout_s

        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
                "Connection": "keep-alive",
            }
        )

        self._logged_in = False

    # --- low-level http ---

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self.auth.base_url.rstrip("/") + path

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> requests.Response:
        url = self._url(path)
        resp = self.s.get(url, params=params, timeout=self.timeout_s, allow_redirects=True)
        LOG.info("GET %s -> %s (len=%s)", path, resp.status_code, len(resp.text or ""))
        return resp

    def post(
        self,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> requests.Response:
        url = self._url(path)
        resp = self.s.post(
            url,
            data=data,
            json=json_body,
            headers=headers,
            timeout=self.timeout_s,
            allow_redirects=True,
        )
        LOG.info("POST %s -> %s (len=%s)", path, resp.status_code, len(resp.text or ""))
        return resp

    # --- login ---

    def login(self) -> None:
        """
        Two-step login to establish cookies (assToken etc).
        """
        # Step 1: GET /login to obtain initial cookies
        r1 = self.get("/login")
        if r1.status_code != 200:
            raise RuntimeError(f"GET /login failed: {r1.status_code}")

        # Step 2: POST /login (Growatt expects form encoded)
        payload = {
            "account": self.auth.user,
            "password": self.auth.password,
        }
        r2 = self.post("/login", data=payload)
        if r2.status_code != 200:
            raise RuntimeError(f"POST /login failed: {r2.status_code}")

        cookies = self.s.cookies.get_dict()
        LOG.debug("Cookies after login: %s", cookies)

        if "assToken" not in cookies:
            # Sometimes Growatt returns "success" but doesn't set assToken if blocked
            raise RuntimeError("Login failed: assToken cookie missing")

        self._logged_in = True
        LOG.info(
            "✅ Login OK (assToken present). Cookies: %s",
            " | ".join(sorted(cookies.keys())),
        )

    # --- pages you already confirmed ---

    def get_index_html(self) -> str:
        r = self.get("/index")
        r.raise_for_status()
        return r.text

    def get_device_home_html(self) -> str:
        """
        /device is the device home and contains data-url entries like /device/photovoltaic
        """
        r = self.get("/device")
        r.raise_for_status()
        return r.text

    def get_env_page_html(self, plant_id: Optional[int] = None) -> str:
        """
        /device/getEnvPage works without plantId in your logs, but we allow passing it.
        """
        params = {"plantId": str(plant_id)} if plant_id is not None else None
        r = self.get("/device/getEnvPage", params=params)
        r.raise_for_status()
        return r.text

    def post_env_list(self, plant_id: int, page: int = 1, page_size: int = 10) -> Dict[str, Any]:
        """
        You already confirmed /device/getEnvList works.
        """
        # Try the most common payloads
        payloads = [
            {"plantId": str(plant_id), "currPage": str(page), "pageSize": str(page_size)},
            {"plantId": str(plant_id), "page": str(page), "rows": str(page_size)},
            {"plantId": str(plant_id), "currentPage": str(page), "pageSize": str(page_size)},
        ]
        last: Optional[requests.Response] = None
        for p in payloads:
            r = self.post("/device/getEnvList", data=p)
            last = r
            obj = try_json(r)
            if not obj.get("_non_json") and "datas" in obj:
                return obj
        # fallback return
        return try_json(last) if last is not None else {"_non_json": True, "text": "no response"}

    def get_pv_page_html(self, plant_id: int) -> str:
        """
        CRITICAL: your probe showed /device/photovoltaic returns 500 unless plantId is provided.
        Always pass ?plantId=...
        """
        r = self.get("/device/photovoltaic", params={"plantId": str(plant_id)})
        r.raise_for_status()
        return r.text

    # --- parsing plant daily energy from pv page ---

    @staticmethod
    def parse_plant_etoday_kwh(pv_html: str) -> Optional[float]:
        """
        Extract plant 'Energía hoy' from pv page.
        Usually appears in: <span class="val_device_plantEToday">123.4</span>
        """
        m = re.search(r'class\s*=\s*["\']val_device_plantEToday["\'][^>]*>\s*([0-9.]+)\s*<', pv_html)
        if not m:
            # Sometimes the value is in a JS var; try another generic number near plantEToday
            m2 = re.search(r'plantEToday[^0-9]*([0-9]+(?:\.[0-9]+)?)', pv_html)
            if not m2:
                return None
            try:
                return float(m2.group(1))
            except Exception:
                return None
        try:
            return float(m.group(1))
        except Exception:
            return None

    # --- inverter discovery & data ---

    def get_inverter_page_html(self, plant_id: int) -> str:
        """
        In many accounts, inverter page is /device/getInverterPage.
        If it fails, we try to discover the correct endpoint from /device home HTML.
        """
        # Try direct
        candidates = [
            ("/device/getInverterPage", {"plantId": str(plant_id)}),
            ("/device/getInverterPage", None),
            ("/device/inverter", {"plantId": str(plant_id)}),
        ]
        last_exc: Optional[Exception] = None
        for path, params in candidates:
            try:
                r = self.get(path, params=params)
                if r.status_code == 200 and "<html" not in (r.text[:200].lower()):
                    # could still be html snippet, which is fine
                    return r.text
                if r.status_code == 200 and "inv" in r.text.lower():
                    return r.text
            except Exception as e:
                last_exc = e

        # Discover from /device home
        device_html = self.get_device_home_html()
        endpoints = regex_find_ajax_endpoints(device_html)
        # pick something that looks like inverter page
        inv_paths = [p for p in endpoints if "inv" in p.lower() and "page" in p.lower()]
        inv_paths += [p for p in endpoints if "inverter" in p.lower()]
        inv_paths = inv_paths[:5]

        for p in inv_paths:
            try:
                r = self.get(p, params={"plantId": str(plant_id)})
                if r.status_code == 200:
                    return r.text
            except Exception as e:
                last_exc = e

        raise RuntimeError(f"Could not load inverter page HTML for plant {plant_id}: {last_exc}")

    def _post_inv_list_with_payload(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        r = self.post("/device/getInvList", data=payload)
        obj = try_json(r)
        if obj.get("_non_json"):
            return None
        if isinstance(obj, dict) and "datas" in obj and isinstance(obj["datas"], list):
            return obj
        return None

    def get_inverter_list(
        self,
        plant_id: int,
        max_pages: int = 10,
        page_size: int = 50,
        dump: bool = True,
        sleep_s: float = 0.2,
    ) -> List[Dict[str, Any]]:
        """
        Returns list of inverter objects as Growatt returns them (dicts).
        Tries:
          - POST /device/getInvList with multiple parameter conventions.
          - If that fails, tries to discover alternative endpoints from inverter page HTML.
        """
        if not self._logged_in:
            raise RuntimeError("Not logged in. Call cli.login() first.")

        # Ensure plant context is set (Growatt sometimes needs you to open /device?plantId=..)
        try:
            self.get("/device", params={"plantId": str(plant_id)})
        except Exception:
            pass

        # First attempt: /device/getInvList
        invs: List[Dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            payload_candidates = [
                {"plantId": str(plant_id), "currPage": str(page), "pageSize": str(page_size)},
                {"plantId": str(plant_id), "page": str(page), "rows": str(page_size)},
                {"plantId": str(plant_id), "currentPage": str(page), "pageSize": str(page_size)},
                {"plantId": str(plant_id), "pageNum": str(page), "pageSize": str(page_size)},
            ]
            got_page = False
            for p in payload_candidates:
                obj = self._post_inv_list_with_payload(p)
                if obj is not None:
                    datas = obj.get("datas") or []
                    if dump:
                        dump_json(self.out_dir, f"{plant_id}__invlist__p{page}.json", obj)
                    for d in datas:
                        if isinstance(d, dict):
                            invs.append(d)
                    got_page = True
                    # stop if fewer than page_size
                    if isinstance(datas, list) and len(datas) < page_size:
                        return invs
                    break
            if not got_page:
                break
            time.sleep(sleep_s)

        # Second attempt: discover endpoints from inverter page HTML
        inv_html = self.get_inverter_page_html(plant_id)
        if dump:
            dump_text(self.out_dir, f"{plant_id}__invpage.html", inv_html)

        endpoints = regex_find_ajax_endpoints(inv_html)
        # likely list endpoints include 'getInvList' or similar
        list_paths = [p for p in endpoints if "getinvlist" in p.lower()]
        list_paths += [p for p in endpoints if "invlist" in p.lower()]
        list_paths = list(dict.fromkeys(list_paths))[:5]

        for path in list_paths:
            for page in range(1, max_pages + 1):
                payload_candidates = [
                    {"plantId": str(plant_id), "currPage": str(page), "pageSize": str(page_size)},
                    {"plantId": str(plant_id), "page": str(page), "rows": str(page_size)},
                    {"plantId": str(plant_id), "currentPage": str(page), "pageSize": str(page_size)},
                ]
                for p in payload_candidates:
                    r = self.post(path, data=p)
                    obj = try_json(r)
                    if not obj.get("_non_json") and "datas" in obj and isinstance(obj["datas"], list):
                        if dump:
                            dump_json(self.out_dir, f"{plant_id}__{path}__p{page}.json", obj)
                        for d in obj["datas"]:
                            if isinstance(d, dict):
                                invs.append(d)
                        if len(obj["datas"]) < page_size:
                            return invs
                        break
                time.sleep(sleep_s)

        # If still nothing, return empty list (caller can handle)
        if dump:
            dump_json(self.out_dir, f"{plant_id}__invlist__EMPTY.json", {"plantId": plant_id, "inverters": []})
        return invs

    def get_inverter_daily(
        self,
        inverter: Dict[str, Any],
        plant_id: Optional[int] = None,
        dump: bool = True,
    ) -> Dict[str, Any]:
        """
        Best-effort: fetch inverter daily metrics (especially eToday kWh).
        Growatt may use different endpoints. We try common ones and anything we
        can discover from inverter page HTML.
        """
        # extract likely identifiers
        inv_id = (
            inverter.get("invId")
            or inverter.get("id")
            or inverter.get("deviceId")
            or inverter.get("device_id")
        )
        inv_sn = (
            inverter.get("invSn")
            or inverter.get("sn")
            or inverter.get("deviceSn")
            or inverter.get("datalogSn")
        )

        candidates: List[Tuple[str, Dict[str, Any]]] = []

        # Common patterns (these may differ by account/version)
        if inv_id:
            candidates += [
                ("/device/getInvDetail", {"invId": str(inv_id)}),
                ("/device/getInvDetail", {"id": str(inv_id)}),
                ("/device/getInvData", {"invId": str(inv_id)}),
                ("/device/getInvData", {"id": str(inv_id)}),
                ("/device/inv/getInvData", {"invId": str(inv_id)}),
            ]
        if inv_sn:
            candidates += [
                ("/device/getInvDetail", {"sn": str(inv_sn)}),
                ("/device/getInvData", {"sn": str(inv_sn)}),
            ]
        # Some installs still expose these older routes (you saw 404 with TEST_SN, but try anyway)
        if inv_sn:
            candidates += [
                ("/panel/inverter/getInverterData", {"sn": str(inv_sn)}),
                ("/indexbC/inv/getInvData", {"sn": str(inv_sn)}),
            ]

        # If plant_id provided, some endpoints need it
        if plant_id is not None:
            extra: List[Tuple[str, Dict[str, Any]]] = []
            for path, params in candidates:
                params2 = dict(params)
                params2.setdefault("plantId", str(plant_id))
                extra.append((path, params2))
            candidates = extra + candidates

        # Try GETs first
        last_obj: Optional[Dict[str, Any]] = None
        for path, params in candidates:
            try:
                r = self.get(path, params=params)
                obj = try_json(r)
                last_obj = obj
                if dump:
                    tag = f"inv_{inv_id or inv_sn or 'unknown'}__{path}__GET.json"
                    dump_json(self.out_dir, tag, {"path": path, "params": params, "resp": obj})
                if not obj.get("_non_json"):
                    # success heuristic: contains daily energy key(s)
                    # common keys: eToday, etoday, e_today, etodayEnergy, etc.
                    flat = json.dumps(obj).lower()
                    if "etoday" in flat or '"eToday"'.lower() in flat or "today" in flat and "kwh" in flat:
                        return obj
            except Exception as e:
                if dump:
                    dump_json(
                        self.out_dir,
                        f"inv_{inv_id or inv_sn or 'unknown'}__{path}__GET__error.json",
                        {"path": path, "params": params, "error": str(e)},
                    )
                continue

        # Try POSTs for same candidates (some Growatt endpoints are POST-only)
        for path, params in candidates:
            try:
                r = self.post(path, data=params)
                obj = try_json(r)
                last_obj = obj
                if dump:
                    tag = f"inv_{inv_id or inv_sn or 'unknown'}__{path}__POST.json"
                    dump_json(self.out_dir, tag, {"path": path, "data": params, "resp": obj})
                if not obj.get("_non_json"):
                    flat = json.dumps(obj).lower()
                    if "etoday" in flat or '"eToday"'.lower() in flat or "today" in flat and "kwh" in flat:
                        return obj
            except Exception as e:
                if dump:
                    dump_json(
                        self.out_dir,
                        f"inv_{inv_id or inv_sn or 'unknown'}__{path}__POST__error.json",
                        {"path": path, "data": params, "error": str(e)},
                    )
                continue

        # Return whatever we last saw (may be non-json login redirect)
        return last_obj or {"_non_json": True, "text": "No inverter daily endpoint succeeded."}

    # ---------- convenience: one-shot plant+inverters snapshot ----------

    def snapshot_plant_and_inverters(
        self,
        plant_id: int,
        dump: bool = True,
        include_inverter_details: bool = True,
    ) -> Dict[str, Any]:
        """
        One-shot snapshot:
          - pv page html
          - plant etoday kWh
          - inverter list
          - inverter daily details best-effort
        """
        pv_html = self.get_pv_page_html(plant_id)
        if dump:
            dump_text(self.out_dir, f"{plant_id}__pvpage.html", pv_html)

        plant_kwh = self.parse_plant_etoday_kwh(pv_html)

        invs = self.get_inverter_list(plant_id, dump=dump)

        inv_details: List[Dict[str, Any]] = []
        if include_inverter_details:
            for inv in invs:
                det = self.get_inverter_daily(inv, plant_id=plant_id, dump=dump)
                inv_details.append({"inv": inv, "detail": det})
                time.sleep(0.15)

        snap = {
            "plantId": plant_id,
            "plant_eToday_kWh": plant_kwh,
            "inverter_count": len(invs),
            "inverters": invs,
            "inverter_details": inv_details,
        }
        if dump:
            dump_json(self.out_dir, f"{plant_id}__snapshot.json", snap)
        return snap


# ---------- minimal CLI (optional) ----------
def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s")


def main() -> None:
    """
    Optional: run directly to test quickly.

    Required env:
      GROWATT_USER
      GROWATT_PASS

    Optional:
      PLANT_IDS="9275498,9309589"
      OUT_DIR="out"
    """
    _setup_logging()

    user = os.environ.get("GROWATT_USER") or os.environ.get("GROWATT_USERNAME")
    pw = os.environ.get("GROWATT_PASS") or os.environ.get("GROWATT_PASSWORD")
    if not user or not pw:
        raise RuntimeError("Missing GROWATT_USER/GROWATT_PASS environment variables.")

    plant_ids_raw = os.environ.get("PLANT_IDS", "")
    plant_ids = [int(x.strip()) for x in plant_ids_raw.split(",") if x.strip().isdigit()]
    if not plant_ids:
        raise RuntimeError("Set PLANT_IDS env like: PLANT_IDS=9275498,9309589")

    out_dir = os.environ.get("OUT_DIR", "out")

    cli = GrowattMonitoringClient(GrowattAuth(user=user, password=pw), out_dir=out_dir)
    cli.login()

    all_snaps = []
    for pid in plant_ids:
        LOG.info("🏭 Snapshot plantId=%s", pid)
        snap = cli.snapshot_plant_and_inverters(pid, dump=True, include_inverter_details=True)
        all_snaps.append(snap)

    dump_json(out_dir, "ALL__snapshot.json", all_snaps)
    LOG.info("✅ Done. Wrote artifacts to %s", out_dir)


if __name__ == "__main__":
    main()
