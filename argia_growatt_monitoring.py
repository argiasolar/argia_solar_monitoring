# argia_growatt_monitoring.py
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _log(msg: str) -> None:
    # keep it simple; your repo already routes logs elsewhere if needed
    print(msg, flush=True)


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def _safe_json_loads(text: str) -> Any:
    """
    Growatt sometimes returns HTML or JSON-with-garbage.
    Try to extract JSON object/array from a response body.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    # find first { or [ and try from there
    i1 = text.find("{")
    i2 = text.find("[")
    starts = [i for i in (i1, i2) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    try:
        return json.loads(text[start:])
    except Exception:
        return None


def _is_login_html(text: str) -> bool:
    t = (text or "").lower()
    # your logs show "<html data-name="dumpLogin">" and "not login"
    return ("dumplogin" in t) or ("login page" in t and "assToken" not in t) or ("not login" in t)


def _extract_urls_from_html(html: str) -> List[str]:
    """
    Useful for debugging: scrape AJAX endpoints referenced in PV page.
    """
    if not html:
        return []
    # capture /something/something or /newInvAPI.do?... etc.
    urls = set(re.findall(r'(["\'])(/[^"\']+?)\1', html))
    # urls is list of tuples ('"', '/path'); keep only path
    out = []
    for _, u in urls:
        # ignore assets
        if u.endswith((".png", ".jpg", ".css", ".js", ".gif", ".ico")):
            continue
        out.append(u)
    # keep it stable
    out = sorted(set(out))
    # keep only likely API-ish endpoints
    return [u for u in out if any(x in u for x in (".do", "/panel/", "/device/", "op=", "get"))]


@dataclass
class GrowattWebClient:
    base: str
    username: str
    password: str
    out_dir: str = "out"
    debug: bool = False

    def __post_init__(self) -> None:
        self.s = requests.Session()
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        )
        self.s.headers.update(
            {
                "User-Agent": ua,
                "Accept-Language": "en-US,en;q=0.9,es;q=0.8,pl;q=0.7,cs;q=0.6",
                "Connection": "keep-alive",
            }
        )
        _ensure_dir(self.out_dir)

    # -------------------------
    # Low-level request helpers
    # -------------------------
    def _req(self, method: str, path: str, *, params=None, data=None, headers=None, timeout=45) -> Tuple[int, Dict[str, str], Any, str]:
        url = self.base.rstrip("/") + path
        resp = self.s.request(method, url, params=params, data=data, headers=headers, timeout=timeout)
        text = resp.text or ""
        parsed = None
        try:
            parsed = resp.json()
        except Exception:
            parsed = _safe_json_loads(text)
        return resp.status_code, dict(resp.headers), parsed, text

    def _ajax_headers(self, referer_path: str) -> Dict[str, str]:
        return {
            "Origin": self.base.rstrip("/"),
            "Referer": self.base.rstrip("/") + referer_path,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

    # -------------------------
    # Login + plant priming (the critical part)
    # -------------------------
    def login(self) -> None:
        # GET /login first (web UI does it)
        st, _, _, _ = self._req("GET", "/login", timeout=30)
        _log(f"GET /login -> {st}")
        if st != 200:
            raise RuntimeError(f"GET /login failed: HTTP {st}")

        payload = {"account": self.username, "password": self.password}
        headers = self._ajax_headers("/login")
        st, _, _, body = self._req("POST", "/login", data=payload, headers=headers, timeout=30)
        _log(f"POST /login -> {st} (len={len(body or '')})")

        cookies = self.s.cookies.get_dict()
        if self.debug:
            _log("Cookies after login: " + (" | ".join([f"{k}" for k in sorted(cookies.keys())])))

        if "assToken" not in cookies:
            snippet = (body or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(f"Login failed: assToken missing. HTTP={st} body='{snippet}'")

        _log("✅ Login OK (assToken present).")

        # Optional: hit /index once (keeps session consistent)
        st, _, _, html = self._req("GET", "/index", timeout=30)
        if self.debug:
            _log(f"GET /index -> {st} (len={len(html or '')})")

    def _seed_plant_cookies(self, plant_id: str) -> None:
        # These match your working growatt_weather_fetch.py approach
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    def prime_plant_session(self, plant_id: str) -> str:
        """
        CRITICAL: this is what flipped your probe from 500/not-login to 200 PV page.
        It warms the session with plantId context like the UI.
        Returns the PV page HTML (useful for autodiscovery/debug).
        """
        self._seed_plant_cookies(plant_id)

        # Warm up /device
        st, _, _, dev_html = self._req("GET", "/device", params={"plantId": plant_id}, timeout=30)
        if self.debug:
            _log(f"GET /device?plantId={plant_id} -> {st} (len={len(dev_html or '')})")

        # PV page must be requested with plantId param
        st, _, _, pv_html = self._req("GET", "/device/photovoltaic", params={"plantId": plant_id}, timeout=30)
        if self.debug:
            _log(f"GET /device/photovoltaic?plantId={plant_id} -> {st} (len={len(pv_html or '')})")

        # Save PV HTML for debugging if needed
        if self.debug:
            _write_text(os.path.join(self.out_dir, f"growatt_pv_{plant_id}.html"), pv_html or "")
            urls = _extract_urls_from_html(pv_html or "")
            if urls:
                _write_text(os.path.join(self.out_dir, f"growatt_pv_{plant_id}_urls.txt"), "\n".join(urls))

        if st != 200 or _is_login_html(pv_html):
            snippet = (pv_html or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(f"PV page not accessible after priming. HTTP={st} snippet='{snippet}'")

        return pv_html or ""

    # -------------------------
    # ENV (already proven) – kept for completeness
    # -------------------------
    def get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        self._seed_plant_cookies(plant_id)
        headers = self._ajax_headers("/device/getEnvPage")
        data = {"plantId": str(plant_id), "currPage": str(curr_page), "alias": alias}
        st, _, parsed, raw = self._req("POST", "/device/getEnvList", headers=headers, data=data, timeout=45)
        if self.debug:
            _log(f"POST /device/getEnvList (plantId={plant_id}) -> {st}")
        return parsed if parsed is not None else {"_non_json": True, "text": raw, "_http": st}

    # -------------------------
    # Plant daily kWh + inverter daily kWh
    # -------------------------
    def _try_json_endpoints(self, plant_id: str, candidates: List[Tuple[str, str, Dict[str, Any]]]) -> Tuple[Optional[Any], Optional[str]]:
        """
        Try multiple endpoints until we get JSON that is not login HTML.
        candidates: (method, path, params_or_data)
        Returns (parsed_json, used_path) or (None, None)
        """
        for method, path, payload in candidates:
            if method.upper() == "GET":
                st, _, parsed, raw = self._req("GET", path, params=payload, timeout=45)
            else:
                headers = self._ajax_headers("/device/photovoltaic")
                st, _, parsed, raw = self._req("POST", path, data=payload, headers=headers, timeout=45)

            if parsed is None and not raw:
                continue
            if _is_login_html(raw):
                if self.debug:
                    _log(f"   ⚠️ {method} {path} -> login-html (HTTP {st})")
                continue
            if parsed is None:
                # still maybe HTML but not login; skip
                if self.debug:
                    _log(f"   ⚠️ {method} {path} -> non-json (HTTP {st})")
                continue

            if self.debug:
                _log(f"   ✅ {method} {path} -> JSON (HTTP {st})")
            return parsed, f"{method} {path}"
        return None, None

    def fetch_daily_kwh_per_plant(self, plant_id: str, day_iso: str) -> Optional[float]:
        """
        Returns plant daily kWh if we can find it via known Growatt web JSON endpoints.
        NOTE: endpoints vary by account/region; we try safe candidates without guessing too wildly.
        """
        # must prime first
        self.prime_plant_session(plant_id)

        # common patterns seen on Growatt ShineServer accounts
        candidates: List[Tuple[str, str, Dict[str, Any]]] = [
            # plant data endpoints (some accounts need plantId in query)
            ("GET", "/newPlantAPI.do", {"op": "getPlantData", "plantId": str(plant_id), "date": day_iso}),
            ("GET", "/newPlantAPI.do", {"op": "getPlantData", "plantId": str(plant_id)}),
            ("GET", "/newPlantAPI.do", {"op": "getPlantDetail", "plantId": str(plant_id)}),
            ("GET", "/newPlantAPI.do", {"op": "getPlantInfo", "plantId": str(plant_id)}),
        ]

        js, used = self._try_json_endpoints(plant_id, candidates)
        if js is None:
            if self.debug:
                _log(f"   ❌ Could not find plant daily kWh JSON endpoint for plantId={plant_id}")
            return None

        # Try to extract a “today energy” style field from typical response shapes
        # We keep this flexible: you can harden once you see your real payload.
        def pick(obj: Any) -> Optional[float]:
            if isinstance(obj, dict):
                # common keys
                for k in ("today_energy", "todayEnergy", "etoday", "eToday", "energyToday", "powerGenerationToday"):
                    if k in obj:
                        try:
                            return float(str(obj[k]).replace(",", "."))
                        except Exception:
                            pass
                # sometimes nested
                for k in ("data", "obj", "plantData", "plant", "result"):
                    if k in obj:
                        v = pick(obj[k])
                        if v is not None:
                            return v
            return None

        val = pick(js)
        if self.debug:
            _log(f"   plantId={plant_id} daily_kWh={val} (source={used})")
        return val

    def fetch_inverter_daily_kwh(self, plant_id: str, day_iso: str) -> List[Dict[str, Any]]:
        """
        Returns list of {sn, daily_kwh, ...} for a plant.
        We:
          1) prime session
          2) try to get inverter list via JSON
          3) for each inverter SN, try to get per-inverter daily energy via JSON
        """
        self.prime_plant_session(plant_id)

        # 1) get inverter list
        inv_list_candidates: List[Tuple[str, str, Dict[str, Any]]] = [
            ("GET", "/newInvAPI.do", {"op": "getInvList", "plantId": str(plant_id)}),
            ("GET", "/newInvAPI.do", {"op": "getInvList"}),
            ("GET", "/newInvAPI.do", {"op": "getInvList", "plantId": str(plant_id), "currPage": "1"}),
        ]
        inv_js, used = self._try_json_endpoints(plant_id, inv_list_candidates)

        if inv_js is None:
            if self.debug:
                _log(f"   ❌ Could not get inverter list JSON for plantId={plant_id}")
            return []

        # Extract inverter SNs
        sns: List[str] = []

        def collect_sns(obj: Any) -> None:
            if isinstance(obj, dict):
                # arrays often under datas/list/obj/datas
                for k in ("datas", "data", "list", "obj", "rows", "result"):
                    if k in obj:
                        collect_sns(obj[k])
                # single inverter record
                for sn_key in ("sn", "deviceSn", "invSn", "inverterSn"):
                    if sn_key in obj and obj[sn_key]:
                        s = str(obj[sn_key]).strip()
                        if s and s.lower() != "null":
                            sns.append(s)
            elif isinstance(obj, list):
                for it in obj:
                    collect_sns(it)

        collect_sns(inv_js)
        sns = sorted(set(sns))

        if self.debug:
            _log(f"   plantId={plant_id} inverter SNs discovered: {len(sns)} (source={used})")

        if not sns:
            return []

        # 2) for each SN, try “inverter data” endpoints (vary per account)
        out: List[Dict[str, Any]] = []

        for sn in sns:
            # candidates (kept conservative)
            inv_data_candidates: List[Tuple[str, str, Dict[str, Any]]] = [
                ("GET", "/newInvAPI.do", {"op": "getInvData", "sn": sn, "date": day_iso}),
                ("GET", "/newInvAPI.do", {"op": "getInvData", "sn": sn}),
                ("GET", "/panel/inverter/getInverterData", {"sn": sn}),          # some accounts use this
                ("GET", "/indexbC/inv/getInvData", {"sn": sn}),                  # some accounts use this
            ]
            js, used2 = self._try_json_endpoints(plant_id, inv_data_candidates)

            if js is None:
                if self.debug:
                    _log(f"   ⚠️ SN={sn} no JSON energy endpoint found")
                continue

            # extract daily energy from payload
            def pick_energy(obj: Any) -> Optional[float]:
                if isinstance(obj, dict):
                    for k in ("etoday", "eToday", "today_energy", "todayEnergy", "energyToday", "e_day", "eday"):
                        if k in obj:
                            try:
                                return float(str(obj[k]).replace(",", "."))
                            except Exception:
                                pass
                    for k in ("data", "obj", "inv", "inverter", "result"):
                        if k in obj:
                            v = pick_energy(obj[k])
                            if v is not None:
                                return v
                return None

            etoday = pick_energy(js)
            out.append(
                {
                    "plantId": str(plant_id),
                    "sn": sn,
                    "day": day_iso,
                    "daily_kwh": etoday,
                    "source": used2,
                }
            )

            # be polite to server
            time.sleep(0.2)

        return out


def build_client_from_env() -> GrowattWebClient:
    base = (_env("GROWATT_BASE", "https://server.growatt.com") or "https://server.growatt.com").rstrip("/")
    user = _env("GROWATT_USERNAME")
    pwd = _env("GROWATT_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("Missing GROWATT_USERNAME or GROWATT_PASSWORD.")
    out_dir = _env("GROWATT_OUT_DIR", "out") or "out"
    debug = _env_bool("GROWATT_DEBUG", False)
    return GrowattWebClient(base=base, username=user, password=pwd, out_dir=out_dir, debug=debug)
