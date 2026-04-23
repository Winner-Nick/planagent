"""Prompt structure / cache-alignment tests.

Verifies that the STABLE_PREFIX portion of the system prompt is byte-
identical across different snapshots + now() values (so DeepSeek's prefix
cache can hit), and that the volatile tail reflects actual runtime data
(current speaker, their plans).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from planagent.agent.prompts import (
    VOLATILE_MARKER,
    WHITEBOARD_MARKER,
    GroupSnapshot,
    make_prompt,
    stable_prefix_bytes,
)

SH = ZoneInfo("Asia/Shanghai")


def _snapshot_a() -> GroupSnapshot:
    return GroupSnapshot(
        group_id="g-aaa",
        wechat_group_id="wx-aaa",
        group_name="Alpha Squad",
        members=[
            {"wechat_user_id": "u-peng", "display_name": "鹏鹏"},
            {"wechat_user_id": "u-chenchen", "display_name": "辰辰"},
        ],
        plans=[
            {
                "id": "p1",
                "title": "Rust",
                "status": "draft",
                "next_fire_at": "2026-05-01T08:00:00+08:00",
                "owner_user_id": "u-peng",
            }
        ],
        speaker_wechat_user_id="u-peng",
        speaker_display_name="鹏鹏",
    )


def _snapshot_b() -> GroupSnapshot:
    return GroupSnapshot(
        group_id="g-bbb",
        wechat_group_id="wx-bbb",
        group_name="Bravo",
        members=[],
        plans=[],
    )


def test_stable_prefix_is_identical_across_snapshots_and_times() -> None:
    t1 = datetime(2026, 4, 23, 9, 0, tzinfo=SH)
    t2 = datetime(2027, 1, 1, 23, 59, tzinfo=SH)
    p1 = make_prompt(_snapshot_a(), now=t1)
    p2 = make_prompt(_snapshot_b(), now=t2)

    # Everything before the volatile marker must be byte-identical.
    head1 = p1.split(VOLATILE_MARKER, 1)[0]
    head2 = p2.split(VOLATILE_MARKER, 1)[0]
    assert head1 == head2

    # And it must match the exposed stable_prefix bytes.
    assert head1.encode("utf-8").startswith(stable_prefix_bytes())


def test_volatile_section_reflects_inputs() -> None:
    t = datetime(2026, 4, 23, 9, 30, tzinfo=SH)
    out = make_prompt(_snapshot_a(), now=t)
    assert VOLATILE_MARKER in out
    tail = out.split(VOLATILE_MARKER, 1)[1]
    # Persona-era volatile renders the speaker by display name and their plans.
    assert "鹏鹏" in tail
    assert "辰辰" in tail
    assert "Rust" in tail
    assert t.isoformat() in tail
    # PR-H whiteboard placeholder must be present so PR-H has a stable slot.
    assert WHITEBOARD_MARKER in tail


def test_make_prompt_same_snapshot_same_time_is_stable() -> None:
    t = datetime(2026, 4, 23, 9, 30, tzinfo=SH)
    snap = _snapshot_a()
    assert make_prompt(snap, now=t) == make_prompt(snap, now=t)


def test_stable_prefix_contains_persona_rules() -> None:
    prefix = stable_prefix_bytes().decode("utf-8")
    # Sanity on persona invariants that fix the PR-G bugs.
    assert "小计" in prefix
    # Hard rule: empty content on tool-call messages.
    assert "content 必须是空字符串" in prefix
    # Must have the mandatory-reminder-after-start_at rule.
    assert "schedule_reminder" in prefix
