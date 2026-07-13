"""Tests for the SHARED inverter status classifier (argia.analytics.status).

Consolidation contract: this module is the only status authority. It must
(a) reproduce the dashboard's state-machine semantics, (b) delegate fault
semantics to vendor_flags.fault_tokens (string summaries; Huawei state
tokens benign), and (c) delegate peer judgement to inverter_health's
per-kW, leave-one-out detector (GTO1 MWKNE9500D false-positive fix).
"""


from argia.analytics import status as S
from argia.analytics.status import InverterBucket, classify_plant_bucket


def b(sn, e, reported=True, status_flag=1, fault_code="0", derate=None,
      rated=None):
    return InverterBucket(inverter_sn=sn, energy_kwh=e, reported=reported,
                          status_flag=status_flag, fault_code=fault_code,
                          derating_mode=derate, rated_kw=rated)


def one(bucket, *, sun_up=True, peers=()):
    res = classify_plant_bucket([bucket, *peers], plant_key="P",
                                sun_up=sun_up)
    return res[bucket.inverter_sn]


# --- vendor fault semantics (delegated to vendor_flags) ----------------------

class TestVendorFault:
    def test_status_flag_3_is_fault(self):
        assert S.is_vendor_fault(3, "0") is True

    def test_growatt_fault_token_is_fault(self):
        assert S.is_vendor_fault(1, "FT=302") is True

    def test_huawei_state_tokens_are_benign(self):
        """The 2026-07-03 MEX lesson: IS=/RS= are STATE, not faults."""
        assert S.is_vendor_fault(1, "IS=512,RS=1") is False
        assert S.is_vendor_fault(1, "IS=40960,RS=1") is False

    def test_huawei_devstatus_is_fault(self):
        assert S.is_vendor_fault(1, "DS=1") is True

    def test_none_and_zero_are_healthy(self):
        assert S.is_vendor_fault(1, None) is False
        assert S.is_vendor_fault(1, "0") is False
        assert S.is_vendor_fault(None, "0.0") is False

    def test_regression_numeric_coercion_bug(self):
        """The dashboard's old numeric coercion turned "FT=302" into None,
        silencing the vendor-fault channel entirely. Never again."""
        st, reason = one(b("A", 5.0, fault_code="FT=302"),
                         peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.FAULT
        assert "FT=302" in reason


# --- state machine ------------------------------------------------------------

class TestStateMachine:
    def test_healthy_is_online(self):
        st, _ = one(b("A", 100.0), peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.ONLINE

    def test_online_while_zero_is_offline(self):
        """SAG-Mexico regression: producing 0 with peers producing."""
        st, reason = one(b("A", 0.0), peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.OFFLINE
        assert "peers" in reason

    def test_zero_when_nobody_produces_is_idle(self):
        st, _ = one(b("A", 0.0), peers=(b("B", 0.0),))
        assert st == S.IDLE_NIGHT

    def test_silent_in_daylight_is_offline(self):
        st, _ = one(b("A", 0.0, reported=False), peers=(b("B", 100.0),))
        assert st == S.OFFLINE

    def test_silent_at_night_with_plant_context_is_idle(self):
        st, _ = one(b("A", 0.0, reported=False), sun_up=False,
                    peers=(b("B", 0.0),))
        assert st == S.IDLE_NIGHT

    def test_whole_plant_silent_is_no_data(self):
        res = classify_plant_bucket(
            [b("A", 0.0, reported=False), b("B", 0.0, reported=False)],
            plant_key="P", sun_up=False)
        assert res["A"][0] == S.NO_DATA and res["B"][0] == S.NO_DATA

    def test_fault_beats_everything(self):
        st, _ = one(b("A", 1.0, status_flag=3), peers=(b("B", 100.0),))
        assert st == S.FAULT

    def test_derated_when_flag_set(self):
        st, _ = one(b("A", 100.0, derate=1), peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.DERATED

    def test_night_reported_is_idle(self):
        st, _ = one(b("A", 0.0), sun_up=False, peers=(b("B", 0.0),))
        assert st == S.IDLE_NIGHT


# --- peer judgement (delegated to inverter_health) ------------------------------

class TestPeerJudgement:
    def test_underperforming_below_85pct(self):
        st, reason = one(b("A", 50.0), peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.UNDERPERFORMING
        assert "50%" in reason and "CRITICAL" in reason  # 50% < 70% crit

    def test_warning_band_labelled(self):
        st, reason = one(b("A", 80.0), peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.UNDERPERFORMING and "WARNING" in reason

    def test_healthy_at_90pct(self):
        st, _ = one(b("A", 90.0), peers=(b("B", 100.0), b("C", 100.0)))
        assert st == S.ONLINE

    def test_regression_smaller_inverter_not_falsely_flagged(self):
        """GTO1 MWKNE9500D: 60 kW among 124 kW peers. Raw comparison reads a
        healthy unit at ~48% of peers; per-kW it is 100%. The shared
        classifier must use nameplate normalization."""
        small = b("MWKNE9500D", 60.0, rated=60.0)
        peers = tuple(b(f"J{i}", 124.0, rated=124.0) for i in range(4))
        st, _ = one(small, peers=peers)
        assert st == S.ONLINE

    def test_smaller_inverter_still_flagged_when_truly_weak(self):
        small = b("MWKNE9500D", 30.0, rated=60.0)   # 50% per-kW
        peers = tuple(b(f"J{i}", 124.0, rated=124.0) for i in range(4))
        st, reason = one(small, peers=peers)
        assert st == S.UNDERPERFORMING

    def test_dead_peer_does_not_mask_a_laggard(self):
        """Peer median (not mean): real GTO1 2026-06-28 case."""
        lagging = b("L", 462.0)
        peers = (b("A", 829.0), b("D", 0.0, status_flag=3, fault_code="FT=302"),
                 b("C", 773.0))
        res = classify_plant_bucket([lagging, *peers], plant_key="P",
                                    sun_up=True)
        assert res["L"][0] == S.UNDERPERFORMING
        assert res["D"][0] == S.FAULT
        assert res["A"][0] == S.ONLINE

    def test_single_inverter_plant_never_underperforming(self):
        st, _ = one(b("A", 10.0))
        assert st == S.ONLINE


class TestDisplayStatusRecovery:
    """v96: an inverter that was OFFLINE/FAULT earlier today but is
    producing in its latest bucket reads RECOVERED, not a stale OFFLINE.
    This is the tested reference the dashboard JS mirrors."""

    def test_offline_then_producing_is_recovered(self):
        assert S.display_status(S.OFFLINE, S.ONLINE) == S.RECOVERED
        assert S.display_status(S.OFFLINE, S.UNDERPERFORMING) == S.RECOVERED
        assert S.display_status(S.OFFLINE, S.DERATED) == S.RECOVERED

    def test_fault_then_producing_is_recovered(self):
        assert S.display_status(S.FAULT, S.ONLINE) == S.RECOVERED

    def test_still_down_stays_down(self):
        assert S.display_status(S.OFFLINE, S.OFFLINE) == S.OFFLINE
        assert S.display_status(S.FAULT, S.FAULT) == S.FAULT
        # latest bucket not a producing state -> not recovered
        assert S.display_status(S.OFFLINE, S.IDLE_NIGHT) == S.OFFLINE
        assert S.display_status(S.OFFLINE, S.NO_DATA) == S.OFFLINE

    def test_healthy_and_soft_states_pass_through(self):
        assert S.display_status(S.ONLINE, S.ONLINE) == S.ONLINE
        assert S.display_status(S.UNDERPERFORMING, S.ONLINE) == \
            S.UNDERPERFORMING   # only hard-down recovers
        assert S.display_status(S.DERATED, S.ONLINE) == S.DERATED
