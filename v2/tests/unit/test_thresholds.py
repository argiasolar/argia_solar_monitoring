"""Tests for argia.core.thresholds."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from argia.core.thresholds import (
    DEFAULT_THRESHOLDS,
    KNOWN_METRICS,
    THRESHOLDS_HEADER,
    VALID_CHANNELS,
    Condition,
    Severity,
    Threshold,
    ThresholdSet,
    load_thresholds,
)


# ============================================================
# Constants
# ============================================================


class TestConstants:
    def test_header_has_9_cols(self):
        assert len(THRESHOLDS_HEADER) == 9

    def test_known_metrics_includes_core_set(self):
        # If you remove a metric, you must update the rest of the codebase
        # that depends on it. This test pins the set.
        assert "inverter_offline" in KNOWN_METRICS
        assert "inverter_relative" in KNOWN_METRICS
        assert "pr_daily" in KNOWN_METRICS
        assert "plant_offline" in KNOWN_METRICS
        assert "data_stale" in KNOWN_METRICS

    def test_valid_channels(self):
        assert VALID_CHANNELS == frozenset({"sheet", "email", "slack"})

    def test_default_thresholds_all_have_9_cols(self):
        for row in DEFAULT_THRESHOLDS:
            assert len(row) == 9, f"Row has {len(row)} cols, not 9: {row}"

    def test_default_thresholds_use_known_metrics(self):
        for row in DEFAULT_THRESHOLDS:
            metric = row[1]
            assert metric in KNOWN_METRICS, f"Unknown metric in defaults: {metric}"

    def test_default_thresholds_use_valid_severities(self):
        for row in DEFAULT_THRESHOLDS:
            severity = row[2]
            assert severity in {"INFO", "WARNING", "CRITICAL"}, \
                f"Unknown severity: {severity}"


# ============================================================
# load_thresholds — basic parsing
# ============================================================


def _row(plant_key="ALL", metric="pr_daily", severity="WARNING",
         condition="below", value="0.75", duration_min="-",
         enabled="TRUE", channels="sheet", notes=""):
    return {
        "plant_key": plant_key, "metric": metric, "severity": severity,
        "condition": condition, "value": value, "duration_min": duration_min,
        "enabled": enabled, "channels": channels, "notes": notes,
    }


def _mock_sheets(rows):
    sheets = MagicMock()
    sheets.read_table.return_value = rows
    return sheets


class TestLoadBasic:
    def test_empty_returns_empty_set(self):
        sheets = _mock_sheets([])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_single_valid_row(self):
        sheets = _mock_sheets([_row()])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 1
        t = ts.all_thresholds[0]
        assert t.plant_key == "ALL"
        assert t.metric == "pr_daily"
        assert t.severity == Severity.WARNING
        assert t.condition == Condition.BELOW
        assert t.value == 0.75
        assert t.enabled is True
        assert t.channels == frozenset({"sheet"})

    def test_disabled_row_loads_but_marked(self):
        sheets = _mock_sheets([_row(enabled="FALSE")])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 1
        assert ts.all_thresholds[0].enabled is False

    def test_truthy_variants(self):
        for truthy in ["TRUE", "true", "yes", "Y", "1", "x"]:
            sheets = _mock_sheets([_row(enabled=truthy)])
            ts = load_thresholds(sheets)
            assert ts.all_thresholds[0].enabled is True, f"{truthy!r} should be truthy"

    def test_falsy_variants(self):
        for falsy in ["FALSE", "false", "no", "0", ""]:
            sheets = _mock_sheets([_row(enabled=falsy)])
            ts = load_thresholds(sheets)
            assert ts.all_thresholds[0].enabled is False, f"{falsy!r} should be falsy"


class TestLoadValidation:
    def test_unknown_metric_skipped(self):
        sheets = _mock_sheets([_row(metric="totally_made_up_metric")])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_invalid_severity_skipped(self):
        sheets = _mock_sheets([_row(severity="MEDIUM")])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_invalid_condition_skipped(self):
        sheets = _mock_sheets([_row(condition="approximately")])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_missing_plant_key_skipped(self):
        sheets = _mock_sheets([_row(plant_key="")])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_missing_metric_skipped(self):
        sheets = _mock_sheets([_row(metric="")])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_duration_with_zero_duration_min_skipped(self):
        """A duration condition with duration_min=0 would never fire —
        must be filtered as malformed."""
        sheets = _mock_sheets([_row(
            metric="inverter_offline", condition="duration",
            value="0", duration_min="0",
        )])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0

    def test_duration_with_negative_duration_min_skipped(self):
        sheets = _mock_sheets([_row(
            metric="inverter_offline", condition="duration",
            duration_min="-30",
        )])
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == 0


class TestChannelsParsing:
    def test_single_channel(self):
        sheets = _mock_sheets([_row(channels="email")])
        ts = load_thresholds(sheets)
        assert ts.all_thresholds[0].channels == frozenset({"email"})

    def test_multiple_channels(self):
        sheets = _mock_sheets([_row(channels="sheet,email,slack")])
        ts = load_thresholds(sheets)
        assert ts.all_thresholds[0].channels == frozenset({"sheet", "email", "slack"})

    def test_channels_whitespace_stripped(self):
        sheets = _mock_sheets([_row(channels=" sheet , email ")])
        ts = load_thresholds(sheets)
        assert ts.all_thresholds[0].channels == frozenset({"sheet", "email"})

    def test_unknown_channel_dropped(self):
        sheets = _mock_sheets([_row(channels="sheet,carrier_pigeon,email")])
        ts = load_thresholds(sheets)
        # Pigeon dropped, others kept
        assert ts.all_thresholds[0].channels == frozenset({"sheet", "email"})

    def test_empty_channels(self):
        sheets = _mock_sheets([_row(channels="")])
        ts = load_thresholds(sheets)
        assert ts.all_thresholds[0].channels == frozenset()


# ============================================================
# ThresholdSet lookup
# ============================================================


class TestLookup:
    def _ts(self, *rows):
        sheets = _mock_sheets(list(rows))
        return load_thresholds(sheets)

    def test_get_returns_specific_when_present(self):
        ts = self._ts(
            _row(plant_key="ALL", value="0.75"),
            _row(plant_key="QRO1", value="0.70"),
        )
        result = ts.get("QRO1", "pr_daily", Severity.WARNING)
        assert result is not None
        assert result.value == 0.70  # plant-specific wins

    def test_get_falls_back_to_all(self):
        ts = self._ts(_row(plant_key="ALL", value="0.75"))
        result = ts.get("MEX1", "pr_daily", Severity.WARNING)
        assert result is not None
        assert result.value == 0.75
        assert result.plant_key == "ALL"

    def test_get_returns_none_when_disabled(self):
        ts = self._ts(_row(plant_key="ALL", value="0.75", enabled="FALSE"))
        result = ts.get("QRO1", "pr_daily", Severity.WARNING)
        assert result is None

    def test_get_returns_none_when_no_match(self):
        ts = self._ts(_row(plant_key="ALL", metric="pr_daily"))
        result = ts.get("QRO1", "inverter_offline", Severity.CRITICAL)
        assert result is None

    def test_disabled_specific_falls_back_to_all(self):
        """If the plant-specific row is DISABLED, ALL should be used."""
        ts = self._ts(
            _row(plant_key="ALL", value="0.75"),
            _row(plant_key="QRO1", value="0.70", enabled="FALSE"),
        )
        result = ts.get("QRO1", "pr_daily", Severity.WARNING)
        assert result is not None
        assert result.value == 0.75
        assert result.plant_key == "ALL"

    def test_plant_key_case_insensitive(self):
        ts = self._ts(_row(plant_key="QRO1", value="0.70"))
        # Query with lowercase
        result = ts.get("qro1", "pr_daily", Severity.WARNING)
        assert result is not None
        assert result.value == 0.70

    def test_thresholds_for_plant_includes_all_and_specific(self):
        ts = self._ts(
            _row(plant_key="ALL", metric="pr_daily", severity="WARNING", value="0.75"),
            _row(plant_key="ALL", metric="inverter_offline", severity="CRITICAL",
                 condition="duration", value="0", duration_min="60"),
            _row(plant_key="QRO1", metric="pr_daily", severity="WARNING", value="0.70"),
            _row(plant_key="MEX1", metric="pr_daily", severity="CRITICAL", value="0.40"),
        )
        qro_thresholds = ts.thresholds_for_plant("QRO1")
        # QRO1 should see: its own pr_daily override + the ALL inverter_offline
        assert len(qro_thresholds) == 2
        # The MEX1-specific row should NOT be visible to QRO1
        plant_keys = {t.plant_key.upper() for t in qro_thresholds}
        assert "MEX1" not in plant_keys

    def test_duplicate_specific_row_kept_first(self):
        """Same (plant, metric, severity) twice → first kept, second logged."""
        ts = self._ts(
            _row(plant_key="QRO1", value="0.70"),
            _row(plant_key="QRO1", value="0.65"),  # dup
        )
        result = ts.get("QRO1", "pr_daily", Severity.WARNING)
        assert result.value == 0.70  # first wins


class TestApplyGloballyProperty:
    def test_all_is_global(self):
        t = Threshold(
            plant_key="ALL", metric="pr_daily", severity=Severity.WARNING,
            condition=Condition.BELOW, value=0.75, duration_min=0,
            enabled=True, channels=frozenset(),
        )
        assert t.applies_globally is True

    def test_specific_not_global(self):
        t = Threshold(
            plant_key="QRO1", metric="pr_daily", severity=Severity.WARNING,
            condition=Condition.BELOW, value=0.70, duration_min=0,
            enabled=True, channels=frozenset(),
        )
        assert t.applies_globally is False

    def test_all_case_insensitive(self):
        t = Threshold(
            plant_key="all", metric="pr_daily", severity=Severity.WARNING,
            condition=Condition.BELOW, value=0.75, duration_min=0,
            enabled=True, channels=frozenset(),
        )
        assert t.applies_globally is True


# ============================================================
# Defaults round-trip
# ============================================================


class TestDefaultsRoundTrip:
    """The DEFAULT_THRESHOLDS list, fed back through the loader, must
    produce a non-empty ThresholdSet. Catches bad-default bugs early."""

    def test_defaults_load_cleanly(self):
        rows = []
        for default in DEFAULT_THRESHOLDS:
            rows.append({
                THRESHOLDS_HEADER[i]: default[i]
                for i in range(len(THRESHOLDS_HEADER))
            })
        sheets = _mock_sheets(rows)
        ts = load_thresholds(sheets)
        assert len(ts.all_thresholds) == len(DEFAULT_THRESHOLDS)
        # All defaults should be enabled
        assert all(t.enabled for t in ts.all_thresholds)
