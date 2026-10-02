"""v293 - ARGIA for Prologis: the service shop end to end (Flask test
client, temp SQLite, synthetic registry and catalogue). Browse, cart,
checkout, quote and accept, order lifecycle by role, the services and
prices admin, export, both languages, no em dash."""
from __future__ import annotations

import importlib
import io
import pathlib
import re
import sys
import zipfile

import pytest

pytest.importorskip("flask", reason="flask not installed here")

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

from argia.prologis import store as S      # noqa: E402
from argia.prologis import totp as TOTP    # noqa: E402

NEW_PW = "Sunny-rooftops-2026"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGIA_PL_DIR", str(tmp_path))
    monkeypatch.setenv("ARGIA_PL_DB", str(tmp_path / "pl.db"))
    monkeypatch.setenv("ARGIA_PL_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("ARGIA_PL_REGISTRY", str(FIX / "registry.json"))
    monkeypatch.setenv("ARGIA_PL_INSECURE_COOKIE", "1")
    import prologis_app
    app_mod = importlib.reload(prologis_app)
    app_mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    assert app_mod.main(["x", "--seed-catalog", str(FIX / "catalog.json")]) == 0
    return app_mod, pws, tmp_path


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf" value="([0-9a-f]+)"', html)
    return m.group(1) if m else ""


def sign_in(app_mod, user, pw):
    cl = app_mod.app.test_client()
    cl.post("/login", data={"username": user, "password": pw, "next": "/"})
    page = cl.get("/mfa").get_data(as_text=True)
    secret = re.search(r'name="pending" value="([A-Z2-7]+)"', page).group(1)
    code = TOTP.code_at(secret, int(__import__("time").time() // 30))
    cl.post("/mfa", data={"code": code, "pending": secret, "csrf": _csrf(page), "next": "/"})
    page = cl.get("/password").get_data(as_text=True)
    cl.post("/password", data={"pw1": NEW_PW, "pw2": NEW_PW, "csrf": _csrf(page)})
    assert cl.get("/").status_code == 200
    return cl


def _clean(h: str) -> str:
    return re.sub(r"<script.*?</script>", "", h, flags=re.S)


def test_shop_needs_login(env):
    app_mod, _, _ = env
    cl = app_mod.app.test_client()
    for path in ("/shop/", "/shop/T-CLEAN", "/shop/cart", "/admin/services/", "/admin/services/new"):
        r = cl.get(path)
        assert r.status_code == 302 and "/login" in r.headers["Location"], path


def test_seed_cli_never_overwrites(env, capsys):
    app_mod, _, tmp = env
    assert app_mod.main(["x", "--seed-catalog", str(FIX / "catalog.json")]) == 0
    assert "0 added, 6 already present" in capsys.readouterr().out


def test_viewer_browses_but_cannot_order(env):
    app_mod, pws, _ = env
    v = sign_in(app_mod, "viewer", pws["viewer"])
    h = v.get("/shop/").get_data(as_text=True)
    assert "Test cleaning" in h and "MXN 100 per kWp" in h and "Rate card" in h and "+10 %" in h
    assert "Test draft item" not in h                                       # drafts stay with ARGIA
    assert "/shop/cart" not in h and "Manage services" not in h
    item = v.get("/shop/T-CLEAN").get_data(as_text=True)
    assert "Add to cart" not in item and "a Prologis manager places orders" in item
    assert v.get("/shop/T-DRAFT").status_code == 404 and v.get("/shop/T-RATE").status_code == 404
    assert v.get("/shop/cart").status_code == 403
    tok = _csrf(v.get("/password").get_data(as_text=True))                  # the shop has no form for a viewer
    assert tok and v.post("/shop/add", data={"item": "T-CLEAN", "site": "TST001", "csrf": tok}).status_code == 403
    assert v.get("/admin/services/").status_code == 403


def test_manager_orders_operator_delivers(env):
    app_mod, pws, tmp = env
    mgr = sign_in(app_mod, "manager", pws["manager"])
    page = mgr.get("/shop/T-CLEAN").get_data(as_text=True)
    assert "Add to cart" in page and 'data-kwp="619.4"' in page
    tok = _csrf(page)
    r = mgr.post("/shop/add", data={"item": "T-CLEAN", "site": ["TST001", "TST002"], "note": "north roof first", "csrf": tok})
    assert r.status_code == 302 and r.headers["Location"].endswith("/shop/cart")
    assert "choose at least one site" in mgr.post("/shop/add", data={"item": "T-CLEAN", "csrf": tok}).get_data(as_text=True)
    assert mgr.post("/shop/add", data={"item": "T-CLEAN", "site": "NOPE", "csrf": tok}).status_code == 400
    assert mgr.post("/shop/add", data={"item": "T-CLEAN", "site": "TST001"}).status_code == 400          # CSRF
    mgr.post("/shop/add", data={"item": "T-IV", "site": "TST003", "qty": "12", "csrf": tok})
    cart = mgr.get("/shop/cart").get_data(as_text=True)
    assert "MXN 93,580.00" in cart and "MXN 14,972.80" in cart and "MXN 108,552.80" in cart   # subtotal, IVA 16 %, total
    assert "1 line(s) on quote" in cart and "north roof first" in cart
    assert chr(0x2014) not in cart and "None" not in _clean(cart)
    r = mgr.post("/shop/checkout", data={"title": "Q4 cleaning + IV", "po": "4500012345", "preferred": "", "notes": "Gate B", "csrf": _csrf(cart)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/tickets/SO-0001/")
    so = mgr.get("/tickets/SO-0001/").get_data(as_text=True)
    assert "ARGIA preparing quote" in so and "4500012345" in so and "Gate B" in so
    assert "Accept" not in so                                               # nothing to accept before the quote
    assert ">Cancelled<" in so or 'value="CANCELLED"' in so                 # the customer may cancel
    assert 'value="CONFIRMED"' not in so                                    # ...but not confirm
    assert "Cancel order" in so
    assert mgr.post("/tickets/SO-0001/status", data={"to": "CONFIRMED", "csrf": _csrf(so)}).status_code == 403
    lst = mgr.get("/tickets/?kind=order").get_data(as_text=True)
    assert "SO-0001" in lst and "MXN 108,552.80*" in lst
    assert "SO-0001" not in mgr.get("/tickets/").get_data(as_text=True)    # incidents tab stays incidents

    op = sign_in(app_mod, "operator", pws["operator"])
    so = op.get("/tickets/SO-0001/").get_data(as_text=True)
    assert so.count('action="/tickets/SO-0001/quote"') == 1                # only the on-quote line has a price form
    assert 'value="CONFIRMED"' not in so                                    # no confirming before Prologis accepts
    c = S.connect(str(tmp / "pl.db"))
    tid = c.execute("SELECT id FROM tickets WHERE number='SO-0001'").fetchone()[0]
    iv = c.execute("SELECT id FROM order_lines WHERE ticket_id=? AND item_code='T-IV'", (tid,)).fetchone()[0]
    assert op.post("/tickets/SO-0001/quote", data={"line": iv, "price": "abc", "csrf": _csrf(so)}).status_code == 400
    assert op.post("/tickets/SO-0001/quote", data={"line": iv, "price": "450", "csrf": _csrf(so)}).status_code == 302
    so = mgr.get("/tickets/SO-0001/").get_data(as_text=True)
    assert mgr.post("/tickets/SO-0001/quote", data={"line": iv, "price": "1", "csrf": _csrf(so)}).status_code == 403
    assert "Quote awaiting Prologis" in so and "Accept the quote" in so and "MXN 114,816.80" in so
    mgr.post("/tickets/SO-0001/approve", data={"ok": "1", "note": "PO ok", "csrf": _csrf(so)})
    so = op.get("/tickets/SO-0001/").get_data(as_text=True)
    for to, extra in (("CONFIRMED", {}), ("SCHEDULED", {"scheduled": "2026-10-20"}), ("IN_PROGRESS", {}), ("COMPLETED", {})):
        r = op.post("/tickets/SO-0001/status", data={"to": to, "csrf": _csrf(so), **extra})
        assert r.status_code == 302, to
    assert op.post("/tickets/SO-0001/status", data={"to": "CANCELLED", "csrf": _csrf(so)}).status_code == 400
    up = op.post("/tickets/SO-0001/comment", data={"body": "Done, report attached", "csrf": _csrf(so),
                                                     "file": (io.BytesIO(b"%PDF-1.7"), "cleaning_report.pdf")}, content_type="multipart/form-data")
    assert up.status_code == 302
    so = op.get("/tickets/SO-0001/").get_data(as_text=True)
    assert "cleaning_report.pdf" in so and "2026-10-20" in so and "accepted the quote" in so
    assert chr(0x2014) not in so and "None" not in _clean(so)
    site = op.get("/sites/TST003/").get_data(as_text=True)
    assert "SO-0001" in site                                                # the order shows on each site it touches


def test_scheduling_without_date_is_refused(env):
    app_mod, pws, _ = env
    op = sign_in(app_mod, "operator", pws["operator"])
    page = op.get("/shop/T-THERMO").get_data(as_text=True)
    op.post("/shop/add", data={"item": "T-THERMO", "site": "TST001", "csrf": _csrf(page)})
    cart = op.get("/shop/cart").get_data(as_text=True)
    op.post("/shop/checkout", data={"title": "for Prologis", "csrf": _csrf(cart)})
    so = op.get("/tickets/SO-0001/").get_data(as_text=True)
    assert "Quote awaiting Prologis" in so                                  # ARGIA-placed order waits for Prologis
    assert op.post("/tickets/SO-0001/status", data={"to": "CONFIRMED", "csrf": _csrf(so)}).status_code == 400
    ad = sign_in(app_mod, "admin", pws["admin"])
    so = ad.get("/tickets/SO-0001/").get_data(as_text=True)
    ad.post("/tickets/SO-0001/approve", data={"ok": "1", "csrf": _csrf(so)})
    so = op.get("/tickets/SO-0001/").get_data(as_text=True)
    assert op.post("/tickets/SO-0001/status", data={"to": "CONFIRMED", "csrf": _csrf(so)}).status_code == 302
    assert op.post("/tickets/SO-0001/status", data={"to": "SCHEDULED", "csrf": _csrf(so)}).status_code == 400
    assert op.post("/tickets/SO-0001/status", data={"to": "SCHEDULED", "scheduled": "20-10-2026", "csrf": _csrf(so)}).status_code == 400


def test_cart_remove_and_bad_preferred_date(env):
    app_mod, pws, tmp = env
    mgr = sign_in(app_mod, "manager", pws["manager"])
    page = mgr.get("/shop/T-THERMO").get_data(as_text=True)
    mgr.post("/shop/add", data={"item": "T-THERMO", "site": ["TST001", "TST002"], "csrf": _csrf(page)})
    cart = mgr.get("/shop/cart").get_data(as_text=True)
    assert "Preferred date must be a date" in mgr.post("/shop/checkout", data={"preferred": "tomorrow", "csrf": _csrf(cart)}).get_data(as_text=True)
    ids = re.findall(r'name="id" value="(\d+)"', cart)
    assert len(ids) == 2
    for i in ids:
        mgr.post("/shop/cart/remove", data={"id": i, "csrf": _csrf(cart)})
    assert "Your cart is empty" in mgr.get("/shop/cart").get_data(as_text=True)
    assert "the cart is empty" in mgr.post("/shop/checkout", data={"csrf": _csrf(cart)}).get_data(as_text=True)


def test_admin_adds_edits_publishes_removes(env):
    app_mod, pws, tmp = env
    ad = sign_in(app_mod, "admin", pws["admin"])
    lst = ad.get("/admin/services/").get_data(as_text=True)
    assert "T-DRAFT" in lst and "Draft" in lst and "5 published" in lst
    form = ad.get("/admin/services/new").get_data(as_text=True)
    tok = _csrf(form)
    base = {"is_new": "1", "code": "t-wash", "category": "cleaning", "name_en": "Test wash", "name_es": "Lavado", "unit": "kwp",
            "price_mxn": "1,250.5", "lead_days": "7", "sort": "15", "basis": "catalog", "published": "1", "active": "1",
            "orderable": "1", "includes_en": "a; b", "csrf": tok}
    r = ad.post("/admin/services/save", data=base)
    assert r.status_code == 302 and "saved=T-WASH" in r.headers["Location"]
    assert "T-WASH saved" in ad.get(r.headers["Location"]).get_data(as_text=True)
    assert "already exists" in ad.post("/admin/services/save", data=base).get_data(as_text=True)
    assert "must be a number" in ad.post("/admin/services/save", data={**base, "code": "T-X", "price_mxn": "abc"}).get_data(as_text=True)
    assert "unit" in ad.post("/admin/services/save", data={**base, "code": "T-X", "unit": "litre"}).get_data(as_text=True)
    assert "whole numbers" in ad.post("/admin/services/save", data={**base, "code": "T-X", "lead_days": "2.5"}).get_data(as_text=True)
    shop = ad.get("/shop/").get_data(as_text=True)
    assert "Test wash" in shop and "MXN 1,250.50 per kWp" not in shop and "MXN 1,250 per kWp" in shop   # >= 100 rounds
    # publish the draft with a price change
    ed = ad.get("/admin/services/T-DRAFT").get_data(as_text=True)
    assert 'value="77"' in ed
    ad.post("/admin/services/save", data={"code": "T-DRAFT", "category": "corrective", "name_en": "Test draft item", "unit": "unit",
                                          "price_mxn": "80", "lead_days": "3", "sort": "60", "basis": "catalog", "published": "1",
                                          "active": "1", "orderable": "1", "csrf": tok})
    assert "Test draft item" in ad.get("/shop/").get_data(as_text=True)
    ad.post("/admin/services/T-WASH/remove", data={"csrf": tok})
    assert "Test wash" not in ad.get("/shop/").get_data(as_text=True)
    assert ad.post("/admin/services/NOPE/remove", data={"csrf": tok}).status_code == 404
    c = S.connect(str(tmp / "pl.db"))
    acts = [r["action"] for r in c.execute("SELECT action FROM audit WHERE action LIKE 'catalog_%'")]
    assert acts.count("catalog_add") >= 7 and "catalog_edit" in acts and "catalog_remove" in acts
    detail = c.execute("SELECT detail FROM audit WHERE action='catalog_edit' AND target='T-DRAFT'").fetchone()[0]
    assert "price_mxn: 77.0 -> 80.0" in detail and "published: False -> True" in detail
    for path in ("/admin/services/", "/admin/services/T-CLEAN", "/admin/services/new", "/shop/", "/shop/T-IV"):
        h = ad.get(path).get_data(as_text=True)
        assert chr(0x2014) not in h and "None" not in _clean(h), path
    mgr = sign_in(app_mod, "manager", pws["manager"])
    assert mgr.get("/admin/services/").status_code == 403
    assert mgr.post("/admin/services/save", data={**base, "code": "T-HACK", "csrf": _csrf(mgr.get("/password").get_data(as_text=True))}).status_code == 403


def test_export_and_spanish(env):
    app_mod, pws, _ = env
    ad = sign_in(app_mod, "admin", pws["admin"])
    page = ad.get("/shop/T-THERMO").get_data(as_text=True)
    ad.post("/shop/add", data={"item": "T-THERMO", "site": "TST001", "csrf": _csrf(page)})
    cart = ad.get("/shop/cart").get_data(as_text=True)
    ad.post("/shop/checkout", data={"title": "x", "csrf": _csrf(cart)})
    z = zipfile.ZipFile(io.BytesIO(ad.get("/export.zip").data))
    assert {"order_lines.csv", "catalog.csv"} <= set(z.namelist())
    cat_csv = z.read("catalog.csv").decode()
    assert "T-THERMO" in cat_csv and "T-DRAFT" not in cat_csv              # drafts are not exported to Prologis
    ad.get("/lang/es")
    h = ad.get("/shop/").get_data(as_text=True)
    assert "Tienda de servicios" in h and "por kWp" in h and "Tarifas y costos adicionales" in h
    h = ad.get("/tickets/SO-0001/").get_data(as_text=True)
    assert "Líneas del pedido" in h and "Pedido" in h
    home = ad.get("/").get_data(as_text=True)
    assert "1 órdenes de servicio" in home
