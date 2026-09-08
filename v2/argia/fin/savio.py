"""Savio API client (AR side) — https://api.savio.mx/docs, verified
2026-09-08.

    base   https://api.savio.mx/api/v1  (sandbox api-sandbox.savio.mx)
    auth   API key in the request header (ARGIA_SAVIO_KEY_FILE, file-to-file)
    pages  cursor: send nextCursor back as `cursor` with the same limit
           and filters; nextCursor null = done
    limits 150/min, 10 000/day (midnight Mexico City); 429 + Retry-After,
           a rejected request burns no quota

The transport is pluggable: ``HttpTransport`` (urllib, no third-party
dependency) in production, ``FakeTransport`` (the committed fixtures)
in tests and on a dev box, or the ``savio_mock`` service on pio06 with
the base URL pointed at 127.0.0.1:8530. Nothing here writes to PG.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

DEFAULT_BASE = "https://api.savio.mx/api/v1"
KEY_FILE = os.environ.get("ARGIA_SAVIO_KEY_FILE", "/root/.argia_savio")
MAX_RETRY_429 = 3
PAGE_LIMIT = 100


class SavioError(RuntimeError):
    pass


def read_key(path: Optional[str] = None) -> str:
    """The API key from its file (chmod 600); never from the environment
    or a command line. Empty when the file is absent (the mock needs none)."""
    try:
        return Path(path or KEY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class HttpTransport:
    """GET/POST JSON against the real API. Honors 429 with Retry-After."""

    def __init__(self, base: str = DEFAULT_BASE, key: str = "", timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep):
        self.base = base.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.sleep = sleep

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[dict] = None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "User-Agent": "argia-fin/1"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
            headers["X-API-Key"] = self.key
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(MAX_RETRY_429 + 1):
            req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8") or "{}")
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    if attempt >= MAX_RETRY_429:
                        raise SavioError(f"{method} {path}: gave up after {MAX_RETRY_429} rate-limit retries") from e
                    wait = float(e.headers.get("Retry-After", "5") or 5)
                    self.sleep(min(wait, 60.0))
                    continue
                raise SavioError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}") from e
            except urllib.error.URLError as e:
                raise SavioError(f"{method} {path}: {e.reason}") from e
        raise SavioError(f"{method} {path}: gave up after {MAX_RETRY_429} rate-limit retries")


class FakeTransport:
    """Replays the committed fixtures (tests/fixtures/fin/savio). The
    invoice pages follow the cursor exactly as the API would; every call
    is recorded so a test can assert what was asked."""

    def __init__(self, fixture_dir: Path):
        self.dir = Path(fixture_dir)
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []

    def _load(self, name: str) -> dict:
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None, body: Optional[dict] = None) -> dict:
        params = dict(params or {})
        self.calls.append((method, path, params))
        p = path.strip("/")
        if p == "invoice":
            page = self._load("invoices_p2.json" if params.get("cursor") == "cur_demo_page2" else "invoices_p1.json")
            if params.get("cursor") not in (None, "", "cur_demo_page2"):
                raise SavioError(f"GET invoice: HTTP 400 unknown cursor {params['cursor']!r}")
            return page
        if p == "customer":
            return self._load("customers.json")
        if p == "payment":
            return self._load("payments.json")
        raise SavioError(f"{method} {path}: HTTP 404 (fake)")


class SavioClient:
    def __init__(self, transport):
        self.t = transport

    def _pages(self, path: str, params: Dict[str, Any]) -> Iterator[dict]:
        cursor: Optional[str] = params.pop("cursor", None)
        seen = set()
        while True:
            page = self.t.request("GET", path, {**params, "cursor": cursor, "limit": params.get("limit", PAGE_LIMIT)})
            for row in page.get("data") or []:
                yield row
            cursor = page.get("nextCursor")
            if not cursor:
                return
            if cursor in seen:
                raise SavioError(f"{path}: cursor loop on {cursor!r}")
            seen.add(cursor)

    def invoices(self, updated_after: Optional[str] = None, cursor: Optional[str] = None) -> Iterator[dict]:
        """Every invoice with its CFDIs and items, oldest update first."""
        params: Dict[str, Any] = {"include": "cfdis,items", "limit": PAGE_LIMIT}
        if updated_after:
            params["updated_after"] = updated_after
        if cursor:
            params["cursor"] = cursor
        return self._pages("invoice", params)

    def customers(self) -> Iterator[dict]:
        return self._pages("customer", {"limit": PAGE_LIMIT})

    def payments(self) -> Iterator[dict]:
        return self._pages("payment", {"limit": PAGE_LIMIT})


def client_from_env(fixture_dir: Optional[Path] = None) -> SavioClient:
    """ARGIA_SAVIO_BASE=fake -> the fixtures; a URL -> HTTP (the mock on
    127.0.0.1:8530 or the real API with the key file)."""
    base = os.environ.get("ARGIA_SAVIO_BASE", "fake").strip()
    if base == "fake":
        return SavioClient(FakeTransport(fixture_dir or Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "fin" / "savio"))
    return SavioClient(HttpTransport(base, read_key()))
