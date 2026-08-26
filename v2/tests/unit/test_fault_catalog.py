"""Unit tests — argia.alerts.fault_catalog."""

from argia.alerts.fault_catalog import explain_fault, is_normal_state


def test_empty_and_zero_are_none():
    assert explain_fault("GROWATT", "") is None
    assert explain_fault("HUAWEI", "0") is None
    assert explain_fault("", None) is None


def test_huawei_documented_states():
    assert explain_fault("HUAWEI", "IS=512,RS=1") == \
        "on-grid (normal operation), grid-connected"
    assert "fault" in explain_fault("HUAWEI", "IS=768,RS=0").lower()
    assert "communication" in explain_fault("HUAWEI", "IS=771,RS=0")


def test_huawei_unknown_state_is_honest():
    out = explain_fault("HUAWEI", "IS=9999,RS=1")
    assert "not in catalog" in out and "9999" in out


def test_solaredge_modes():
    assert "throttled" in explain_fault("SOLAREDGE", "MODE=THROTTLED")
    assert "dusk" in explain_fault("SOLAREDGE", "MODE=SHUTTING_DOWN")
    assert "FAULT" in explain_fault("SOLAREDGE", "MODE=FAULT")


def test_growatt_points_to_vendor_portal():
    out = explain_fault("GROWATT", "24")
    assert "Growatt" in out and "24" in out


def test_is_normal_state():
    assert is_normal_state("HUAWEI", "IS=512,RS=1")
    assert is_normal_state("SOLAREDGE", "MODE=MPPT")
    assert is_normal_state("GROWATT", "0")
    assert not is_normal_state("HUAWEI", "IS=768,RS=0")
    assert not is_normal_state("SOLAREDGE", "MODE=FAULT")
    assert not is_normal_state("GROWATT", "24")
