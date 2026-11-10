"""v367 - the team board in a real browser (Tomasz, 10 Nov 2026):

1. "when we say blocked we can automatically create new ticket that will be related to unblocking"
2. "a parent ticket where we can relate new tickets to it" (chosen over #tags: progress + one filter)
3. "a way to move to next task without closing and opening the side window over and over again"

The Flask app runs on the in-memory store in a thread; Chromium drives the page. All data invented.
"""
from __future__ import annotations

import sys
import threading

import pytest

pytest.importorskip("flask")
pw = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")

from tests.unit.test_board_app import BUNDLE, STAFF   # noqa: E402,F401  (puts the bundle on sys.path)

sys.path.insert(0, str(BUNDLE))
import board_app as A                                  # noqa: E402
from argia.board.service import Board                 # noqa: E402
from argia.board.store import MemStore                # noqa: E402

PEOPLE = [p["username"] for p in STAFF]


@pytest.fixture
def site():
    from werkzeug.serving import make_server
    b = Board(MemStore())
    eng = b.create("tomasz", {"title": "engine.argia.com.mx", "col": "backlog"}, PEOPLE)
    for t, col in (("Offer page v2", "doing"), ("CFE tariff import", "todo"), ("Login with Google", "done")):
        b.create("tomasz", {"title": t, "col": col, "parent_id": eng["id"], "assignee": "arturo"}, PEOPLE)
    for t in ("Helioscope for SAG", "Helioscope for Taigene", "Setup monitoring for PPA"):
        b.create("tomasz", {"title": t, "col": "todo", "assignee": "arturo"}, PEOPLE)
    A.app.config.update(TESTING=True, BOARD=b, STAFF=lambda: STAFF, ALLOW_FILE="/nonexistent",
                        OWNER_EMAILS={"tomasz.zemelka@argia.com.mx"}, OWNER_USERS=set())
    srv = make_server("127.0.0.1", 0, A.app, threaded=True)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_port}/board/", b, eng["id"]
    srv.shutdown()
    for k in ("BOARD", "STAFF", "ALLOW_FILE", "OWNER_EMAILS", "OWNER_USERS"):
        A.app.config.pop(k, None)


def _wait(pg, js, arg=None):
    pg.wait_for_function(js, arg=arg, timeout=8000)


def test_parent_navigation_and_unblocking_in_the_browser(site):
    url, b, eng = site
    with pw.sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1600, "height": 950}, extra_http_headers={"X-Remote-User": "tomasz"})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(url)
        pg.wait_for_selector(".bc")
        # 2. parent: progress on the parent, a chip on each card under it, a filter
        assert pg.locator(".bc.parent").count() == 1
        assert "1/3" in pg.locator(".bc.parent .prog").inner_text()
        assert pg.locator(".bc .par").count() == 3 and pg.is_visible("#fpar")
        order = pg.eval_on_selector_all(".bc", "els => els.map(e => +e.dataset.id)")
        n = len(order)
        # 3. move through the cards with the panel open
        pg.locator(".bc").first.click()
        pg.wait_for_selector("#f_title")
        assert pg.inner_text("#dpos") == f"1/{n}" and pg.is_disabled("#dprev")
        pg.locator("#dbody .kicker").first.click()                      # out of any field
        pg.keyboard.press("j")
        _wait(pg, "n => document.getElementById('dpos').textContent === '2/' + n", n)
        pg.click("#dnext")
        _wait(pg, "n => document.getElementById('dpos').textContent === '3/' + n", n)
        pg.wait_for_selector("#f_title")
        pg.focus("#f_title")
        pg.keyboard.press("Alt+ArrowUp")                                # works inside a field
        _wait(pg, "n => document.getElementById('dpos').textContent === '2/' + n", n)
        assert int(pg.locator(".bc.cur").get_attribute("data-id")) == order[1]
        assert pg.url.endswith("#SB-%04d" % order[1])
        # an unsaved change is not lost silently
        pg.fill("#f_title", "changed")
        seen = []
        pg.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
        pg.click("#dnext")
        pg.wait_for_timeout(300)
        assert seen and pg.inner_text("#dpos") == f"2/{n}"
        # the board stays clickable while the panel is open
        pg.once("dialog", lambda d: d.accept())
        pg.locator(f'.bc[data-id="{order[-1]}"]').click()
        _wait(pg, "n => document.getElementById('dpos').textContent === n + '/' + n", n)
        # 1. blocked -> an unblocking card in This week
        pg.wait_for_selector("#f_blocked")
        pg.check("#f_blocked")
        pg.fill("#f_blocked_reason", "Waiting for the CFE quote")
        assert pg.is_checked("#ub_make")
        pg.select_option("#ub_who", "arturo")
        pg.fill("#ub_due", "2026-11-14")
        pg.click("#dsave")
        _wait(pg, "() => [...document.querySelectorAll('.blist[data-col=\"todo\"] .bc .t')].some(e => e.textContent.startsWith('Unblock: '))")
        ub = pg.locator(".bc", has_text="Unblock: ")
        assert ("SB-%04d" % order[-1]) in ub.locator(".unb").inner_text()
        _wait(pg, "() => [...document.querySelectorAll('#dbody .rc')].some(e => e.textContent.includes('Unblock: '))")
        uid = int(ub.get_attribute("data-id"))
        u = b.card(uid)
        assert (u["assignee"], u["due"], u["unblocks_id"]) == ("arturo", "2026-11-14", order[-1])
        # its Done unblocks the card
        b.move("arturo", uid, "done", [uid])
        pg.reload()
        pg.wait_for_selector("#f_blocked")
        assert not pg.is_checked("#f_blocked") and "automatically" in pg.inner_text("#evs")
        # a card added under the parent from its panel, then the parent filter
        pg.locator(".bc.parent").click()
        pg.wait_for_selector("#k_title")
        pg.fill("#k_title", "Engine: PDF export")
        pg.press("#k_title", "Enter")
        _wait(pg, "() => [...document.querySelectorAll('#dbody .rc')].some(e => e.textContent.includes('Engine: PDF export'))")
        pg.click("#dclose")
        pg.select_option("#fpar", str(eng))
        # the parent, its 3 cards, the new one and the unblocking card (it inherits the topic)
        assert pg.locator(".bc").count() == 6
        assert errors == []
        br.close()
