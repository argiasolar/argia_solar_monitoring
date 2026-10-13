"""v325 - the /board/ Flask app: who gets in, what each role may do, the
CSRF header, the JSON API, the pages, and the wiring (nginx, systemd,
auth, landing tile)."""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest

pytest.importorskip("flask")

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

import board_app as A                              # noqa: E402
from argia.board.service import Board              # noqa: E402
from argia.board.store import MemStore             # noqa: E402

STAFF = [{"username": "tomasz", "name": "Tomasz Zemelka", "email": "tomasz.zemelka@argia.com.mx"},
         {"username": "arturo", "name": "Arturo Gonzalez", "email": "arturo.gonzalez@argia.solar"},
         {"username": "vit", "name": "Vit Kovarik", "email": "vit.kovarik@argia.com.mx"},
         {"username": "tania", "name": "Tania Moreno", "email": "tania.moreno@argia.com.mx"}]
H = {"X-Argia-Board": "1"}


@pytest.fixture
def env(tmp_path):
    allow = tmp_path / "board_allow.txt"
    allow.write_text("# owners\n", encoding="utf-8")
    b = Board(MemStore())
    A.app.config.update(TESTING=True, BOARD=b, STAFF=lambda: STAFF, ALLOW_FILE=str(allow), FILES_DIR=str(tmp_path / "files"),
                        OWNER_EMAILS={"tomasz.zemelka@argia.com.mx"}, OWNER_USERS=set())
    yield A.app.test_client(), b, allow
    for k in ("BOARD", "STAFF", "ALLOW_FILE", "OWNER_EMAILS", "OWNER_USERS", "FILES_DIR"):
        A.app.config.pop(k, None)


def as_(u):
    return {"X-Remote-User": u}


def post(c, url, user, body=None, csrf=True):
    return c.post(url, json=body or {}, headers={**as_(user), **(H if csrf else {})})


# ------------------------------------------------------------------- access
def test_me_and_access(env):
    c, b, allow = env
    assert c.get("/board/me", headers=as_("tomasz")).get_json() == {"allowed": True, "role": "owner"}
    for who in ("", "arturo", "ghost"):
        assert c.get("/board/me", headers=as_(who)).get_json()["allowed"] is False
        assert c.get("/board/", headers=as_(who)).status_code == 403
        assert c.get("/board/api/state", headers=as_(who)).status_code == 403
    page = c.get("/board/", headers=as_("arturo")).get_data(as_text=True)
    assert "private for now" in page and '"cards"' not in page


def test_allow_file_makes_owners_live(env):
    c, b, allow = env
    allow.write_text("arturo.gonzalez@argia.solar\nvit  # by username\n", encoding="utf-8")
    assert c.get("/board/me", headers=as_("arturo")).get_json()["role"] == "owner"
    assert c.get("/board/me", headers=as_("vit")).get_json()["role"] == "owner"


def test_members_get_their_role_and_disabled_accounts_do_not(env):
    c, b, _ = env
    assert post(c, "/board/api/members", "tomasz", {"username": "arturo", "role": "editor"}).status_code == 200
    assert post(c, "/board/api/members", "tomasz", {"username": "vit", "role": "viewer"}).status_code == 200
    assert c.get("/board/me", headers=as_("arturo")).get_json()["role"] == "editor"
    assert c.get("/board/me", headers=as_("vit")).get_json()["role"] == "viewer"
    b.s.set_member("gone", "editor", "tomasz")                       # not (any more) an active internal account
    assert c.get("/board/me", headers=as_("gone")).get_json()["allowed"] is False
    # only owners manage members, and only known people
    assert post(c, "/board/api/members", "arturo", {"username": "tania", "role": "editor"}).status_code == 403
    assert post(c, "/board/api/members", "tomasz", {"username": "nobody", "role": "editor"}).status_code == 400
    assert post(c, "/board/api/members", "tomasz", {"username": "tania", "role": "god"}).status_code == 400
    r = post(c, "/board/api/members", "tomasz", {"username": "vit", "role": None})
    assert [m["username"] for m in r.get_json()["members"]] == ["arturo", "gone"]


def test_viewer_reads_comments_watches_but_cannot_change(env):
    c, b, _ = env
    b.s.set_member("vit", "viewer", "tomasz")
    cid = post(c, "/board/api/cards", "tomasz", {"title": "Ship board"}).get_json()["card"]["id"]
    assert c.get("/board/api/state", headers=as_("vit")).status_code == 200
    assert post(c, "/board/api/cards", "vit", {"title": "x"}).status_code == 403
    assert post(c, f"/board/api/cards/{cid}", "vit", {"title": "y"}).status_code == 403
    assert post(c, f"/board/api/cards/{cid}/move", "vit", {"col": "done", "order": [cid]}).status_code == 403
    assert post(c, f"/board/api/cards/{cid}/archive", "vit").status_code == 403
    assert post(c, f"/board/api/cards/{cid}/comment", "vit", {"body": "Looks good"}).status_code == 200
    assert post(c, f"/board/api/cards/{cid}/watch", "vit", {"on": True}).get_json()["card"]["watchers"] == ["tomasz", "vit"]
    st = c.get("/board/api/state", headers=as_("vit")).get_json()
    assert st["role"] == "viewer" and st["rights"] == ["comment", "watch"]


def test_writes_need_the_csrf_header(env):
    c, b, _ = env
    r = post(c, "/board/api/cards", "tomasz", {"title": "No header"}, csrf=False)
    assert r.status_code == 403 and b.cards() == []
    # a plain HTML form post (no JSON, no header) is refused too
    r = c.post("/board/api/cards", data={"title": "form"}, headers=as_("tomasz"))
    assert r.status_code == 403


# ---------------------------------------------------------------------- API
def test_card_lifecycle_through_the_api(env):
    c, b, _ = env
    r = post(c, "/board/api/cards", "tomasz", {"title": "Define G2 handover", "col": "todo", "process": "P3", "assignee": "arturo"})
    assert r.status_code == 201
    card = r.get_json()["card"]
    cid = card["id"]
    assert card["number"] == f"SB-{cid:04d}" and card["days"] == 0 and card["overdue"] is False
    r = post(c, f"/board/api/cards/{cid}", "tomasz", {"blocked": True, "blocked_reason": "Waiting for Vit"})
    assert r.get_json()["card"]["blocked"] is True
    r = post(c, f"/board/api/cards/{cid}/move", "tomasz", {"col": "doing", "order": [cid]})
    assert r.get_json()["card"]["col"] == "doing"
    r = post(c, f"/board/api/cards/{cid}/comment", "tomasz", {"body": "Draft in Drive"})
    assert [e["kind"] for e in r.get_json()["events"]] == ["created", "blocked", "moved", "comment"]
    d = c.get(f"/board/api/cards/{cid}", headers=as_("tomasz")).get_json()
    assert d["card"]["comments"] == 1 and len(d["events"]) == 4
    assert post(c, f"/board/api/cards/{cid}/archive", "tomasz").get_json() == {"ok": True}
    assert c.get(f"/board/api/cards/{cid}", headers=as_("tomasz")).status_code == 404
    assert c.get("/board/api/state", headers=as_("tomasz")).get_json()["cards"] == []


@pytest.mark.parametrize("body,code", [
    ({"title": ""}, 400), ({"title": "x", "col": "later"}, 400), ({"title": "x", "assignee": "ghost"}, 400),
    ({"title": "x", "due": "tomorrow"}, 400), ({"title": "x", "blocked": True}, 400), ({"title": "ok"}, 201),
])
def test_create_validation(env, body, code):
    c, _, _ = env
    r = post(c, "/board/api/cards", "tomasz", body)
    assert r.status_code == code
    if code == 400:
        assert r.get_json()["error"]


def test_unknown_card_and_bad_move(env):
    c, _, _ = env
    assert post(c, "/board/api/cards/999", "tomasz", {"title": "x"}).status_code == 404
    cid = post(c, "/board/api/cards", "tomasz", {"title": "a"}).get_json()["card"]["id"]
    assert post(c, f"/board/api/cards/{cid}/move", "tomasz", {"col": "doing", "order": "x"}).status_code == 400
    assert post(c, f"/board/api/cards/{cid}/move", "tomasz", {"col": "doing"}).status_code == 400


def test_seed_only_for_owner_and_only_once(env):
    c, b, _ = env
    b.s.set_member("arturo", "editor", "tomasz")
    assert post(c, "/board/api/seed", "arturo").status_code == 403
    n = post(c, "/board/api/seed", "tomasz").get_json()["added"]
    assert n == 9 and post(c, "/board/api/seed", "tomasz").get_json()["added"] == 0


# ------------------------------------------------------- attachments (v326)
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def upload(c, cid, user, files, body="", csrf=True):
    import io
    data = {"body": body, "files": [(io.BytesIO(d), n) for n, d in files]}
    return c.post(f"/board/api/cards/{cid}/files", data=data, content_type="multipart/form-data",
                  headers={**as_(user), **(H if csrf else {})})


def test_paste_a_screenshot_and_download_it(env):
    c, b, _ = env
    cid = post(c, "/board/api/cards", "tomasz", {"title": "SAG strings"}).get_json()["card"]["id"]
    r = upload(c, cid, "tomasz", [("screenshot-20261013-120000.png", PNG), ("scope.pdf", b"%PDF-1.4")], "Inverter 3 alarm")
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["card"]["files"] == 2 and [f["filename"] for f in d["files"]] == ["screenshot-20261013-120000.png", "scope.pdf"]
    assert d["events"][-1]["kind"] == "comment" and d["events"][-1]["body"] == "Inverter 3 alarm"
    fid = d["files"][0]["id"]
    g = c.get(f"/board/api/cards/{cid}/files/{fid}", headers=as_("tomasz"))
    assert g.status_code == 200 and g.data == PNG and g.mimetype == "image/png"
    assert "inline" in g.headers.get("Content-Disposition", "inline") and "sandbox" in g.headers["Content-Security-Policy"]
    assert g.headers["X-Content-Type-Options"] == "nosniff"
    # the card endpoint lists the files; the board shows the count
    assert len(c.get(f"/board/api/cards/{cid}", headers=as_("tomasz")).get_json()["files"]) == 2
    assert c.get("/board/api/state", headers=as_("tomasz")).get_json()["cards"][0]["files"] == 2


def test_office_files_download_instead_of_opening(env):
    c, _, _ = env
    cid = post(c, "/board/api/cards", "tomasz", {"title": "x"}).get_json()["card"]["id"]
    fid = upload(c, cid, "tomasz", [("plan.xlsx", b"PK\x03\x04")]).get_json()["files"][0]["id"]
    g = c.get(f"/board/api/cards/{cid}/files/{fid}", headers=as_("tomasz"))
    assert g.headers["Content-Disposition"].startswith("attachment")


def test_upload_rules(env):
    c, b, _ = env
    b.s.set_member("vit", "viewer", "tomasz")
    cid = post(c, "/board/api/cards", "tomasz", {"title": "x"}).get_json()["card"]["id"]
    assert upload(c, cid, "tomasz", [("a.png", PNG)], csrf=False).status_code == 403
    assert upload(c, cid, "ghost", [("a.png", PNG)]).status_code == 403
    assert upload(c, cid, "tomasz", [("tool.exe", b"MZ")]).status_code == 400
    assert upload(c, cid, "tomasz", [("invoice.pdf", b"MZ\x90\x00")]).status_code == 400
    assert upload(c, cid, "tomasz", [("x.svg", b"<svg onload=alert(1)>")]).status_code == 400
    assert upload(c, cid, "tomasz", []).status_code == 400
    assert upload(c, 999, "tomasz", [("a.png", PNG)]).status_code == 404
    assert upload(c, cid, "vit", [("a.png", PNG)], "viewer screenshot").status_code == 200   # viewers may attach
    assert c.get(f"/board/api/cards/{cid}/files/999", headers=as_("tomasz")).status_code == 404
    assert c.get(f"/board/api/cards/{cid}/files/1", headers=as_("ghost")).status_code == 403


def test_too_large_request_is_a_json_413(env):
    c, _, _ = env
    cid = post(c, "/board/api/cards", "tomasz", {"title": "x"}).get_json()["card"]["id"]
    old = A.app.config["MAX_CONTENT_LENGTH"]
    A.app.config["MAX_CONTENT_LENGTH"] = 1000
    try:
        r = upload(c, cid, "tomasz", [("a.png", PNG + b"0" * 2000)])
    finally:
        A.app.config["MAX_CONTENT_LENGTH"] = old
    assert r.status_code == 413 and "15 MB" in r.get_json()["error"]


def test_page_has_the_paste_and_attach_ui(env):
    c, _, _ = env
    h = c.get("/board/", headers=as_("tomasz")).get_data(as_text=True)
    assert "addEventListener('paste'" in h and "/files'" in h and "f_files" in h and "screenshot-" in h
    # v326 regression: the drawer is a scrolling flex column - without this the title textarea shrank to a sliver
    assert ".dbody>*{flex-shrink:0}" in A.CSS


# -------------------------------------------------------------------- pages
def test_board_page_is_the_portal_page_with_safe_boot_data(env):
    c, b, _ = env
    post(c, "/board/api/cards", "tomasz", {"title": "</script><script>alert(1)</script>"})
    r = c.get("/board/", headers=as_("tomasz"))
    h = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert 'class="ph noprint"' in h and "Team board" in h and "portal.argia.com.mx/board" in h
    assert "<script>alert(1)" not in h                                  # the boot JSON cannot close its script tag
    boot = re.search(r'<script type="application/json" id="boot">(.*?)</script>', h, re.S).group(1)
    data = json.loads(boot)
    assert data["role"] == "owner" and data["cards"][0]["title"].startswith("</script>")
    assert "/board/members/" in h and chr(0x2014) not in h


def test_members_tab_only_for_owners(env):
    c, b, _ = env
    b.s.set_member("arturo", "editor", "tomasz")
    owner = c.get("/board/members/", headers=as_("tomasz"))
    assert owner.status_code == 200 and "Arturo Gonzalez" in owner.get_data(as_text=True)
    assert c.get("/board/members/", headers=as_("arturo")).status_code == 403
    assert "/board/members/" not in c.get("/board/", headers=as_("arturo")).get_data(as_text=True)


# ------------------------------------------------------------------- wiring
def test_wiring_nginx_systemd_auth_tile():
    ng = (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")
    assert "location /board/ {\n    client_max_body_size 160m;" in ng and "proxy_pass http://127.0.0.1:8516;" in ng
    assert "X-Remote-User $argia_user" in ng
    unit = (BUNDLE / "argia-board.service").read_text(encoding="utf-8")
    assert "ARGIA_BOARD_PORT=8516" in unit and "ARGIA_BOARD_EMAILS=tomasz.zemelka@argia.com.mx" in unit and "board_app.py" in unit
    assert "ARGIA_BOARD_FILES=/opt/argia/board_files" in unit
    import auth_core
    assert auth_core.area_for_path("/board/api/state") == auth_core.ALL
    gen = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
    assert "('board', 'Team board', 'Tablero del equipo', '/board/'" in gen and 'id="tile-{key}"' in gen
    import portal_chrome as PC
    assert "fetch('/board/me'" in PC.JS and "tb.classList.add('boardok')" in PC.JS
    assert "#tile-board:not(.boardok){display:none!important}" in PC.CSS and "board" in PC.SECTIONS
    ops = (V2 / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    assert "argia-board" in ops and "board_allow.txt" in ops
    other_ports = [p for p in re.findall(r"127\.0\.0\.1:(\d+)", ng) if p == "8516"]
    assert len(other_ports) == 1                                        # nobody else on 8516
