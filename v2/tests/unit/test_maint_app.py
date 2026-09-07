"""v226 — the /maintenance/ Flask app against an in-memory fake of the
ticket tables: access, create, transitions, comments with attachments,
notifications, the alert prefill."""
from __future__ import annotations

import csv
import io
import pathlib
import re
import sys

import pytest

pytest.importorskip("flask")          # the laptop venv has no Flask; CI and the server do

V2 = pathlib.Path(__file__).resolve().parents[2]
BUNDLE = V2 / "server" / "bundle"
sys.path.insert(0, str(BUNDLE))
sys.path.insert(0, str(V2))

import maint_app as M   # noqa: E402
from argia.maintenance import tickets as TK   # noqa: E402

STAFF = [{"username": "tomasz", "name": "Tomasz Zemelka", "email": "tomasz@x"},
         {"username": "juan", "name": "Juan Perez", "email": "juan@x"},
         {"username": "arturo", "name": "Arturo Gonzalez", "email": ""}]
USERS = {"tomasz": {"level": "argia", "is_admin": 1, "disabled": 0}, "juan": {"level": "argia", "is_admin": 0, "disabled": 0},
         "cust": {"level": "custom", "is_admin": 0, "disabled": 0}, "gone": {"level": "argia", "is_admin": 0, "disabled": 1}}

TCOLS = list(TK.TICKET_COLS) + ["followers", "alert_keys"]


class FakeDB:
    """Just enough PostgreSQL for the app: tickets, events, followers,
    alerts, attachments; SQL matched by its shape."""

    def __init__(self):
        self.tickets, self.events, self.followers, self.alerts, self.files = {}, [], set(), {}, []
        self.sql = []
        self.ledger = [{"alert_key": "nl1:inv:sn1:inverter_temp_high", "plant_key": "NL1", "inverter_sn": "SN1",
                        "metric": "inverter_temp_high", "severity": "WARNING", "since": "2026-09-06",
                        "message": "NL1 SN1: day-peak temperature 72 degC [WARNING]", "opened_utc": "2026-09-06T12:30:00"}]

    @staticmethod
    def _csv(rows, cols):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
        return buf.getvalue()

    def _tk_rows(self, filt):
        out = []
        for t in self.tickets.values():
            if filt(t):
                r = dict(t)
                r["followers"] = ",".join(sorted(u for tid, u in self.followers if tid == t["id"]))
                r["alert_keys"] = ",".join(sorted(k for (tid, k) in self.alerts if tid == t["id"]))
                out.append(r)
        return out

    def rows_csv(self, sql):
        self.sql.append(sql)
        if sql.startswith("SELECT plant_key, customer"):
            return self._csv([{"plant_key": "NL1", "customer": "PLASTIC OMNIUM PPA land (Monterrey, NL)", "portfolio": "PPA"}],
                             ["plant_key", "customer", "portfolio"])
        if sql.startswith("SELECT plant_key, inverter_sn, coalesce(inverter_label"):
            return self._csv([{"plant_key": "NL1", "inverter_sn": "SN1", "label": "Inverter 1"}], ["plant_key", "inverter_sn", "label"])
        if sql.startswith("SELECT plant_key, inverter_sn, metric, severity, message FROM alert_ledger"):
            return self._csv(self.ledger, ["plant_key", "inverter_sn", "metric", "severity", "message"])
        if sql.startswith("SELECT alert_key, inverter_sn, metric, severity"):
            return self._csv(self.ledger, ["alert_key", "inverter_sn", "metric", "severity", "since", "message"])
        if sql.startswith(TK.SELECT_TICKETS):
            m = re.search(r"WHERE number = '([^']+)'", sql)
            if m:
                return self._csv(self._tk_rows(lambda t: t["number"] == m.group(1)), TCOLS)
            if "'RESOLVED','CLOSED'" in sql:
                return self._csv(self._tk_rows(lambda t: t["status"] in ("RESOLVED", "CLOSED")), TCOLS)
            if "created_at > now()" in sql:
                return self._csv(self._tk_rows(lambda t: True), TCOLS)
            return self._csv(self._tk_rows(lambda t: TK.is_open(t["status"])), TCOLS)
        if "FROM ticket_event" in sql and "kind = 'status'" in sql:
            return self._csv([e for e in self.events if e["kind"] == "status"], ["id", "ticket_id", "ts", "actor", "kind", "body", "meta"])
        if "FROM ticket_event" in sql:
            tid = int(re.search(r"ticket_id = (\d+)", sql).group(1))
            return self._csv([e for e in self.events if e["ticket_id"] == tid], ["id", "ticket_id", "ts", "actor", "kind", "body", "meta"])
        if "FROM ticket_attachment" in sql:
            m = re.search(r"id = (\d+) AND ticket_id = (\d+)", sql)
            if m:
                return self._csv([f for f in self.files if f["id"] == int(m.group(1)) and f["ticket_id"] == int(m.group(2))],
                                 ["filename", "stored_as", "mime"])
            tid = int(re.search(r"ticket_id = (\d+)", sql).group(1))
            return self._csv([f for f in self.files if f["ticket_id"] == tid], ["id", "event_id", "filename", "stored_as", "bytes", "mime"])
        raise AssertionError("unexpected SELECT: " + sql[:80])

    def execute(self, sql):
        self.sql.append(sql)
        if sql.startswith("CREATE TABLE"):
            return ""
        if sql.startswith("SELECT coalesce(max("):
            pk = re.search(r"LIKE 'TK-([A-Z0-9]+)-%'", sql).group(1)
            n = sum(1 for t in self.tickets.values() if t["plant_key"] == pk)
            return f"{n + 1}\n"
        if sql.startswith("INSERT INTO ticket ("):
            vals = re.findall(r"'((?:[^']|'')*)'", sql)
            tid = len(self.tickets) + 1
            number, plant, sn, title, desc, cat, prio, status, by, assigned = [v.replace("''", "'") for v in vals[:10]]
            self.tickets[tid] = dict(id=tid, number=number, plant_key=plant, inverter_sn=sn, title=title, description=desc,
                                     category=cat, priority=prio, status=status, created_by=by, assigned_to=assigned,
                                     created_at="2026-09-08 12:00:00+00", updated_at="2026-09-08 12:00:00+00")
            return f"{tid}\n"
        if sql.startswith("INSERT INTO ticket_event"):
            m = re.search(r"VALUES \((\d+), '((?:[^']|'')*)', '(\w+)', '((?:[^']|'')*)', '((?:[^']|'')*)'::jsonb", sql, re.S)
            eid = len(self.events) + 1
            self.events.append({"id": eid, "ticket_id": int(m.group(1)), "ts": f"2026-09-08 12:{eid:02d}:00+00",
                                "actor": m.group(2), "kind": m.group(3), "body": m.group(4).replace("''", "'"),
                                "meta": m.group(5).replace("''", "'")})
            return f"{eid}\n"
        if sql.startswith("INSERT INTO ticket_follower"):
            m = re.search(r"VALUES \((\d+), '([^']+)'\)", sql)
            self.followers.add((int(m.group(1)), m.group(2)))
            return ""
        if sql.startswith("DELETE FROM ticket_follower"):
            m = re.search(r"ticket_id = (\d+) AND username = '([^']+)'", sql)
            self.followers.discard((int(m.group(1)), m.group(2)))
            return ""
        if sql.startswith("INSERT INTO ticket_alert"):
            m = re.search(r"VALUES \((\d+), '([^']+)'\)", sql)
            self.alerts[(int(m.group(1)), m.group(2))] = self.alerts.get((int(m.group(1)), m.group(2)), 0) + 1
            return ""
        if sql.startswith("INSERT INTO ticket_attachment"):
            m = re.search(r"VALUES \((\d+), (\d+|NULL), '([^']+)', '([^']+)', (\d+), '([^']*)', '([^']*)'\)", sql)
            fid = len(self.files) + 1
            self.files.append({"id": fid, "ticket_id": int(m.group(1)), "event_id": 0 if m.group(2) == "NULL" else int(m.group(2)),
                               "filename": m.group(3), "stored_as": m.group(4), "bytes": int(m.group(5)), "mime": m.group(6)})
            return f"{fid}\n"
        if sql.startswith("UPDATE ticket SET"):
            tid = int(re.search(r"WHERE id = (\d+)", sql).group(1))
            t = self.tickets[tid]
            for col in ("status", "assigned_to", "priority", "root_cause", "resolution"):
                m = re.search(rf"\b{col} = '((?:[^']|'')*)'", sql)
                if m:
                    t[col] = m.group(1).replace("''", "'")
            m = re.search(r"lost_kwh = ([\d.]+|NULL)", sql)
            if m:
                t["lost_kwh"] = "" if m.group(1) == "NULL" else m.group(1)
            for st, col in TK.STATUS_STAMP.items():
                if f"{col} = coalesce({col}, now())" in sql and not t.get(col):
                    t[col] = "2026-09-08 13:00:00+00"
            return ""
        raise AssertionError("unexpected DML: " + sql[:80])


@pytest.fixture
def client(monkeypatch, tmp_path):
    db = FakeDB()
    M.app.config.update(ROWS_CSV=db.rows_csv, EXEC=db.execute, USER_ROW=lambda u: USERS.get(u), STAFF=lambda: STAFF,
                        ENSURED=False, TESTING=True)
    monkeypatch.setattr(M, "FILES_DIR", str(tmp_path))
    from argia.alerts import naming
    monkeypatch.setattr(naming, "load_names", lambda: naming.Names({"NL1": "Plastic Omnium"}, {("NL1", "SN1"): "Inverter 1"}))
    sent = []
    from argia.alerts import emailer
    monkeypatch.setattr(emailer, "load_smtp", lambda path=None: {"SMTP_HOST": "h", "SMTP_PORT": "25", "SMTP_USER": "svc@x"})
    monkeypatch.setattr(emailer, "send", lambda msg, cfg, timeout=30: sent.append(msg) or True)
    c = M.app.test_client()
    c.db, c.sent = db, sent
    return c


def H(u="tomasz"):
    return {"X-Remote-User": u}


def create(c, **extra):
    data = {"plant": "NL1", "inverter": "NL1|SN1", "title": "Inverter 1 cooling", "category": "inverter/derating",
            "priority": "P2", "assigned_to": "juan", "follower": ["arturo"], "description": "Hot since Aug 26",
            "alert_key": "nl1:inv:sn1:inverter_temp_high"}
    data.update(extra)
    return c.post("/new/", data=data, headers=H())


class TestAccess:
    def test_customers_and_disabled_get_no_access(self, client):
        assert client.get("/", headers=H("cust")).status_code == 403
        assert client.get("/", headers=H("gone")).status_code == 403
        assert client.get("/", headers=H("nobody")).status_code == 403
        r = client.get("/", headers=H())
        assert r.status_code == 200 and b"Tickets" in r.data and b"No tickets" in r.data
        assert client.get("/healthz").data == b"ok\n"


class TestCreate:
    def test_open_ticket_numbers_links_and_notifies(self, client):
        r = create(client)
        assert r.status_code == 302 and r.headers["Location"].endswith("/maintenance/t/TK-NL1-0001/")
        t = client.db.tickets[1]
        assert t["number"] == "TK-NL1-0001" and t["inverter_sn"] == "SN1" and t["assigned_to"] == "juan" and t["status"] == "NEW"
        assert (1, "arturo") in client.db.followers and (1, "nl1:inv:sn1:inverter_temp_high") in client.db.alerts
        assert [e["kind"] for e in client.db.events] == ["created", "assign", "alert"]
        # mail to juan (assignee) — arturo has no e-mail, tomasz did it
        assert len(client.sent) == 1 and client.sent[0]["To"] == "juan@x"
        assert client.sent[0]["Subject"] == "[TK-NL1-0001] Plastic Omnium: Inverter 1 cooling — New"   # v227: threads; a reply files as a comment
        assert client.sent[0]["Reply-To"] == "svc@x"
        # second ticket on the same plant counts up
        create(client, title="Second")
        assert client.db.tickets[2]["number"] == "TK-NL1-0002"

    def test_bad_input(self, client):
        assert client.post("/new/", data={"plant": "", "title": "x"}, headers=H()).status_code == 400
        assert client.post("/new/", data={"plant": "NL1", "title": "x"}, headers=H("cust")).status_code == 403

    def test_prefill_from_alert(self, client):
        r = client.get("/new/?alert=nl1:inv:sn1:inverter_temp_high", headers=H())
        html = r.data.decode()
        assert 'value="Plastic Omnium · Inverter 1: inverter running hot"' in html
        assert '<option value="NL1|SN1" selected data-plant="NL1">' in html and '<option value="P3" selected title="P3 Medium' in html
        assert "getElementById('plant')" in html                     # the inverter list follows the plant
        assert '<option value="inverter/derating" selected>' in html
        assert "day-peak temperature 72 degC" in html and "NL1 SN1:" not in html


class TestV227:
    def test_external_email_followers_are_notified_and_can_be_added_later(self, client):
        create(client, emails="tech@contractor.mx, bad address, Customer@Client.com")
        assert (1, "tech@contractor.mx") in client.db.followers and (1, "customer@client.com") in client.db.followers
        assert not any("bad" in u for _t, u in client.db.followers)
        assert client.sent[0]["To"] == "customer@client.com, juan@x, tech@contractor.mx"
        assert client.post("/t/TK-NL1-0001/follow", data={"email": "nonsense"}, headers=H()).status_code == 400
        r = client.post("/t/TK-NL1-0001/follow", data={"email": "Second@Client.com"}, headers=H())
        assert r.status_code == 302 and (1, "second@client.com") in client.db.followers
        assert client.db.events[-1]["kind"] == "follow" and "second@client.com" in client.db.events[-1]["body"]
        page = client.get("/t/TK-NL1-0001/", headers=H()).data.decode()
        assert "second@client.com" in page and 'placeholder="add follower by e-mail"' in page

    def test_creation_links_every_open_alert_on_the_asset(self, client):
        client.db.ledger.append({"alert_key": "nl1:inv:sn1:inverter_silent", "plant_key": "NL1", "inverter_sn": "SN1",
                                 "metric": "inverter_silent", "severity": "WARNING", "since": "2026-09-07",
                                 "message": "NL1 SN1: no data 13:50-14:35 [WARNING]", "opened_utc": "2026-09-07T12:30:00"})
        create(client, alert_key="")
        assert {k for _t, k in client.db.alerts} == {"nl1:inv:sn1:inverter_temp_high", "nl1:inv:sn1:inverter_silent"}
        assert [e["kind"] for e in client.db.events].count("alert") == 2

    def test_data_resolves_a_ticket_with_alerts_people_cannot(self, client):
        create(client)
        client.post("/t/TK-NL1-0001/status", data={"to": "IN_PROGRESS"}, headers=H())
        page = client.get("/t/TK-NL1-0001/", headers=H()).data.decode()
        assert "→ Resolved: by the data" in page and "Verification" in page and 'value="RESOLVED"' not in page
        assert client.post("/t/TK-NL1-0001/status", data={"to": "RESOLVED"}, headers=H()).status_code == 400
        # a ticket without alerts is resolved by a person
        create(client, alert_key="", inverter="", title="Roof inspection")
        client.db.alerts = {k: v for k, v in client.db.alerts.items() if k[0] != 2}
        client.post("/t/TK-NL1-0002/status", data={"to": "IN_PROGRESS"}, headers=H())
        assert client.post("/t/TK-NL1-0002/status", data={"to": "RESOLVED"}, headers=H()).status_code == 302

    def test_tooltips_legend_and_stats_page(self, client):
        create(client)
        page = client.get("/", headers=H()).data.decode()
        assert "How it works" in page and "<b>P2</b> energy is being lost" in page
        # v228: report-style tooltips (.ti badge + .tipbox) on every column header, no title= in the table
        table = page.split("<table", 1)[1].split("</table>", 1)[0]
        assert 'class="tipbox"' in table and 'title="' not in table
        for text in ("Time since the ticket was opened.", "Who is working on it.", "Resolve target of the priority",
                     "the plant is in the number", "sets the SLA clock"):
            assert text in table, text
        assert table.count('class="ti"') >= 8   # one badge per column header at least
        t = client.get("/t/TK-NL1-0001/", headers=H()).data.decode()
        assert 'class="tipbox"' in t and "Who works on it." in t and "Followers are notified" in t
        assert "In progress — someone is working on it." in t and "P2 High" in t
        assert re.search(r'<h2 class="ct">Add an update<span class="tw">', t) and 'class="ct" title=' not in t
        st = client.get("/stats/", headers=H()).data.decode()
        assert "Open tickets by status" in st and "<svg" in st and "Opened / resolved per week" in st and "Plastic Omnium" in st
        assert client.get("/stats/", headers=H("cust")).status_code == 403


class TestWork:
    def test_status_flow_and_stamps(self, client):
        create(client)
        assert client.post("/t/TK-NL1-0001/status", data={"to": "RESOLVED"}, headers=H("juan")).status_code == 400   # NEW -> RESOLVED not allowed
        r = client.post("/t/TK-NL1-0001/status", data={"to": "IN_PROGRESS"}, headers=H("juan"))
        assert r.status_code == 302 and client.db.tickets[1]["status"] == "IN_PROGRESS" and client.db.tickets[1]["started_at"]
        ev = client.db.events[-1]
        assert ev["kind"] == "status" and ev["actor"] == "juan" and '"from": "NEW"' in ev["meta"] and '"to": "IN_PROGRESS"' in ev["meta"]
        assert client.sent[-1]["To"] == "tomasz@x" and "New → In progress" in client.sent[-1].get_content() if not client.sent[-1].is_multipart() else True
        page = client.get("/t/TK-NL1-0001/", headers=H()).data.decode()
        assert "→ Waiting" in page and "→ Verification" in page and "→ Resolved" in page and "→ Closed" not in page
        assert '<p class="pill ok"' not in page                    # no stray flash message (the alert row once leaked into it)
        assert '<p class="pill ok" style="display:inline-flex">saved</p>' in client.get("/t/TK-NL1-0001/?m=saved", headers=H()).data.decode()
        assert "New → In progress" in page and "Juan Perez" in page

    def test_comment_with_attachment_and_download(self, client, tmp_path):
        create(client)
        data = {"body": "Filters replaced", "files": [(io.BytesIO(b"\x89PNG fake"), "IMG_4201.png"), (io.BytesIO(b"evil"), "run.exe")]}
        r = client.post("/t/TK-NL1-0001/comment", data=data, headers=H("juan"), content_type="multipart/form-data")
        assert r.status_code == 302
        assert [f["filename"] for f in client.db.files] == ["IMG_4201.png"]           # .exe refused
        stored = tmp_path / "TK-NL1-0001" / client.db.files[0]["stored_as"]
        assert stored.read_bytes() == b"\x89PNG fake"
        page = client.get("/t/TK-NL1-0001/", headers=H()).data.decode()
        assert "Filters replaced" in page and "IMG_4201.png" in page
        f = client.get("/t/TK-NL1-0001/file/1", headers=H())
        assert f.status_code == 200 and f.data == b"\x89PNG fake"
        assert client.get("/t/TK-NL1-0001/file/9", headers=H()).status_code == 404
        assert client.get("/t/TK-NL1-0001/file/1", headers=H("cust")).status_code == 403
        # the mail carries the update and the file name
        m = client.sent[-1]
        assert m["To"] == "tomasz@x" and "Filters replaced" in m.get_body(("plain",)).get_content() and "IMG_4201.png" in m.get_body(("plain",)).get_content()

    def test_assign_follow_priority_link_resolution(self, client):
        create(client, assigned_to="")
        assert client.post("/t/TK-NL1-0001/assign", data={"assigned_to": "juan"}, headers=H()).status_code == 302
        assert client.db.tickets[1]["assigned_to"] == "juan" and client.sent[-1]["To"] == "juan@x"
        client.post("/t/TK-NL1-0001/follow", data={"on": "1"}, headers=H("juan"))
        assert (1, "juan") in client.db.followers
        client.post("/t/TK-NL1-0001/follow", data={"on": "0"}, headers=H("juan"))
        assert (1, "juan") not in client.db.followers
        assert client.post("/t/TK-NL1-0001/priority", data={"priority": "P9"}, headers=H()).status_code == 400
        client.post("/t/TK-NL1-0001/priority", data={"priority": "P1"}, headers=H())
        assert client.db.tickets[1]["priority"] == "P1"
        client.post("/t/TK-NL1-0001/link", data={"alert_key": "k2"}, headers=H())
        assert (1, "k2") in client.db.alerts
        assert client.post("/t/TK-NL1-0001/resolution", data={"root_cause": "bogus"}, headers=H()).status_code == 400
        client.post("/t/TK-NL1-0001/resolution", data={"root_cause": "environment", "resolution": "Cleaned the heat sink", "lost_kwh": "1,234.5"}, headers=H())
        t = client.db.tickets[1]
        assert t["root_cause"] == "environment" and t["lost_kwh"] == "1234.5" and client.db.events[-1]["kind"] == "resolution"

    def test_dashboard_and_resolved_lists(self, client):
        create(client)
        create(client, title="Old one", priority="P4")
        client.post("/t/TK-NL1-0002/status", data={"to": "CLOSED"}, headers=H())
        page = client.get("/", headers=H()).data.decode()
        assert "TK-NL1-0001" in page and "TK-NL1-0002" not in page and "My tickets (1)" in page and "All open (1)" in page
        assert "Open by plant: Plastic Omnium 1" in page
        res = client.get("/resolved/", headers=H()).data.decode()
        assert "TK-NL1-0002" in res and "TK-NL1-0001" not in res
        assert client.get("/t/TK-NL1-0099/", headers=H()).status_code == 404


class TestWiring:
    def test_service_nginx_chrome_and_landing(self):
        unit = (BUNDLE / "argia-maint.service").read_text(encoding="utf-8")
        assert "maint_app.py" in unit and "ARGIA_MAINT_PORT=8514" in unit and "ARGIA_TICKET_FILES=/opt/argia/tickets" in unit
        ng = (BUNDLE / "nginx-argia_session.conf").read_text(encoding="utf-8")
        assert "location /maintenance/ {" in ng and "proxy_pass http://127.0.0.1:8514/;" in ng and "client_max_body_size 16m;" in ng
        assert "proxy_set_header X-Remote-User $argia_user;" in ng.split("location /maintenance/")[1].split("}")[0]
        import portal_chrome as C
        assert C.SECTIONS["maintenance"][0] == "Maintenance" and [s for s, _, _ in C.SECTIONS["maintenance"][2]] == ["", "new", "resolved"]
        assert "maint" in C._ICONS
        pg = (BUNDLE / "portal_gen.py").read_text(encoding="utf-8")
        assert "('maint', 'Maintenance', 'Mantenimiento', '/maintenance/'" in pg
        ops = (V2 / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
        assert "argia-maint" in ops
