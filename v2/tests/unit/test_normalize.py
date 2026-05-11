"""Tests for argia.core.normalize."""

import pytest

from argia.core.normalize import (
    chunked,
    looks_like_growatt_site_id,
    looks_like_huawei_station_code,
    looks_like_solaredge_site_id,
    normalize_sn,
    normalize_text,
    pick,
    safe_float,
)


class TestSafeFloat:
    def test_plain_number(self):
        assert safe_float(3.14) == 3.14

    def test_integer(self):
        assert safe_float(42) == 42.0

    def test_string_number(self):
        assert safe_float("3.14") == 3.14

    def test_string_with_comma_thousands(self):
        # Sheets sometimes exports "1,234.5"
        assert safe_float("1,234.5") == 1234.5

    def test_negative(self):
        assert safe_float("-15.5") == -15.5

    def test_none_uses_default(self):
        assert safe_float(None, default=0.0) == 0.0

    def test_none_default_is_none(self):
        assert safe_float(None) is None

    def test_empty_string(self):
        assert safe_float("", default=0.0) == 0.0

    def test_whitespace_only(self):
        assert safe_float("   ", default=0.0) == 0.0

    def test_garbage_string(self):
        assert safe_float("not a number") is None

    def test_garbage_string_with_default(self):
        assert safe_float("garbage", default=-1.0) == -1.0

    def test_nan_returns_default(self):
        assert safe_float(float("nan"), default=0.0) == 0.0

    def test_inf_returns_default(self):
        assert safe_float(float("inf"), default=0.0) == 0.0

    def test_neg_inf_returns_default(self):
        assert safe_float(float("-inf"), default=0.0) == 0.0

    def test_list_returns_default(self):
        assert safe_float([1, 2, 3], default=0.0) == 0.0

    def test_dict_returns_default(self):
        assert safe_float({"a": 1}, default=-1.0) == -1.0


class TestNormalizeText:
    def test_string_stripped(self):
        assert normalize_text("  hi  ") == "hi"

    def test_none_becomes_empty(self):
        assert normalize_text(None) == ""

    def test_int_to_string(self):
        assert normalize_text(42) == "42"

    def test_float_to_string(self):
        assert normalize_text(3.14) == "3.14"

    def test_already_clean(self):
        assert normalize_text("clean") == "clean"


class TestNormalizeSn:
    def test_uppercase(self):
        assert normalize_sn("abc123") == "ABC123"

    def test_strip_internal_whitespace(self):
        # Some APIs return "ES24 70051825" — must collapse to "ES2470051825"
        assert normalize_sn("ES24 70051825") == "ES2470051825"

    def test_strip_outer_whitespace(self):
        assert normalize_sn("  abc  ") == "ABC"

    def test_none_becomes_empty(self):
        assert normalize_sn(None) == ""

    def test_already_normalized(self):
        assert normalize_sn("DYD1EZR007") == "DYD1EZR007"

    def test_idempotent(self):
        # Calling twice shouldn't change anything
        sn = "  es 24 70051825  "
        assert normalize_sn(normalize_sn(sn)) == normalize_sn(sn)


class TestPick:
    def test_first_match(self):
        assert pick({"a": "x", "b": "y"}, ["a", "b"]) == "x"

    def test_skip_empty_string(self):
        assert pick({"a": "", "b": "y"}, ["a", "b"]) == "y"

    def test_skip_none(self):
        assert pick({"a": None, "b": "y"}, ["a", "b"]) == "y"

    def test_skip_null_string(self):
        # APIs sometimes return literal "null"
        assert pick({"a": "null", "b": "y"}, ["a", "b"]) == "y"

    def test_zero_is_kept(self):
        # 0 should NOT be skipped — it's a valid value
        assert pick({"a": 0, "b": 1}, ["a", "b"]) == 0

    def test_no_match_returns_none(self):
        assert pick({"a": "x"}, ["b", "c"]) is None

    def test_empty_dict(self):
        assert pick({}, ["a"]) is None

    def test_non_dict_returns_none(self):
        assert pick(None, ["a"]) is None  # type: ignore[arg-type]
        assert pick([], ["a"]) is None  # type: ignore[arg-type]


class TestChunked:
    def test_even_split(self):
        assert chunked([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]

    def test_uneven_last_chunk(self):
        assert chunked([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]

    def test_chunk_larger_than_list(self):
        assert chunked([1, 2, 3], 10) == [[1, 2, 3]]

    def test_empty_list(self):
        assert chunked([], 3) == []

    def test_size_one(self):
        assert chunked([1, 2, 3], 1) == [[1], [2], [3]]

    def test_zero_size_raises(self):
        with pytest.raises(ValueError):
            chunked([1, 2], 0)

    def test_negative_size_raises(self):
        with pytest.raises(ValueError):
            chunked([1, 2], -1)


class TestSiteIdValidators:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("9275498", True),
            ("10069072", True),
            ("123456", True),  # 6 digits — minimum
            ("123456789012", True),  # 12 digits — maximum
            ("12345", False),  # too short
            ("1234567890123", False),  # too long
            ("NE=35314736", False),
            ("abc123", False),
            ("", False),
            (None, False),
        ],
    )
    def test_growatt(self, value, expected):
        assert looks_like_growatt_site_id(value) is expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("NE=35314736", True),
            ("NE=1", True),
            ("9275498", False),
            ("ne=123", False),  # case-sensitive
            ("", False),
            (None, False),
        ],
    )
    def test_huawei(self, value, expected):
        assert looks_like_huawei_station_code(value) is expected

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("123456", True),
            ("1234", True),  # 4 digits — minimum
            ("123", False),  # too short
            ("NE=123456", False),
            ("", False),
        ],
    )
    def test_solaredge(self, value, expected):
        assert looks_like_solaredge_site_id(value) is expected
