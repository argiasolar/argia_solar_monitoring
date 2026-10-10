"""v305 - mail where money is lost, flag the rest; the server watched from
the Pi; reconciliation of lost-connection days; no Google Cloud.

Tomasz, 2026-10-04: "I am not using google cloud for anything ... make sure
we have our server monitored, either from Pi or github ... make sure we are
having good reconciliation specially when inverters went offline (lost
connection only), and the warning should be send where we are loosing
money, if something is potentially going to loose money only flag it on
dashboard, mails should be last resort" + on Drive/finance jobs: "we do not
need to send emails in regards to it, it is not solar monitoring job".

The production cases these tests come from (checked read-only on pio06):
* TAM1 JNMAE5X00K, 30 Sep: "26% of peers" CRITICAL - it went silent at
  11:31 while its peers produced until 19:12 (a partial day counter);
* SAG 26 Sep: inverter counters = vendor plant daily = 2,518 kWh, PASS -
  while the next night's lifetime counter proved +153 kWh (the logger had
  lost its connection); 24 Sep - 2 Oct: ~1,200 kWh proven, not billed;
* SAG 25 Sep: both counters frozen at 264 kWh, the day corrected later to
  2,013 kWh - and the reconciliation still said PASS.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys

import pytest

from argia.alerts import grading, monitor as M
from argia.alerts.engine import Candidate
from argia.recon import engine as E
from argia.recon import retry as R

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2 / "scripts"))
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 4, 14, 0, tzinfo=UTC)


def cand(metric, sev, key=None):
    return Candidate(alert_key=key or f"mex1:inv:sn1:{metric}", plant_key="MEX1", inverter_sn="SN1", metric=metric,
                     severity=sev, value=1.0, threshold=None, message=f"MEX1 SN1: {metric} [{sev}]")


# ------------------------------------------------------- money rule
class TestPotentialLossIsAFlag:
    @pytest.mark.parametrize("sev", ["WARNING", "CRITICAL"])
    def test_a_silent_inverter_intraday_is_a_flag_at_any_length(self, sev):
        c = grading.grade(cand("inverter_silent", sev), "acute")
        assert c.severity == "INFO" and c.message.endswith("[INFO]")

    def test_a_day_without_data_is_a_flag(self):
        assert grading.grade(cand("data_stale", "WARNING"), "daily").severity == "INFO"

    def test_the_daily_off_verdict_stays_critical(self):
        # the counter proved the unit did not produce: a measured loss
        assert grading.grade(cand("inverter_silent", "CRITICAL"), "daily").severity == "CRITICAL"

    @pytest.mark.parametrize("kind,want", [("off", "CRITICAL"), ("unconfirmed", "INFO"),
                                           ("comms", "INFO"), ("unclassified", "INFO")])
    def test_daily_silent_kinds(self, kind, want):
        import alerts_daily as D
        assert D.silent_severity(kind, "CRITICAL" if kind in ("off", "unconfirmed") else "WARNING") == want

    @pytest.mark.parametrize("metric", ["plant_offline", "inverter_relative", "energy_daily_pct", "inverter_fault"])
    def test_measured_losses_still_critical(self, metric):
        assert grading.grade(cand(metric, "CRITICAL"), "daily").severity == "CRITICAL"


class TestLastResortForBlindness:
    def test_blind_for_three_days_becomes_one_warning(self):
        k = "mex1:plant:data_stale"
        c = grading.grade(cand("data_stale", "WARNING", k), "daily")
        two = grading.escalate_blind([c], {k: NOW - dt.timedelta(days=2, hours=23)}, NOW)[0]
        assert two.severity == "INFO"
        three = grading.escalate_blind([c], {k: NOW - dt.timedelta(days=3)}, NOW)[0]
        assert three.severity == "WARNING"
        assert three.message.endswith("no data for 3 days, production cannot be confirmed: check the datalogger [WARNING]")

    def test_only_potential_info_and_only_with_an_open_record(self):
        k = "mex1:inv:sn1:inverter_relative"
        c = cand("inverter_relative", "WARNING", k)
        assert grading.escalate_blind([c], {k: NOW - dt.timedelta(days=9)}, NOW)[0] is c
        s = grading.grade(cand("inverter_silent", "WARNING"), "daily")
        assert grading.escalate_blind([s], {}, NOW)[0].severity == "INFO"          # nothing open yet

    def test_the_daily_job_wires_it_after_grading(self):
        src = (V2 / "scripts/alerts_daily.py").read_text(encoding="utf-8")
        assert src.index('grading.grade_all(candidates, tier="daily")') < src.index("grading.escalate_blind(")
        assert "severity=silent_severity(b.kind, b.severity.value)" in src


# --------------------------------------- lost connection is not a loss
class _Row:
    def __init__(self, ts, sn, p):
        self.timestamp_utc, self.inverter_sn, self.power_w, self.plant_key = ts, sn, p, "TAM1"


class _Bundle:
    def __init__(self, rows):
        self.rows = rows

    def rows_for_plant(self, pk):
        return [r for r in self.rows if r.plant_key == pk]


class _Portfolio:
    class P:
        plant_key = "TAM1"

    def active_plants(self):
        return [self.P()]


def mx(h, m=0):
    return dt.datetime(2026, 9, 30, 6, 0, tzinfo=UTC) + dt.timedelta(hours=h, minutes=m)


class TestStoppedEarly:
    def test_the_30_september_tam1_case(self):
        import alerts_daily as D
        rows = [_Row(mx(h, m), "5X00K", 9000.0) for h in range(7, 12) for m in (0, 30)] + [_Row(mx(11, 31), "5X00K", 8000.0)]
        for sn in ("7D006", "7D007"):
            rows += [_Row(mx(h, m), sn, 9000.0 if h < 19 else 0.0) for h in range(7, 22) for m in (0, 30)]
        rows.append(_Row(mx(19, 12), "7D006", 120.0))
        got = D.stopped_early(_Bundle(rows), _Portfolio())
        assert list(got) == [("TAM1", "5X00K")] and got[("TAM1", "5X00K")] == (mx(11, 31), mx(19, 12))

    def test_an_inverter_that_sleeps_at_sunset_is_not_cut_short(self):
        import alerts_daily as D
        rows = [_Row(mx(h), "A", 5000.0 if h < 19 else 0.0) for h in range(7, 22)]
        rows += [_Row(mx(h), "B", 5000.0) for h in range(7, 19)] + [_Row(mx(18, 50), "B", 50.0)]   # B's logger sleeps after its last watts
        assert D.stopped_early(_Bundle(rows), _Portfolio()) == {}

    def test_rows_without_power_are_not_readings(self):
        import alerts_daily as D
        rows = [_Row(mx(h), "A", 5000.0) for h in range(7, 19)]
        rows += [_Row(mx(h), "B", 5000.0) for h in range(7, 12)] + [_Row(mx(h), "B", None) for h in range(12, 19)]
        assert list(D.stopped_early(_Bundle(rows), _Portfolio())) == [("TAM1", "B")]

    def test_a_skipped_inverter_is_not_judged_against_its_peers(self):
        import alerts_daily as D
        from argia.analytics.inverter_health import InverterReading
        rs = [InverterReading("TAM1", "5X00K", 48.0, 50.0), InverterReading("TAM1", "7D006", 219.0, 50.0),
              InverterReading("TAM1", "7D007", 220.0, 50.0)]
        judged = D.build_candidates(rs, {})
        assert [c.inverter_sn for c in judged if c.metric == "inverter_relative"] == ["5X00K"]     # was "26% of peers"
        assert D.build_candidates(rs, {}, relative_skip=frozenset({("TAM1", "5X00K")})) == []


class TestReconciliationOfLostConnection:
    def test_the_sag_26_september_day_is_not_a_plain_pass(self):
        r = E.with_lifetime_catchup(E.daily_recon(2518.2, 2518.2, 2518.2, 100.0), 153.0)
        assert r.status == E.STATUS_REVIEW and r.reference_kwh == 2518.2          # billing basis untouched
        assert ("lost connection: the lifetime counter proves +153.0 kWh not in the day counters"
                " (the inverters kept producing; 2671.2 kWh in total) - the month is settled on the vendor portal's month counter") in r.note

    def test_small_catch_up_is_noise(self):
        r0 = E.daily_recon(2518.2, 2518.2, 2518.2, 100.0)
        assert E.with_lifetime_catchup(r0, 4.9) is r0                               # below 5 kWh
        assert E.with_lifetime_catchup(r0, 20.0) is r0                              # below 1 % of 2,518
        assert E.with_lifetime_catchup(r0, None) is r0
        fail = E.daily_recon(900.0, 1000.0, 1000.0, 100.0)
        assert E.with_lifetime_catchup(fail, 50.0).status == E.STATUS_FAIL         # never softens a FAIL

    def test_frozen_counters_with_a_corrected_day_are_review(self):
        r = E.daily_recon(263.6, 263.6, 2012.8, 100.0)                              # SAG 25 Sep
        assert r.status == E.STATUS_REVIEW
        assert "counters froze (lost connection) - the day was corrected from the vendor history" in r.note
        assert E.daily_recon(1000.0, 1000.0, 950.0, 100.0).status == E.STATUS_PASS  # a short KPI row: the self-heal raises it

    def test_these_reviews_do_not_trigger_vendor_refetches(self):
        r = E.with_lifetime_catchup(E.daily_recon(2518.2, 2518.2, 2518.2, 100.0), 153.0)
        assert R.needs_retry(r.status, r.note, 100.0) is False
        f = E.daily_recon(263.6, 263.6, 2012.8, 100.0)
        assert R.needs_retry(f.status, f.note, 100.0) is False

    def test_the_job_reads_the_catch_up_from_loss_daily(self):
        src = (V2 / "scripts/recon_snapshot.py").read_text(encoding="utf-8")
        assert "catchup = lifetime_catchup(date_iso)" in src
        assert "E.with_lifetime_catchup(E.daily_recon(ikwh, vendor_daily, kpi, completeness)" in src


# ------------------------------------------------------ server health
def st(first, sent=None, active=True):
    return ((sent or first), active, sent is not None, first)


class TestMonitoringScope:
    def test_only_the_monitoring_chain_is_watched(self):
        for u in ("argia-telemetry", "argia-alerts-snap", "argia-kpi", "argia-portal-gen", "argia-dailyperf", "argia-dbdump"):
            assert u in M.MONITORING_UNITS
        for u in ("argia-fin-drive", "argia-archive", "argia-archive-month", "argia-invoice", "argia-cfe-push",
                  "argia-finmail-weekly", "argia-demo-gen", "argia-cpa-gen", "argia-client-pages", "argia-finreport"):
            assert u not in M.MONITORING_UNITS
        assert M.in_scope("unit-failed:argia-telemetry.service") and not M.in_scope("unit-failed:argia-fin-drive.service")
        assert M.in_scope("postgres-down") and M.in_scope("telemetry-stale") and not M.in_scope("cfe-heartbeat")

    def test_the_mailer_only_gathers_watched_units(self):
        src = (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert "UNITS = monitor.MONITORING_UNITS" in src

    def test_telemetry_stopped_is_critical_inside_the_collection_window(self):
        noon = dt.datetime(2026, 10, 4, 12, 0)
        assert M.telemetry_alerts(12.0, noon) == []
        (a,) = M.telemetry_alerts(55.0, noon)
        assert a.key == "telemetry-stale" and a.severity == M.SEV_CRIT and "newest reading 55 min old" in a.detail
        assert M.telemetry_alerts(None, noon)[0].detail.count("no reading at all") == 1
        assert M.telemetry_alerts(600.0, dt.datetime(2026, 10, 4, 22, 0)) == []      # night

    def test_disk_is_critical_from_90_percent(self):
        assert M.infra_alerts([], 86.0, True)[0].severity == M.SEV_WARN
        assert M.infra_alerts([], 91.0, True)[0].severity == M.SEV_CRIT


class TestLastResortMail:
    def test_six_hours_then_once_a_day(self):
        a = M.Alert("unit-failed:argia-telemetry.service", M.SEV_CRIT, "job failed: argia-telemetry", "")
        assert M.plan_last_resort([a], {}, NOW) == []                                         # first sighting
        assert M.plan_last_resort([a], {a.key: st(NOW - dt.timedelta(hours=5))}, NOW) == []  # the Pi has pushed
        assert M.plan_last_resort([a], {a.key: st(NOW - dt.timedelta(hours=6))}, NOW) == [a]
        mailed = st(NOW - dt.timedelta(hours=20), sent=NOW - dt.timedelta(hours=10))
        assert M.plan_last_resort([a], {a.key: mailed}, NOW) == []
        mailed = st(NOW - dt.timedelta(hours=40), sent=NOW - dt.timedelta(hours=24))
        assert M.plan_last_resort([a], {a.key: mailed}, NOW) == [a]

    def test_never_a_warning_or_an_out_of_scope_problem(self):
        old = {k: st(NOW - dt.timedelta(days=3)) for k in ("disk-full", "unit-failed:argia-fin-drive.service", "recon-fail:GTO2:2026-10-01")}
        active = [M.Alert("disk-full", M.SEV_WARN, "disk", ""),
                  M.Alert("unit-failed:argia-fin-drive.service", M.SEV_CRIT, "job failed: argia-fin-drive", ""),
                  M.Alert("recon-fail:GTO2:2026-10-01", M.SEV_WARN, "recon", "")]
        assert M.plan_last_resort(active, old, NOW) == []

    def test_no_recovery_mails_and_health_written_only_for_real(self):
        src = (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert "recoveries_to_mail" not in src and "plan_sends(" not in src
        assert src.index("if args.dry_run:") < src.index("write_health(health)")


class TestHealthDoc:
    def test_problems_and_the_record(self):
        acts = [M.Alert("unit-failed:argia-kpi.service", M.SEV_CRIT, "job failed: argia-kpi", ""),
                M.Alert("cfe-heartbeat", M.SEV_WARN, "CFE Pi heartbeat stale", "")]
        doc = M.health_doc(acts, {"unit-failed:argia-kpi.service": st(NOW - dt.timedelta(hours=2))}, NOW, 3.0, 32.0, True)
        assert doc["generated_utc"] == "2026-10-04T14:00:00Z" and doc["status"] == "critical"
        assert doc["problems"] == [{"key": "unit-failed:argia-kpi.service", "severity": "CRITICAL",
                                    "title": "job failed: argia-kpi", "since_utc": "2026-10-04T12:00:00Z"}]
        assert [o["key"] for o in doc["other"]] == ["cfe-heartbeat"]
        assert (doc["telemetry_age_min"], doc["disk_pct"], doc["pg_ok"]) == (3.0, 32.0, True)
        assert M.health_doc([], {}, NOW, None, None, False)["status"] == "ok"

    def test_written_atomically(self, tmp_path):
        import alert_mailer as AM
        p = tmp_path / "h" / "health.json"
        AM.write_health({"status": "ok"}, str(p))
        assert p.read_text(encoding="utf-8").strip().startswith("{") and not (tmp_path / "h" / "health.json.tmp").exists()


# ----------------------------------------------------------- the Pi
spec = importlib.util.spec_from_file_location("health_watch", V2 / "pi" / "report_watch" / "health_watch.py")
HW = importlib.util.module_from_spec(spec)
spec.loader.exec_module(HW)
T = NOW.timestamp()


def health(age_min=5, problems=(), at=None):
    ref = dt.datetime.fromtimestamp(at, UTC) if at is not None else NOW
    gen = (ref - dt.timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"generated_utc": gen, "status": "critical" if problems else "ok", "problems": list(problems)}


P_KPI = {"key": "unit-failed:argia-kpi.service", "severity": "CRITICAL", "title": "job failed: argia-kpi",
         "since_utc": "2026-10-04T12:00:00Z"}


class TestPiHealthWatch:
    def test_healthy_is_silent(self):
        pushes, s = HW.decide({}, health(), True, T)
        assert pushes == [] and s["fails"] == 0

    def test_a_critical_problem_pushes_once_then_every_12_h_then_resolves(self):
        pushes, s = HW.decide({}, health(problems=[P_KPI]), True, T)
        assert pushes == [("high", "ARGIA: job failed: argia-kpi", "job failed: argia-kpi - since 2026-10-04 12:00 UTC (server health check).")]
        pushes, s = HW.decide(s, health(problems=[P_KPI], at=T + 6 * 3600), True, T + 6 * 3600)
        assert pushes == []
        pushes, s = HW.decide(s, health(problems=[P_KPI], at=T + 12 * 3600), True, T + 12 * 3600)
        assert [p[0] for p in pushes] == ["high"]
        pushes, s = HW.decide(s, health(at=T + 13 * 3600), True, T + 13 * 3600)
        assert pushes == [("low", "ARGIA: resolved - job failed: argia-kpi", "No longer reported by the server.")]
        assert s["pushed"] == {}

    def test_warnings_are_not_pushed(self):
        w = dict(P_KPI, key="disk-full", severity="WARNING", title="disk 86%")
        assert HW.decide({}, health(problems=[w]), True, T)[0] == []

    def test_a_stale_file_means_the_monitoring_is_dead(self):
        pushes, s = HW.decide({}, health(age_min=50), True, T)
        assert pushes[0][:2] == ("high", "ARGIA server: monitoring stopped") and "50 min old" in pushes[0][2]
        assert HW.decide(s, health(age_min=55, at=T + 600), True, T + 600)[0] == []   # not every 5 min
        pushes, _ = HW.decide(s, health(at=T + 1200), True, T + 1200)
        assert pushes == [("low", "ARGIA server: monitoring running again", "The health file is fresh again.")]

    def test_unreadable_twice_pushes_but_not_during_a_portal_outage(self):
        pushes, s = HW.decide({}, None, False, T)
        assert pushes == [] and s["fails"] == 1
        pushes, s2 = HW.decide(s, None, False, T + 300)
        assert pushes[0][1] == "ARGIA server: health file unreadable"
        assert HW.decide(s, None, False, T + 300, portal_down=True)[0] == []        # report_watch already said DOWN

    def test_does_nothing_without_the_backup_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(HW, "KEY", str(tmp_path / "no_key"))
        monkeypatch.setattr(HW, "fetch", lambda: (_ for _ in ()).throw(AssertionError("fetched")))
        assert HW.main() == 0

    def test_report_watch_runs_it(self):
        src = (V2 / "pi" / "report_watch" / "report_watch.sh").read_text(encoding="utf-8")
        assert 'python3 "$(dirname "$0")/health_watch.py"' in src


# ------------------------------------------------- dashboard flags
class TestFlagsOnTheDashboard:
    def test_portal_pill(self):
        src = (V2 / "server/monitoring_gen.py").read_text(encoding="utf-8")
        assert '<span class="pill off" data-en="FLAG" data-es="AVISO"' in src and "{sev_pill(a[\"sev\"])}" in src

    def test_phone_app_pill_and_order(self):
        sys.path.insert(0, str(V2 / "server" / "bundle"))
        import app_view as A
        assert 'data-en="Flag"' in A.sev_pill("INFO") and 'pill p3' in A.sev_pill("INFO")
        assert 'pill crit' in A.sev_pill("CRITICAL") and 'pill warn' in A.sev_pill("WARNING")
        out = A.all_alerts([{"alerts": [{"sev": "INFO", "since": "1"}, {"sev": "WARNING", "since": "2"},
                                        {"sev": "CRITICAL", "since": "3"}]}])
        assert [a["sev"] for _, a in out] == ["CRITICAL", "WARNING", "INFO"]

    def test_a_flag_is_not_counted_as_a_warning(self):
        src = (V2 / "server/bundle/portal_gen.py").read_text(encoding="utf-8")
        assert "elif sev == 'WARNING':      # v305: an INFO flag is not a warning" in src


class TestDriveIsNotTheMonitoringsJob:
    def test_the_daily_report_survives_a_drive_failure(self):
        src = (V2 / "scripts/report_daily.py").read_text(encoding="utf-8")
        i = src.index("drive = DriveClient()")
        assert src.rindex("try:", 0, i) > src.rindex("pdf_id = html_id = None", 0, i) - 1
        assert "Drive archive copy failed" in src and src.index("Drive archive copy failed") < src.index("send_report(pdf_path")


class _P:
    def __init__(self, pk, kwp):
        self.plant_key, self.kwp_dc = pk, kwp


class _Fleet:
    def __init__(self, *plants):
        self.plants = plants

    def active_plants(self):
        return list(self.plants)


class _R:
    def __init__(self, pk, sn, ts, p):
        self.plant_key, self.inverter_sn, self.timestamp_utc, self.power_w = pk, sn, ts, p


def oct2(h, m=0):
    return dt.datetime(2026, 10, 2, 6, 0, tzinfo=UTC) + dt.timedelta(hours=h, minutes=m)


class TestCutOffPlants:
    """SAG 2 Oct: FusionSolar repeated 12,301 + 15,520 + 15,519 W from 16:35
    (removed as repeats since v302); the fleet produced until ~18:50."""

    def fleet(self):
        rows = []
        for sn in ("A", "B", "C"):
            rows += [_R("MEX1", sn, oct2(h, m), 20000.0) for h in range(7, 16) for m in (0, 30)]
            rows.append(_R("MEX1", sn, oct2(16, 35), 14000.0))
        for pk in ("GTO1", "NL1"):
            rows += [_R(pk, "X", oct2(h, m), 30000.0 if h < 19 else 0.0) for h in range(7, 22) for m in (0, 30)]
            rows.append(_R(pk, "X", oct2(18, 50), 400.0))
        return _Bundle2(rows), _Fleet(_P("MEX1", 597.78), _P("GTO1", 900.0), _P("NL1", 800.0))

    def test_sag_2_october_is_cut_off(self):
        import alerts_daily as D
        b, f = self.fleet()
        assert D.cut_off_plants(b, f) == {"MEX1": (oct2(16, 35), 42.0)}

    def test_a_plant_that_goes_to_zero_is_not_cut_off(self):
        import alerts_daily as D
        b, f = self.fleet()
        b.rows += [_R("MEX1", sn, oct2(16, 40), 0.0) for sn in ("A", "B", "C")]    # a real outage reads 0 W
        assert D.cut_off_plants(b, f) == {}

    def test_its_energy_verdict_is_a_flag_not_a_loss(self):
        import alerts_daily as D
        c = Candidate(alert_key="mex1:plant:energy_daily_pct", plant_key="MEX1", inverter_sn="", metric="energy_daily_pct",
                      severity="CRITICAL", value=0.12, threshold=0.7,
                      message="[MEX1] produced 264 kWh vs 2251 expected (12%) - below 70%")
        other = Candidate(alert_key="gto1:plant:energy_daily_pct", plant_key="GTO1", inverter_sn="", metric="energy_daily_pct",
                          severity="CRITICAL", value=0.5, threshold=0.7, message="[GTO1] x")
        out = D.unconfirmed_energy([c, other], {"MEX1": (oct2(16, 35), 42.0)})
        assert out[0].severity == "INFO" and out[1] is other
        assert out[0].message.endswith("its readings stopped at 16:35 MX while producing 42 kW (lost connection?):"
                                       " energy unconfirmed until the counter reports [INFO]")
        src = (V2 / "scripts/alerts_daily.py").read_text(encoding="utf-8")
        assert src.index("candidates = unconfirmed_energy(candidates, cut_off)") < src.index("grading.escalate_blind(")


class _Bundle2:
    def __init__(self, rows):
        self.rows = rows

    def rows_for_plant(self, pk):
        return [r for r in self.rows if r.plant_key == pk]


def test_recon_catch_up_lookup_never_stops_the_reconciliation(monkeypatch):
    """The laptop run of v305 failed: no psql there, FileNotFoundError escaped."""
    import recon_snapshot as RS
    monkeypatch.setattr(RS, "psql_rows", lambda sql: (_ for _ in ()).throw(FileNotFoundError("psql")))
    assert RS.lifetime_catchup("2026-10-01") == {}


class TestMonthCounterIsTheReference:
    """Tomasz, 2026-10-04: "we always have to reconcile against the portal
    counter for the total month production". SAG September: invoice 71,437.91
    kWh = vendor portal month counter 71,438 = lifetime delta 71,438; the daily
    rows (some healed from the vendor history) sum to 71,447 - while loss_daily
    added 2,411 kWh of lifetime catch-up on top (73,859) and v305's first cut
    reported it as 'unbilled'. It was double counted."""

    def test_nothing_left_to_credit_when_the_days_already_hold_the_month(self):
        from argia.analytics import losses as L
        counters = [("2026-08-31", 2100.0, 1236358.0), ("2026-09-25", 263.6, 1290000.0), ("2026-09-30", 1598.0, 1307796.0)]
        counted = {f"2026-09-{d:02d}": 71447.0 / 30 for d in range(1, 31)}
        budget = L.month_budget(counters, counted)
        assert round(budget["2026-09"]) == round(1307796.0 - 1236358.0 - 71447.0)       # -9 kWh: nothing uncounted
        assert L.cap_to_month({"2026-09-25": 205.8, "2026-09-26": 153.0}, budget) == {}

    def test_a_real_gap_is_credited_up_to_the_month_counter(self):
        from argia.analytics import losses as L
        counters = [("2026-08-31", 0.0, 1000.0), ("2026-09-02", 0.0, 1300.0)]
        counted = {"2026-09-01": 100.0, "2026-09-02": 100.0}                            # 100 kWh of a frozen day missing
        b = L.month_budget(counters, counted)
        assert b == {"2026-09": 100.0}
        assert L.cap_to_month({"2026-09-01": 150.0, "2026-09-02": 30.0}, b) == {"2026-09-01": 100.0}

    def test_one_lifetime_reading_credits_nothing(self):
        from argia.analytics import losses as L
        assert L.month_budget([("2026-09-02", 0.0, 1300.0)], {"2026-09-01": 1.0, "2026-09-02": 1.0}) == {}
        assert L.cap_to_month({"2026-09-01": 50.0}, {}) == {}

    def test_without_the_month_start_the_covered_span_is_used(self):
        from argia.analytics import losses as L
        # nights 9, 10, 11 Sep: the 10th froze at 300 of 1,000 kWh, the 11th made 900
        counters = [("2026-09-09", 0.0, 5000.0), ("2026-09-10", 300.0, 5300.0), ("2026-09-11", 900.0, 6900.0)]
        counted = {"2026-09-09": 1000.0, "2026-09-10": 300.0, "2026-09-11": 900.0}
        assert L.month_budget(counters, counted) == {"2026-09": 700.0}         # 1,900 - (300 + 900)

    def test_snapshot_nights_after_the_counted_days_are_ignored(self):
        from argia.analytics import losses as L
        counters = [("2026-08-31", 0.0, 1000.0), ("2026-09-01", 0.0, 1100.0), ("2026-09-05", 0.0, 1500.0)]
        assert L.month_budget(counters, {"2026-09-01": 100.0}) == {"2026-09": 0.0}     # not 400

    def test_loss_daily_caps_its_credit(self):
        src = (V2 / "scripts/loss_daily.py").read_text(encoding="utf-8")
        assert "credit = L.cap_to_month(credit, L.month_budget(cnt.get(k, []), counted))" in src
        assert "date_trunc('month', DATE '{d0}')::date - 1" in src


# ------------------------------------------- v305.2: the server watches the Pi
spec_ps = importlib.util.spec_from_file_location("pi_status", V2 / "pi" / "report_watch" / "pi_status.py")
PS = importlib.util.module_from_spec(spec_ps)
spec_ps.loader.exec_module(PS)


class TestPiReportsOnItself:
    def test_the_status_document(self):
        now = T
        files = {"logs": {"backup_pull": ("/x", now - 7 * 86400, "2026-09-27 pull OK"), "deploy": ("/y", now - 300, "ok")},
                 "dumps": [("argia_mont_20260926.dump", now - 8 * 86400), ("argia_mont_20260927.dump", now - 7 * 86400)],
                 "weekly": [("argia_mont_20260927.dump", now - 7 * 86400)],
                 "cfe_heartbeat": (now - 6 * 86400, False)}
        doc = PS.build(now, files, ["*/5 * * * * bash report_watch.sh"], "03f8b7a", 17000, "ARGIAPi")
        assert doc["ts"] == "2026-10-04T14:00:00Z" and doc["host"] == "ARGIAPi" and doc["git_head"] == "03f8b7a"
        assert doc["backup"] == {"newest": "argia_mont_20260927.dump", "age_h": 168.0, "daily_count": 2, "weekly_count": 1,
                                 "pull_exec": None, "sealed": False}    # v319: plain dump = not sealed
        assert doc["cfe_heartbeat"] == {"age_h": 144.0, "writable": False}
        assert doc["jobs"]["backup_pull"] == {"log_mtime": "2026-09-27T14:00:00Z", "last": "2026-09-27 pull OK"}

    def test_report_watch_runs_it_hourly(self):
        src = (V2 / "pi" / "report_watch" / "report_watch.sh").read_text(encoding="utf-8")
        assert 'python3 "$(dirname "$0")/pi_status.py"' in src and "-ge 3300" in src

    def test_does_nothing_off_the_pi(self, tmp_path, monkeypatch):
        monkeypatch.setattr(PS, "HOME", str(tmp_path))
        monkeypatch.setattr(PS.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
        monkeypatch.setattr(PS.os.path, "expanduser", lambda p: str(tmp_path))
        assert PS.main() == 0


class TestServerWatchesThePi:
    def status(self, age_h=0.5, backup_h=10.0):
        ts = (NOW - dt.timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"ts": ts, "backup": {"newest": "argia_mont_20261003.dump", "age_h": backup_h}, "jobs": {}, "crontab": ["a", "b"]}

    def test_a_healthy_pi(self):
        assert M.pi_alerts(self.status(), NOW) == []

    def test_a_silent_pi_is_critical_and_in_scope(self):
        (a,) = M.pi_alerts(self.status(age_h=4), NOW)
        assert a.key == "pi-silent" and a.severity == M.SEV_CRIT and "4 h old" in a.detail and M.in_scope(a.key)
        assert M.pi_alerts(None, NOW)[0].key == "pi-silent"

    def test_a_week_without_the_backup_pull(self):
        (a,) = M.pi_alerts(self.status(backup_h=168), NOW)
        assert a.key == "pi-backup-stale" and "168 h old" in a.detail and M.in_scope(a.key)
        assert M.pi_alerts(dict(self.status(), backup={}), NOW)[0].detail.count("missing") == 1

    def test_summary_in_the_health_file(self):
        s = M.pi_summary(self.status())
        assert s["backup"]["age_h"] == 10.0 and s["cron_lines"] == 2 and M.pi_summary(None) is None
        src = (V2 / "scripts/alert_mailer.py").read_text(encoding="utf-8")
        assert 'health["pi"] = monitor.pi_summary(pi_status)' in src and "+ monitor.pi_alerts(pi_status, now))" in src

    def test_mail_hook_only_for_what_the_server_cannot_say_itself(self, monkeypatch, tmp_path):
        calls = []
        hook = tmp_path / "send_mail_hook.sh"
        hook.write_text("#!/bin/sh\n")
        hook.chmod(0o755)
        monkeypatch.setattr(HW.os.path, "expanduser", lambda p: str(hook) if p.endswith("send_mail_hook.sh") else p)
        monkeypatch.setattr(HW.subprocess, "run", lambda args, **k: calls.append(args[1] if len(args) > 1 else args))
        monkeypatch.setenv("ARGIA_PUSH", "off")
        HW.push("high", "ARGIA server: monitoring stopped", "x")
        HW.push("high", "ARGIA: job failed: argia-kpi", "x")        # the server mails that one itself after 6 h
        assert calls == ["ARGIA server: monitoring stopped"]
