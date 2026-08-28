"""Self-service password change (/account/) in the setup app.

Rules that must hold, whatever the UI looks like:
  * a user changes only their OWN password — the account comes from
    the basic-auth identity nginx forwards, never from a form field;
  * the current password must be proven before the new one is set;
  * wrong current-password attempts are throttled;
  * the new password is trimmed, has a floor length, must be repeated
    correctly and must differ from the current one;
  * the route is reachable by non-admins (all.htpasswd), while /setup/
    stays admin-only.

setup_app imports flask, absent from the test venv, so the pure
helpers are lifted out with ast and the route wiring is asserted
against the source itself.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "server" / "bundle" / "setup_app.py"
NGINX = ROOT / "server" / "bundle" / "nginx-argia_auth.conf"
MON_NGINX = ROOT / "server" / "bundle" / "nginx-monitoring.argia.com.mx.conf"
SOURCE = SRC.read_text(encoding="utf-8")

FNS = ("password_problem", "pw_lock_left", "pw_note_fail",
       "pw_clear_fails", "clean_password")
CONSTS = ("PW_MIN", "PW_FAIL_MAX", "PW_FAIL_WINDOW")


def _load():
    tree = ast.parse(SOURCE)
    fns = [n for n in tree.body
           if isinstance(n, ast.FunctionDef) and n.name in FNS]
    consts = [n for n in tree.body
              if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) in CONSTS for t in n.targets)]
    assert len(fns) == len(FNS) and len(consts) == len(CONSTS)
    ns = {"_time": __import__("time"), "_PW_FAILS": {}}
    exec(compile(ast.Module(body=consts + fns, type_ignores=[]),
                 "<setup_app:account>", "exec"), ns)
    return ns


NS = _load()
problem = NS["password_problem"]


class TestPasswordRules:
    GOOD = "Sol4rMonitor26"

    def test_accepts_a_good_change(self):
        assert problem(self.GOOD, self.GOOD, "oldPass2026") is None

    def test_rejects_empty(self):
        assert problem("", "", "old") == "Enter a new password."

    def test_rejects_too_short(self):
        msg = problem("Sol4r26", "Sol4r26", "old")
        assert msg and "at least 10" in msg

    def test_minimum_is_ten(self):
        assert NS["PW_MIN"] == 10
        ten = "abcdefgh12"
        assert problem(ten, ten, "old") is None
        assert problem(ten[:9], ten[:9], "old") is not None

    def test_rejects_mismatch(self):
        msg = problem(self.GOOD, self.GOOD + "x", "old")
        assert msg and "do not match" in msg

    def test_rejects_reuse_of_current(self):
        msg = problem(self.GOOD, self.GOOD, self.GOOD)
        assert msg and "same as the current" in msg

    def test_mismatch_checked_before_reuse(self):
        """Order matters: a typo must not be reported as 'reused'."""
        msg = problem(self.GOOD, "typo-here-1", self.GOOD)
        assert "do not match" in msg

    def test_trim_then_check(self):
        """A pasted password is trimmed before the rules run, so the
        two fields still match when one carries a stray space."""
        clean = NS["clean_password"]
        a, b = clean(f" {self.GOOD} "), clean(self.GOOD)
        assert problem(a, b, "old") is None


class TestThrottle:
    def setup_method(self):
        self.f = {}
        self.t = 1000.0

    def fail(self, n=1, user="eduardo"):
        for _ in range(n):
            NS["pw_note_fail"](user, now=self.t, fails=self.f)

    def left(self, user="eduardo", at=None):
        return NS["pw_lock_left"](user, now=self.t if at is None else at,
                                  fails=self.f)

    def test_clean_user_is_free(self):
        assert self.left() == 0

    def test_below_the_limit_is_free(self):
        self.fail(NS["PW_FAIL_MAX"] - 1)
        assert self.left() == 0

    def test_locks_at_the_limit(self):
        self.fail(NS["PW_FAIL_MAX"])
        assert self.left() > 0

    def test_lock_expires_after_the_window(self):
        self.fail(NS["PW_FAIL_MAX"])
        after = self.t + NS["PW_FAIL_WINDOW"] + 1
        assert self.left(at=after) == 0

    def test_window_rolls_not_slides(self):
        """Failures outside the window start a fresh count rather than
        keeping the user locked out forever."""
        self.fail(NS["PW_FAIL_MAX"])
        late = self.t + NS["PW_FAIL_WINDOW"] + 5
        NS["pw_note_fail"]("eduardo", now=late, fails=self.f)
        assert NS["pw_lock_left"]("eduardo", now=late, fails=self.f) == 0

    def test_success_clears_the_counter(self):
        self.fail(NS["PW_FAIL_MAX"])
        NS["pw_clear_fails"]("eduardo", fails=self.f)
        assert self.left() == 0

    def test_throttle_is_per_user(self):
        self.fail(NS["PW_FAIL_MAX"])
        assert self.left(user="arturo") == 0


class TestRouteWiring:
    def _fn(self, name):
        tree = ast.parse(SOURCE)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        return ast.dump(fn)

    def test_identity_comes_from_the_proxy_header(self):
        src = self._fn("account_change")
        assert "X-Remote-User" in src

    def test_username_never_taken_from_the_form(self):
        """The whole point: a user cannot aim the change at someone
        else by posting a username field."""
        src = self._fn("account_change")
        for field in ("'username'", "'user'", "'target'"):
            assert f"request.form.get({field}" not in SOURCE[
                SOURCE.index("def account_change"):]
        assert "'username'" not in src

    def test_current_password_is_verified(self):
        src = self._fn("account_change")
        assert "verify_pw" in src

    def test_csrf_checked(self):
        assert "check_csrf" in self._fn("account_change")

    def test_throttle_used_and_cleared(self):
        src = self._fn("account_change")
        for f in ("pw_lock_left", "pw_note_fail", "pw_clear_fails"):
            assert f in src

    def test_update_is_scoped_to_the_signed_in_user(self):
        body = SOURCE[SOURCE.index("def account_change"):]
        assert ("UPDATE users SET hash=? WHERE username=? "
                "AND disabled=0" in body)

    def test_sync_regenerates_htpasswd(self):
        assert "id='sync'" in self._fn("account_change")


class TestNginxExposure:
    REPORT = NGINX.read_text(encoding="utf-8")
    MON = MON_NGINX.read_text(encoding="utf-8")

    def test_account_served_on_both_sites(self):
        for conf in (self.REPORT, self.MON):
            assert "location /account/" in conf
            assert "proxy_pass http://127.0.0.1:8511/account/" in conf

    def test_account_open_to_every_signed_in_user(self):
        """No admin htpasswd on the /account/ location — otherwise
        exactly the people who need it most could not reach it."""
        for conf in (self.REPORT, self.MON):
            block = conf[conf.index("location /account/"):]
            block = block[:block.index("}")]
            assert "admin.htpasswd" not in block

    def test_monitoring_account_uses_all_htpasswd(self):
        block = self.MON[self.MON.index("location /account/"):]
        block = block[:block.index("}")]
        assert "all.htpasswd" in block

    def test_identity_header_forwarded(self):
        for conf in (self.REPORT, self.MON):
            block = conf[conf.index("location /account/"):]
            block = block[:block.index("}")]
            assert "X-Remote-User $remote_user" in block

    def test_setup_stays_admin_only(self):
        block = self.REPORT[self.REPORT.index("location /setup/"):]
        block = block[:block.index("}")]
        assert "admin.htpasswd" in block
