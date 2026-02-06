# argia_growatt_monitoring.py
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def write_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def safe_json_loads(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
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


def is_login_html(text: str) -> bool:
    t = (text or "").lower()
    return ("dumplogin" in t) or ("errornologin" in t) or ("not login" in t) or ("/login" in t and "<html" in t)


def extract_urls_from_html(html: str) -> List[str]:
    if not html:
        return []
    hits = re.findall(r'(["\'])(/[^"\']+?)\1', html)
    out = set()
    for _, u in hits:
        if u.endswith((".png", ".jpg", ".css", ".js", ".gif", ".ico")):
            continue
        out.add(u)
    out = sorted(out)
    # keep only likely endpoints (avoid huge noise)
    return [u for u in out if any(x in u for x in (".do", "/panel/", "/device/", "op=", "get"))]


@dataclass
class GrowattAuth:
    """
    What your probe imports.
    Keep it dead simple: values come from env by default.
    """
    username: str
    password: str
    base: str = "https://server.growatt.com"


class GrowattMonitoringClient:
    """
    Compatible surface for argia_probe.py:
      - login()
      - probe_* helpers
      - env list
      - plant daily kWh
      - inverter daily kWh
    """

    def __init__(self, auth: GrowattAuth, out_dir: str = "out", debug: bool = False) -> None:
        self.base = (auth.base or "https://server.growatt.com").rstrip("/")
        self.username = auth.username
        self.password = auth.password
        self.out_dir = out_dir
        self.debug = debug

        ensure_dir(self.out_dir)

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

    # ---------------------------
    # HTTP helpers
    # ---------------------------
    def _url(self, path: str) -> str:
        return self.base + path

    def _req(self, method: str, path: str, *, params=None, data=None, headers=None, timeout=45) -> Tuple[int, Dict[str, str], Any, str]:
        resp = self.s.request(method, self._url(path), params=params, data=data, headers=headers, timeout=timeout)
        text = resp.text or ""
        parsed = None
        try:
            parsed = resp.json()
        except Exception:
            parsed = safe_json_loads(text)
        return resp.status_code, dict(resp.headers), parsed, text

    def _ajax_headers(self, referer_path: str) -> Dict[str, str]:
        return {
            "Origin": self.base,
            "Referer": self.base + referer_path,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

    def _seed_plant_context(self, plant_id: str) -> None:
        # This is what makes the session “stick” per plant like the UI.
        self.s.cookies.set("selectedPlantId", str(plant_id))
        self.s.cookies.set("selPage", "/device")
        self.s.cookies.set("selPageTwo", "/device/photovoltaic")
        self.s.cookies.set("selPageThree", "/device/getEnvPage")

    # ---------------------------
    # Auth
    # ---------------------------
    def login(self) -> None:
        st, _, _, _ = self._req("GET", "/login", timeout=30)
        log(f"GET /login -> {st}")
        if st != 200:
            raise RuntimeError(f"GET /login failed: HTTP {st}")

        payload = {"account": self.username, "password": self.password}
        headers = self._ajax_headers("/login")
        st, _, _, body = self._req("POST", "/login", data=payload, headers=headers, timeout=30)
        log(f"POST /login -> {st} (len={len(body or '')})")

        cookies = self.s.cookies.get_dict()
        if self.debug:
            log("Cookies after login: " + ", ".join([f"'{k}': '{cookies[k]}'" for k in cookies.keys()]))

        if "assToken" not in cookies:
            snippet = (body or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(f"Login failed: assToken cookie missing. HTTP={st} body_snippet='{snippet}'")

        log("✅ Login OK (assToken present). Cookies: " + " | ".join(sorted(cookies.keys())))

        # Optional warm-up (harmless, improves consistency)
        st, _, _, html = self._req("GET", "/index", timeout=30)
        if self.debug:
            log(f"GET /index -> {st} (len={len(html or '')})")

    # ---------------------------
    # Plant priming (critical)
    # ---------------------------
    def prime_plant(self, plant_id: str) -> str:
        """
        This is the reason your probe eventually started returning 200 for /device/photovoltaic?plantId=...
        """
        self._seed_plant_context(plant_id)

        st, _, _, _ = self._req("GET", "/device", params={"plantId": plant_id}, timeout=30)
        if self.debug:
            log(f"GET /device?plantId={plant_id} -> {st}")

        st, _, _, pv_html = self._req("GET", "/device/photovoltaic", params={"plantId": plant_id}, timeout=30)
        if self.debug:
            log(f"GET /device/photovoltaic?plantId={plant_id} -> {st} (len={len(pv_html or '')})")
            write_text(os.path.join(self.out_dir, f"growatt_pv_{plant_id}.html"), pv_html or "")
            urls = extract_urls_from_html(pv_html or "")
            if urls:
                write_text(os.path.join(self.out_dir, f"growatt_pv_{plant_id}_urls.txt"), "\n".join(urls))

        if st != 200 or is_login_html(pv_html):
            snippet = (pv_html or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(f"PV page not accessible after priming. HTTP={st} snippet='{snippet}'")

        return pv_html or ""

    # ---------------------------
    # ENV (already working for you)
    # ---------------------------
    def post_get_env_list(self, plant_id: str, curr_page: int = 1, alias: str = "") -> Any:
        self._seed_plant_context(plant_id)
        headers = self._ajax_headers("/device/getEnvPage")
        data = {"plantId": str(plant_id), "currPage": str(curr_page), "alias": alias}
        st, _, parsed, raw = self._req("POST", "/device/getEnvList", headers=headers, data=data, timeout=45)
        if self.debug:
            log(f"POST /device/getEnvList -> {st}")
        if parsed is None:
            return {"_non_json": True, "text": raw, "_http": st}
        return parsed

    # ---------------------------
    # Plant daily kWh (best effort)
    # ---------------------------
    def fetch_plant_daily_kwh(self, plant_id: str, day_iso: str) -> Optional[float]:
        self.prime_plant(plant_id)

        candidates: List[Tuple[str, str, Dict[str, Any]]] = [
            ("GET", "/newPlantAPI.do", {"op": "getPlantData", "plantId": str(plant_id), "date": day_iso}),
            ("GET", "/newPlantAPI.do", {"op": "getPlantData", "plantId": str(plant_id)}),
            ("GET", "/newPlantAPI.do", {"op": "getPlantDetail", "plantId": str(plant_id)}),
            ("GET", "/newPlantAPI.do", {"op": "getPlantInfo", "plantId": str(plant_id)}),
        ]

        js, used = self._try_json_candidates(candidates)
        if js is None:
            if self.debug:
                log(f"❌ No JSON plant endpoint matched for plantId={plant_id}")
            return None

        def pick(obj: Any) -> Optional[float]:
            if isinstance(obj, dict):
                for k in ("today_energy", "todayEnergy", "etoday", "eToday", "energyToday", "powerGenerationToday"):
                    if k in obj:
                        try:
                            return float(str(obj[k]).replace(",", "."))
                        except Exception:
                            pass
                for k in ("data", "obj", "plantData", "plant", "result"):
                    if k in obj:
                        v = pick(obj[k])
                        if v is not None:
                            return v
            return None

        val = pick(js)
        if self.debug:
            log(f"plantId={plant_id} daily_kWh={val} (source={used})")
        return val

    # ---------------------------
    # Inverter list + inverter daily kWh (best effort)
    # ---------------------------
    def fetch_inverter_daily_kwh(self, plant_id: str, day_iso: str) -> List[Dict[str, Any]]:
        self.prime_plant(plant_id)

        inv_list_candidates: List[Tuple[str, str, Dict[str, Any]]] = [
            ("GET", "/newInvAPI.do", {"op": "getInvList", "plantId": str(plant_id)}),
            ("GET", "/newInvAPI.do", {"op": "getInvList"}),
            ("GET", "/newInvAPI.do", {"op": "getInvList", "plantId": str(plant_id), "currPage": "1"}),
        ]

        inv_js, used = self._try_json_candidates(inv_list_candidates)
        if inv_js is None:
            if self.debug:
                log(f"❌ No inverter list JSON for plantId={plant_id}")
            return []

        sns: List[str] = []

        def collect(obj: Any) -> None:
            if isinstance(obj, dict):
                for k in ("datas", "data", "list", "obj", "rows", "result"):
                    if k in obj:
                        collect(obj[k])
                for sn_key in ("sn", "deviceSn", "invSn", "inverterSn"):
                    if sn_key in obj and obj[sn_key]:
                        s = str(obj[sn_key]).strip()
                        if s and s.lower() != "null":
                            sns.append(s)
            elif isinstance(obj, list):
                for it in obj:
                    collect(it)

        collect(inv_js)
        sns = sorted(set(sns))

        if self.debug:
            log(f"plantId={plant_id} inverter SNs: {len(sns)} (source={used})")

        out: List[Dict[str, Any]] = []
        for sn in sns:
            inv_data_candidates: List[Tuple[str, str, Dict[str, Any]]] = [
                ("GET", "/newInvAPI.do", {"op": "getInvData", "sn": sn, "date": day_iso}),
                ("GET", "/newInvAPI.do", {"op": "getInvData", "sn": sn}),
                ("GET", "/panel/inverter/getInverterData", {"sn": sn}),
                ("GET", "/indexbC/inv/getInvData", {"sn": sn}),
            ]
            js, used2 = self._try_json_candidates(inv_data_candidates)
            if js is None:
                if self.debug:
                    log(f"⚠️ SN={sn} no JSON data endpoint matched")
                continue

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
            out.append({"plantId": str(plant_id), "sn": sn, "day": day_iso, "daily_kwh": etoday, "source": used2})
            time.sleep(0.2)

        return out

    # ---------------------------
    # Internal helper: try endpoints until JSON (not login html)
    # ---------------------------
    def _try_json_candidates(self, candidates: List[Tuple[str, str, Dict[str, Any]]]) -> Tuple[Optional[Any], Optional[str]]:
        for method, path, payload in candidates:
            if method.upper() == "GET":
                st, _, parsed, raw = self._req("GET", path, params=payload, timeout=45)
            else:
                headers = self._ajax_headers("/device/photovoltaic")
                st, _, parsed, raw = self._req("POST", path, data=payload, headers=headers, timeout=45)

            if is_login_html(raw):
                if self.debug:
                    log(f"⚠️ {method} {path} -> login-html (HTTP {st})")
                continue
            if parsed is None:
                if self.debug:
                    log(f"⚠️ {method} {path} -> non-json (HTTP {st})")
                continue

            if self.debug:
                log(f"✅ {method} {path} -> JSON (HTTP {st})")
            return parsed, f"{method} {path}"
        return None, None


def auth_from_env() -> GrowattAuth:
    user = env("GROWATT_USERNAME")
    pwd = env("GROWATT_PASSWORD")
    if not user or not pwd:
        raise RuntimeError("Missing GROWATT_USERNAME or GROWATT_PASSWORD.")
    base = env("GROWATT_BASE", "https://server.growatt.com") or "https://server.growatt.com"
    return GrowattAuth(username=user, password=pwd, base=base)
