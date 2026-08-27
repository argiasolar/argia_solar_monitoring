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
