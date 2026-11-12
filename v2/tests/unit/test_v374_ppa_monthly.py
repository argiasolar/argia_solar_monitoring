"""v374 - the PPA monthly performance report (Tomasz, 12 Nov 2026: "a section called PPA monthly performance
report ... from production, weather, money, availability, etc perspective ... use prologis ideas ... make it for
each month going into the past, and make it available to download"). Decided: a new portal section (staff and
invited stakeholders), contract curve AND design for the measured sun side by side, PDF + Excel, draft until an
operator publishes it. All plants, numbers and people here are invented.
"""
from __future__ import annotations

import datetime as dt
import importlib
import io
import json
import pathlib
import re
import sys

import pytest

V2 = pathlib.Path(__file__).resolve().parents[2]
for _p in (V2, V2 / "server" / "bundle", V2 / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from argia.kpi import design_model as DM                   # noqa: E402
from argia.report import ppa_monthly as PM                 # noqa: E402
from argia.report import ppa_monthly_view as V             # noqa: E402

EM = chr(0x2014)       # the character no output may carry (v262)
MONTHS = [{"ghi": 150.0, "poa": 160.0, "grid_kwh": 15000.0, "soiling_pct": 2.0} for _ in range(12)]


def design(cod="2026-01-01"):
    return DM.parse({"version": 1, "plants": {"DEMO1": {
        "source": "HelioScope 'demo'", "report_date": "2026-11-01", "kwp_dc": 100.0, "kwp_ac": 80.0, "sensor": "ghi",
        "cod": cod, "degradation": 0.0, "annual_kwh": 180000.0, "months": MONTHS}}})["DEMO1"]


def days(pk, ym, n, energy, irr, avail="1.0", billable=None):
    return [{"plant_key": pk, "prod_date": f"{ym}-{d:02d}", "energy_kwh": str(energy), "irradiance_kwh_m2": str(irr),
             "availability": avail, "billable_kwh": str(billable if billable is not None else energy),
             "cloud_cover_pct": "40"} for d in range(1, n + 1)]


def ctx(daily=None, losses=(), events=(), tickets=(), designs=None, invoices=()):
    plants = [{"plant_key": "DEMO1", "name": "Demo Roof", "kwp_dc": "100", "tariff_mxn_per_kwh": "2.0"},
              {"plant_key": "DEMO2", "name": "Other Land", "kwp_dc": "50", "tariff_mxn_per_kwh": "3.0"}]
    if daily is None:
        daily = days("DEMO1", "2026-07", 31, 500, 6.0) + days("DEMO2", "2026-07", 31, 200, 6.0)
    contract = [{"plant_key": "DEMO1", "year": "2026", "month": "7", "contract_kwh": "15500", "tariff_mxn": "2.5"},
                {"plant_key": "DEMO2", "year": "2026", "month": "7", "contract_kwh": "6200", "tariff_mxn": "3.0"}]
    return PM.Context(plants, daily, contract, invoices, losses, events, tickets, [],
                      {"DEMO1": design()} if designs is None else designs)


# ------------------------------------------------------------------ the figures of a plant-month
class TestPlantMonth:
    def test_promise_weather_engineering(self):
        p = PM.plant_month(ctx(), "DEMO1", "2026-07", "2026-08-15")
        assert p["actual_kwh"] == 15500.0 and p["contract_kwh"] == 15500.0 and p["promise"] == 1.0
        assert p["design_typ_kwh"] == pytest.approx(15000.0)
        # the design year has 150 kWh/m2 in July (4.84/day); 6.0 a day measured = 186 -> 24 % more sun
        assert p["sun_index"] == pytest.approx(186.0 / 150.0)
        assert p["design_sun_kwh"] == pytest.approx(15000.0 / 150.0 * 186.0)
        assert p["epi"] == pytest.approx(15500.0 / (15000.0 / 150.0 * 186.0))
        assert p["verdict"] == {"promise": "good", "engineering": "bad", "availability": "good"}

    def test_the_bridge_adds_up_exactly(self):
        lo = [{"plant_key": "DEMO1", "prod_date": f"2026-07-{d:02d}", "lost_kwh": "30", "unavailability_kwh": "20",
               "overheating_kwh": "0", "underperformance_kwh": "10", "excused_kwh": "0", "lost_mxn": "75"}
              for d in range(1, 32)]
        ev = [{"plant_key": "DEMO1", "start_ts": "2026-07-04 13:00:00+00", "end_ts": "2026-07-05 02:00:00+00",
               "category": "customer", "cost_mxn": "", "deemed_kwh": "", "note": "Customer roof works | detail"}]
        p = PM.plant_month(ctx(losses=lo, events=ev), "DEMO1", "2026-07", "2026-08-15")
        b = p["bridge"]
        assert p["contract_kwh"] + sum(b.values()) == pytest.approx(p["actual_kwh"])
        assert b["customer"] == -20.0 and b["availability"] == -600.0      # 4 Jul MX time is the customer's day
        assert p["events"][0]["days"] == ["2026-07-04"] and p["events"][0]["note"] == "Customer roof works"
        assert p["loss"]["lost_mxn"] == 75.0 * 31

    def test_partial_month_is_prorated_never_padded(self):
        c = ctx(daily=days("DEMO1", "2026-07", 10, 500, 6.0))
        p = PM.plant_month(c, "DEMO1", "2026-07", "2026-08-15")
        assert p["data_days"] == 10 and p["contract_kwh"] == pytest.approx(15500.0 * 10 / 31)
        assert any("10 of 31 days" in en for en, _ in p["notes"])

    def test_month_in_progress_stops_before_today(self):
        p = PM.plant_month(ctx(), "DEMO1", "2026-07", "2026-07-11")
        assert p["data_days"] == 10 and not any("of 31 days" in en for en, _ in p["notes"])

    def test_without_a_design_one_unsplit_step(self):
        p = PM.plant_month(ctx(), "DEMO2", "2026-07", "2026-08-15")
        assert not p["has_design"] and p["epi"] is None and set(p["bridge"]) == {"unsplit"}
        assert p["contract_kwh"] + p["bridge"]["unsplit"] == pytest.approx(p["actual_kwh"])
        assert any("No HelioScope design" in en for en, _ in p["notes"])

    def test_design_not_used_before_its_interconnection(self):
        p = PM.plant_month(ctx(designs={"DEMO1": design(cod="2026-09-01")}), "DEMO1", "2026-07", "2026-08-15")
        assert p["epi"] is None and "unsplit" in p["bridge"]
        assert any("interconnection" in en for en, _ in p["notes"])

    def test_revenue_counts_the_compensated_customer_energy(self):
        d = days("DEMO1", "2026-07", 31, 500, 6.0)
        d[3].update(energy_kwh="0", billable_kwh="480")
        p = PM.plant_month(ctx(daily=d + days("DEMO2", "2026-07", 31, 200, 6.0)), "DEMO1", "2026-07", "2026-08-15")
        assert p["compensated_kwh"] == 480.0 and p["revenue_earned_mxn"] == pytest.approx((15000 + 480) * 2.5)
        assert p["budget_mxn"] == pytest.approx(15500 * 2.5)

    def test_invoice_used_only_when_produced(self):
        inv = [{"plant_key": "DEMO1", "ref_month": "2026-07-01", "amount_mxn": "38750", "produced_kwh": "15500",
                "check_status": "OK"},
               {"plant_key": "DEMO2", "ref_month": "2026-07-01", "amount_mxn": "0", "produced_kwh": "",
                "check_status": "EXPECTED_ONLY"}]
        c = ctx(invoices=inv)
        assert PM.plant_month(c, "DEMO1", "2026-07", "2026-08-15")["revenue_invoiced_mxn"] == 38750.0
        assert PM.plant_month(c, "DEMO2", "2026-07", "2026-08-15")["revenue_invoiced_mxn"] is None

    def test_no_data_no_plant(self):
        assert PM.plant_month(ctx(), "DEMO1", "2026-06", "2026-08-15") is None

    def test_far_above_contract_is_flagged(self):
        c = ctx(daily=days("DEMO2", "2026-07", 31, 300, 6.0))
        assert any("above the contract curve" in en for en, _ in PM.plant_month(c, "DEMO2", "2026-07", "2026-08-15")["notes"])


class TestPieces:
    def test_episodes_below_target(self):
        d = days("DEMO1", "2026-07", 6, 500, 6.0)
        for i in (1, 2, 4):
            d[i]["availability"] = "0.9"
        lo = [{"plant_key": "DEMO1", "prod_date": "2026-07-02", "unavailability_kwh": "40"}]
        c = ctx(daily=d, losses=lo)
        eps = PM.episodes(d, 0.98, c, "DEMO1")
        assert [(e["start"], e["end"], e["days"]) for e in eps] == [("2026-07-02", "2026-07-03", 2), ("2026-07-05", "2026-07-05", 1)]
        assert eps[0]["unavailability_kwh"] == 40.0 and eps[1]["unavailability_kwh"] is None

    def test_mexico_city_day_of_a_utc_stamp(self):
        assert PM._local_date("2026-10-25 02:00:00+00") == dt.date(2026, 10, 24)
        assert PM._local_date("2026-10-11T07:01:00Z") == dt.date(2026, 10, 11)
        assert PM._local_date("2026-10-11 05:59:00+00:00") == dt.date(2026, 10, 10)
        assert PM._local_date("2026-10-11") == dt.date(2026, 10, 11) and PM._local_date("") is None

    def test_tickets_of_the_month(self):
        tk = [{"number": "TK-1", "plant_key": "DEMO1", "created_at": "2026-07-03 10:00:00+00",
               "started_at": "2026-07-03 14:00:00+00", "resolved_at": "2026-07-09 10:00:00+00", "title": "Inverter"},
              {"number": "TK-2", "plant_key": "DEMO1", "created_at": "2026-06-20 10:00:00+00", "started_at": "",
               "resolved_at": "", "title": "Comms"},
              {"number": "TK-3", "plant_key": "OTHER", "created_at": "2026-07-03 10:00:00+00", "title": "x"}]
        s = PM.ticket_stats(ctx(tickets=tk), "2026-07", ["DEMO1", "DEMO2"])
        assert (s["opened"], s["resolved"], s["open_at_end"], s["median_response_h"]) == (1, 1, 1, 4.0)
        assert [t["number"] for t in s["open_list"]] == ["TK-2"]

    def test_month_helpers(self):
        assert PM.ym_add("2026-11", 2) == "2027-01" and PM.ym_add("2026-01", -1) == "2025-12"
        assert PM.month_range("2025-11", "2026-02") == ["2025-11", "2025-12", "2026-01", "2026-02"]
        assert PM.deadline("2026-10") == "2026-11-10" and PM.days_in("2024-02") == 29


class TestReport:
    def test_portfolio_is_a_ratio_of_sums(self):
        r = PM.build(ctx(), "2026-07", "2026-08-15", "x")
        P = r["portfolio"]
        assert P["actual_kwh"] == 15500.0 + 6200.0 and P["promise"] == pytest.approx(21700.0 / 21700.0)
        assert P["epi_plants"] == 1 and P["epi"] == pytest.approx(r["plants"][0]["epi"])
        assert P["contract_kwh"] + sum(v for v in P["bridge_kwh"].values() if v is not None) == pytest.approx(
            P["actual_with_contract_kwh"])
        assert P["bridge_mxn"]["unsplit"] == pytest.approx(r["plants"][1]["bridge"]["unsplit"] * 3.0)
        assert r["complete"] and r["deadline"] == "2026-08-10" and r["first_month"] == "2026-07"
        json.dumps(r)                                                  # the frozen form is plain JSON

    def test_headline_says_promise_weather_engineering(self):
        r = PM.build(ctx(), "2026-07", "2026-08-15")
        en, es = r["headline"]["en"], r["headline"]["es"]
        assert en.startswith("July 2026: the PPA portfolio delivered 21.7 MWh, 0% above the contract curve.")
        assert "The sun was 24% stronger than in a typical July." in en
        assert "El sol fue 24% más fuerte que en un julio típico." in es and es.startswith("Julio 2026:")
        assert EM not in en + es

    def test_month_to_date_is_not_publishable(self):
        r = PM.build(ctx(), "2026-07", "2026-07-20")
        assert not r["complete"] and "so far" in r["headline"]["en"]
        with pytest.raises(ValueError, match="not over yet"):
            PM.freeze(r, "tomasz", "2026-07-21T10:00:00Z")

    def test_freeze_records_who_when_and_the_deadline(self):
        c = ctx(daily=days("DEMO1", "2026-12", 31, 500, 6.0))
        r = PM.build(c, "2026-12", "2027-01-15", "2027-01-15T12:00:00Z")
        f = PM.freeze(r, "tomasz", "2027-01-09T15:00:00Z")
        assert f["published"] == {"by": "tomasz", "utc": "2027-01-09T15:00:00Z", "digest": PM.digest(r), "on_time": True}
        assert PM.freeze(r, "tomasz", "2027-01-11T15:00:00Z")["published"]["on_time"] is False

    def test_months_before_the_report_existed_are_history(self):
        r = PM.build(ctx(), "2026-07", "2026-11-12")
        assert PM.freeze(r, "tomasz", "2026-11-12T15:00:00Z")["published"]["on_time"] is None
        assert "overdue" not in V.status_pill(r, V.print_lang("en"), "2026-11-12")

    def test_digest_ignores_when_it_was_computed(self):
        a = PM.build(ctx(), "2026-07", "2026-08-15", "t1")
        b = PM.build(ctx(), "2026-07", "2026-08-20", "t2")
        assert PM.digest(a) == PM.digest(b)

    def test_daily_rows_add_up_to_the_month(self):
        c = ctx()
        rows = PM.daily_rows(c, "2026-07", "2026-08-15")
        assert sum(r["energy_kwh"] for r in rows) == PM.build(c, "2026-07", "2026-08-15")["portfolio"]["actual_kwh"]
        assert rows[0]["design_sun_kwh"] == pytest.approx(15000.0 / 150.0 * 6.0)

    def test_co2_factors_match_the_report(self):
        src = (V2 / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
        table = re.search(r"CO2_BY_YEAR = (\{[^}]*\})", src).group(1)
        assert eval(table) == PM.CO2_BY_YEAR                                       # noqa: S307
        assert "CO2_PLANT_OVERRIDE = {'MEX1': 0.202}" in src and PM.CO2_PLANT_OVERRIDE == {"MEX1": 0.202}


# ------------------------------------------------------------------ the page, the PDF, the workbook
class TestView:
    @pytest.fixture()
    def rep(self):
        return PM.build(ctx(), "2026-07", "2026-08-15", "2026-08-15T12:00:00Z")

    def test_web_body_is_bilingual_and_complete(self, rep):
        h = V.body(rep, V.web_lang(), "2026-08-15", {"pdf_en": "report-en.pdf", "xlsx": "report.xlsx"})
        for s in ("From the promise to the result", "Plant by plant", "Weather: sun vs a typical month", "Money",
                  "Days below the availability target", "Maintenance tickets", "The last 13 months",
                  "Data quality and caveats", "How the numbers are made", 'data-es="De la promesa al resultado"',
                  'href="report-en.pdf"', "<svg", "Demo Roof"):
            assert s in h, s
        assert EM not in h

    def test_print_is_one_language(self, rep):
        en, es = V.print_document(rep, "en", "2026-08-15"), V.print_document(rep, "es", "2026-08-15")
        assert "data-en" not in en and "From the promise to the result" in en and "De la promesa" not in en
        assert "De la promesa al resultado" in es and "@page{size:A4 landscape" in es
        assert EM not in en + es

    def test_status(self):
        L = V.print_lang("en")
        rep = PM.build(ctx(daily=days("DEMO1", "2026-11", 30, 500, 6.0)), "2026-11", "2026-12-15")
        assert "publish by 2026-12-10" in V.status_pill(rep, L, "2026-12-05")
        assert "overdue" in V.status_pill(rep, L, "2026-12-11")
        pub = V.status_pill(PM.freeze(rep, "tomasz", "2026-12-12T10:00:00Z"), L, "2027-01-01")
        assert "Published 2026-12-12" in pub and "after the day-10 deadline" in pub

    def test_index(self):
        cards = [{"month": "2026-08", "href": "/ppa-report/2026-08/", "status": "mtd", "actual_kwh": 1000.0,
                  "promise": 0.9, "epi": 1.01, "sun_index": None, "lost_mxn": None},
                 {"month": "2026-07", "href": "/ppa-report/2026-07/", "status": "published", "actual_kwh": 2000.0,
                  "promise": 1.1, "epi": None, "sun_index": 0.8, "lost_mxn": 12.0}]
        h = V.index_body(cards, V.print_lang("en"), "2026-08-15")
        assert "Month to date" in h and "Published" in h and "/ppa-report/2026-07/report.xlsx" in h
        assert h.index("August 2026") < h.index("July 2026")

    def test_bridge_chart_has_every_step(self, rep):
        svg = V.bridge_svg(rep["portfolio"], V.print_lang("en"))
        for lab in ("Contract curve", "Design vs", "Weather", "Actual"):
            assert lab in svg

    def test_workbook(self, rep):
        openpyxl = pytest.importorskip("openpyxl")
        from argia.report import ppa_monthly_xlsx as X
        c = ctx()
        wb = openpyxl.load_workbook(io.BytesIO(X.workbook(rep, PM.daily_rows(c, "2026-07", "2026-08-15"))))
        assert wb.sheetnames == ["Summary", "Plants", "Bridge", "Daily", "Trend", "Events", "Method"]
        d = wb["Daily"]
        assert sum(d.cell(i, 4).value for i in range(2, d.max_row + 1)) == rep["portfolio"]["actual_kwh"]
        assert wb["Plants"].cell(2, 1).value == "Demo Roof"


# ------------------------------------------------------------------ the generator: drafts, publishing, freezing
class TestGenerator:
    @pytest.fixture()
    def G(self, monkeypatch):
        pytest.importorskip("openpyxl")
        import ppa_monthly_gen as G

        def fake_pdf(doc, path, chromium):
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
            pathlib.Path(path).write_bytes(b"%PDF " + doc[:2000].encode())
            return True
        monkeypatch.setattr(G, "render_pdf", fake_pdf)
        return G

    def test_every_month_gets_a_page_excel_and_pdfs(self, G, tmp_path):
        c = ctx(daily=days("DEMO1", "2026-07", 31, 500, 6.0) + days("DEMO1", "2026-08", 20, 500, 6.0))
        st = G.generate(c, str(tmp_path / "web"), str(tmp_path / "store"), "2026-08-15", chromium="x")
        m = tmp_path / "web" / "ppa-report"
        assert st["months"] == 2 and (m / "index.html").exists()
        for f in ("index.html", "report.xlsx", "report-en.pdf", "report-es.pdf"):
            assert (m / "2026-07" / f).exists(), f
        assert "Month to date" in (m / "index.html").read_text(encoding="utf-8")
        again = G.generate(c, str(tmp_path / "web"), str(tmp_path / "store"), "2026-08-16", chromium="x")
        assert again["rendered"] == 1                     # only the month in progress changed (one more day)

    def test_publish_freezes_and_never_changes(self, G, tmp_path):
        web, store = str(tmp_path / "web"), str(tmp_path / "store")
        info = G.publish(ctx(), web, store, "2026-07", "tomasz", "2026-08-05", "x", now="2026-08-05T15:00:00Z")
        assert info["by"] == "tomasz" and info["on_time"] is None         # before Nov 2026: history, no deadline
        frozen = (tmp_path / "store" / "published" / "2026-07" / "report.json").read_text(encoding="utf-8")
        changed = ctx(daily=days("DEMO1", "2026-07", 31, 100, 6.0) + days("DEMO2", "2026-07", 31, 100, 6.0))
        G.generate(changed, web, store, "2026-08-20", chromium="x")
        assert (tmp_path / "store" / "published" / "2026-07" / "report.json").read_text(encoding="utf-8") == frozen
        page = (tmp_path / "web" / "ppa-report" / "2026-07" / "index.html").read_text(encoding="utf-8")
        assert "Published" in page and "21.7" in page and "/setup/ppa-report/" not in page
        with pytest.raises(SystemExit, match="already published"):
            G.publish(ctx(), web, store, "2026-07", "tomasz", "2026-08-05", "x")

    def test_publish_refuses_without_the_pdf(self, G, tmp_path, monkeypatch):
        monkeypatch.setattr(G, "render_pdf", lambda *a: False)
        with pytest.raises(SystemExit, match="did not render"):
            G.publish(ctx(), str(tmp_path / "w"), str(tmp_path / "s"), "2026-07", "tomasz", "2026-08-05", "x")
        assert not (tmp_path / "s" / "published" / "2026-07").exists()

    def test_draft_page_offers_publishing_to_admins_only(self, G, tmp_path):
        G.generate(ctx(), str(tmp_path / "web"), str(tmp_path / "store"), "2026-08-15", chromium="x")
        page = (tmp_path / "web" / "ppa-report" / "2026-07" / "index.html").read_text(encoding="utf-8")
        assert 'class="adminonly" href="/setup/ppa-report/?month=2026-07"' in page
        assert "/setup/ppa-report/" not in (tmp_path / "web" / "ppa-report" / "2026-08" / "index.html").read_text(encoding="utf-8")

    def test_read_only_on_postgres(self):
        src = (V2 / "scripts" / "ppa_monthly_gen.py").read_text(encoding="utf-8")
        assert all(s.lstrip().upper().startswith("SELECT") for s in __import__("ppa_monthly_gen").SQL.values())
        assert "psql_exec" not in src


# ------------------------------------------------------------------ access and the publish button
class TestAccess:
    def test_its_own_grant(self):
        import auth_core as ac
        assert "ppa_report" in ac.AREAS
        assert ac.area_for_path("/ppa-report/2026-10/report-en.pdf") == "ppa_report"
        assert ac.area_for_path("/setup/ppa-report/") == ac.ADMIN
        assert ac.may({"level": "custom", "reports": "ppa_report"}, "ppa_report")
        assert not ac.may({"level": "custom", "reports": "financial"}, "ppa_report")
        assert ac.may({"level": "argia", "reports": ""}, "ppa_report")

    def test_setup_offers_the_grant(self):
        pytest.importorskip("flask")
        import auth_core as ac
        import setup_app as sa
        assert sa.AREAS == ac.AREAS and sa.AREA_LABEL["ppa_report"] == "PPA monthly report"

    def test_section_and_landing(self):
        import portal_chrome as C
        assert C.SECTIONS["ppa-report"][0] == "PPA monthly report" and "pperf" in C._ICONS
        assert "'/ppa-report/'" in (V2 / "server" / "bundle" / "portal_gen.py").read_text(encoding="utf-8")

    def test_timer_and_wrapper(self):
        svc = (V2 / "server" / "bundle" / "argia-ppa-monthly.service").read_text(encoding="utf-8")
        assert "run_job.sh ppamonthly ppa_monthly_gen.py" in svc
        sh = (V2 / "pi" / "ppa_publish.sh").read_text(encoding="utf-8")
        assert "/tmp/argia_ppamonthly.lock" in sh and "--publish" in sh and "flock -w" in sh


class TestPublishButton:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        pytest.importorskip("flask")
        monkeypatch.setenv("ARGIA_AUTH_DIR", str(tmp_path))
        monkeypatch.setenv("ARGIA_PPA_REPORT_DIR", str(tmp_path / "ppa"))
        import setup_app as sa
        importlib.reload(sa)
        sa.AUTH_DIR, sa.DB_PATH = str(tmp_path), str(tmp_path / "users.db")
        c = sa.db()
        c.execute("INSERT INTO users(username,hash,level,reports,is_admin) VALUES('tomasz','x','argia','',1)")
        c.execute("INSERT INTO users(username,hash,level,reports,is_admin) VALUES('vit','x','custom','ppa_report',0)")
        c.commit()
        c.close()
        calls = []

        class R:
            returncode, stdout, stderr = 0, "PUBLISHED 2026-07 by tomasz", ""
        monkeypatch.setattr(sa, "_sh", lambda args, timeout: calls.append(args) or R())
        sa.app.config["TESTING"] = True
        return sa, sa.app.test_client(), calls

    def test_confirm_page_then_publish(self, client):
        sa, cl, calls = client
        r = cl.get("/ppa-report/?month=2026-07", headers={"X-Remote-User": "tomasz"})
        assert r.status_code == 200 and b"Publish 2026-07" in r.data and b'name="csrf"' in r.data
        r = cl.post("/ppa-report/publish", headers={"X-Remote-User": "tomasz"},
                    data={"csrf": sa.CSRF, "month": "2026-07", "confirm": "yes"})
        assert r.status_code == 303 and r.headers["Location"].endswith("/ppa-report/2026-07/")
        assert calls == [["bash", sa.PPA_PUBLISH_SH, "2026-07", "tomasz"]]

    def test_refusals(self, client):
        sa, cl, calls = client
        assert b"Only ARGIA administrators" in cl.get("/ppa-report/?month=2026-07", headers={"X-Remote-User": "vit"}).data
        assert b"complete month" in cl.get("/ppa-report/?month=2099-01", headers={"X-Remote-User": "tomasz"}).data
        cl.post("/ppa-report/publish", headers={"X-Remote-User": "tomasz"}, data={"csrf": "wrong", "month": "2026-07",
                                                                                 "confirm": "yes"})
        cl.post("/ppa-report/publish", headers={"X-Remote-User": "vit"}, data={"csrf": sa.CSRF, "month": "2026-07",
                                                                              "confirm": "yes"})
        r = cl.post("/ppa-report/publish", headers={"X-Remote-User": "tomasz"}, data={"csrf": sa.CSRF, "month": "2026-07"})
        assert b"Not confirmed" in r.data and calls == []

    def test_a_failed_run_says_so(self, client, monkeypatch):
        sa, cl, _ = client

        class R:
            returncode, stdout, stderr = 1, "2026-07: the PDF did not render - nothing was published", ""
        monkeypatch.setattr(sa, "_sh", lambda args, timeout: R())
        r = cl.post("/ppa-report/publish", headers={"X-Remote-User": "tomasz"},
                    data={"csrf": sa.CSRF, "month": "2026-07", "confirm": "yes"})
        assert b"was NOT published" in r.data and b"did not render" in r.data
