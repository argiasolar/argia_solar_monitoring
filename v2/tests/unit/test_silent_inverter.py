"""v203 — a single inverter going silent while its siblings produce
(Holiday Inn Express / SLP2, 2026-09-04: Inverter 1 JFM7DXN03J sent
nothing 14:20-19:59 MX, Inverter 2 reported ~105 kW throughout; the
vendor counter later showed +145 kWh across the gap — a datalogger
link drop, no energy lost; the portal said "stale", nobody was told).

Three layers: the acute detector (opens within 45 min), the daily
classifier through the vendor counter (comms vs OFF), and the Growatt
code catalog (FT=302 = no AC connection).
"""
import datetime as dt

from argia.alerts.engine import ENGINE_METRICS, candidate_from_acute_breach, candidate_from_fault_breach
from argia.alerts.fault_catalog import GROWATT_FAULT_TYPE, explain_fault
from argia.analytics.acute import SILENT_CRIT_MIN, SILENT_WARN_MIN, evaluate_acute
from argia.analytics.inverter_health import Severity
from argia.analytics.silent import COMMS_RATIO, evaluate_silent_gaps
from argia.analytics.vendor_flags import evaluate_inverter_faults
from argia.core.time_utils import MX_TZ, UTC


def mx(h, m=0, day=4):
    return dt.datetime(2026, 9, day, h, m, tzinfo=MX_TZ).astimezone(UTC)


# ------------------------------------------------------------ acute

def _tail(now, silent_since_min):
    """SLP2 tail: Inverter 2 fresh at 105 kW every 5 min; Inverter 1 last seen
    ``silent_since_min`` ago."""
    rows = []
    for k in range(0, 36):
        t = now - dt.timedelta(minutes=5 * k)
        rows.append((t, "SLP2", "JFM7DXN039", 105000.0, 57.0, 1, "0"))
        if 5 * k >= silent_since_min:
            rows.append((t, "SLP2", "JFM7DXN03J", 100000.0, 60.0, 1, "0"))
    return rows


CONF = {"SLP2": ["JFM7DXN03J", "JFM7DXN039"]}


def _silent(breaches):
    return [b for b in breaches if b.metric == "inverter_silent"]


class TestAcuteSilent:
    def test_opens_after_45_min_next_to_a_producing_sibling(self):
        now = mx(15, 5)
        assert _silent(evaluate_acute(_tail(now, 30), ["SLP2"], now, configured_inverters=CONF)) == []
        b = _silent(evaluate_acute(_tail(now, 50), ["SLP2"], now, configured_inverters=CONF))
        assert len(b) == 1 and b[0].inverter_sn == "JFM7DXN03J" and b[0].severity is Severity.WARNING
        assert "no data for 50 min" in b[0].message and "1 sibling(s) report up to 105 kW" in b[0].message
        assert "the vendor counter decides" in b[0].message

    def test_critical_after_three_hours_even_when_gone_from_the_tail(self):
        now = mx(17, 30)     # 17:30 MX is outside the 09-17 window -> quiet
        assert _silent(evaluate_acute(_tail(now, 200), ["SLP2"], now, configured_inverters=CONF)) == []
        now = mx(16, 55)
        rows = [r for r in _tail(now, 999)]                  # Inverter 1 absent from the 3 h tail
        b = _silent(evaluate_acute(rows, ["SLP2"], now, absent_gap_hours=3.0, configured_inverters=CONF))
        assert len(b) == 1 and b[0].severity is Severity.CRITICAL and ">= 3.0 h" in b[0].message
        # without a tail span the absent unit cannot be judged
        assert _silent(evaluate_acute(rows, ["SLP2"], now, configured_inverters=CONF)) == []

    def test_quiet_when_the_whole_plant_is_dark_or_at_dusk(self):
        now = mx(15, 5)
        dark = [(r[0], r[1], r[2], 0.0, r[4], r[5], r[6]) for r in _tail(now, 60)]
        assert _silent(evaluate_acute(dark, ["SLP2"], now, configured_inverters=CONF)) == []
        dusk = [(r[0], r[1], r[2], 3000.0, r[4], r[5], r[6]) for r in _tail(now, 60)]  # < 5 kW
        assert _silent(evaluate_acute(dusk, ["SLP2"], now, configured_inverters=CONF)) == []
        assert _silent(evaluate_acute(_tail(now, 60), ["SLP2"], now)) == []           # no config: off

    def test_engine_owns_the_metric_and_the_key_matches_the_daily_tier(self):
        assert "inverter_silent" in ENGINE_METRICS
        now = mx(15, 5)
        b = _silent(evaluate_acute(_tail(now, 50), ["SLP2"], now, configured_inverters=CONF))[0]
        c = candidate_from_acute_breach(b)
        assert c.alert_key == "slp2:inv:jfm7dxn03j:inverter_silent" and c.metric == "inverter_silent"
        assert SILENT_WARN_MIN == 45 and SILENT_CRIT_MIN == 180


# ------------------------------------------------------------ daily

def _day(self_growth_kwh, came_back=True):
    """SLP2 2026-09-04 in miniature: both units report 09:00-14:20, Inverter 2
    keeps reporting to 19:00 with its counter climbing 574 -> 794; Inverter 1
    reappears at 19:59 with 574 + self_growth (or never)."""
    rows = []
    t = mx(9, 0)
    c1 = c2 = 100.0
    while t <= mx(14, 20):
        rows.append((t, "JFM7DXN039", c2, 105000.0)); rows.append((t, "JFM7DXN03J", c1, 100000.0))
        last_c1 = c1
        c1 += 8.0; c2 += 8.5; t += dt.timedelta(minutes=10)
    c1 = last_c1
    c2_at_gap = c2 - 8.5
    while t <= mx(19, 0):
        rows.append((t, "JFM7DXN039", c2, 60000.0)); c2 += 5.0; t += dt.timedelta(minutes=10)
    if came_back:
        rows.append((mx(19, 59), "JFM7DXN03J", c1 + self_growth_kwh, 0.0))
        rows.append((mx(19, 59), "JFM7DXN039", c2, 0.0))
    return rows, c2 - c2_at_gap


RATED = {"JFM7DXN03J": 125.0, "JFM7DXN039": 125.0}


class TestDailyClassification:
    def test_counter_climbed_like_the_sibling_is_a_comms_gap(self):
        rows, sib_growth = _day(self_growth_kwh=145.0)
        b = evaluate_silent_gaps("SLP2", rows, RATED, mx(20, 0), configured=list(RATED))
        assert len(b) == 1 and b[0].inverter_sn == "JFM7DXN03J"
        assert b[0].kind == "comms" and b[0].severity is Severity.WARNING
        assert b[0].self_kwh == 145.0 and abs(b[0].sibling_kwh_per_kw - sib_growth / 125.0) < 1e-9
        assert b[0].ratio >= COMMS_RATIO and "no energy lost" in b[0].message
        assert "14:20-19:59 MX" in b[0].message and "339 min" in b[0].message

    def test_flat_counter_means_the_unit_was_off(self):
        rows, sib_growth = _day(self_growth_kwh=2.0)
        b = evaluate_silent_gaps("SLP2", rows, RATED, mx(20, 0))[0]
        assert b.kind == "off" and b.severity is Severity.CRITICAL
        assert "kWh lost" in b.message and f"~{sib_growth - 2.0:.0f} kWh lost" in b.message

    def test_never_came_back_is_unconfirmed_critical(self):
        rows, _ = _day(0.0, came_back=False)
        b = evaluate_silent_gaps("SLP2", rows, RATED, mx(20, 0))[0]
        assert b.kind == "unconfirmed" and b.severity is Severity.CRITICAL and b.gap_end_utc is None

    def test_whole_plant_gap_and_night_gaps_are_not_this_metric(self):
        rows, _ = _day(145.0)
        alone = [r for r in rows if r[1] == "JFM7DXN03J"]          # sibling never reported
        assert evaluate_silent_gaps("SLP2", alone, RATED, mx(20, 0)) == []
        # a 60-min gap starting at 07:00 is outside the 09-17 window
        early = [(mx(7, 0), "A", 0.0, 1000.0), (mx(8, 0), "A", 5.0, 1000.0),
                 (mx(7, 10), "B", 0.0, 9000.0), (mx(7, 40), "B", 3.0, 9000.0)]
        assert evaluate_silent_gaps("X", early, {"A": 10, "B": 10}, mx(20, 0)) == []

    def test_script_wires_the_daily_owner(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alerts_daily.py"
               ).read_text(encoding="utf-8")
        assert "daily_silent_candidates(bundle, portfolio, date_iso)" in src
        snap = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alerts_snapshot.py"
                ).read_text(encoding="utf-8")
        assert "configured_inverters=configured" in snap


# ------------------------------------------------------------ catalog

class TestGrowattCatalog:
    def test_grid_side_codes_are_named(self):
        assert explain_fault("GROWATT", "FT=302").startswith("Growatt error 302: no AC connection")
        assert "utility side" in explain_fault("GROWATT", "FT=300")
        assert "over temperature" in explain_fault("GROWATT", "FT=408")
        assert 302 in GROWATT_FAULT_TYPE and 300 in GROWATT_FAULT_TYPE

    def test_unknown_codes_still_say_so_honestly(self):
        assert "not in catalog" in explain_fault("GROWATT", "FC1=14") or "check the Growatt" in explain_fault("GROWATT", "FC1=14")
        assert "check the Growatt" in explain_fault("GROWATT", "FT=999")

    def test_fault_alert_message_carries_the_explanation(self):
        t0 = mx(13, 7, day=3)
        samples = [(t0 + dt.timedelta(minutes=5 * k), "SLP2", "JFM7DXN039", "FT=302") for k in range(10)]
        samples += [(t0 + dt.timedelta(minutes=5 * k), "SLP2", "JFM7DXN039", "0") for k in range(10, 40)]
        b = evaluate_inverter_faults(samples)
        assert b, "fault breach expected"
        c = candidate_from_fault_breach(b[0])
        assert "FT=302" in c.message and "no AC connection" in c.message



class TestV205Fixes:
    def test_empty_vendor_reply_is_a_gap_not_a_dark_plant(self):
        """MEX1 2026-09-05: FusionSolar answered with no values (power None)
        for hours -> plant_offline CRITICAL was mailed. None is not 0 W."""
        now = mx(10, 33)
        rows = [(now - dt.timedelta(minutes=5 * k), "MEX1", sn, None, None, 1, "0")
                for k in range(6) for sn in ("A", "B", "C")]
        assert [b for b in evaluate_acute(rows, ["MEX1"], now) if b.metric == "plant_offline"] == []
        zeros = [(t, p, sn, 0.0, tc, st, f) for t, p, sn, _pw, tc, st, f in rows]
        assert [b.metric for b in evaluate_acute(zeros, ["MEX1"], now)] == ["plant_offline"]

    def test_kpi_eod_does_not_fail_the_unit_on_a_dark_plant(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        tail = src[src.index("if plants_with_data == 0:"):]
        assert "return 2" in tail and "return 1" not in tail and tail.count("return 0") == 1
