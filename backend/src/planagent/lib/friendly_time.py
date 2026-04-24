"""Human-friendly Chinese rendering of a future (or past) datetime.

Rules (PR-M, mirrors the persona "时间说人话" section):

- Inside 60 min (absolute delta): "N 分钟后" / "N 分钟前" / "半小时后" for 25-35m.
- Today (same local date, future): "今天 HH:MM" — with "今晚" after 18:00,
  "今早" before 09:00, "今天中午" for 11:00-13:00.
- Yesterday / tomorrow: "昨天 HH:MM" / "明天 HH:MM" with the same period
  prefix ("明天早上"/"明天晚上"/...).
- Same ISO week, future ≤ 6 days: "周X HH:MM".
- Further out: "M 月 D 日 <period prefix> HH:MM".

The output is intentionally ISO-free — the agent uses `friendly()` for
anything it shows the user; raw ISO strings remain available for tool
arguments.

Pure function, no globals, no I/O. `now` defaults to `datetime.now(dt.tzinfo)`
when omitted but callers in tests should always pass an explicit `now` for
deterministic assertions.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

_WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]


def _to_shanghai(dt: datetime) -> datetime:
    """Convert *dt* to Asia/Shanghai.

    Naive datetimes are treated as UTC — not local — because that's what
    comes out of SQLite for our `DateTime(timezone=True)` columns (SQLite
    drops tzinfo on read). Interpreting naive values as Shanghai would
    silently shift every rendered time by 8 hours against reality.
    Callers that hold a truly-local datetime should stamp tzinfo before
    calling this helper.
    """
    if dt.tzinfo is None:
        from datetime import UTC as _UTC
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(SHANGHAI)


def _period_prefix(hour: int) -> str:
    """Return a period word like '早上' / '上午' / '中午' / '下午' / '晚上'."""
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 18:
        return "下午"
    return "晚上"


def _hhmm(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


def friendly(dt: datetime, now: datetime | None = None) -> str:
    """Render ``dt`` in colloquial Chinese relative to ``now``.

    Both arguments are converted to Asia/Shanghai before comparison. If
    ``now`` is omitted, "current wall clock in Shanghai" is used — most
    production callers should pass an explicit ``now`` to keep renders
    stable across a turn.
    """
    target = _to_shanghai(dt)
    base = _to_shanghai(now) if now is not None else datetime.now(SHANGHAI)

    delta = target - base
    total_seconds = int(delta.total_seconds())
    abs_seconds = abs(total_seconds)

    # --- Within an hour: minute-granularity ---------------------------------
    if abs_seconds < 60:
        return "刚刚" if total_seconds >= 0 else "刚才"
    if abs_seconds < 60 * 60:
        minutes = round(abs_seconds / 60)
        # 25-35 min rounds to the colloquial "半小时"
        if 25 <= minutes <= 35:
            return "半小时后" if total_seconds >= 0 else "半小时前"
        suffix = "后" if total_seconds >= 0 else "前"
        return f"{minutes} 分钟{suffix}"

    target_date = target.date()
    base_date = base.date()
    day_delta = (target_date - base_date).days

    period = _period_prefix(target.hour)
    hhmm = _hhmm(target)

    # --- Today --------------------------------------------------------------
    if day_delta == 0:
        if target.hour >= 18:
            return f"今晚 {hhmm}"
        if target.hour < 9:
            return f"今早 {hhmm}"
        if 11 <= target.hour < 13:
            return f"今天中午 {hhmm}"
        return f"今天 {hhmm}"

    # --- Adjacent days ------------------------------------------------------
    if day_delta == 1:
        return f"明天{period} {hhmm}"
    if day_delta == -1:
        return f"昨天{period} {hhmm}"

    # --- Within this ISO week, future ≤ 6 days ------------------------------
    if 2 <= day_delta <= 6:
        base_weekday = base.weekday()  # 0=Mon
        target_weekday = target.weekday()
        # Only collapse to 周X when the target is still inside *this* week.
        if target_weekday > base_weekday:
            return f"周{_WEEKDAYS[target_weekday]} {hhmm}"

    # --- Further: M 月 D 日 <period> HH:MM ---------------------------------
    return f"{target.month} 月 {target.day} 日 {period} {hhmm}"


__all__ = ["friendly", "SHANGHAI"]
