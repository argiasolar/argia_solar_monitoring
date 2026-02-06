import os
import re
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

LOG = logging.getLogger("argia.growatt.monitoring")


@dataclass
class GrowattAuth:
    username: str
    password: str


class GrowattMonitoringClient:
    """
    Monitoring-only Growatt client.

    Core principle: DO NOT reinvent the wheel.
    - First, attempt to reuse whatever working Growatt login / request logic already exists in argia_growatt.py
      by auto-discovering likely functions/classes.
    - Only if we cannot reuse, fall back to our minimal session login.

    This avoids modifying argia_growatt.py and keeps daily sync untouched.
    """

    def __init__(self, auth: GrowattAuth, timeout_s: int = 30):
        self.auth = auth
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "argia-monitoring/1.0",
                "Accept": "application/json, text/plain, */*",
            }
        )

        self._growatt_mod: Any = None
        self._login_fn: Optional[Callable[..., Any]] = None
        self._realtime_fn: Optional[Callable[..., Any]] = None
        self._reuse_reason: str = ""
        self._reuse_enabled: bool = False

    # -------------------------
    # Reuse detection
    # -------------------------
    def _import_argia_growatt(self) -> Optional[Any]:
        try:
            import argia_growatt  # type: ignore
            return argia_growatt
        except Exception as e:
            LOG.warning("Could not import argia_growatt.py (%s).", e)
            return None

    def _find_login_function(self, mod: Any) -> Optional[Callable[..., Any]]:
        """
        Try to discover a login function in argia_growatt.py without knowing its exact name.
        We prioritize functions that:
        - accept (session, user, pass) OR (session) and read envs
        - contain 'token' / 'cookie' / 'assToken' patterns in their source
        """
        candidates_by_name = [
            "login_server",
            "login",
            "server_login",
            "growatt_login",
            "do_login",
            "auth",
            "authenticate",
        ]

        # 1) direct known names
        for name in candidates_by_name:
            fn = getattr(mod, name, None)
            if callable(fn):
                self._reuse_reason = f"Found login candidate by name: {name}"
                return fn

        # 2) search any callable whose source hints at assToken/cookie/session
        for name in dir(mod):
            if name.startswith("_"):
                continue
            obj = getattr(mod, name, None)
            if not callable(obj):
                continue
            try:
                src = getattr(obj, "__code__", None)
                if not src:
                    continue
                # heuristics via function name + constants
                consts = " ".join([str(c) for c in (obj.__code__.co_consts or [])])  # type: ignore
                if re.search(r"assToken|JSESSIONID|cookie|Set-Cookie|login", consts, re.IGNORECASE):
                    self._reuse_reason = f"Found login candidate by heuristics: {name}"
                    return obj
            except Exception:
                continue

        return None

    def _find_realtime_function(self, mod: Any) -> Optional[Callable[..., Any]]:
        """
        Try to discover an inverter realtime fetch function in argia_growatt.py.
        This is optional; we can still use our own get_inverter_realtime() endpoints.
        """
        candidates_by_name = [
            "get_inverter_realtime",
            "get_inv_data",
            "get_inverter_data",
            "fetch_inverter_data",
            "fetch_inv_data",
            "get_device_data",
            "get_realtime",
        ]
        for name in candidates_by_name:
            fn = getattr(mod, name, None)
            if callable(fn):
                return fn

        # heuristic scan
        for name in dir(mod):
            if name.startswith("_"):
                continue
            obj = getattr(mod, name, None)
            if not callable(obj):
                continue
            try:
                consts = " ".join([str(c) for c in (obj.__code__.co_consts or [])])  # type: ignore
                if re.search(r"invData|getInvData|inverter|realtime|sn", consts, re.IGNORECASE):
                    return obj
            except Exception:
                continue
        return None

    def _try_enable_reuse(self) -> None:
        """
        Attempt to reuse existing argia_growatt.py logic.
        """
        mod = self._import_argia_growatt()
        if not mod:
            self._reuse_enabled = False
            return

        self._growatt_mod = mod
        self._login_fn = self._find_login_function(mod)
        self._realtime_fn = self._find_realtime_function(mod)

        if self._login_fn:
            self._reuse_enabled = True
            LOG.info("✅ Reuse enabled (argia_growatt): %s", self._reuse_reason)
        else:
            self._reuse_enabled = False
            LOG.warning("argia_growatt imported but no reusable login function was discovered.")

    # -------------------------
    # Login
    # -------------------------
    def login(self) -> None:
        # Prefer reuse
        self._try_enable_reuse()
        if self._reuse_enabled and self._login_fn:
            self._call_reused_login()
            return

        # Fallback if reuse not possible
        self._fallback_login()
        # Validate because Growatt can set cookies without being authenticated
        self._assert_logged_in()

    def _call_reused_login(self) -> None:
        """
        Call the discovered login function in a few common calling conventions.
        This is the key: we adapt to YOUR existing signature rather than rewriting it.
        """
        fn = self._login_fn
        assert fn is not None

        LOG.info("🔁 Using argia_growatt login function: %s", getattr(fn, "__name__", str(fn)))

        # Try signatures in order:
        tried = []

        # 1) (session, user, pass)
        try:
            fn(self.session, self.auth.username, self.auth.password)  # type: ignore
            self._assert_logged_in()
            return
        except TypeError as e:
            tried.append(f"(session,user,pass) -> TypeError: {e}")
        except Exception as e:
            tried.append(f"(session,user,pass) -> {e}")

        # 2) (session, user, password=...)
        try:
            fn(self.session, self.auth.username, password=self.auth.password)  # type: ignore
            self._assert_logged_in()
            return
        except TypeError as e:
            tried.append(f"(session,user,password=) -> TypeError: {e}")
        except Exception as e:
            tried.append(f"(session,user,password=) -> {e}")

        # 3) (session)
        try:
            # assume it reads envs internally
            os.environ.setdefault("GROWATT_USERNAME", self.auth.username)
            os.environ.setdefault("GROWATT_PASSWORD", self.auth.password)
            fn(self.session)  # type: ignore
            self._assert_logged_in()
            return
        except TypeError as e:
            tried.append(f"(session) -> TypeError: {e}")
        except Exception as e:
            tried.append(f"(session) -> {e}")

        # 4) ()
        try:
            os.environ.setdefault("GROWATT_USERNAME", self.auth.username)
            os.environ.setdefault("GROWATT_PASSWORD", self.auth.password)
            fn()  # type: ignore
            # If their login uses internal session, we cannot validate ours reliably.
            # But we still try the auth check; if it fails, we error.
            self._assert_logged_in()
            return
        except TypeError as e:
            tried.append(f"() -> TypeError: {e}")
        except Exception as e:
            tried.append(f"() -> {e}")

        raise RuntimeError(
            "Failed to reuse argia_growatt login function. Attempts:\n- " + "\n- ".join(tried)
        )

    def _fallback_login(self) -> None:
        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")
        login_url = os.getenv("GROWATT_LOGIN_URL", f"{base}/login")

        payload = {
            "account": self.auth.username,
            "password": self.auth.password,
        }

        LOG.info("🔐 Fallback login -> %s", login_url)
        r = self.session.post(login_url, data=payload, timeout=self.timeout_s, allow_redirects=True)

        LOG.info("Login HTTP %s, len=%s", r.status_code, len(r.text or ""))
        LOG.debug("Login response headers: %s", dict(r.headers))
        LOG.debug("Cookies after login: %s", self.session.cookies.get_dict())

    def _assert_logged_in(self) -> None:
        """
        Growatt may set JSESSIONID even when not authenticated.
        We validate by calling endpoints that, if not logged in, redirect to errorNoLogin or login HTML.
        """
        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")

        checks = [
            ("GET", f"{base}/newPlantAPI.do", {"op": "getPlantList"}),
            ("GET", f"{base}/newInvAPI.do", {"op": "getInvList"}),
        ]

        for method, url, params in checks:
            try:
                js = self._get_json(url, params=params, allow_redirects=True)
                if self._looks_like_login_html(js):
                    continue
                if self._looks_like_error_no_login(js):
                    continue
                # If we got JSON not resembling login/error, assume authenticated
                return
            except Exception:
                continue

        raise RuntimeError(
            "Login validation failed: session still treated as NOT logged in "
            "(errorNoLogin / login HTML). This means your working argia_growatt login "
            "uses a different endpoint/payload, and we must reuse it by name/signature."
        )

    @staticmethod
    def _looks_like_login_html(js: Any) -> bool:
        if isinstance(js, dict) and js.get("_non_json") is True:
            t = js.get("text", "") or ""
            return ("Login Page" in t) or ("dumpLogin" in t) or ("/login" in t)
        return False

    @staticmethod
    def _looks_like_error_no_login(js: Any) -> bool:
        if isinstance(js, dict) and js.get("_non_json") is True:
            t = js.get("text", "") or ""
            return ("errorNoLogin" in t) or ("no ha iniciado sesión" in t) or ("no ha iniciado ses" in t)
        return False

    # -------------------------
    # Request helpers
    # -------------------------
    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None, allow_redirects: bool = True) -> Any:
        r = self.session.get(url, params=params, timeout=self.timeout_s, allow_redirects=allow_redirects)
        LOG.info("GET %s -> %s", r.url, r.status_code)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"_non_json": True, "text": (r.text or "")[:2000]}

    # -------------------------
    # Data calls
    # -------------------------
    def get_inverter_realtime(self, inverter_sn: str) -> Dict[str, Any]:
        """
        Prefer reusing your existing argia_growatt realtime function if we found one.
        Otherwise use conservative endpoint tries.
        """
        if self._realtime_fn:
            fn = self._realtime_fn
            LOG.debug("🔁 Using argia_growatt realtime function: %s", getattr(fn, "__name__", str(fn)))

            tried = []

            # Try common conventions
            try:
                out = fn(self.session, inverter_sn)  # type: ignore
                return out if isinstance(out, dict) else {"_raw": out}
            except TypeError as e:
                tried.append(f"(session,sn) -> TypeError: {e}")
            except Exception as e:
                tried.append(f"(session,sn) -> {e}")

            try:
                out = fn(inverter_sn, self.session)  # type: ignore
                return out if isinstance(out, dict) else {"_raw": out}
            except TypeError as e:
                tried.append(f"(sn,session) -> TypeError: {e}")
            except Exception as e:
                tried.append(f"(sn,session) -> {e}")

            try:
                out = fn(inverter_sn)  # type: ignore
                return out if isinstance(out, dict) else {"_raw": out}
            except TypeError as e:
                tried.append(f"(sn) -> TypeError: {e}")
            except Exception as e:
                tried.append(f"(sn) -> {e}")

            LOG.warning("Failed to reuse realtime fn, falling back. Attempts: %s", tried)

        base = os.getenv("GROWATT_BASE_URL", "https://server.growatt.com")

        # conservative endpoint tries; once we confirm the right one, we can hard-pin it
        candidates = [
            (f"{base}/panel/inverter/getInverterData", {"sn": inverter_sn}),
            (f"{base}/indexbC/inv/getInvData", {"sn": inverter_sn}),
            (f"{base}/newInvAPI.do", {"op": "getInvData", "sn": inverter_sn}),
        ]

        last_err: Optional[Exception] = None
        for url, params in candidates:
            try:
                js = self._get_json(url, params=params, allow_redirects=True)
                return js if isinstance(js, dict) else {"_raw": js}
            except Exception as e:
                last_err = e

        raise RuntimeError(f"Could not get realtime for inverter {inverter_sn}: {last_err}")
