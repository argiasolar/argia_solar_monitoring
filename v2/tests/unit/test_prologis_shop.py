"""v293 - ARGIA for Prologis: the service shop, pure parts and store.
Catalogue rules, quantities, totals with IVA, the order lifecycle, the
quote/accept loop, permissions, seeding that never overwrites a price,
numbering kept apart from incidents. Synthetic catalogue only (the real
prices live on the server)."""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from types import SimpleNamespace as NS

import pytest

from argia.prologis import catalog as CAT
from argia.prologis import store as S

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "prologis" / "catalog.json"
SEED = json.loads(FIX.read_text(encoding="utf-8"))
A = NS(code="TST001", kwp=619.4)
B = NS(code="TST002", kwp=316.4)


def _item(**kw):
    d = dict(code="X-1", category="cleaning", name_en="X", name_es="X", unit="site", price_mxn=100.0, published=True)
    d.update(kw)
    return CAT.Item(**d)


class TestCatalogPure:
    def test_fixture_is_valid_and_synthetic(self):
        for d in SEED:
            assert CAT.validate(CAT.from_dict(d)) == [], d["code"]
            assert d["code"].startswith("T-")

    @pytest.mark.parametrize("kw,needle", [
        (dict(code="bad code"), "code"), (dict(code="new"), "reserved"), (dict(code="CART"), "reserved"), (dict(category="x"), "category"), (dict(unit="litre"), "unit"),
        (dict(name_en=" "), "English name"), (dict(price_mxn=-1), "price"), (dict(lead_days=400), "lead"),
        (dict(basis="draft"), "basis"), (dict(unit="percent", price_mxn=10), "mark-up"),
        (dict(basis="quote", price_mxn=5), "quote-basis"),
    ])
    def test_validate_rejects(self, kw, needle):
        errs = CAT.validate(_item(**kw))
        assert any(needle in e for e in errs), errs

    def test_from_dict_normalises(self):
        it = CAT.from_dict({"code": " t-a ", "category": "cleaning", "name_en": "A", "unit": "site", "price_mxn": ""})
        assert it.code == "T-A" and it.name_es == "A" and it.on_quote and it.basis == "catalog" and not it.published

    def test_qty_rules(self):
        assert CAT.qty_for("kwp", 619.437, None) == 619.44            # kWp comes from the site, typed qty ignored
        assert CAT.qty_for("site", 619.4, 99) == 1.0
        assert CAT.qty_for("string", None, 12) == 12
        for unit, kwp, q in (("kwp", None, None), ("kwp", 0, 5), ("string", None, 0), ("string", None, -2), ("m", None, 100001)):
            with pytest.raises(ValueError):
                CAT.qty_for(unit, kwp, q)

    def test_build_lines_one_per_site(self):
        ls = CAT.build_lines(_item(unit="kwp", price_mxn=100), [A, B])
        assert [(ln.site_code, ln.qty, ln.total) for ln in ls] == [("TST001", 619.4, 61940.0), ("TST002", 316.4, 31640.0)]
        ls = CAT.build_lines(_item(unit="string", price_mxn=None), [A], 7)
        assert ls[0].qty == 7 and ls[0].total is None
        assert CAT.build_lines(_item(unit="unit"), [], 3)[0].site_code == ""          # portfolio-wide line
        with pytest.raises(ValueError):
            CAT.build_lines(_item(unit="kwp"), [])                                      # per-kWp needs a site
        with pytest.raises(ValueError):
            CAT.build_lines(_item(orderable=False, unit="hour"), [A], 1)                # rate card is not orderable
        with pytest.raises(ValueError):
            CAT.build_lines(_item(active=False), [A])

    def test_totals_iva_and_quotes(self):
        ls = [CAT.Line("A", "a", "site", "S1", 1, 1000.0), CAT.Line("B", "b", "kwp", "S1", 200.5, 150.0),
              CAT.Line("C", "c", "string", "S1", 4, None)]
        tt = CAT.totals(ls)
        assert tt == {"subtotal": 31075.0, "iva": 4972.0, "total": 36047.0, "on_quote": 1, "lines": 3}
        assert CAT.totals([]) == {"subtotal": 0, "iva": 0.0, "total": 0.0, "on_quote": 0, "lines": 0}

    def test_target_date(self):
        d0 = dt.date(2026, 10, 2)
        ls = [CAT.Line("A", "a", "site", "", 1, 1.0, 5), CAT.Line("B", "b", "site", "", 1, 1.0, 15)]
        assert CAT.target_date(d0, ls) == dt.date(2026, 10, 17)                         # the longest lead time
        assert CAT.target_date(d0, ls, dt.date(2026, 11, 1)) == dt.date(2026, 11, 1)    # later preferred date wins
        assert CAT.target_date(d0, ls, dt.date(2026, 10, 3)) == dt.date(2026, 10, 17)   # earlier one does not

    def test_price_text(self):
        t_es = lambda en, es: es                                                         # noqa: E731
        assert CAT.price_text(_item(unit="kwp", price_mxn=150)) == "MXN 150 per kWp"
        assert CAT.price_text(_item(unit="site", price_mxn=25000), t_es) == "MXN 25,000 por sitio"
        assert CAT.price_text(_item(unit="string", price_mxn=None)) == "On quote"
        assert CAT.price_text(_item(unit="percent", price_mxn=10, orderable=False)) == "+10 %"
        assert CAT.price_text(_item(unit="hour", price_mxn=62.5)) == "MXN 62.50 per hour"

    def test_lifecycle_table_is_closed(self):
        keys = {k for k, _, _ in CAT.ORDER_STATUSES}
        assert set(CAT.ORDER_TRANSITIONS) == keys
        assert all(set(v) <= keys for v in CAT.ORDER_TRANSITIONS.values())
        assert CAT.ORDER_TRANSITIONS["INVOICED"] == () and CAT.ORDER_TRANSITIONS["CANCELLED"] == ()
        assert "CANCELLED" not in CAT.ORDER_TRANSITIONS["IN_PROGRESS"]                 # no cancelling work under way


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    S.seed_catalog(con, SEED, "test")
    return con


class TestCatalogStore:
    def test_seed_never_overwrites(self, c):
        it = S.catalog_item(c, "T-CLEAN")
        it.price_mxn = 123.0
        S.save_item(c, it, "admin")
        assert S.seed_catalog(c, SEED, "again") == 0
        assert S.catalog_item(c, "T-CLEAN").price_mxn == 123.0

    def test_visibility(self, c):
        pub = {i.code for i in S.catalog(c)}
        assert "T-DRAFT" not in pub and "T-CLEAN" in pub
        assert "T-DRAFT" in {i.code for i in S.catalog(c, include_drafts=True)}
        S.remove_item(c, "T-THERMO", "admin")
        assert "T-THERMO" not in {i.code for i in S.catalog(c, include_drafts=True)}
        gone = S.catalog_item(c, "T-THERMO")
        assert gone and not gone.active and not gone.published                          # soft: the row stays

    def test_save_is_validated_and_audited(self, c):
        with pytest.raises(ValueError):
            S.save_item(c, _item(code="T-BAD", unit="nope"), "admin")
        S.save_item(c, _item(code="T-NEW", price_mxn=10.0), "admin", "1.2.3.4")
        it = S.catalog_item(c, "t-new")
        it.price_mxn = 12.5
        S.save_item(c, it, "admin")
        rows = c.execute("SELECT action, detail FROM audit WHERE target='T-NEW' ORDER BY id").fetchall()
        assert [r["action"] for r in rows] == ["catalog_add", "catalog_edit"]
        assert "price_mxn: 10.0 -> 12.5" in rows[1]["detail"]


def _cart(c, user="mgr", code="T-CLEAN", sites=(A,), qty=None):
    S.cart_add(c, user, CAT.build_lines(S.catalog_item(c, code), list(sites), qty), "gate code 1234")


class TestOrders:
    def test_cart_merges_repeats(self, c):
        _cart(c, sites=(A, B))
        _cart(c, sites=(A,))                                                            # same cleaning, same site: not doubled
        _cart(c, code="T-IV", qty=5)
        _cart(c, code="T-IV", qty=7)                                                    # typed quantity adds up
        rows = [(r["item_code"], r["site_code"], r["qty"]) for r in S.cart(c, "mgr")]
        assert rows == [("T-CLEAN", "TST001", 619.4), ("T-CLEAN", "TST002", 316.4), ("T-IV", "TST001", 12.0)]

    def test_priced_order_by_manager_needs_no_further_approval(self, c):
        _cart(c, sites=(A, B))
        _cart(c, code="T-THERMO")
        tk = S.place_order(c, "mgr", "Q4 cleaning", "PO-77", "", "", today=dt.date(2026, 10, 2), role="manager")
        assert tk["number"] == "SO-0001" and tk["kind"] == "order" and tk["status"] == "ORDERED"
        assert tk["approval"] == "not_required" and tk["site_code"] == "MULTI" and tk["po_number"] == "PO-77"
        assert tk["target_date"] == "2026-10-17" and tk["estimate_mxn"] == 94580.0     # 93,580 + 1,000
        lines = S.order_lines(c, tk["id"])
        assert len(lines) == 3 and lines[0]["note"] == "gate code 1234"
        assert S.cart(c, "mgr") == []

    def test_order_placed_by_operator_waits_for_prologis(self, c):
        _cart(c, user="op", code="T-THERMO")
        tk = S.place_order(c, "op", "x", role="operator")
        assert tk["approval"] == "pending" and tk["site_code"] == "TST001"

    def test_empty_cart_and_removed_items(self, c):
        with pytest.raises(ValueError):
            S.place_order(c, "mgr", "x")
        _cart(c, code="T-THERMO")
        S.remove_item(c, "T-THERMO", "admin")                                           # removed after it went in the cart
        assert S.cart_lines(c, "mgr") == []
        with pytest.raises(ValueError):
            S.place_order(c, "mgr", "x")

    def test_numbering_is_separate_from_incidents(self, c):
        S.create_ticket(c, "TST001", "fault", "", "OTHER", "op")
        _cart(c, code="T-THERMO")
        so = S.place_order(c, "mgr", "x")
        inc2 = S.create_ticket(c, "TST001", "fault 2", "", "OTHER", "op")
        assert so["number"] == "SO-0001" and inc2["number"] == "PL-0002"

    def test_quote_accept_and_lifecycle(self, c):
        _cart(c, code="T-IV", qty=12)
        _cart(c, code="T-THERMO")
        tk = S.place_order(c, "mgr", "IV + thermo", today=dt.date(2026, 10, 2))
        assert tk["approval"] == "quote_needed" and tk["estimate_mxn"] == 1000.0
        reload = lambda: c.execute("SELECT * FROM tickets WHERE id=?", (tk["id"],)).fetchone()   # noqa: E731
        with pytest.raises(ValueError):                                                 # not before the quote is accepted
            S.set_order_status(c, reload(), "CONFIRMED", "op", "operator")
        iv = [r for r in S.order_lines(c, tk["id"]) if r["item_code"] == "T-IV"][0]
        S.price_quote_line(c, reload(), iv["id"], 40.0, "op")
        t2 = reload()
        assert t2["approval"] == "pending" and t2["estimate_mxn"] == 1480.0
        S.set_approval(c, t2, False, "mgr", "too high")                                 # declined: ARGIA re-quotes
        S.price_quote_line(c, reload(), iv["id"], 35.0, "op")
        assert reload()["approval"] == "pending"
        S.set_approval(c, reload(), True, "mgr", "PO 99")
        S.set_order_status(c, reload(), "CONFIRMED", "op", "operator", ts="2026-10-03 10:00:00")
        with pytest.raises(ValueError):
            S.price_quote_line(c, reload(), iv["id"], 1.0, "op")                         # price fixed after confirmation
        with pytest.raises(ValueError):
            S.set_order_status(c, reload(), "SCHEDULED", "op", "operator")                # a date is required
        S.set_order_status(c, reload(), "SCHEDULED", "op", "operator", scheduled="2026-10-20")
        S.set_order_status(c, reload(), "IN_PROGRESS", "op", "operator")
        with pytest.raises(ValueError):
            S.set_order_status(c, reload(), "CANCELLED", "mgr", "manager")               # too late to cancel
        S.set_order_status(c, reload(), "COMPLETED", "op", "operator", ts="2026-10-20 18:00:00")
        S.set_order_status(c, reload(), "INVOICED", "op", "operator")
        t3 = reload()
        assert t3["status"] == "INVOICED" and t3["scheduled_date"] == "2026-10-20"
        assert t3["responded_utc"] == "2026-10-03 10:00:00" and t3["resolved_utc"] == "2026-10-20 18:00:00"
        acts = [r["action"] for r in c.execute("SELECT action FROM audit")]
        assert acts.count("order_quote") == 2 and acts.count("order_status") == 5 and "order_place" in acts

    def test_customer_may_only_cancel(self, c):
        _cart(c, code="T-THERMO")
        tk = S.place_order(c, "mgr", "x")
        with pytest.raises(PermissionError):
            S.set_order_status(c, tk, "CONFIRMED", "mgr", "manager")
        S.set_order_status(c, tk, "CANCELLED", "mgr", "manager", note="not needed")
        assert S.open_orders(c) == []

    def test_bad_quote_values(self, c):
        _cart(c, code="T-IV", qty=2)
        tk = S.place_order(c, "mgr", "x")
        line = S.order_lines(c, tk["id"])[0]["id"]
        for bad in (-1.0, 60_000_000.0):
            with pytest.raises(ValueError):
                S.price_quote_line(c, tk, line, bad, "op")
        with pytest.raises(ValueError):
            S.price_quote_line(c, tk, 999, 5.0, "op")
        _cart(c, code="T-THERMO")
        t2 = S.place_order(c, "mgr", "y")
        with pytest.raises(ValueError):                                                 # list prices are not re-quoted
            S.price_quote_line(c, t2, S.order_lines(c, t2["id"])[0]["id"], 1.0, "op")

    def test_permissions(self):
        assert not S.can("viewer", "order") and S.can("manager", "order")
        assert S.can("operator", "order_work") and not S.can("manager", "order_work")
        assert S.can("admin", "catalog_edit") and not any(S.can(r, "catalog_edit") for r in ("viewer", "manager", "operator"))

    def test_migration_adds_columns_to_a_v292_database(self, tmp_path):
        import sqlite3
        p = tmp_path / "old.db"
        old = sqlite3.connect(p)
        old.execute("CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE NOT NULL, site_code TEXT NOT NULL, "
                    "title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', sla_class TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NEW', "
                    "kw_lost REAL, estimate_mxn REAL, approval TEXT NOT NULL DEFAULT 'not_required', detected_utc TEXT NOT NULL, "
                    "approved_utc TEXT NOT NULL DEFAULT '', responded_utc TEXT NOT NULL DEFAULT '', resolved_utc TEXT NOT NULL DEFAULT '', "
                    "created_by TEXT NOT NULL, created_utc TEXT NOT NULL, sample INTEGER NOT NULL DEFAULT 0)")
        old.execute("INSERT INTO tickets (number, site_code, title, sla_class, detected_utc, created_by, created_utc) "
                    "VALUES ('PL-0001','TST001','old','OTHER','2026-10-01 10:00:00','op','2026-10-01 10:00:00')")
        old.commit()
        old.close()
        con = S.connect(str(p))
        cols = {r[1] for r in con.execute("PRAGMA table_info(tickets)")}
        assert {"kind", "po_number", "preferred_date", "scheduled_date", "target_date"} <= cols
        assert con.execute("SELECT kind FROM tickets").fetchone()[0] == "incident"
        S.connect(str(p)).close()                                                       # idempotent
