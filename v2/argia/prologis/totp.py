"""TOTP second factor (RFC 6238 / RFC 4226), standard library only (v292).

Any authenticator app (Microsoft / Google Authenticator, 1Password...)
reads the otpauth URI. 30-second steps, 6 digits, SHA-1 - the defaults
every app supports. ``verify`` accepts the previous and next step to
absorb clock drift, and refuses a code already used (``last_step``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse
from typing import Optional, Tuple

STEP = 30
DIGITS = 6


def new_secret() -> str:
    """160 random bits, base32 without padding (what the apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, step: int) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(n % 10 ** DIGITS).zfill(DIGITS)


def verify(secret: str, code: str, now: Optional[float] = None,
           last_step: int = -1, window: int = 1) -> Tuple[bool, int]:
    """(ok, step_used). A code is valid in its step +- ``window``, and
    only once: a step <= ``last_step`` is refused (replay)."""
    c = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(c) != DIGITS or not secret:
        return False, last_step
    t = int((time.time() if now is None else now) // STEP)
    for s in range(t - window, t + window + 1):
        if s > last_step and hmac.compare_digest(code_at(secret, s), c):
            return True, s
    return False, last_step


def uri(secret: str, account: str, issuer: str = "ARGIA for Prologis") -> str:
    label = urllib.parse.quote(f"{issuer}:{account}")
    q = urllib.parse.urlencode({"secret": secret, "issuer": issuer,
                                "digits": DIGITS, "period": STEP})
    return f"otpauth://totp/{label}?{q}"
