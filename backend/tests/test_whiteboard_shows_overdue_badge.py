"""PR-I §F: whiteboard renders `⚠️` for overdue plans and stays ≤ 400 chars."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from planagent.agent.prompts import WHITEBOARD_MARKER, Whiteboard


def test_overdue_plan_gets_warning_badge_in_whiteboard() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    wb = Whiteboard(
        peer_display_name="辰辰",
        peer_last_inbound_at=datetime(2026, 4, 23, 22, 38, tzinfo=shanghai),
        peer_open_plans=1,
        peer_overdue_count=1,
        unconsumed_notes=[],
        plans_by_owner={
            "辰辰": [
                {
                    "title": "你老公说爱你",
                    "status": "overdue",
                    "due_at": "2026-04-23T22:38:00+08:00",
                },
                {
                    "title": "英语晨读",
                    "status": "active",
                    "next_fire_at": "2026-04-24T07:00:00+08:00",
                },
            ],
        },
    )
    rendered = wb.render()
    assert rendered.startswith(WHITEBOARD_MARKER)
    assert "⚠️" in rendered
    # The overdue row in the board carries the badge.
    assert "⚠️ 你老公说爱你" in rendered
    # The non-overdue row does NOT get the badge prefix.
    assert "⚠️ 英语晨读" not in rendered
    # Peer summary's overdue bit also has the badge.
    assert "⚠️逾期 1" in rendered
    assert len(rendered) <= 400, f"rendered {len(rendered)} chars:\n{rendered}"


def test_completed_today_surfaces_in_peer_line() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    wb = Whiteboard(
        peer_display_name="鹏鹏",
        peer_last_inbound_at=datetime(2026, 4, 23, 9, 0, tzinfo=shanghai),
        peer_open_plans=2,
        peer_overdue_count=0,
        peer_completed_today=3,
    )
    rendered = wb.render()
    assert "今日完成 3" in rendered
    assert len(rendered) <= 400
