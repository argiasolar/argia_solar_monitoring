"""The nginx side of the session login.

nginx config is not executed by these tests, so these are structural
asserts on the file that ships.  They exist because the three ways this
config can be wrong are all silent:

  * a location that forgets `auth_request off` on the login page itself
    makes signing in impossible (you are redirected to the page you
    cannot reach);
  * an `error_page 401 = 200` style rewrite would turn a refusal into a
    served page;
  * leaving an `auth_basic` directive anywhere brings back the browser
    dialog this whole change exists to remove.

The rollback file is also pinned: it must stay in the tree, unchanged
and complete, or there is no way back if the session service fails.
"""
import re
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[2] / "server" / "bundle"
NEW = (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")
OLD = (BUNDLE / "nginx-argia_auth.conf").read_text(encoding="utf-8")


def locations(conf):
    return re.findall(r'location\s+(?:=\s+)?(\S+)\s*\{', conf)


def directives(conf):
    """The config without its comment lines — a comment explaining what
    was removed is not a directive that brings it back."""
    return "\n".join(ln for ln in conf.splitlines()
                     if not ln.lstrip().startswith("#"))


NEW_D = directives(NEW)


class TestNoBasicAuthLeftBehind:
    def test_no_auth_basic_directive(self):
        assert not re.search(r'^\s*auth_basic\b', NEW, re.M)

    def test_no_htpasswd_file_is_referenced(self):
        assert "htpasswd" not in NEW_D

    def test_the_realm_is_gone(self):
        assert '"ARGIA"' not in NEW_D


class TestAuthRequestWiring:
    def test_auth_request_is_on_by_default(self):
        assert re.search(r'^\s*auth_request /_argia_auth;', NEW, re.M)

    def test_the_internal_endpoint_is_internal(self):
        i = NEW.index("location = /_argia_auth")
        assert "internal;" in NEW[i:i + 200]

    def test_it_forwards_the_cookie_and_the_original_uri(self):
        i = NEW.index("location = /_argia_auth")
        block = NEW[i:i + 400]
        assert "X-Original-URI $request_uri" in block
        assert "Cookie $http_cookie" in block

    def test_it_does_not_forward_the_request_body(self):
        i = NEW.index("location = /_argia_auth")
        assert "proxy_pass_request_body off" in NEW[i:i + 400]

    def test_the_username_is_captured_for_the_backends(self):
        assert ("auth_request_set $argia_user "
                "$upstream_http_x_argia_user;") in NEW

    def test_setup_and_account_receive_that_username(self):
        for loc in ("location /setup/", "location /account/"):
            i = NEW.index(loc)
            assert "X-Remote-User $argia_user" in NEW[i:i + 400]

    def test_no_backend_still_trusts_remote_user(self):
        """$remote_user is only set by Basic auth; after this change it
        is always empty, so passing it would sign every request in as
        nobody."""
        assert "$remote_user" not in NEW


class TestTheWayIn:
    def test_401_goes_to_the_login_page(self):
        assert "error_page 401 = @argia_login;" in NEW
        assert "return 302 /login?next=$request_uri;" in NEW

    def test_403_goes_to_the_no_access_body(self):
        assert "error_page 403 /no-access.html;" in NEW

    def test_the_refusal_is_never_rewritten_to_success(self):
        assert not re.search(r'error_page\s+40[13]\s*=\s*200', NEW)

    def test_login_logout_and_whoami_skip_the_check(self):
        for loc in ("location = /login", "location = /logout",
                    "location = /session/whoami"):
            i = NEW.index(loc)
            assert "auth_request off;" in NEW[i:i + 300], loc

    def test_the_public_pages_skip_the_check(self):
        for loc in ("location = /logged-out.html",
                    "location = /no-access.html"):
            i = NEW.index(loc)
            assert "auth_request off;" in NEW[i:i + 200], loc

    def test_nothing_else_skips_the_check(self):
        """Every `auth_request off` is a hole in the login; count them."""
        allowed = {"/login", "/logout", "/session/whoami",
                   "/logged-out.html", "/no-access.html",
                   "/favicon.ico", "/favicon.svg"}
        holes = set()
        for m in re.finditer(r'location\s+(?:=\s+)?(\S+)\s*\{([^}]*)\}', NEW):
            if "auth_request off" in m.group(2):
                holes.add(m.group(1))
        assert holes <= allowed, f"unexpected public paths: {holes - allowed}"


class TestEveryProtectedPathStillServes:
    def test_all_ten_plants_have_a_location(self):
        locs = locations(NEW)
        for p in ("gto1", "mex1", "mex2", "nl1", "slp1", "slp2",
                  "gto2", "qro1", "nl2", "mex3"):
            assert f"/{p}/" in locs, p

    def test_the_shared_areas_have_a_location(self):
        locs = locations(NEW)
        for a in ("/financial/", "/capex/", "/monitoring/",
                  "/setup/", "/account/"):
            assert a in locs, a

    def test_no_location_from_the_old_conf_was_dropped(self):
        old = {p for p in locations(OLD) if p.startswith("/")}
        new = set(locations(NEW))
        # the old per-plant monitoring blocks are folded into the auth
        # service, so they need no location of their own any more
        folded = {"/monitoring/gto2/", "/monitoring/qro1/",
                  "/monitoring/nl2/", "/monitoring/mex3/",
                  "/monitoring/capex/", "/monitoring/assets/"}
        assert (old - new) <= folded, f"dropped: {(old - new) - folded}"


class TestRollbackStaysAvailable:
    def test_the_basic_auth_conf_is_still_in_the_tree(self):
        assert (BUNDLE / "nginx-argia_auth.conf").exists()

    def test_it_is_still_a_complete_working_config(self):
        assert "auth_basic_user_file /opt/argia/auth/all.htpasswd;" in OLD
        assert len(locations(OLD)) >= 18

    def test_the_new_file_documents_how_to_roll_back(self):
        assert "nginx-argia_auth.conf" in NEW
        assert "Rollback" in NEW
