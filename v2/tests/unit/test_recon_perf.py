"""Unit tests — argia.recon.perf (PR_STC)."""

from argia.recon.perf import pr_stc


def test_hot_day_corrects_upward():
    # 55°C cells, gamma -0.34%/°C: raw 0.78 is really ~0.87 at STC
    v = pr_stc(0.78, 55.0, -0.0034)
    assert v is not None and 0.86 < v < 0.88


def test_stc_temperature_is_identity():
    assert pr_stc(0.80, 25.0, -0.0034) == 0.80


def test_cold_day_corrects_downward():
    v = pr_stc(0.85, 10.0, -0.0034)
    assert v is not None and v < 0.85


def test_missing_inputs_are_none():
    assert pr_stc(None, 50.0, -0.0034) is None
    assert pr_stc(0.8, None, -0.0034) is None
    assert pr_stc(0.8, 50.0, None) is None


def test_implausible_inputs_are_none():
    assert pr_stc(0.8, 200.0, -0.0034) is None      # broken temp sensor
    assert pr_stc(0.8, 50.0, 0.0034) is None        # positive gamma = typo
    assert pr_stc(0.8, 50.0, -0.5) is None          # absurd coefficient
