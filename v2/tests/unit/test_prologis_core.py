"""v292 - ARGIA for Prologis: the pure parts (TOTP, MSA response clock,
registry, sample metering, SQLite store). Synthetic data only."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from types import SimpleNamespace as NS

import pytest

from argia.prologis import metering as M
from argia.prologis import registry as R
from argia.prologis import sla as SLA
from argia.prologis import store as S
from argia.prologis import totp as TOTP

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "prologis" / "registry.json"
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"      # base32("12345678901234567890")


class TestTotp:
    def test_rfc6238_vectors(self):
        # RFC 6238 appendix B, SHA-1, last 6 digits of the 8-digit values
        assert TOTP.code_at(RFC_SECRET, 59 // 30) == "287082"
        assert TOTP.code_at(RFC_SECRET, 1111111109 // 30) == "081804"
        assert TOTP.code_at(RFC_SECRET, 1234567890 // 30) == "005924"

    def test_window_and_replay(self):
        now = 1_800_000_000.0
        code = TOTP.code_at(RFC_SECRET, int(now // 30) - 1)
        ok, step = TOTP.verify(RFC_SECRET, code, now=now)
        assert ok and step == int(now // 30) - 1
        assert TOTP.verify(RFC_SECRET, code, now=now, last_step=step) == (False, step)     # replay refused
        assert not TOTP.verify(RFC_SECRET, "000000", now=now)[0] or TOTP.code_at(RFC_SECRET, int(now // 30)) == "000000"
        assert not TOTP.verify(RFC_SECRET, "12a", now=now)[0]
        assert not TOTP.verify(RFC_SECRET, TOTP.code_at(RFC_SECRET, int(now // 30) - 3), now=now)[0]

    def test_secret_and_uri(self):
        s = TOTP.new_secret()
        assert len(s) == 32 and s.isalnum()
        u = TOTP.uri(s, "ana@prologis")
        assert u.startswith("otpauth://totp/") and f"secret={s}" in u and "issuer=ARGIA" in u


class TestSla:
    def test_classes_follow_the_msa(self):
        assert SLA.suggest(None, safety=True) == "EMERGENCY"
        assert SLA.suggest(620) == "OUT_500"
        assert SLA.suggest(300) == "OUT_100_500"
        assert SLA.suggest(80) == "STRING_100"
        assert SLA.suggest(20) == "STRING_25"
        assert SLA.suggest(0.8) == "MICRO"
        assert SLA.suggest(None, comm_loss=True) == "COMM_1"
        assert SLA.suggest(None, comm_loss=True, operation_confirmed=True) == "COMM_2"
        assert SLA.BY_CODE["OUT_500"].hours == 24 and not SLA.BY_CODE["OUT_500"].approval_needed
        assert SLA.BY_CODE["OUT_100_500"].hours == 48 and SLA.BY_CODE["OUT_100_500"].approval_needed
        assert SLA.BY_CODE["STRING_25"].hours == 240 and SLA.BY_CODE["OTHER"].hours is None

    def test_clock_waits_for_approval_and_pauses(self):
        d = dt.datetime(2026, 11, 2, 9, 0)
        cls = SLA.BY_CODE["OUT_100_500"]
        assert SLA.clock_start(cls, d, None) is None
        start = SLA.clock_start(cls, d, d + dt.timedelta(hours=3))
        assert start == d + dt.timedelta(hours=3)
        assert SLA.clock_start(SLA.BY_CODE["OUT_500"], d, None) == d          # no approval needed
        used = SLA.elapsed_hours(start, start + dt.timedelta(hours=30),
                                 [(start + dt.timedelta(hours=5), start + dt.timedelta(hours=15))])
        assert used == pytest.approx(20.0)
        assert SLA.elapsed_hours(start, start + dt.timedelta(hours=10), [(start + dt.timedelta(hours=8), None)]) == pytest.approx(8.0)

    def test_status_and_compliance(self):
        c = SLA.BY_CODE["OUT_500"]
        assert SLA.status(c, 23.0, True) == "met" and SLA.status(c, 25.0, True) == "breached"
        assert SLA.status(c, 10.0, False) == "running" and SLA.status(c, 30.0, False) == "breached"
        assert SLA.status(SLA.BY_CODE["OTHER"], 100.0, False) == "due"
        assert SLA.status(c, None, False) == "not_started"
        assert SLA.compliance(["met", "met", "breached", "running"]) == pytest.approx(2 / 3)
        assert SLA.compliance(["running"]) is None


class TestRegistry:
    def test_fixture_loads(self):
        rg = R.load(str(FIX))
        assert len(rg.sites) == 9 and len(rg.operating) == 7 and rg.site("tst001").code == "TST001"
        assert rg.kwp_total == pytest.approx(sum(s.kwp for s in rg.sites))
        assert rg.projects[1].stage_index == R.STAGES.index("Construction")

    def test_all_problems_reported_at_once(self):
        data = json.loads(FIX.read_text())
        data["sites"][1]["code"] = data["sites"][0]["code"]
        data["sites"][2]["lat"] = 48.1
        data["projects"][0]["stage"] = "Dreaming"
        with pytest.raises(ValueError) as ex:
            R.parse(data)
        msg = str(ex.value)
        assert "duplicate" in msg and "outside Mexico" in msg and "Dreaming" in msg


SITE = NS(code="TST001", lat=19.65, lon=-99.21, kwp=619.4, operating=True)


class TestMetering:
    def test_deterministic_and_plausible(self):
        d = dt.date(2026, 4, 10)
        a = M.day_result(SITE.code, SITE.lat, SITE.lon, SITE.kwp, d)
        b = M.day_result(SITE.code, SITE.lat, SITE.lon, SITE.kwp, d)
        assert a.kwh == b.kwh and a.series == b.series
        for day in (dt.date(2026, 1, 15), dt.date(2026, 4, 10), dt.date(2026, 7, 15), dt.date(2026, 10, 1)):
            r = M.day_result(SITE.code, SITE.lat, SITE.lon, SITE.kwp, day)
            assert 1.5 < r.kwh / SITE.kwp < 7.5, day                     # kWh/kWp/day, central Mexico
            assert max(v for _, v in r.series if v is not None) <= SITE.kwp * M.AC_RATIO + 0.01
            assert 0 <= r.availability <= 1

    def test_night_is_zero_and_noon_is_not(self):
        assert M.sun_elevation(19.65, -99.21, dt.datetime(2026, 6, 21, 2, 0)) < 0
        assert M.sun_elevation(19.65, -99.21, dt.datetime(2026, 6, 21, 13, 15)) > 80   # near zenith at the solstice
        lv = M.live(SITE, dt.datetime(2026, 6, 21, 23, 30))
        assert lv.status in ("night", "comm_loss") and lv.source == "sample"

    def test_events_show_up(self):
        days = [dt.date(2026, 1, 1) + dt.timedelta(days=i) for i in range(365)]
        evs = [(d, M.event_for(SITE.code, d, 1, 0)) for d in days]
        evs = [(d, e) for d, e in evs if e]
        assert 3 <= len(evs) <= 30
        d, e = next((d, e) for d, e in evs if e.kind == "inverter_off")
        r = M.day_result(SITE.code, SITE.lat, SITE.lon, SITE.kwp, d)
        assert r.availability < 1
        d2, _ = next((d, e) for d, e in evs if e.kind == "comm_loss")
        r2 = M.day_result(SITE.code, SITE.lat, SITE.lon, SITE.kwp, d2)
        assert any(v is None for _, v in r2.series) and r2.availability == 1      # data gap is not downtime

    def test_pre_pto_and_portfolio(self):
        pre = NS(code="TST009", lat=19.6, lon=-99.2, kwp=500, operating=False)
        assert M.live(pre, dt.datetime(2026, 10, 2, 12, 0)).status == "pre_pto"
        k = M.portfolio_kpis([SITE, pre], dt.datetime(2026, 10, 2, 12, 0), days=10)
        assert k["sites"] == 2 and k["operating"] == 1 and 0.6 < k["pr_30d"] < 0.9
        assert k["kw_now"] <= SITE.kwp and k["co2_t_30d"] == pytest.approx(k["mwh_30d"] * M.CO2_KG_PER_KWH, abs=0.2)


@pytest.fixture
def c(tmp_path):
    con = S.connect(str(tmp_path / "pl.db"))
    yield con
    con.close()


class TestStore:
    def test_passwords(self):
        h = S.hash_pw("correct horse 12")
        assert h.startswith("scrypt$") and S.check_pw(h, "correct horse 12") and not S.check_pw(h, "wrong")
        assert not S.check_pw("garbage", "x")
        assert S.password_problem("short", "ana") and S.password_problem("ana-is-great-1234", "ana")
        assert S.password_problem("aaaaaaaaaaaaaa") and S.password_problem("Long-enough-9x!") is None
        p = S.make_password()
        assert len(p) == 16 and not set(p) & set("0O1lI")

    def test_user_lifecycle_is_audited(self, c):
        pw = S.create_user(c, "Ana@Prologis", "Ana P", "ana@prologis.com", "Prologis", "manager", "boss")
        u = S.user(c, "ana@prologis")
        assert u and S.check_pw(u["pw"], pw) and u["must_change"] == 1 and u["role"] == "manager"
        with pytest.raises(ValueError):
            S.create_user(c, "ana@prologis", "x", "", "Prologis", "viewer", "boss")
        with pytest.raises(ValueError):
            S.create_user(c, "bad user!", "x", "", "Prologis", "viewer", "boss")
        S.set_role(c, "ana@prologis", "viewer", "boss")
        S.set_disabled(c, "ana@prologis", True, "boss")
        acts = [r["action"] for r in c.execute("SELECT action FROM audit ORDER BY id")]
        assert acts == ["user_create", "user_role", "user_disable"]

    def test_lockout(self, c):
        now = 1000.0
        for _ in range(S.FAIL_MAX):
            S.note_fail(c, "u:ana", now)
        assert S.locked_for(c, "u:ana", now + 1) > 0
        assert S.locked_for(c, "u:ana", now + S.FAIL_WINDOW_S + 1) == 0

    def test_sessions(self, c):
        S.create_user(c, "bob", "Bob", "", "ARGIA", "operator", "x")
        sid = S.new_session(c, "bob", "1.2.3.4", 100.0)
        assert S.session(c, sid, 200.0)["mfa_ok"] == 0
        S.mark_mfa(c, sid)
        assert S.session(c, sid, 300.0)["mfa_ok"] == 1
        assert c.execute("SELECT count(*) FROM sessions WHERE sid_hash=?", (sid,)).fetchone()[0] == 0   # only the hash is stored
        assert S.session(c, sid, 300.0 + S.IDLE_S + 1) is None                                       # idle expiry
        sid2 = S.new_session(c, "bob", "", 100.0)
        S.set_disabled(c, "bob", True, "x")
        assert S.session(c, sid2, 101.0) is None

    def test_ticket_lifecycle_and_waiting_spans(self, c):
        t = S.create_ticket(c, "tst001", "Inverter down", "d", "OUT_100_500", "ana", needs_approval=True)
        assert t["number"] == "PL-0001" and t["approval"] == "pending" and t["site_code"] == "TST001"
        S.set_approval(c, t, True, "mgr")
        with pytest.raises(ValueError):
            S.set_approval(c, c.execute("SELECT * FROM tickets").fetchone(), True, "mgr")
        t = c.execute("SELECT * FROM tickets").fetchone()
        S.set_status(c, t, "RESPONDED", "op", ts="2026-11-02 10:00:00")
        t = c.execute("SELECT * FROM tickets").fetchone()
        assert t["responded_utc"] == "2026-11-02 10:00:00"
        S.set_status(c, t, "WAITING_OWNER", "op", ts="2026-11-02 11:00:00")
        t = c.execute("SELECT * FROM tickets").fetchone()
        S.set_status(c, t, "IN_PROGRESS", "op", ts="2026-11-02 15:00:00")
        t = c.execute("SELECT * FROM tickets").fetchone()
        with pytest.raises(ValueError):
            S.set_status(c, t, "NEW", "op")
        assert S.waiting_spans(S.ticket_events(c, t["id"])) == [("2026-11-02 11:00:00", "2026-11-02 15:00:00")]
        assert S.next_number(c) == "PL-0002"

    def test_documents_dedupe_and_audit(self, c, tmp_path):
        a = S.add_document(c, str(tmp_path / "f"), "tst001", "as_built", "a.pdf", b"%PDF-1", "application/pdf", "op")
        b = S.add_document(c, str(tmp_path / "f"), "tst002", "as_built", "copy.pdf", b"%PDF-1", "application/pdf", "op")
        rows = c.execute("SELECT * FROM documents ORDER BY id").fetchall()
        assert a != b and rows[0]["sha256"] == rows[1]["sha256"]
        files = list((tmp_path / "f").rglob("*"))
        assert sum(p.is_file() for p in files) == 1                          # stored once
        if os.name != "nt":                                                   # Windows has no POSIX modes
            assert oct(next(p for p in files if p.is_file()).stat().st_mode)[-3:] == "600"
        with pytest.raises(ValueError):
            S.add_document(c, str(tmp_path / "f"), "", "secret_stuff", "x.pdf", b"x", "", "op")

    def test_permissions_matrix(self):
        assert S.can("admin", "users") and not S.can("manager", "users")
        assert S.can("manager", "ticket_approve") and not S.can("operator", "ticket_approve")
        assert S.can("operator", "ticket_work") and not S.can("manager", "ticket_work")
        assert not S.can("viewer", "ticket_new") and S.can("viewer", "comment")
        assert not S.can("viewer", "export") and not S.can("operator", "export")
