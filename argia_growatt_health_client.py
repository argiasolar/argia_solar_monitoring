#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, json, time, math, logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger("argia.growatt.health")

INVALID_FS_CHARS = r'["<>:|*?\r\n]'


def normalize_text(x: Any) -> str:
    return "" if x is None else str(x).strip()


def normalize_sn(x: Any) -> str:
    return re.sub(r"\s+", "", normalize_text(x)).upper()


def safe_float(x: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if x is None:
            return default
        if isinstance(x, str):
            s = x.strip()
            if s == "":
                return default
            s = s.replace(",", "")
            x = s
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def try_parse_json(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except Exception:
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


def pick(d: Dict[str, Any], keys: List[str]) -> Optional[Any]:
    for k in keys:
        if k in d and d[k] not in (None, "", "null"):
            return d[k]
    return None


def norm_key(k: str) -> str:
    """
    Normalize key names across Growatt JSON variants.
    Examples:
      "Vpv1(V)" -> "vpv1"
      "Vac-RS"  -> "vacrs"
      "pac(W)"  -> "pac"
      "Istr_12(A)" -> "istr12"
    """
    k = (k or "").strip().lower()
    # remove units and punctuation
    k = re.sub(r"\(.*?\)", "", k)
    k = re.sub(r"[^a-z0-9]+", "", k)
    return k


@dataclass
class GrowattAuth:
    user: str
    password: str


class GrowattMonitoringClient:
    BASE = "https://server.growatt.com"

    # Safety blacklist
    UNSAFE_PREFIXES = ("/commonDeviceSetC/",)
    UNSAFE_CONTAINS = (
        "setmax", "settlx", "setinverter",
        "delmax", "deltlx", "delinverter",
        "delete", "set", "save",
    )

    def __init__(self, auth: GrowattAuth, timeout: int = 45, debug_dir: str = "out_health"):
        self.auth = auth
        self.timeout = timeout
        self.debug_dir = debug_dir
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "Mozilla/5.0 (ARGIA Growatt Health Bot)", "Accept": "*/*"})

        if self.debug_dir and not os.path.isdir(self.debug_dir):
            os.makedirs(self.debug_dir, exist_ok=True)

    def _safe_filename(self, name: str) -> str:
        name = re.sub(INVALID_FS_CHARS, "_", name)
        name = name.replace("/", "_").strip("_")
        return name

    def _save_debug(self, plant_id: str, label: str, content: str, ext: str = "txt") -> None:
        if not self.debug_dir:
            return
        fn = self._safe_filename(f"{plant_id}__{label}.{ext}")
        path = os.path.join(self.debug_dir, fn)
        with open(path, "w", encoding="utf-8", errors="ignore") as f:
            f.write(content or "")

    def get(self, path: str, params: Optional[dict] = None, referer: Optional[str] = None) -> requests.Response:
        url = self.BASE + path
        headers = {}
        if referer:
            headers["Referer"] = referer
        return self.s.get(url, params=params, headers=headers, timeout=self.timeout, allow_redirects=True)

    def post(self, path: str, data: Optional[dict] = None, referer: Optional[str] = None) -> requests.Response:
        url = self.BASE + path
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if referer:
            headers["Referer"] = referer
        return self.s.post(url, data=data or {}, headers=headers, timeout=self.timeout, allow_redirects=True)

    def login(self) -> None:
        r1 = self.get("/login")
        LOG.info("GET /login -> %s", r1.status_code)
        r2 = self.post("/login", data={"account": self.auth.user, "password": self.auth.password}, referer=self.BASE + "/login")
        LOG.info("POST /login -> %s (len=%s)", r2.status_code, len(r2.text or ""))
        cookies = self.s.cookies.get_dict()
        if "assToken" not in cookies:
            raise RuntimeError("Growatt login failed: assToken cookie missing")
        LOG.info("✅ Growatt login OK (assToken present).")

    def warm_plant_context(self, plant_id: str) -> None:
        self.get("/device")
        pv = self.get("/device/photovoltaic", params={"plantId": plant_id}, referer=self.BASE + "/device")
        if pv.status_code == 200:
            self._save_debug(plant_id, "pvpage", pv.text or "", "html")

        self.s.cookies.set("selectedPlantId", str(plant_id), domain="server.growatt.com", path="/")
        self.s.cookies.set("selPage", "%2Fpanel", domain="server.growatt.com", path="/")

    def get_max_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getMAXPage", params={"ttt": str(now_ms())}, referer=self.BASE + "/index")
        self._save_debug(plant_id, "max_page", r.text or "", "html")
        return r.text or ""

    def get_inverter_page_html(self, plant_id: str) -> str:
        r = self.get("/device/getInverterPage", params={"plantId": str(plant_id)}, referer=self.BASE + "/device")
        self._save_debug(plant_id, "inv_page", r.text or "", "html")
        return r.text or ""

    @staticmethod
    def discover_ajax_urls(html: str) -> List[str]:
        urls: List[str] = []
        for m in re.finditer(r"url\s*:\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))
        for m in re.finditer(r"\$\.(?:post|get)\(\s*['\"](\/[^'\"]+)['\"]", html):
            urls.append(m.group(1))
        seen = set()
        out = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    def _is_safe_endpoint(self, endpoint: str) -> bool:
        ep = endpoint.lower()
        if any(ep.startswith(p) for p in self.UNSAFE_PREFIXES):
            return False
        if any(bad in ep for bad in self.UNSAFE_CONTAINS):
            return False
        # allow list OR data-ish reads
        if not any(tok in ep for tok in ("list", "data", "detail", "kpi", "real", "status", "info")):
            return False
        return True

    def _call_json(self, endpoint: str, payload: dict) -> Optional[dict]:
        r = self.post(endpoint, data=payload, referer=self.BASE + "/index")
        js = try_parse_json(r.text or "")
        if js:
            return js
        r2 = self.get(endpoint, params=payload, referer=self.BASE + "/index")
        js2 = try_parse_json(r2.text or "")
        if js2:
            return js2
        return None

    @staticmethod
    def _extract_items(data: dict) -> List[dict]:
        for k in ("datas", "data", "rows", "result"):
            items = data.get(k)
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    # ---------------------------------------------------------
    # Device list (status) – unchanged
    # ---------------------------------------------------------
    def fetch_devices_best_for_sns(self, plant_id: str, wanted_sns: List[str], page_size: int = 50, max_pages: int = 6) -> Dict[str, Dict[str, Any]]:
        wanted = {normalize_sn(x) for x in wanted_sns if x}
        html_max = self.get_max_page_html(plant_id)
        html_inv = self.get_inverter_page_html(plant_id)
        urls = self.discover_ajax_urls(html_max) + self.discover_ajax_urls(html_inv)

        urls += [
            "/device/getMAXList",
            "/device/getMaxList",
            "/device/getInverterList",
            "/device/getInverterListData",
            "/panel/getDeviceList",
            "/panel/getPlantDeviceList",
            "/device/getDatalogList",
        ]

        seen = set()
        candidates = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                if self._is_safe_endpoint(u):
                    candidates.append(u)

        payload_variants = [
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size), "ind": "1"},
            {"plantId": str(plant_id), "currPage": "1", "pageSize": str(page_size)},
            {"currPage": "1", "pageSize": str(page_size)},
        ]

        best_items: List[Dict[str, Any]] = []
        best_ep = ""
        best_score = (-1, -1, -1, -1)

        def device_type_num(it: Dict[str, Any]) -> Optional[int]:
            v = pick(it, ["deviceType", "deviceTypeNum", "type"])
            n = safe_float(v, None)
            if n is None:
                return None
            try:
                return int(n)
            except Exception:
                return None

        def extract_etoday(it: Dict[str, Any]) -> Optional[float]:
            v = pick(it, ["eToday", "EToday", "todayEnergy", "generationToday", "dayEnergy", "day_energy"])
            if v is not None:
                return safe_float(v, None)
            m = it.get("dataItemMap")
            if isinstance(m, dict):
                return safe_float(pick(m, ["eToday", "day_cap", "daily_cap", "dayEnergy"]), None)
            return None

        def sn_from_item(it: Dict[str, Any]) -> str:
            return normalize_sn(pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"]) or "")

        for ep in candidates:
            all_items: List[Dict[str, Any]] = []
            for page in range(1, max_pages + 1):
                page_items: List[Dict[str, Any]] = []
                for base in payload_variants:
                    payload = dict(base)
                    payload["currPage"] = str(page)
                    payload["pageSize"] = str(page_size)
                    js = self._call_json(ep, payload)
                    if not js:
                        continue
                    items = self._extract_items(js)
                    if items:
                        page_items = items
                        break
                if not page_items:
                    break
                all_items.extend(page_items)
                if len(page_items) < page_size:
                    break

            if not all_items:
                continue

            hits_any = hits_type4 = hits_etoday = 0
            for it in all_items:
                sn = sn_from_item(it)
                if not sn or (wanted and sn not in wanted):
                    continue
                hits_any += 1
                if device_type_num(it) == 4:
                    hits_type4 += 1
                et = extract_etoday(it)
                if et is not None and et > 0:
                    hits_etoday += 1

            score = (hits_etoday, hits_type4, hits_any, len(all_items))
            if score > best_score:
                best_score = score
                best_items = all_items
                best_ep = ep

        if best_ep:
            LOG.info("✅ Best device list endpoint for plant %s is %s score=%s", plant_id, best_ep, best_score)
        else:
            LOG.warning("❌ No device list endpoint produced usable rows for plant %s", plant_id)

        out: Dict[str, Dict[str, Any]] = {}

        def better(existing: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
            def dt(it): return pick(it, ["deviceType", "deviceTypeNum", "type"])
            ex_is4 = safe_float(dt(existing), None) == 4
            ca_is4 = safe_float(dt(cand), None) == 4
            if ca_is4 and not ex_is4:
                return cand
            if ex_is4 and not ca_is4:
                return existing
            # then prefer higher eToday
            def et(it): return safe_float(pick(it, ["eToday", "EToday"]), 0.0) or 0.0
            return cand if et(cand) > et(existing) else existing

        for it in best_items:
            sn = normalize_sn(pick(it, ["sn", "deviceSn", "invSn", "serialNum", "serialNo"]) or "")
            if not sn:
                continue
            if wanted and sn not in wanted:
                continue
            out[sn] = it if sn not in out else better(out[sn], it)

        return out

    # ---------------------------------------------------------
    # KPI / String data – UPDATED extraction
    # ---------------------------------------------------------
    def fetch_health_kpi_for_sn(self, plant_id: str, sn: str) -> Dict[str, Any]:
        sn = normalize_sn(sn)

        html_max = self.get_max_page_html(plant_id)
        html_inv = self.get_inverter_page_html(plant_id)

        urls = self.discover_ajax_urls(html_max) + self.discover_ajax_urls(html_inv)
        urls += [
            "/device/getInverterData",
            "/device/getInverterDetail",
            "/device/getInverterDetailData",
            "/device/getInverterDetailData2",
            "/device/getInverterRealTimeData",
            "/device/getInvRealTimeData",
            "/panel/getDeviceData",
            "/panel/getDeviceDetail",
            "/panel/getInverterData",
            "/panel/getInverterDetail",
        ]

        seen = set()
        candidates = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            if self._is_safe_endpoint(u):
                candidates.append(u)

        payloads = [
            {"sn": sn},
            {"deviceSn": sn},
            {"invSn": sn},
            {"serialNum": sn},
            {"serialNo": sn},
            {"plantId": str(plant_id), "sn": sn},
            {"plantId": str(plant_id), "deviceSn": sn},
        ]

        # canonical keys we want to output (match growatt_health.py expectations)
        canonical = ["Vpv1", "Ipv1", "VacRS", "VacST", "VacTR", "PacR", "PacS", "PacT", "Pac"]
        for i in range(1, 17):
            canonical.append(f"Vstr{i}")
            canonical.append(f"Istr{i}")

        # normalized-to-canonical mapping
        want_map: Dict[str, str] = {norm_key(k): k for k in canonical}

        # Add a few common Growatt synonyms -> canonical
        want_map.update({
            "vpv1v": "Vpv1",
            "ipv1a": "Ipv1",
            "vacrs": "VacRS",
            "vacst": "VacST",
            "vactr": "VacTR",
            "pacr": "PacR",
            "pacs": "PacS",
            "pact": "PacT",
            "pacw": "Pac",
            "pac": "Pac",
        })

        def capture(flat: Dict[str, Any], key: str, val: Any) -> None:
            nk = norm_key(key)
            if nk in want_map and want_map[nk] not in flat:
                flat[want_map[nk]] = val

        def flatten(obj: Any, flat: Dict[str, Any]) -> None:
            # dict
            if isinstance(obj, dict):
                # name/value pair patterns
                # {name:"Vpv1(V)", value:123} OR {key:"vpv1", val:123}
                if any(k in obj for k in ("name", "key")) and any(k in obj for k in ("value", "val", "data", "v")):
                    k = obj.get("name", obj.get("key"))
                    v = obj.get("value", obj.get("val", obj.get("data", obj.get("v"))))
                    if isinstance(k, str):
                        capture(flat, k, v)

                for k, v in obj.items():
                    if isinstance(k, str):
                        capture(flat, k, v)
                    if isinstance(v, (dict, list)):
                        flatten(v, flat)

            # list
            elif isinstance(obj, list):
                for it in obj:
                    flatten(it, flat)

        for ep in candidates[:80]:
            for p in payloads:
                js = self._call_json(ep, p)
                if not js:
                    continue

                flat: Dict[str, Any] = {}
                flatten(js, flat)

                if flat:
                    # save a compact sample so we can inspect shape
                    self._save_debug(
                        plant_id,
                        f"kpi_hit__{sn}__{self._safe_filename(ep)}",
                        json.dumps(js, ensure_ascii=False)[:20000],
                        "json",
                    )
                    flat["_endpoint"] = ep
                    return flat

        # nothing found: save endpoint list + one sample response (first working json)
        self._save_debug(plant_id, f"kpi_miss__{sn}", "\n".join(candidates[:80]), "txt")
        return {}
