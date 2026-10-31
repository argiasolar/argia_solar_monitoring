"""v338 - site handover from Prologis to ARGIA, and the one-line header.

Tomasz, 31 Oct 2026:
* "make a handover process as part of sites ... see where we are in the
  handover process in terms of documentation, physical walkthrough, protocols
  signed, measurements etc" and "create some artificial ... so we can show the
  functionality";
* "make Prologis logo same size as ARGIA";
* "make the web menu in one line, and try to reuse the full browser size".

Pinned here: the checklist and its progress maths, validation, sample rows
that never overwrite real work, roles, audit, the pages, the CLI, and the
header (logo height, account menu, full width, no wrapping).
"""
from __future__ import annotations

import datetime as dt
import importlib
import pathlib
import sqlite3
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
from argia.prologis import handover as HO                  # noqa: E402

TODAY = dt.date(2026, 10, 21)
CODES = ["A1", "A2", "A3", "A4", "A5", "A6"]


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    HO.ensure(c)
    return c


class TestChecklist:
    def test_template_is_consistent(self):
        assert len(HO.ITEM_KEYS) == len(set(HO.ITEM_KEYS)) == 21
        cats = {k for k, *_ in HO.CATEGORIES}
        assert cats == {"docs", "walk", "meas", "data", "sign"}
        for key, cat, who, en, es in HO.ITEMS:
            assert cat in cats and who in HO.WHO and en and es, key
        assert all(any(i[1] == cat for i in HO.ITEMS) for cat in cats)          # no empty step

    def test_a_site_without_rows_is_all_to_do_and_not_started(self):
        its = HO.items(_db(), "A1")
        assert [i["key"] for i in its] == HO.ITEM_KEYS and {i["status"] for i in its} == {HO.TODO}
        sm = HO.summary(its, TODAY)
        assert sm["stage"] == HO.NOT_STARTED and sm["pct"] == 0 and sm["done"] == 0 and sm["total"] == 21

    def test_progress_ignores_not_applicable_and_counts_blocked_and_overdue(self):
        its = HO.items(_db(), "A1")
        for i in its[:10]:
            i["status"] = HO.DONE
        its[10]["status"] = HO.NA
        its[11].update(status=HO.BLOCKED, due="2026-10-30")
        its[12].update(status=HO.PROGRESS, due="2026-10-20")                   # due yesterday
        its[13].update(status=HO.DONE, due="2026-10-01")                       # done items are never overdue
        sm = HO.summary(its, TODAY)
        assert sm["total"] == 20 and sm["done"] == 11 and sm["pct"] == pytest.approx(11 / 20)
        assert sm["blocked"] == 1 and sm["overdue"] == 1 and sm["stage"] == HO.IN_PROGRESS
        assert sm["by_cat"]["docs"] == (6, 6)

    def test_every_applicable_item_done_is_handed_over(self):
        its = HO.items(_db(), "A1")
        for i in its:
            i["status"] = HO.DONE
        its[0]["status"] = HO.NA
        assert HO.summary(its, TODAY)["stage"] == HO.HANDED_OVER and HO.summary(its)["pct"] == 1.0

    def test_set_item_stores_and_updates(self):
        c = _db()
        HO.set_item(c, "A1", "walk_visit", HO.DONE, "2026-11-02", " joint visit with FM ", "op", CODES)
        HO.set_item(c, "A1", "walk_visit", HO.PROGRESS, "", "", "op2", CODES)
        r = c.execute("SELECT * FROM handover_items").fetchall()
        assert len(r) == 1 and r[0]["status"] == HO.PROGRESS and r[0]["updated_by"] == "op2" and r[0]["due"] == ""

    @pytest.mark.parametrize("site,item,status,due,note,msg", [
        ("ZZ", "walk_visit", "done", "", "", "unknown site"),
        ("A1", "nope", "done", "", "", "unknown checklist item"),
        ("A1", "walk_visit", "finished", "", "", "unknown status"),
        ("A1", "walk_visit", "done", "31/10/2026", "", "YYYY-MM-DD"),
        ("A1", "walk_visit", "done", "2026-02-30", "", "day is out of range"),
        ("A1", "walk_visit", "done", "", "x" * 301, "longer than 300"),
    ])
    def test_nonsense_is_refused(self, site, item, status, due, note, msg):
        c = _db()
        with pytest.raises(ValueError, match=msg):
            HO.set_item(c, site, item, status, due, note, "op", CODES)
        assert c.execute("SELECT count(*) FROM handover_items").fetchone()[0] == 0


class TestSample:
    def test_plan_shows_every_state_and_is_deterministic(self):
        rows = HO.sample_plan(CODES, TODAY)
        assert rows == HO.sample_plan(CODES, TODAY)
        by_site = {}
        for code, key, status, due, note in rows:
            by_site.setdefault(code, []).append(status)
        assert by_site["A1"] == [HO.DONE] * 21                                   # one site handed over
        assert "A5" not in by_site and "A6" not in by_site                       # the last third not started
        assert any(r[2] == HO.BLOCKED for r in rows) and any(r[2] == HO.PROGRESS for r in rows)
        assert any(r[3] and r[3] < TODAY.isoformat() for r in rows)               # one overdue item
        assert all(r[3] >= TODAY.isoformat() for r in rows if r[2] == HO.BLOCKED)

    def test_seed_never_overwrites_real_work_and_clear_removes_only_samples(self):
        c = _db()
        HO.set_item(c, "A1", "docs_asbuilt", HO.BLOCKED, "", "real note", "op", CODES)
        n = HO.seed_sample(c, CODES, TODAY)
        assert n == len(HO.sample_plan(CODES, TODAY)) - 1
        assert HO.seed_sample(c, CODES, TODAY) == 0                               # idempotent
        real = c.execute("SELECT * FROM handover_items WHERE site_code='A1' AND item='docs_asbuilt'").fetchone()
        assert real["status"] == HO.BLOCKED and real["sample"] == 0 and real["note"] == "real note"
        assert HO.summary(HO.items(c, "A2"))["sample"] is True
        HO.set_item(c, "A2", "docs_asbuilt", HO.DONE, "", "", "op", CODES)        # a real edit adopts the row
        assert c.execute("SELECT sample FROM handover_items WHERE site_code='A2' AND item='docs_asbuilt'").fetchone()[0] == 0
        removed = HO.clear_sample(c)
        assert removed == n - 1
        left = c.execute("SELECT site_code, item FROM handover_items ORDER BY site_code").fetchall()
        assert [tuple(r) for r in left] == [("A1", "docs_asbuilt"), ("A2", "docs_asbuilt")]


# ------------------------------------------------------------------ the app
pytest.importorskip("flask", reason="flask not installed here")
BUNDLE = V2 / "server" / "bundle"
FIX = V2 / "tests" / "fixtures" / "prologis" / "registry.json"
sys.path.insert(0, str(BUNDLE))
from argia.prologis import store as S                      # noqa: E402
from tests.unit.test_prologis_app import sign_in, _csrf     # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k, v in (("ARGIA_PL_DIR", tmp_path), ("ARGIA_PL_DB", tmp_path / "pl.db"), ("ARGIA_PL_FILES", tmp_path / "files"),
                 ("ARGIA_PL_REGISTRY", FIX), ("ARGIA_PL_INSECURE_COOKIE", "1")):
        monkeypatch.setenv(k, str(v))
    monkeypatch.delenv("ARGIA_PL_CLIENT", raising=False)
    import prologis_app
    mod = importlib.reload(prologis_app)
    mod.app.config["TESTING"] = True
    c = S.connect(str(tmp_path / "pl.db"))
    pws = {r: S.create_user(c, r, r.title() + " User", f"{r}@x.test", "Prologis", r, "test") for r in S.ROLES}
    c.close()
    return mod, pws, tmp_path


def _code(mod):
    return mod.reg().sites[0].code


class TestPages:
    def test_pages_need_the_login(self, env):
        mod, _, _ = env
        cl = mod.app.test_client()
        for path in ("/sites/handover/", f"/sites/{_code(mod)}/handover"):
            r = cl.get(path)
            assert r.status_code == 302 and "/login" in r.headers["Location"], path
        assert cl.post(f"/sites/{_code(mod)}/handover").status_code == 302

    def test_viewer_sees_progress_but_no_forms(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        lst = cl.get("/sites/handover/").get_data(as_text=True)
        assert "Handover from Prologis" in lst and all(f"/sites/{s.code}/handover" in lst for s in mod.reg().sites)
        assert 'class="tab on" href="/sites/handover/"' in lst
        assert 'href="/sites/handover/"' in cl.get("/sites/").get_data(as_text=True)          # the tab on the sites page
        page = cl.get(f"/sites/{_code(mod)}/handover").get_data(as_text=True)
        assert "Documentation" in page and "Handover protocol signed by both sides" in page and 'class="hof"' not in page
        assert f'href="/sites/{_code(mod)}/handover"' in cl.get(f"/sites/{_code(mod)}/").get_data(as_text=True)  # card on the site page
        tok = _csrf(cl.get("/password").get_data(as_text=True))
        assert cl.post(f"/sites/{_code(mod)}/handover", data={"csrf": tok, "item": "walk_visit", "status": "done"}).status_code == 403
        assert cl.get("/sites/NOPE/handover").status_code == 404

    @pytest.mark.parametrize("role", ["manager", "operator", "admin"])
    def test_editors_record_an_item_with_an_audit_row(self, env, role):
        mod, pws, tmp = env
        cl = sign_in(mod, role, pws[role], tmp)
        code = _code(mod)
        page = cl.get(f"/sites/{code}/handover").get_data(as_text=True)
        assert 'class="hof"' in page
        r = cl.post(f"/sites/{code}/handover", data={"csrf": _csrf(page), "item": "meas_iv", "status": "progress",
                                                      "due": "2026-11-15", "note": "crew booked"})
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/sites/{code}/handover#i-meas_iv")
        c = S.connect(str(tmp / "pl.db"))
        row = c.execute("SELECT * FROM handover_items").fetchone()
        assert (row["site_code"], row["item"], row["status"], row["due"], row["note"], row["updated_by"]) == \
               (code, "meas_iv", "progress", "2026-11-15", "crew booked", role)
        assert c.execute("SELECT count(*) FROM audit WHERE action='handover_item' AND target=?", (f"{code}/meas_iv",)).fetchone()[0] == 1
        page = cl.get(f"/sites/{code}/handover").get_data(as_text=True)
        assert "crew booked" in page and 'value="2026-11-15"' in page
        c.close()

    def test_a_bad_value_shows_the_error_and_stores_nothing(self, env):
        mod, pws, tmp = env
        cl = sign_in(mod, "operator", pws["operator"], tmp)
        code = _code(mod)
        page = cl.get(f"/sites/{code}/handover").get_data(as_text=True)
        r = cl.post(f"/sites/{code}/handover", data={"csrf": _csrf(page), "item": "meas_iv", "status": "done", "due": "31.10.2026"})
        assert r.status_code == 200 and "YYYY-MM-DD" in r.get_data(as_text=True)
        c = S.connect(str(tmp / "pl.db"))
        assert c.execute("SELECT count(*) FROM handover_items").fetchone()[0] == 0
        c.close()

    def test_sample_is_labelled_and_the_cli_seeds_and_clears(self, env, capsys):
        mod, pws, tmp = env
        assert mod.main(["prologis_app.py", "--seed-sample-handover"]) == 0
        assert "sample handover rows written:" in capsys.readouterr().out
        cl = sign_in(mod, "viewer", pws["viewer"], tmp)
        lst = cl.get("/sites/handover/").get_data(as_text=True)
        assert "Sample handover data" in lst and "Handed over" in lst
        assert mod.main(["prologis_app.py", "--clear-sample-handover"]) == 0
        assert "Sample handover data" not in cl.get("/sites/handover/").get_data(as_text=True)


class TestHeader:
    def test_logo_height_matches_argia_letters(self):
        import prologis_ui as UI
        # ARGIA's mark is all letters (22 px); the Prologis capitals are 50.5 % of its logo file -> 44 px
        assert ".brand img.pl.rev{height:44px;background:none" in UI.CSS and ".brand img.ar{height:22px" in UI.CSS

    def test_menu_stays_on_one_line_and_the_page_uses_the_width(self):
        import prologis_ui as UI
        css = UI.CSS
        assert "nav.main{display:flex;gap:2px;margin-left:auto;flex-wrap:nowrap" in css and "white-space:nowrap}" in css
        assert ".topin{max-width:1920px" in css and "main{max-width:1920px" in css and ".foot{max-width:1920px" in css
        assert "1360px" not in css
        # below 1200 px the menu wraps to a second line rather than hiding links in a scroll area
        assert "@media(max-width:1199px){nav.main{flex-wrap:wrap;overflow:visible" in css

    def test_account_links_fold_into_one_menu(self, env):
        mod, pws, tmp = env
        for role, users in (("admin", True), ("viewer", False)):
            cl = sign_in(mod, role, pws[role], tmp)
            h = cl.get("/").get_data(as_text=True)
            who = h[h.index('<div class="who">'):h.index("</details></div>")]
            assert '<details class="um"><summary>' in who and 'href="/logout"' in who and 'href="/security/"' in who
            assert 'href="/lang/es"' in who and ('href="/admin/users/"' in who) is users
            nav = h[h.index('<nav class="main">'):h.index("</nav>")]
            assert nav.count("<a ") == 11 and "/logout" not in nav


def test_routes_are_in_the_contract():
    text = (V2 / "tests" / "contracts" / "routes.txt").read_text(encoding="utf-8")
    for line in ("prologis_app  GET    /sites/handover/", "prologis_app  GET    /sites/<code>/handover", "prologis_app  POST   /sites/<code>/handover"):
        assert line in text, line
