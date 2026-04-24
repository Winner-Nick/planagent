"""PR-I §C: `_normalize_cron` helper unit tests.

- 5-field cron passes through unchanged (no warning).
- 6-field cron with a numeric 0-59 seconds leader gets the leader stripped
  and returns a warning.
- 6-field cron with a non-numeric leading field → ValueError.
- Any other arity → ValueError.
"""

from __future__ import annotations

import pytest

from planagent.agent.tools import _normalize_cron


def test_five_field_passthrough() -> None:
    normalized, warning = _normalize_cron("0 20 * * 1-5")
    assert normalized == "0 20 * * 1-5"
    assert warning is None


def test_five_field_trims_surrounding_whitespace() -> None:
    normalized, warning = _normalize_cron("  0 9 * * *  ")
    assert normalized == "0 9 * * *"
    assert warning is None


def test_six_field_numeric_seconds_dropped_with_warning() -> None:
    normalized, warning = _normalize_cron("0 50 9 * * *")
    assert normalized == "50 9 * * *"
    assert warning == "cron normalized: dropped leading seconds field"


def test_six_field_nonzero_seconds_dropped_with_warning() -> None:
    normalized, warning = _normalize_cron("30 0 9 * * *")
    assert normalized == "0 9 * * *"
    assert warning is not None


def test_six_field_nonnumeric_leader_rejected() -> None:
    with pytest.raises(ValueError, match="numeric seconds"):
        _normalize_cron("*/10 0 9 * * *")


def test_six_field_out_of_range_seconds_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        _normalize_cron("60 0 9 * * *")


def test_garbage_rejected() -> None:
    with pytest.raises(ValueError, match="invalid cron"):
        _normalize_cron("foo bar")


def test_empty_string_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        _normalize_cron("")


def test_too_few_fields_rejected() -> None:
    with pytest.raises(ValueError, match="5 fields"):
        _normalize_cron("0 20 *")
