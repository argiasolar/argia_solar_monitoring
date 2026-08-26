"""Team email alerts — SMTP via the service@argia.com.mx account.

Config lives in a root-only file (default /root/.argia_mail; KEY=VALUE:
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS) — never in the repo, env
exports, or logs. Missing/incomplete config -> ``load_smtp`` returns
None and callers log-and-skip: alerting must never crash a job.

Message building is pure (unit-tested); only ``send`` talks SMTP.
"""

from __future__ import annotations

import logging
import smtplib
import socket
from email.message import EmailMessage
from typing import Dict, List, Optional

LOG = logging.getLogger("argia.alerts.emailer")


class _SMTP4(smtplib.SMTP):
    """SMTP that connects over IPv4 only. Google's IP-authorized relay
    checks the CONNECTING address: pio06 prefers IPv6, but only its
    IPv4 (37.235.105.173) is registered — an IPv6 connection gets
    '550 5.7.1 Invalid credentials for relay' (live, 2026-08-26)."""

    def _get_socket(self, host, port, timeout):
        infos = socket.getaddrinfo(host, port, socket.AF_INET,
                                   socket.SOCK_STREAM)
        af, st, pr, _cn, sa = infos[0]
        s = socket.socket(af, st, pr)
        if timeout is not None:
            s.settimeout(timeout)
        s.connect(sa)
        return s

DEFAULT_CONFIG_PATH = "/root/.argia_mail"
REQUIRED_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER")


def parse_smtp_config(text: str) -> Optional[Dict[str, str]]:
    """KEY=VALUE lines -> config dict; None unless valid. Pure.

    Two modes: password auth (SMTP_PASS present) or IP-authorized relay
    (SMTP_AUTH=none — e.g. Google Workspace smtp-relay.gmail.com with
    the server IP registered; no credential exists at all)."""
    cfg: Dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, _, v = ln.partition("=")
        cfg[k.strip()] = v.strip()
    if not all(cfg.get(k) for k in REQUIRED_KEYS):
        return None
    if cfg.get("SMTP_PASS") or cfg.get("SMTP_AUTH", "").lower() == "none":
        return cfg
    return None


def load_smtp(path: str = DEFAULT_CONFIG_PATH) -> Optional[Dict[str, str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_smtp_config(fh.read())
    except OSError:
        return None


def build_email(subject: str, body: str, sender: str,
                recipients: List[str]) -> EmailMessage:
    """Plain-text alert mail. Pure."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"ARGIA Monitoring <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    return msg


def send(msg: EmailMessage, cfg: Dict[str, str],
         timeout: int = 30) -> bool:
    """Deliver via STARTTLS. Returns True on success; logs (without
    secrets) and returns False on any failure."""
    cls = _SMTP4 if cfg.get("SMTP_FORCE_IPV4", "1") == "1" else smtplib.SMTP
    try:
        with cls(cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]),
                 timeout=timeout) as s:
            s.starttls()
            if cfg.get("SMTP_AUTH", "").lower() != "none":
                s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"])
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        LOG.error("SMTP send failed (%s:%s as %s): %s", cfg.get("SMTP_HOST"),
                  cfg.get("SMTP_PORT"), cfg.get("SMTP_USER"),
                  type(e).__name__)
        return False
