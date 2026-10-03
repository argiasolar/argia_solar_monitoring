"""v302 - frozen (repeated) telemetry readings are not live data.

The rule (argia/telemetry/fresh.py) in Python, on the real pattern seen on
2 Oct 2026 (SAG's Huawei logger offline, FusionSolar repeating the last
values every 5 minutes), plus the cases that must NOT be called frozen."""
from __future__ import annotations

import datetime as dt

from argia.telemetry import fresh as F

T0 = dt.datetime(2026, 10, 2, 16, 25)


def at(minutes, p, e):
    return (T0 + dt.timedelta(minutes=minutes), p, e)


def test_the_2_october_pattern():
    # real readings, then the logger stops: the same 12,301 W / 514.7 kWh
    # come back every 5 minutes, with empty replies in between
    rows = [at(-10, 22827.0, 509.22), at(-5, 15000.0, 512.1), at(0, 12301.0, 514.7),
            at(5, 12301.0, 514.7), at(10, None, None), at(15, None, None),
            at(190, 12301.0, 514.7), at(220, 12301.0, 514.7)]
    assert F.repeats(rows) == [False, False, False, True, False, False, True, True]


def test_not_frozen_when_the_counter_moves():
    # held at the export limit: constant power, counter climbing - production
    rows = [at(0, 50000.0, 100.0), at(5, 50000.0, 104.2), at(10, 50000.0, 108.3)]
    assert F.repeats(rows) == [False, False, False]


def test_not_frozen_at_night_zero_or_tiny_power():
    assert F.repeats([at(0, 0.0, 514.7), at(5, 0.0, 514.7)]) == [False, False]          # 0 W at night is real
    # 300 W for 5 min = 0.025 kWh: a 0.1 kWh counter may legitimately not move
    assert F.repeats([at(0, 300.0, 10.0), at(5, 300.0, 10.0)]) == [False, False]
    assert F.REPEAT_MIN_KWH == 0.05


def test_needs_both_values_and_a_predecessor():
    assert F.is_repeat(None, at(0, 1000.0, 1.0)) is False
    assert F.is_repeat(at(0, 12301.0, None), at(5, 12301.0, None)) is False             # no counter: no claim
    assert F.is_repeat(at(0, 12301.0, 514.7), at(5, 12302.0, 514.7)) is False           # power changed
    assert F.is_repeat(at(0, 12301.0, 514.7), at(5, 12301.0, 514.8)) is False           # counter moved


def test_sql_fragments_name_the_rule():
    cte = F.repeat_cte("now() - interval '2 days'")
    assert cte.startswith("rep AS (") and "PARTITION BY plant_key, inverter_sn ORDER BY ts_utc" in cte
    assert "power_w IS NOT NULL" in cte and F.REPEAT_SQL in cte
    assert F.not_repeat("t") == ("NOT EXISTS (SELECT 1 FROM rep WHERE rep.plant_key = t.plant_key"
                                 " AND rep.inverter_sn = t.inverter_sn AND rep.ts_utc = t.ts_utc)")
