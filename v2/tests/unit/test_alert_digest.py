"""Anti-noise harness (2026-08-27): severity-aware send policy.

The first server night sent 13 mails, mostly WARNING churn. Policy:
CRITICAL interrupts; WARNING rides one daily digest; WARN recoveries
are silent.
"""

import datetime as dt

from argia.alerts.monitor import (
    Alert,
    SEV_CRIT,
    SEV_WARN,
    plan_sends,
    recoveries_to_mail,
)
from argia.recon.engine import effective_completeness

NOW = dt.datetime(2026, 8, 27, 13, 7, tzinfo=dt.timezone.utc)


def _crit(k):
    return Alert(k, SEV_CRIT, k, k)


def _warn(k):
    return Alert(k, SEV_WARN, k, k)


def test_new_critical_mails_any_tick():
    send, _ = plan_sends([_crit("c")], {}, NOW, warn_digest=False)
    assert [a.key for a in send] == ["c"]


def test_new_warning_waits_for_digest():
    send, _ = plan_sends([_warn("w")], {}, NOW, warn_digest=False)
    assert send == []
    send, _ = plan_sends([_warn("w")], {}, NOW, warn_digest=True)
    assert [a.key for a in send] == ["w"]


def test_tracked_never_mailed_warning_makes_first_digest():
    # appeared overnight: persisted active, last_sent NULL (ever_sent=False)
    st = {"w": (NOW - dt.timedelta(hours=5), True, False)}
    send, _ = plan_sends([_warn("w")], st, NOW, warn_digest=True)
    assert [a.key for a in send] == ["w"]


def test_warning_resends_daily_not_6h():
    sent_8h_ago = {"w": (NOW - dt.timedelta(hours=8), True, True)}
    send, _ = plan_sends([_warn("w")], sent_8h_ago, NOW, warn_digest=True)
    assert send == []                       # 8h < 24h: not even on digest
    sent_25h_ago = {"w": (NOW - dt.timedelta(hours=25), True, True)}
    send, _ = plan_sends([_warn("w")], sent_25h_ago, NOW, warn_digest=True)
    assert [a.key for a in send] == ["w"]


def test_critical_keeps_6h_resend():
    old = {"c": (NOW - dt.timedelta(hours=7), True, True)}
    send, _ = plan_sends([_crit("c")], old, NOW, warn_digest=False)
    assert [a.key for a in send] == ["c"]


def test_legacy_behavior_when_policy_off():
    # warn_digest=None: WARNs behave like before (old tests keep passing)
    send, _ = plan_sends([_warn("w")], {}, NOW)
    assert [a.key for a in send] == ["w"]


def test_all_recoveries_returned_but_only_crit_mailed():
    state = {"warn-k": (NOW, True, True), "crit-k": (NOW, True, True)}
    _, recovered = plan_sends([], state, NOW, warn_digest=False)
    assert recovered == ["crit-k", "warn-k"]      # both flip inactive
    mail = recoveries_to_mail(recovered,
                              {"warn-k": SEV_WARN, "crit-k": SEV_CRIT})
    assert mail == ["crit-k"]
    # unknown severity mails (safe side)
    assert recoveries_to_mail(["x"], {}) == ["x"]


# --------------------------- recon completeness scaling (GTO2 round 2)
def test_effective_completeness_scales_by_silent_inverters():
    assert effective_completeness(100.0, 3, 4) == 75.0
    assert effective_completeness(64.25, 4, 4) == 64.25
    assert effective_completeness(100.0, 0, 4) == 0.0


def test_effective_completeness_honest_about_missing_inputs():
    assert effective_completeness(None, 3, 4) is None
    assert effective_completeness(88.0, None, 4) == 88.0
    assert effective_completeness(88.0, 3, 0) == 88.0
    assert effective_completeness(88.0, 5, 4) == 88.0   # clamped to 1.0


# ---------------------------------------------------------------- v202
# Anti-noise round 2 (Tomasz 2026-09-05: "meaningless notifications").

def _st(ts, active=True, ever_sent=True, first_seen=None):
    return (ts, active, ever_sent, first_seen if first_seen is not None else ts)


def test_flappy_keys_wait_for_a_second_sighting():
    # NL2 datalogger hiccup: "stale 53 min" seen once -> no mail yet
    send, _ = plan_sends([_crit("plant-stale:NL2")], {}, NOW, warn_digest=False)
    assert send == []
    # a unit that failed once, a silent inverter at 08:07: same
    for k in ("unit-failed:argia-telemetry.service", "inverter-silent:GTO2:X",
              "recon-fail:GTO2:2026-09-04", "cfe-probe"):
        assert plan_sends([_crit(k)], {}, NOW, warn_digest=False)[0] == [], k
    # seen at the previous tick (persisted active, never mailed) -> mails now
    st = {"plant-stale:NL2": _st(NOW - dt.timedelta(minutes=30), ever_sent=False)}
    send, _ = plan_sends([_crit("plant-stale:NL2")], st, NOW, warn_digest=False)
    assert [a.key for a in send] == ["plant-stale:NL2"]
    # re-activation after a recovery counts as a first sighting again
    st = {"plant-stale:NL2": _st(NOW - dt.timedelta(hours=9), active=False)}
    assert plan_sends([_crit("plant-stale:NL2")], st, NOW, warn_digest=False)[0] == []


def test_plant_dark_disk_and_postgres_still_mail_on_sight():
    for k in ("plant-dark:TAM1", "disk-full", "postgres-down"):
        assert [a.key for a in plan_sends([_crit(k)], {}, NOW, warn_digest=False)[0]] == [k]


def test_long_running_critical_resends_daily():
    # TAM1 dark since 3 days: last mail 7 h ago used to re-mail; now once a day
    st = {"plant-dark:TAM1": _st(NOW - dt.timedelta(hours=7),
                                 first_seen=NOW - dt.timedelta(days=3))}
    assert plan_sends([_crit("plant-dark:TAM1")], st, NOW, warn_digest=False)[0] == []
    st = {"plant-dark:TAM1": _st(NOW - dt.timedelta(hours=25),
                                 first_seen=NOW - dt.timedelta(days=3))}
    assert [a.key for a in plan_sends([_crit("plant-dark:TAM1")], st, NOW,
                                      warn_digest=False)[0]] == ["plant-dark:TAM1"]
    # first day: the 6 h cadence is unchanged
    st = {"plant-dark:TAM1": _st(NOW - dt.timedelta(hours=7),
                                 first_seen=NOW - dt.timedelta(hours=10))}
    assert len(plan_sends([_crit("plant-dark:TAM1")], st, NOW, warn_digest=False)[0]) == 1


def test_recovery_rules():
    sev = {"plant-stale:NL2": SEV_CRIT, "plant-dark:TAM1": SEV_CRIT}
    # never mailed -> never a recovery mail
    assert recoveries_to_mail(["plant-stale:NL2"], sev, mailed_keys=set()) == []
    # mailed, but the outage lasted 1 h and nothing else goes out -> silent
    assert recoveries_to_mail(["plant-stale:NL2"], sev, mailed_keys={"plant-stale:NL2"},
                              active_hours={"plant-stale:NL2": 1.0}) == []
    # ... rides along when a mail goes out anyway
    assert recoveries_to_mail(["plant-stale:NL2"], sev, mailed_keys={"plant-stale:NL2"},
                              active_hours={"plant-stale:NL2": 1.0},
                              with_alerts=True) == ["plant-stale:NL2"]
    # a 3-day outage ending earns its own mail
    assert recoveries_to_mail(["plant-dark:TAM1"], sev, mailed_keys={"plant-dark:TAM1"},
                              active_hours={"plant-dark:TAM1": 72.0}) == ["plant-dark:TAM1"]
    # legacy call shape unchanged
    assert recoveries_to_mail(["plant-dark:TAM1"], sev) == ["plant-dark:TAM1"]


def test_mailer_wires_the_new_state_shape():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "alert_mailer.py"
           ).read_text(encoding="utf-8")
    assert "first_seen = CASE WHEN alert_state.active" in src        # re-activation resets
    assert "mailed_keys=mailed_keys" in src and "with_alerts=bool(to_send)" in src
    assert "coalesce(severity, 'CRITICAL'), first_seen" in src
