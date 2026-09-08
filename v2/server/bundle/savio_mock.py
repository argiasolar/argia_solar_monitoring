#!/usr/bin/env python3
"""A stand-in for api.savio.mx — serves the committed demo fixtures with
the real API's shapes (cursor pages, include=cfdis,items, 429 with
Retry-After on demand) on 127.0.0.1:8530 so the sync job and the
finance pages can be exercised end to end on pio06 without a Savio key
(v244). Loopback only; nginx never proxies it.

    ARGIA_SAVIO_MOCK_PORT   default 8530
    ARGIA_SAVIO_FIXTURES    default <repo>/v2/tests/fixtures/fin/savio
    GET /api/v1/invoice?cursor=&limit=&include=   -> invoices_p1 / _p2
    GET /api/v1/customer                          -> customers
    GET /api/v1/payment                           -> payments
    GET /api/v1/_ratelimit/<n>                    -> the next n calls answer 429 (tests)
    GET /health                                   -> {"ok": true, "fixtures": ...}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request

V2 = os.environ.get("ARGIA_V2_DIR", "/root/argia_v2/v2")
FIX = Path(os.environ.get("ARGIA_SAVIO_FIXTURES", os.path.join(V2, "tests", "fixtures", "fin", "savio")))

app = Flask(__name__)
app.config["RATELIMIT_LEFT"] = 0


def load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@app.before_request
def _maybe_429():
    if request.path.startswith("/api/v1/") and not request.path.startswith("/api/v1/_") and app.config["RATELIMIT_LEFT"] > 0:
        app.config["RATELIMIT_LEFT"] -= 1
        r = jsonify({"error": "rate limited (mock)"})
        r.status_code = 429
        r.headers["Retry-After"] = "1"
        return r
    return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "fixtures": str(FIX), "files": sorted(p.name for p in FIX.glob("*.json"))})


@app.get("/api/v1/_ratelimit/<int:n>")
def ratelimit(n: int):
    app.config["RATELIMIT_LEFT"] = max(0, min(n, 20))
    return jsonify({"next_429": app.config["RATELIMIT_LEFT"]})


@app.get("/api/v1/invoice")
def invoices():
    cur = (request.args.get("cursor") or "").strip()
    if cur == "":
        return jsonify(load("invoices_p1.json"))
    if cur == "cur_demo_page2":
        return jsonify(load("invoices_p2.json"))
    r = jsonify({"error": f"unknown cursor {cur}"})
    r.status_code = 400
    return r


@app.get("/api/v1/customer")
def customers():
    return jsonify(load("customers.json"))


@app.get("/api/v1/payment")
def payments():
    return jsonify(load("payments.json"))


@app.get("/api/v1/webhooks")
def webhooks():
    return jsonify({"events": ["payment.created", "payment.applied", "payment.deleted", "credit.created", "credit.deleted",
                               "invoice.deleted", "invoice.status.updated", "cfdi.canceled"], "mock": True})


if __name__ == "__main__":
    port = int(os.environ.get("ARGIA_SAVIO_MOCK_PORT", "8530"))
    app.run(host="127.0.0.1", port=port, debug=False)
