"""Whiteboard render budget (PR-H).

The `### 白板 ###` block rides the volatile tail of every turn's prompt. It
must stay under 400 chars so DeepSeek's prefix cache keeps hitting on the
stable prefix + volatile header. This test seeds a realistic scenario and
also a pathological (over-budget) one, asserting `render` trims cleanly.

Pure unit test — no DB, no LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from planagent.agent.prompts import WHITEBOARD_MARKER, Whiteboard


def test_realistic_whiteboard_under_budget() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    wb = Whiteboard(
        peer_display_name="辰辰",
        peer_last_inbound_at=datetime(2026, 4, 23, 15, 42, tzinfo=shanghai),
        peer_open_plans=3,
        peer_overdue_count=1,
        unconsumed_notes=[
            {"kind": "nudge_request", "text": "记得戳鹏鹏学 Rust", "created_at_local": "15:42"},
            {"kind": "info", "text": "辰辰这周项目压力大", "created_at_local": "16:10"},
        ],
        plans_by_owner={
            "鹏鹏": [
                {
                    "title": "Rust 学习",
                    "status": "active",
                    "next_fire_at": "2026-04-23T20:00:00+08:00",
                },
            ],
            "辰辰": [
                {
                    "title": "英语晨读",
                    "status": "active",
                    "next_fire_at": "2026-04-24T07:00:00+08:00",
                },
                {"title": "房租报税", "status": "active"},
            ],
        },
    )
    rendered = wb.render()
    assert rendered.startswith(WHITEBOARD_MARKER)
    assert len(rendered) <= 400, f"rendered {len(rendered)} chars:\n{rendered}"
    assert "辰辰" in rendered
    # Notes present.
    assert "nudge_request" in rendered
    # Plans board has the owner groupings.
    assert "鹏鹏" in rendered
    assert "Rust 学习" in rendered


def test_empty_whiteboard_renders_no_activity() -> None:
    wb = Whiteboard()
    out = wb.render()
    assert out.startswith(WHITEBOARD_MARKER)
    assert len(out) <= 400
    assert "暂无活动" in out


def test_oversized_whiteboard_trims_below_budget() -> None:
    # Pathological input: lots of plans and many notes. render() should
    # trim plans-first, then notes, to land under the 400-char budget.
    shanghai = ZoneInfo("Asia/Shanghai")
    many_plans = {
        "鹏鹏": [
            {"title": f"计划{i}", "status": "active", "next_fire_at": "2026-04-24T07:00:00+08:00"}
            for i in range(10)
        ],
        "辰辰": [
            {"title": f"她计划{i}", "status": "active", "due_at": "2026-04-24T22:00:00+08:00"}
            for i in range(10)
        ],
    }
    many_notes = [
        {
            "kind": "info",
            "text": f"第{i}条关于对方最近状态的信息，这是一条相对长一点的备忘",
            "created_at_local": "12:00",
        }
        for i in range(8)
    ]
    wb = Whiteboard(
        peer_display_name="辰辰",
        peer_last_inbound_at=datetime.now(UTC).astimezone(shanghai),
        peer_open_plans=20,
        peer_overdue_count=3,
        unconsumed_notes=many_notes,
        plans_by_owner=many_plans,
    )
    rendered = wb.render()
    assert len(rendered) <= 400, f"{len(rendered)} chars:\n{rendered}"
    # Peer summary line survives — it's the highest-priority row.
    assert "对方（辰辰）" in rendered
