"""v310: availability from evidence (argia/kpi/availability.py).

Tomasz, 2026-10-04, on the 30-day availability column: "double check it if
it was really the case ... we have to deliver solid results". Recomputed on
production: every "down" day of the PPA plants in September produced 97-123 %
of its expected energy; the old slot measure collapsed a 5-minute-polled day
into 1 to 4 slots, so one short connection gap cost an inverter a third of
its day, and the stored values did not reproduce from the same data. These
tests pin the new rule on the cases found that night.
"""
from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from argia.kpi import availability as A

V2 = pathlib.Path(__file__).resolve().parents[2]
DAY = dt.date(2026, 10, 2)
MTY = (25.67, -100.31)                      # Monterrey


def ts(h, m):
    """MX local time on DAY -> aware UTC."""
    return (dt.datetime.combine(DAY, dt.time(h, m)) - A.MX_OFFSET).replace(tzinfo=dt.timezone.utc)


def feed(sn, kw=50.0, start=(6, 0), end=(20, 0), every=5, power=None, status=1, gaps=(), kwh_per_h=None):
    """5-minute readings for one inverter. ``power(h, m)`` -> W (default: kw
    while the sun is up, 0 at night); ``gaps``: [((h,m),(h,m))] without
    readings; the day counter grows with the power whether or not a reading
    is sent (that is what a datalogger outage looks like)."""
    out, e = [], 0.0
    t = dt.datetime.combine(DAY, dt.time(*start))
    stop = dt.datetime.combine(DAY, dt.time(*end))
    while t < stop:
        h, m = t.hour, t.minute
        sun = A.sun_elevation(*MTY, t) > 3
        p = power(h, m) if power else (kw * 1000 * 0.6 if sun else 0.0)
        e += p / 1000 * every / 60
        in_gap = any(dt.time(*a) <= t.time() < dt.time(*b) for a, b in gaps)
        if not in_gap:
            out.append((ts(h, m), sn, status, p, round(e, 2)))
        t += dt.timedelta(minutes=every)
    return out


INV = [("A", 50.0), ("B", 50.0), ("C", 50.0), ("D", 50.0)]


def run(samples, inverters=INV, expected=None, latlon=MTY):
    return A.compute(samples, inverters, DAY, latlon[0], latlon[1], expected_kwh=expected)


class TestTheOldMeasureWasBroken:
    def test_one_short_gap_cost_a_third_of_the_day(self):
        """Plastic Omnium, 2 Oct: stored 55 %, the plant made 123 % of expected."""
        from argia.archive.kpi_daily import compute_availability
        # two missed polls around one partial poll (only D answered at 12:20) -
        # the old measure cut the day into three "slots" and A, B, C lost one
        gap = [((12, 0), (12, 20)), ((12, 25), (12, 40))]
        s = (feed("A", gaps=[((12, 0), (12, 40))]) + feed("B", gaps=[((12, 0), (12, 40))])
             + feed("C", gaps=[((12, 0), (12, 40))]) + feed("D", gaps=gap))
        old = compute_availability([(t, sn, st) for t, sn, st, _p, _e in s], ["A", "B", "C", "D"])
        assert old == pytest.approx(0.75, abs=1e-4)        # a 40-minute polling hiccup = a quarter of the day
        new = run(s)
        assert new.availability == 1.0 and new.coverage == 1.0   # the day counters prove they produced


class TestStates:
    def test_healthy_day(self):
        r = run(feed("A") + feed("B") + feed("C") + feed("D"))
        assert (r.availability, r.coverage, r.down_steps, r.unknown_steps) == (1.0, 1.0, {}, {})
        assert 30 <= r.steps <= 44                         # the sunny part of an October day, not 06-20

    def test_producing_with_a_fault_code_is_available(self):
        """Hirschmann: one inverter reports status 3 while producing a third of its peers -
        underperformance (PR, losses), not unavailability."""
        weak = feed("A", status=3, power=lambda h, m: 10000.0 if 7 <= h < 19 else 0.0)
        r = run(weak + feed("B") + feed("C") + feed("D"))
        assert r.availability == 1.0 and r.down_steps == {}

    def test_zero_watts_in_sunshine_is_down(self):
        """Ryder inverter 3, 2 Oct: fault, 0 W all day while the others produced."""
        dead = feed("C", status=3, power=lambda h, m: 0.0)
        r = run(feed("A") + feed("B") + dead + feed("D"))
        assert r.availability == pytest.approx(0.75, abs=1e-4)
        assert r.down_steps == {"C": r.steps} and r.coverage == 1.0

    def test_a_stale_zero_with_a_growing_counter_is_not_a_stop(self):
        """MEX1 class: the feed says 0 W but the day counter keeps climbing."""
        s = []
        for t, sn, st, p, e in feed("A"):
            loc = t - dt.timedelta(hours=6)
            s.append((t, sn, st, 0.0 if 11 <= loc.hour < 13 else p, e))
        r = run(s, inverters=[("A", 50.0)])
        assert r.availability == 1.0

    def test_a_real_plant_stop_counts(self):
        """SAG, 16 Sep: all inverters at 0 W for ~3 h, counters flat, 55 % of expected."""
        stop = lambda h, m: 0.0 if 11 <= h < 14 else (30000.0 if 7 <= h < 19 else 0.0)
        r = run(feed("A", power=stop) + feed("B", power=stop))
        assert 0.6 < r.availability < 0.85 and set(r.down_steps) == {"A", "B"}

    def test_dawn_and_dusk_are_not_judged(self):
        """GTO1's small inverter wakes 25 minutes after its peers - not downtime."""
        late = feed("A", start=(7, 30), end=(18, 30))
        r = run(late + feed("B"), inverters=[("A", 60.0), ("B", 136.0)])
        assert r.availability == 1.0 and r.coverage == 1.0


class TestUnknownTime:
    def test_silence_proven_by_the_counter_is_available(self):
        """NL1, 2 Oct: readings missing for hours, the day counters match the peers."""
        s = feed("A", gaps=[((9, 0), (13, 0))]) + feed("B", gaps=[((9, 0), (13, 0))]) + feed("C") + feed("D")
        r = run(s, expected=1000.0)
        assert r.availability == 1.0 and r.coverage == 1.0 and set(r.proven) == {"A", "B"}

    def test_silent_and_never_back_stays_unknown(self):
        """Ryder inverter 2: went silent at 11:31 while producing and never came back -
        not proven down, not proven up: left out, and the coverage says so."""
        r = run(feed("A", end=(11, 31)) + feed("B") + feed("C") + feed("D"))
        assert r.availability == 1.0
        assert r.coverage < 0.95 and "A" in r.unknown_steps and "A" not in r.proven

    def test_a_plant_wide_silence_needs_the_plant_energy(self):
        """A grid loss also kills the datalogger: every inverter silent at once and
        equal counters prove nothing unless the plant made its expected energy."""
        gap = [((10, 0), (14, 0))]
        s = sum((feed(sn, gaps=gap) for sn, _ in INV), [])
        made = sum(max(e for _t, sn2, _s, _p, e in s if sn2 == sn) for sn, _ in INV)
        weak = run(s, expected=made / 0.80)                  # plant at 80 % of expected
        assert weak.proven == () and weak.coverage < 1.0 and weak.availability == 1.0
        fine = run(s, expected=made)                          # plant at 100 % of expected
        assert set(fine.proven) == {"A", "B", "C", "D"} and fine.coverage == 1.0

    def test_a_configured_inverter_that_never_reports_is_uncovered_time(self):
        r = run(feed("A") + feed("B") + feed("C"))
        assert r.availability == 1.0 and r.coverage == pytest.approx(0.75, abs=1e-4)
        assert r.unknown_steps == {"D": r.steps}

    def test_a_dark_day_is_no_data_not_zero(self):
        r = run([])
        assert r.availability is None and r.coverage == 0.0

    def test_counter_below_peers_does_not_prove(self):
        # silent 09-19 AND really stopped then (the counter stayed flat)
        stop = lambda h, m: 0.0 if 9 <= h < 19 else (30000.0 if 7 <= h < 19 else 0.0)
        s = feed("A", power=stop, gaps=[((9, 0), (19, 0))]) + feed("B") + feed("C") + feed("D")
        r = run(s)
        assert "A" not in r.proven and "A" in r.unknown_steps


class TestWeightsAndHelpers:
    def test_rated_kw_weights_the_inverters(self):
        dead = feed("S", power=lambda h, m: 0.0)
        r = run(feed("L") + dead, inverters=[("L", 150.0), ("S", 50.0)])
        assert r.availability == pytest.approx(0.75, abs=1e-4)

    def test_no_coordinates_falls_back_to_8_to_17(self):
        steps = A.judged_steps(DAY, None, None)
        assert len(steps) == 36 and steps[0] == 32

    def test_sun_elevation(self):
        noon = A.sun_elevation(21.12, -101.68, dt.datetime(2026, 10, 2, 13, 0))   # León, ~13:00 MX solar noon
        assert 60 < noon < 75
        assert A.sun_elevation(21.12, -101.68, dt.datetime(2026, 10, 2, 0, 0)) < 0

    def test_window(self):
        assert A.window([], 30) == (None, 0.0)
        assert A.window([(1.0, None)] * 30, 30) == (1.0, 1.0)            # legacy rows: fully covered
        assert A.window([(1.0, 1.0)] * 21, 30) == (1.0, 0.7)              # 9 dark days: data 70 %
        av, cov = A.window([(1.0, 1.0), (0.5, 0.5)], 2)
        assert av == pytest.approx(0.8333, abs=1e-4) and cov == 0.75


class TestWiring:
    def test_kpi_eod_uses_it_and_stamps_the_coverage(self):
        src = (V2 / "scripts" / "kpi_eod.py").read_text(encoding="utf-8")
        assert "from argia.kpi.availability import compute as plant_availability" in src
        assert "r.power_w, r.etoday_kwh" in src and "expected_kwh=exp" in src
        assert 'stamp_column(sheets, "avail_coverage", cov_stamps' in src
        assert "compute_availability(" not in src

    def test_the_column_exists_everywhere(self):
        from argia.store.kpi_mirror import COLMAP, NUMERIC
        assert COLMAP["avail_coverage"] == "avail_coverage" and "avail_coverage" in NUMERIC
        assert "avail_coverage numeric(6,4)," in (V2 / "tests/fixtures/pg/schema.sql").read_text(encoding="utf-8")
        assert "avail_coverage" in (V2 / "server/bundle/schema.sql").read_text(encoding="utf-8")
        assert "ADD COLUMN IF NOT EXISTS avail_coverage numeric(6,4)" in A.ENSURE_SQL

    def test_recompute_writes_only_the_two_columns(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("avr", V2 / "scripts" / "availability_recompute.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sql = m.update_sql("TAM1", "2026-10-02", A.AvailabilityDay(0.6667, 0.75, 38))
        assert sql == ("UPDATE daily_production SET availability = 0.6667, avail_coverage = 0.7500"
                       " WHERE plant_key = 'TAM1' AND prod_date = DATE '2026-10-02';")
        assert "availability = NULL" in m.update_sql("NL2", "2026-09-30", A.AvailabilityDay(None, 0.0, 38))

    def test_readers_weight_by_coverage(self):
        mon = (V2 / "server" / "monitoring_gen.py").read_text(encoding="utf-8")
        assert '" avail_coverage"' in mon and "_avail_window(pairs, owed)" in mon
        rg = (V2 / "server" / "bundle" / "report_gen.py").read_text(encoding="utf-8")
        assert "const CV=" in rg and "avw+=a*c;avk+=c;" in rg
