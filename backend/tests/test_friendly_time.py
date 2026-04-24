"""Unit tests for `planagent.lib.friendly_time.friendly`.

Deterministic: every case pins both ``now`` and ``dt`` so we don't depend
on the host clock or the runner timezone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from planagent.lib.friendly_time import friendly

SHA = ZoneInfo("Asia/Shanghai")

# Fixed "right now" — 2026-04-24 12:00 +08:00, a Friday.
NOW = datetime(2026, 4, 24, 12, 0, tzinfo=SHA)


@pytest.mark.parametrize(
    ("dt", "expected"),
    [
        # --- within an hour ---
        (NOW + timedelta(minutes=5), "5 分钟后"),
        (NOW + timedelta(minutes=1), "1 分钟后"),
        (NOW - timedelta(minutes=3), "3 分钟前"),
        (NOW + timedelta(minutes=30), "半小时后"),
        (NOW + timedelta(minutes=28), "半小时后"),
        (NOW - timedelta(minutes=33), "半小时前"),
        # --- today ---
        (datetime(2026, 4, 24, 15, 32, tzinfo=SHA), "今天 15:32"),
        (datetime(2026, 4, 24, 22, 0, tzinfo=SHA), "今晚 22:00"),
        (datetime(2026, 4, 24, 19, 30, tzinfo=SHA), "今晚 19:30"),
        # --- tomorrow ---
        (datetime(2026, 4, 25, 10, 0, tzinfo=SHA), "明天上午 10:00"),
        (datetime(2026, 4, 25, 8, 30, tzinfo=SHA), "明天早上 08:30"),
        (datetime(2026, 4, 25, 21, 0, tzinfo=SHA), "明天晚上 21:00"),
        # Sunday is still in this ISO week (NOW=Fri, weekday=4; Sun=6).
        (datetime(2026, 4, 26, 9, 0, tzinfo=SHA), "周日 09:00"),
        # --- further ---
        (datetime(2026, 4, 27, 9, 0, tzinfo=SHA), "4 月 27 日 上午 09:00"),
        (datetime(2026, 5, 3, 20, 0, tzinfo=SHA), "5 月 3 日 晚上 20:00"),
    ],
)
def test_friendly_rendering(dt: datetime, expected: str) -> None:
    assert friendly(dt, NOW) == expected


def test_friendly_never_emits_iso_markers() -> None:
    # A scan over a dense sample must never include the ISO-8601 `T` or
    # the `+08:00` literal — those are exactly the shapes we're trying
    # to keep out of the UI.
    samples = [
        NOW + timedelta(minutes=m)
        for m in (1, 5, 17, 45, 61, 60 * 5, 60 * 26, 60 * 72, 60 * 24 * 9)
    ]
    for dt in samples:
        rendered = friendly(dt, NOW)
        assert "T" not in rendered, rendered
        assert "+08:00" not in rendered, rendered
        assert "Z" not in rendered, rendered


def test_friendly_accepts_utc_input() -> None:
    # 2026-04-24 15:32 Shanghai == 07:32 UTC. Pass UTC in, expect the
    # Shanghai-rendered form.
    from datetime import UTC

    utc_dt = datetime(2026, 4, 24, 7, 32, tzinfo=UTC)
    assert friendly(utc_dt, NOW) == "今天 15:32"


def test_friendly_treats_naive_input_as_utc() -> None:
    """SQLite drops tzinfo on `DateTime(timezone=True)` reads; our DB values
    are always stored as UTC. Naive datetimes must therefore be interpreted
    as UTC (and converted to Shanghai), not assumed to already be local.
    2026-04-24T07:32 UTC == 2026-04-24T15:32+08:00.
    """
    naive_utc = datetime(2026, 4, 24, 7, 32)
    assert friendly(naive_utc, NOW) == "今天 15:32"


def test_friendly_week_collapse_from_monday() -> None:
    # NOW = Mon 2026-04-20 10:00. Fri of same ISO week should collapse to
    # "周五 HH:MM", not to "明天…" or the M月D日 fallback.
    mon = datetime(2026, 4, 20, 10, 0, tzinfo=SHA)
    fri = datetime(2026, 4, 24, 14, 0, tzinfo=SHA)
    assert friendly(fri, mon) == "周五 14:00"


def test_friendly_default_now_is_shanghai_wall_clock() -> None:
    # Smoke: calling without `now` should not blow up and should return
    # a non-empty string shaped like one of the templates.
    result = friendly(datetime.now(SHA) + timedelta(minutes=5))
    assert result.endswith("分钟后") or result.startswith("今") or "月" in result
