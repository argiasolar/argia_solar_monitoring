"""v257 — the cost of an outage reaches the places a person reads it.

The pure arithmetic is covered in test_money.py; this file pins the
WIRING, which is what actually broke on 2026-09-16: a correct number
that never reaches a human is worth nothing."""
from __future__ import annotations

import datetime as dt
import pathlib
import sys

V2 = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(V2))
sys.path.insert(0, str(V2 / "scripts"))

from argia.analytics import acute as A, money as M   # noqa: E402

SNAP = (V2 / "scripts" / "alerts_snapshot.py").read_text(encoding="utf-8")
DAILY = (V2 / "scripts" / "daily_perf_mail.py").read_text(encoding="utf-8")
HUAWEI = (V2 / "argia" / "vendors" / "huawei.py").read_text(encoding="utf-8")


class TestTheDarkPlantAlertCarriesTheCost:
    def _dark(self, loss_note=None):
        now = dt.datetime(2026, 9, 16, 19, 30, tzinfo=dt.timezone.utc)   # 13:30 MX
        rows = [(now - dt.timedelta(minutes=m), "MEX1", sn, 0.0, 40.0, 1, "0")
                for m in (0, 5, 10) for sn in ("A", "B", "C")]
        return A.evaluate_acute(rows, ["MEX1"], now, loss_note=loss_note)

    def test_without_a_price_the_alert_is_exactly_as_before(self):
        msgs = [b.message for b in self._dark() if b.metric == "plant_offline"]
        assert msgs and "0 W at 13:30 MX [CRITICAL]" in msgs[0]
        assert "MXN" not in msgs[0]

    def test_with_a_price_the_alert_says_what_it_is_costing(self):
        note = M.loss_phrase(512.0, 2.508) + " so far today"
        msgs = [b.message for b in self._dark({"MEX1": note}) if b.metric == "plant_offline"]
        assert msgs, "the dark-plant alert must still fire"
        assert "512 kWh lost" in msgs[0] and "$1,284 MXN" in msgs[0]
        assert msgs[0].endswith("[CRITICAL]"), "severity stays at the end where readers look"

    def test_a_price_for_another_plant_never_leaks_into_this_one(self):
        msgs = [b.message for b in self._dark({"GTO1": "≈ 9,999 kWh lost — $99,999 MXN"})
                if b.metric == "plant_offline"]
        assert "99,999" not in msgs[0]


class TestTheSnapshotJobComputesIt:
    def test_the_loss_note_is_built_and_handed_to_the_evaluator(self):
        assert "def loss_notes(" in SNAP
        assert "loss_note=loss_notes(" in SNAP

    def test_it_reads_irradiance_nameplate_pr_and_tariff(self):
        blk = SNAP.split("def loss_notes(")[1].split("\ndef ")[0]
        for needed in ("irradiance_wm2", "kwp_dc", "pr_baseline", "tariff_mxn_per_kwh"):
            assert needed in blk, needed
        assert "lost_kwh_intraday" in blk and "loss_phrase" in blk

    def test_the_inverters_are_collapsed_per_timestamp_not_averaged(self):
        """v257 regression, caught on live data: averaging across
        inverter-samples multiplied every loss by the inverter count and
        claimed GTO1 had lost 2,118 kWh before 09:30 — more than the
        plant can make in a morning."""
        blk = SNAP.split("def loss_notes(")[1].split("\n@instrument")[0]
        assert "GROUP BY t.ts_utc, t.plant_key" in blk
        assert "sum(t.power_w) / 1000.0" in blk, "plant kW is the SUM of its inverters"
        assert "lost_kwh_intraday(samples" in blk, "the tested function does the maths"

    def test_a_missing_power_reading_is_not_turned_into_a_zero_in_sql(self):
        """Once SAG's datalogger dropped, power came back NULL. money.py
        treats None as no production; coalescing it to 0.0 in SQL would
        have hidden which plants were merely quiet."""
        blk = SNAP.split("def loss_notes(")[1].split("\n@instrument")[0]
        assert "coalesce(t.power_w" not in blk

    def test_a_pricing_failure_never_blocks_the_outage_alert(self):
        """An alert about a dead plant must go out even if the money
        lookup is down — that was the whole lesson of 2026-09-16."""
        blk = SNAP.split("def loss_notes(")[1].split("\ndef ")[0]
        assert "except Exception" in blk and "return {}" in blk

    def test_the_job_is_still_instrumented(self):
        """v257 regression: inserting the helper once pushed
        @instrument off main and onto the helper."""
        i = SNAP.index('@instrument("alerts_snapshot")')
        assert SNAP[i:].split("\n")[1].startswith("def main("), \
            "@instrument must sit directly on main()"

    def test_the_remail_clock_is_passed_in(self):
        assert "now_mx_hour=mx.hour" in SNAP


class TestTheDailyReportPricesTheDay:
    def test_it_gathers_and_passes_the_cost(self):
        assert "def gather_yesterday_cost(" in DAILY
        assert "cost=gather_yesterday_cost(today)" in DAILY

    def test_it_uses_the_stamped_expectation_not_a_model(self):
        blk = DAILY.split("def gather_yesterday_cost(")[1].split("\ndef ")[0]
        assert "expected_kwh" in blk and "lost_kwh_day" in blk
        # the closed day needs no model: no irradiance column, no intraday maths
        # (the docstring may mention irradiance — the CODE must not use it)
        assert "irradiance_wm2" not in blk and "lost_kwh_intraday" not in blk

    def test_both_the_text_and_the_html_show_it(self):
        assert "_lost_caption(data)" in DAILY
        assert "lost {_num(data.get('lost_kwh'))} kWh" in DAILY

    def test_a_day_that_met_expectation_says_nothing_about_money(self):
        sys.path.insert(0, str(V2 / "scripts"))
        import daily_perf_mail as D
        assert D._lost_caption({"lost_kwh": 0, "lost_mxn": None}) == "closed"
        assert D._lost_caption({}) == "closed"

    def test_a_lossy_day_is_priced_and_a_tariff_free_one_is_not(self):
        import daily_perf_mail as D
        assert D._lost_caption({"lost_kwh": 1439.7, "lost_mxn": 3610.76}) == \
            "closed — 1,440 kWh lost = $3,611 MXN"
        assert D._lost_caption({"lost_kwh": 500.0, "lost_mxn": None}) == \
            "closed — 500 kWh below expectation"


class TestTheHuaweiAlarmCallIsWired:
    def test_the_client_can_fetch_alarms_from_the_right_path(self):
        assert "def fetch_alarms(" in HUAWEI
        assert '"/getAlarmList"' in HUAWEI, "the leading slash matters — _post_json concatenates"

    def test_it_sends_what_the_api_actually_wants(self):
        blk = HUAWEI.split("def fetch_alarms(")[1].split("\n    def ")[0]
        for k in ("stationCodes", "beginTime", "endTime", "language"):
            assert k in blk, k
        assert "self.login()" in blk

    def test_a_failure_reply_yields_no_alarms_rather_than_raising(self):
        blk = HUAWEI.split("def fetch_alarms(")[1].split("\n    def ")[0]
        assert 'payload.get("success") is False' in blk and "return []" in blk
