# argia_growatt.py
# ------------------------------------------------------------
# Robust Growatt fetcher for daily energy (kWh) per plant.
# Goals:
# - Use HTTPS
# - Add "mobile-like" headers
# - Exponential backoff + stop retrying on 403
# - Minimize calls: plant_list first, plant_detail only as fallback
# - Optional session persistence (cookies) to reduce logins
#
# Requires: pip install growattServer requests
# Env vars:
#   GROWATT_USERNAME
#   GROWATT_PASSWORD
# Optional:
#   GROWATT_SERVER_URL (default: https://server.growatt.com/)
#   GROWATT_SESSION_FILE (default: .growatt_session.pkl)
# ------------------------------------------------------------

from __future__ import annotations

import os
import time
import random
import pickle
from dataclasses import dataclass
from typing import Dict, Any, Optional

import growattServer


DEFAULT_SERVER_URL = os.environ.get("GROWATT_SERVER_URL", "https://server.growatt.com/").rstrip("/") + "/"
DEFAULT_SESSION_FILE = os.environ.get("GROWATT_SESSION_FILE", ".growatt_session.pkl")


@dataclass
class GrowattConfig:
    server_url: str = DEFAULT_SERVER_URL
    session_file: str = DEFAULT_SESSION_FILE
    login_max_attempts: int = 3
    login_base_backoff_s: int = 10
    login_jitter_s: int = 3
    # If True, try to load/store requests.Session cookies between runs (works only if environment is persistent)
    persist_session: bool = True


def _is_403_error(exc: Exception) -> bool:
    s = str(exc)
    return "403" in s or "Forbidden" in s or "forbidden" in s


def _now_ts() -> float:
    return time.time()


def _sleep_with_jitter(base: float, jitter_max: int) -> None:
    time.sleep(base + random.randint(0, jitter_max))


def _apply_headers(api: growattServer.GrowattApi, server_url: str) -> None:
    # Many blocks happen due to obvious bot headers.
    # Using something app-like helps in practice, even if not guaranteed.
    api.session.headers.update(
        {
            "User-Agent": "okhttp/4.9.3",
            "Referer": server_url,
            "Accept": "application/json, text/plain, */*",
            "Connection": "keep-alive",
        }
    )


def _load_session(api: growattServer.GrowattApi, session_file: str) -> bool:
    try:
        if not os.path.exists(session_file):
            return False
        with open(session_file, "rb") as f:
            data = pickle.load(f)
        cookies = data.get("cookies")
        headers = data.get("headers")
        if cookies:
            api.session.cookies.update(cookies)
        if headers:
            api.session.headers.update(headers)
        return True
    except Exception:
        return False


def _save_session(api: growattServer.GrowattApi, session_file: str) -> None:
    try:
        data = {
            "cookies": api.session.cookies,
            "headers": dict(api.session.headers),
            "saved_at": _now_ts(),
        }
        with open(session_file, "wb") as f:
            pickle.dump(data, f)
    except Exception:
        # Non-fatal: session persistence is best-effort
        pass


def _login(api: growattServer.GrowattApi, user: str, password: str, cfg: GrowattConfig) -> bool:
    for attempt in range(cfg.login_max_attempts):
        try:
            api.login(user, password)
            return True
        except Exception as e:
            if _is_403_error(e):
                print("❌ [Growatt] 403 Forbidden during login → wygląda na blokadę / bot-protection. Przerywam retry.")
                return False

            backoff = cfg.login_base_backoff_s * (2**attempt)
            print(f"⚠️ [Growatt] Login attempt {attempt+1} failed, retrying in ~{backoff}s... ({e})")
            _sleep_with_jitter(backoff, cfg.login_jitter_s)

    return False


def _get_login_id(api: growattServer.GrowattApi, fallback_user: str) -> str:
    # growattServer stores auth in api.session.auth sometimes
    try:
        if getattr(api, "session", None) and getattr(api.session, "auth", None):
            if api.session.auth and api.session.auth[0]:
                return str(api.session.auth[0])
    except Exception:
        pass
    return str(fallback_user)


def fetch_growatt_data(yesterday_str: str, plants_to_fetch: Dict[str, str], cfg: Optional[GrowattConfig] = None) -> Dict[str, float]:
    """
    Fetch daily energy (kWh) for given plants.

    Args:
        yesterday_str: string date format expected by growattServer (often 'YYYY-MM-DD')
        plants_to_fetch: dict {SiteID: PlantKey} e.g. {'9275498': 'SLP1'}
        cfg: GrowattConfig

    Returns:
        dict {PlantKey: kWh_float}
    """
    cfg = cfg or GrowattConfig()

    print(f"🚀 [Growatt] Connecting to Growatt for {yesterday_str}...")

    user = os.environ.get("GROWATT_USERNAME")
    password = os.environ.get("GROWATT_PASSWORD")

    if not user or not password:
        print("❌ [Growatt] Missing env vars: GROWATT_USERNAME / GROWATT_PASSWORD")
        return {p_key: 0.0 for p_key in plants_to_fetch.values()}

    # Initialize results by PlantKey
    results: Dict[str, float] = {p_key: 0.0 for p_key in plants_to_fetch.values()}

    api = growattServer.GrowattApi()
    api.server_url = cfg.server_url

    # Headers first
    _apply_headers(api, cfg.server_url)

    # Optional: load cookies to avoid logging in too often
    loaded = False
    if cfg.persist_session:
        loaded = _load_session(api, cfg.session_file)
        if loaded:
            print(f"🔁 [Growatt] Loaded session cookies from {cfg.session_file}")

    # Always attempt login at least once; some endpoints need fresh auth
    logged_in = _login(api, user, password, cfg)
    if not logged_in:
        print("❌ [Growatt] Could not login. IP/account might be blocked.")
        return results

    if cfg.persist_session:
        _save_session(api, cfg.session_file)

    # --- Fetch plant list once (cheap-ish) and use it as primary data source
    try:
        login_id = _get_login_id(api, user)
        all_plants = api.plant_list(login_id)  # 1 request
        plant_list_data = all_plants.get("data", []) if isinstance(all_plants, dict) else []

        # Map plantId -> todayEnergy (Growatt often uses todayEnergy in list response)
        today_map: Dict[str, Any] = {}
        for p in plant_list_data:
            pid = p.get("plantId")
            if pid is not None:
                today_map[str(pid)] = p.get("todayEnergy", 0)

        # For each plant, prefer list response (fewer calls); fallback to plant_detail
        for s_id, p_key in plants_to_fetch.items():
            val = today_map.get(str(s_id), 0)

            # If list gave 0/None, try detail endpoint for the given date
            # (This may be heavier; call only when needed.)
            if val in (None, 0, "0", "0.0"):
                try:
                    data = api.plant_detail(s_id, yesterday_str)
                    if isinstance(data, dict):
                        # Different keys appear depending on endpoint/version
                        val = data.get("today_energy") or data.get("todayEnergy") or data.get("energy") or 0
                except Exception as e:
                    # If detail is blocked / fails, keep 0
                    print(f"   ⚠️ [Growatt] plant_detail failed for {p_key} ({s_id}): {e}")
                    val = 0

            try:
                results[p_key] = float(val or 0)
            except Exception:
                results[p_key] = 0.0

            print(f"   📊 [Growatt] {p_key} ({s_id}): {results[p_key]} kWh")

        return results

    except Exception as e:
        # If this is 403, it’s often a ban or endpoint protection
        if _is_403_error(e):
            print("❌ [Growatt] 403 Forbidden during data fetch → prawdopodobnie blokada / bot protection.")
        else:
            print(f"❌ [Growatt] General API Error: {e}")
        return results


# Optional CLI usage:
# python argia_growatt.py
if __name__ == "__main__":
    # Example usage (edit as needed)
    # Yesterday string must match what your growattServer expects.
    # If you generate date externally, pass it into fetch_growatt_data().
    yesterday = os.environ.get("GROWATT_DATE", "2026-01-22")

    # Example plant mapping:
    # export GROWATT_PLANTS="9275498:SLP1,1234567:SLP2"
    plants_env = os.environ.get("GROWATT_PLANTS", "")
    plants: Dict[str, str] = {}
    if plants_env.strip():
        for item in plants_env.split(","):
            item = item.strip()
            if not item:
                continue
            sid, key = item.split(":")
            plants[sid.strip()] = key.strip()
    else:
        # Fallback example
        plants = {"9275498": "SLP1"}

    cfg = GrowattConfig(
        server_url=DEFAULT_SERVER_URL,
        session_file=DEFAULT_SESSION_FILE,
        login_max_attempts=3,
        persist_session=True,
    )

    out = fetch_growatt_data(yesterday, plants, cfg)
    print("✅ [Growatt] Done:", out)
