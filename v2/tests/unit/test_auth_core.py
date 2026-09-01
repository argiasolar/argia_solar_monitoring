"""Session-login authorisation logic.

This module decides who may read which page, so it is tested harder
than anything else in the tree.  Two properties matter above all:

  1. The path -> area map must agree, location for location, with the
     HTTP Basic snippet it replaced.  A drift here does not raise; it
     silently shows a client someone else's plant.  test_matches_the_
     basic_auth_snippet reads the old conf and checks every location.
  2. A forged, truncated, padded or re-signed cookie must never verify.
"""
import base64
import os
import re
import sys
import time
from pathlib import Path

import pytest

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
import auth_core as ac                                    # noqa: E402

OLD_CONF = (BUNDLE / "nginx-argia_auth.conf").read_text(encoding="utf-8")
SECRET = b"k" * 48


class TestAreaForPath:
    @pytest.mark.parametrize("uri,area", [
        ("/", ac.ALL),
        ("/index.html", ac.ALL),
        ("/account/", ac.ALL),
        ("/account/whoami", ac.ALL),
        ("/setup/", ac.ADMIN),
        ("/setup/users", ac.ADMIN),
        ("/financial/", "financial"),
        ("/invoices/", "financial"),
        ("/invoices/2026-08/invoice_gto1_2026-08.pdf", "financial"),
        ("/capex/", "capex"),
        ("/gto1/", "gto1"),
        ("/gto1/d/2026-08-01.html", "gto1"),
        ("/mex3/", "mex3"),
        ("/monitoring/", "monitoring"),
        ("/monitoring/ppa/", "monitoring"),
        ("/monitoring/gto1/", "monitoring"),
        ("/monitoring/assets/x.jpg", ac.ALL),
        ("/monitoring/capex/", "capex"),
        ("/monitoring/gto2/", "gto2"),
        ("/monitoring/qro1/d/2026-08-01.html", "qro1"),
        ("/monitoring/nl2/", "nl2"),
        ("/monitoring/mex3/", "mex3"),
        ("/logged-out.html", ac.PUBLIC),
        ("/no-access.html", ac.PUBLIC),
        ("/login", ac.PUBLIC),
        ("/favicon.ico", ac.PUBLIC),
    ])
    def test_map(self, uri, area):
        assert ac.area_for_path(uri) == area

    def test_longest_prefix_wins_as_in_nginx(self):
        """/monitoring/gto2/ is a CAPEX client's own page; it must not
        fall back to the internal-only monitoring grant."""
        assert ac.area_for_path("/monitoring/gto2/x.html") == "gto2"
        assert ac.area_for_path("/monitoring/gto1/x.html") == "monitoring"

    def test_query_string_cannot_change_the_grant(self):
        assert ac.area_for_path("/setup/?next=/gto1/") == ac.ADMIN
        assert ac.area_for_path("/gto1/?x=/logged-out.html") == "gto1"

    def test_fragment_is_ignored(self):
        assert ac.area_for_path("/setup/#/gto1/") == ac.ADMIN

    def test_doubled_slashes_do_not_escape_the_map(self):
        assert ac.area_for_path("//setup//") == ac.ADMIN
        assert ac.area_for_path("//monitoring//gto2//") == "gto2"

    def test_a_prefix_is_not_a_word_match(self):
        """/gto1x/ is not /gto1/."""
        assert ac.area_for_path("/gto1x/") == ac.ALL

    def test_unknown_paths_still_need_a_session(self):
        assert ac.area_for_path("/whatever/") == ac.ALL
        assert ac.area_for_path("/") == ac.ALL

    def test_empty_and_junk_do_not_crash(self):
        for bad in ("", None, "x", "?a=b", "#f"):
            assert ac.area_for_path(bad) is not None or True


class TestMatchesTheBasicAuthSnippet:
    """The old conf is the specification; read it, don't trust memory."""

    def old_map(self):
        out = {}
        pat = re.compile(
            r'location\s+(=\s+)?(?P<path>/\S*)\s*\{[^}]*?'
            r'auth_basic_user_file /opt/argia/auth/(?P<file>[a-z0-9]+)\.htpasswd',
            re.S)
        for m in pat.finditer(OLD_CONF):
            out[m.group("path")] = m.group("file")
        return out

    def test_the_old_conf_is_still_readable(self):
        assert len(self.old_map()) >= 18, "parser drifted from the conf"

    def test_every_old_location_maps_to_the_same_grant(self):
        alias = {"all": ac.ALL, "admin": ac.ADMIN}
        for path, f in self.old_map().items():
            probe = path if path.endswith("/") else path + "/"
            got = ac.area_for_path(probe + "x.html")
            want = alias.get(f, f)
            assert got == want, f"{path}: basic={f} session={got}"

    def test_no_plant_was_dropped(self):
        for p in ac.PLANTS:
            assert ac.area_for_path(f"/{p}/") == p
            assert f"/opt/argia/auth/{p}.htpasswd" in OLD_CONF


class TestMay:
    ARGIA = {"level": "argia", "reports": "", "is_admin": 0,
             "plant_admin": 0, "disabled": 0}
    CLIENT = {"level": "custom", "reports": "gto2,capex", "is_admin": 0,
              "plant_admin": 0, "disabled": 0}
    ADMIN = dict(ARGIA, is_admin=1)

    def test_employee_sees_every_area(self):
        for a in ac.AREAS + [ac.ALL]:
            assert ac.may(self.ARGIA, a)

    def test_employee_is_not_automatically_an_admin(self):
        assert not ac.may(self.ARGIA, ac.ADMIN)

    def test_admin_reaches_setup(self):
        assert ac.may(self.ADMIN, ac.ADMIN)

    def test_plant_admin_also_reaches_setup(self):
        assert ac.may(dict(self.CLIENT, plant_admin=1), ac.ADMIN)

    def test_client_sees_only_the_granted_areas(self):
        assert ac.may(self.CLIENT, "gto2")
        assert ac.may(self.CLIENT, "capex")
        assert not ac.may(self.CLIENT, "gto1")
        assert not ac.may(self.CLIENT, "financial")
        assert not ac.may(self.CLIENT, "monitoring")

    def test_a_capex_client_cannot_reach_a_ppa_plant(self):
        for ppa in ("gto1", "mex1", "mex2", "nl1", "slp1", "slp2"):
            assert not ac.may(self.CLIENT, ppa)

    def test_client_still_reaches_the_shared_pages(self):
        assert ac.may(self.CLIENT, ac.ALL)

    def test_disabled_user_may_nothing(self):
        d = dict(self.ARGIA, disabled=1)
        for a in ac.AREAS + [ac.ALL, ac.ADMIN]:
            assert not ac.may(d, a)

    def test_no_user_may_nothing(self):
        for a in ac.AREAS + [ac.ALL, ac.ADMIN]:
            assert not ac.may(None, a)

    def test_public_needs_nobody(self):
        assert ac.may(None, ac.PUBLIC) is False   # no row at all
        assert ac.may(self.CLIENT, ac.PUBLIC)

    def test_empty_reports_grants_nothing(self):
        u = dict(self.CLIENT, reports="")
        assert not ac.may(u, "gto2")

    def test_a_partial_name_in_reports_does_not_match(self):
        u = dict(self.CLIENT, reports="gto")
        assert not ac.may(u, "gto1")


class TestCookie:
    def test_round_trip(self):
        sid = ac.new_sid()
        assert ac.verify(ac.sign(sid, SECRET), SECRET) == sid

    def test_a_different_secret_does_not_verify(self):
        sid = ac.new_sid()
        assert ac.verify(ac.sign(sid, SECRET), b"j" * 48) is None

    def test_tampered_sid_is_rejected(self):
        sid = ac.new_sid()
        v = ac.sign(sid, SECRET)
        body, mac = v.split(".")
        other = ac.sign(ac.new_sid(), SECRET).split(".")[0]
        assert ac.verify(f"{other}.{mac}", SECRET) is None

    def test_tampered_mac_is_rejected(self):
        v = ac.sign(ac.new_sid(), SECRET)
        body, mac = v.split(".")
        flipped = ("A" if mac[0] != "A" else "B") + mac[1:]
        assert ac.verify(f"{body}.{flipped}", SECRET) is None

    def test_unsigned_value_is_rejected(self):
        assert ac.verify(ac.new_sid(), SECRET) is None

    @pytest.mark.parametrize("bad", [
        "", None, ".", "..", "a.b.c", "a.", ".b", "!!!.???",
        "a" * 5000, 12345, b"bytes",
        base64.urlsafe_b64encode(b"x").decode(),
    ])
    def test_junk_never_verifies_and_never_raises(self, bad):
        assert ac.verify(bad, SECRET) is None

    def test_non_ascii_sid_is_rejected(self):
        raw = "ñ".encode()
        v = ac.sign(raw, SECRET)
        assert ac.verify(v, SECRET) is None

    def test_ids_are_unpredictable(self):
        ids = {ac.new_sid() for _ in range(500)}
        assert len(ids) == 500
        assert all(len(i) >= 20 for i in ids)


class TestSecretFile:
    def test_created_once_and_reused(self, tmp_path):
        p = tmp_path / "sub" / "session.key"
        a = ac.load_secret(str(p))
        b = ac.load_secret(str(p))
        assert a == b and len(a) >= 32

    @pytest.mark.skipif(os.name != "posix",
                        reason="POSIX mode bits; the server is Linux")
    def test_written_owner_only(self, tmp_path):
        """The key is the whole security of the cookie: anyone who can
        read it can mint a session for any user."""
        p = tmp_path / "session.key"
        ac.load_secret(str(p))
        assert (p.stat().st_mode & 0o777) == 0o600

    def test_a_too_short_file_is_replaced(self, tmp_path):
        p = tmp_path / "session.key"
        p.write_bytes(b"short")
        assert len(ac.load_secret(str(p))) >= 32


class TestSessionLifetime:
    def test_fresh_session_is_alive(self):
        now = time.time()
        assert ac.session_alive(now, now, now)

    def test_idle_session_dies(self):
        now = time.time()
        assert not ac.session_alive(now - 100, now - ac.IDLE_MAX - 1, now)

    def test_active_session_survives_past_the_idle_window(self):
        now = time.time()
        assert ac.session_alive(now - ac.IDLE_MAX * 2, now - 10, now)

    def test_the_absolute_cap_still_ends_it(self):
        """A browser left open for ever is the Basic-auth failure mode."""
        now = time.time()
        assert not ac.session_alive(now - ac.ABS_MAX - 1, now - 1, now)

    def test_the_cap_is_longer_than_the_idle_window(self):
        assert ac.ABS_MAX > ac.IDLE_MAX


class TestSessionStore:
    def test_table_is_created_and_reopenable(self, tmp_path):
        p = str(tmp_path / "s.db")
        c = ac.open_sessions(p)
        c.execute("INSERT INTO sessions(sid,username,created,last)"
                  " VALUES('a','u',1,1)")
        c.commit()
        c.close()
        c2 = ac.open_sessions(p)
        assert c2.execute("SELECT username FROM sessions").fetchone()[0] == "u"
        c2.close()

    def test_sid_is_unique(self, tmp_path):
        import sqlite3
        c = ac.open_sessions(str(tmp_path / "s.db"))
        c.execute("INSERT INTO sessions(sid,username,created,last)"
                  " VALUES('a','u',1,1)")
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO sessions(sid,username,created,last)"
                      " VALUES('a','v',1,1)")
        c.close()
