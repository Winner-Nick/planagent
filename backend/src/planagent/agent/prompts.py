"""System prompt assembly — 小计 persona (PR-G).

Layout is cache-friendly: the stable prefix (persona, tool contract, hard
output rules, plan schema) never changes across turns. The volatile section
(current speaker, active plans, now()) is always appended last so DeepSeek's
prefix cache can hit on everything before `VOLATILE_MARKER`.

PR-G changes vs. PR-D:
- Rewrote the prefix as the 小计 (xiao-ji) persona with explicit tone /
  register rules and hard output constraints (no chain-of-thought, empty
  content on tool calls, one-send-per-turn).
- Stripped `owner_user_id` from the explicit plan schema — orchestrator
  never exposes user_ids to the LLM; the agent addresses humans by name.
- Added an explicit "当你给一个计划设置了 start_at 或 due_at，必须在同一轮
  对话中调用 schedule_reminder" rule so two-minute-from-now plans don't
  silently pass their fire time.
- `DIALOGUE_TEMP` / `ACTION_TEMP` exposed for the orchestrator's temperature
  dispatch (see orchestrator.py docstring for dispatch policy).
- `WHITEBOARD_PLACEHOLDER` reserves the exact marker string PR-H will inject
  cross-user whiteboard content under; edits to that line must stay in sync
  with PR-H.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from planagent.db.models import PlanStatus, ReminderStatus

VOLATILE_MARKER = "### VOLATILE CONTEXT ###"
# PR-H will inject a cross-user whiteboard block under this marker. Do NOT
# rename without updating PR-H's writer.
WHITEBOARD_MARKER = "### 白板 ###"

# Temperature dispatch: free-form chat benefits from a little warmth; tool-
# calling / field-filling must be deterministic. Orchestrator picks between
# these based on whether the previous round produced tool_calls.
DIALOGUE_TEMP = 0.7
ACTION_TEMP = 0.0


# --- Stable prefix (persona) -------------------------------------------------

_PERSONA = """\
你是 **小计**（xiao-ji），鹏鹏和辰辰的贴身计划管家，同时也是他俩的发小。
这个微信后台是模拟群聊的 1:1 会话：每次只有一个人在跟你说话，你的回复只有 TA 看得到。

# 角色定位
- 鹏鹏：这套系统的作者，工程师；随意，可以互相调侃。
- 辰辰：鹏鹏的爱人，本人很忙；跟她说话尽量简短、体贴、别让她多做选择题。
- 你服务他们俩，但不用他俩的任何 user_id / 编号对话——你有他们的名字就够了。

# 说话风格（分两档）
1. 日常闲聊 / 问候 / 调侃 档：
   - 放得开，允许一点儿调皮和吐槽，用名字（鹏鹏 / 辰辰）或亲昵叫法。
   - 一两个 emoji 没问题，不要刷屏。
   - 不要翻译腔、不要"您"、不要 AI 味儿的模板句。
   - 像 2026 年在微信跟朋友聊天的中国年轻人。
2. 记计划 / 改时间 / 调工具 档：
   - 立刻切严谨模式：字段要准确、时间要带 +08:00、不跳步。
   - 必要字段没给你就用 `ask_user_in_group` 发一句专注问题，问完就停。
   - 从不瞎编默认值、不假设截止时间、不自己发明 cron。

# 硬规则（违反就会被拦截，别犯）
- 你只说用户眼下该看的那句话。**严禁**把你自己的推理、流程、自语说出来。
  以下句式都是违规示例：
  - "我已经 ...，接下来 ..."
  - "现在我来 ..." / "那么我来 ..."
  - "我需要知道 ..." / "为了帮你，我需要 ..."
  - "用户的 ID 是 ..."
- 当你调用工具（任意 function call）时，**assistant 消息的 content 必须是空字符串**。
  你想说的话通过 `reply_in_group` / `ask_user_in_group` 工具发出去，不是写在 content 里。
  （DeepSeek 默认会把思考过程写进 content——这里禁止这么做。）
- 一个用户的一轮输入，你对外最多说一段话。想既打招呼又追问，就把两句压成一条自然消息；
  不要重复发送、不要分好几条。
- 不要询问用户的 user_id / 编号 / id。当前说话人就是默认 owner；除非用户明确把计划指向对方
  （比如"这是辰辰她自己的英语打卡"），否则 create_plan_draft 用默认值即可。

# 工具使用原则
- 能用工具就用工具，不要用自然语言"伪执行"。
- 一个新计划只要标题明确就立刻 `create_plan_draft`，后续字段用 `update_plan` 补齐。
- 激活一个计划至少需要：标题 + 负责人（默认是当前说话人）+ （due_at 或 recurrence_cron 之一）。
  以"每次多少分钟"描述的计划（"每天 30 分钟"）还要 expected_duration_per_session_min。
- **重要**：当你给一个计划写入了 start_at 或 due_at，必须在同一轮对话里调用 `schedule_reminder`
  把那个时间点排上。否则我（小计）就等于把这事儿弄丢了。
- 所有对用户展示的时间一律 Asia/Shanghai（+08:00），ISO-8601 带显式时区。
- 想查现有计划优先 `list_plans` / `get_plan`，不要靠记忆猜。
- 标记完成用 `mark_plan_complete`，不要只在自然语言里说"好的，做完了"。
- 一轮最多 6 次工具调用，早点收尾。
"""


def _plan_schema_fragment() -> str:
    """Serialized JSON description of the Plan model + enumerations.

    Stable: no timestamps, no ids — cache-friendly. `owner_user_id` is kept
    internal; the agent only sees an `owner` string at the tool layer.
    """
    payload = {
        "Plan": {
            "fields": {
                "id": "string (uuid, server-assigned)",
                "title": "string",
                "description": "string | null",
                "status": f"enum {[s.value for s in PlanStatus]}",
                "start_at": "ISO-8601 datetime with +08:00 tz | null",
                "due_at": "ISO-8601 datetime with +08:00 tz | null",
                "expected_duration_per_session_min": "integer minutes | null",
                "recurrence_cron": "5-field cron string | null",
                "priority": "integer (0 default)",
                "metadata_json": "object (free-form, notes under .notes[])",
            },
        },
        "Reminder": {
            "fields": {
                "id": "string (uuid)",
                "plan_id": "string",
                "fire_at": "ISO-8601 datetime with tz",
                "message": "string",
                "status": f"enum {[s.value for s in ReminderStatus]}",
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


STABLE_PREFIX = (
    _PERSONA
    + "\n数据模型 (JSON):\n```json\n"
    + _plan_schema_fragment()
    + "\n```\n"
)


# --- Volatile context --------------------------------------------------------


@dataclass
class GroupSnapshot:
    group_id: str
    wechat_group_id: str
    group_name: str | None
    members: list[dict[str, Any]]  # {wechat_user_id, display_name}
    plans: list[dict[str, Any]]  # {id, title, status, next_fire_at, owner_user_id}
    speaker_wechat_user_id: str | None = None
    speaker_display_name: str | None = None


def _plan_line(p: dict[str, Any]) -> str:
    nxt = p.get("next_fire_at")
    parts = [f"- [{p['status']}] {p['title']} (id={p['id']})"]
    if nxt:
        parts.append(f"next_fire_at={nxt}")
    return "  ".join(parts)


def _render_plans_for(
    plans: list[dict[str, Any]],
    owner_wechat_user_id: str | None,
    display_name: str | None,
) -> str:
    if not owner_wechat_user_id:
        relevant: list[dict[str, Any]] = []
    else:
        relevant = [p for p in plans if p.get("owner_user_id") == owner_wechat_user_id]
    label = display_name or "?"
    if not relevant:
        return f"{label}当前计划: （暂无）"
    lines = "\n".join(_plan_line(p) for p in relevant)
    return f"{label}当前计划:\n{lines}"


def _render_volatile(snapshot: GroupSnapshot, now: datetime) -> str:
    # Resolve per-member plan blocks (PR-G shows current speaker + the peer).
    by_uid = {
        m["wechat_user_id"]: (m.get("display_name") or "?")
        for m in snapshot.members
        if m.get("wechat_user_id")
    }

    speaker_label = (
        snapshot.speaker_display_name
        or by_uid.get(snapshot.speaker_wechat_user_id or "")
        or "（未知说话人）"
    )

    peer_blocks: list[str] = []
    for uid, name in by_uid.items():
        if uid == snapshot.speaker_wechat_user_id:
            continue
        peer_blocks.append(_render_plans_for(snapshot.plans, uid, name))

    speaker_block = _render_plans_for(
        snapshot.plans, snapshot.speaker_wechat_user_id, speaker_label
    )

    chunks = [
        f"{VOLATILE_MARKER}",
        f"当前时间 (Asia/Shanghai): {now.isoformat()}",
        f"当前说话人: **{speaker_label}**",
        speaker_block,
    ]
    chunks.extend(peer_blocks)
    # PR-H inserts cross-user whiteboard notes under WHITEBOARD_MARKER.
    # PR-G leaves the marker as a placeholder so the LLM already knows the slot.
    chunks.append(f"{WHITEBOARD_MARKER}\n（暂无跨用户留言）")
    return "\n".join(chunks) + "\n"


def make_prompt(snapshot: GroupSnapshot, *, now: datetime) -> str:
    """Build the full system prompt. Stable prefix first, volatile tail last."""
    return STABLE_PREFIX + "\n" + _render_volatile(snapshot, now)


def stable_prefix_bytes() -> bytes:
    """Exposed for cache-alignment assertions."""
    return STABLE_PREFIX.encode("utf-8")


__all__ = [
    "ACTION_TEMP",
    "DIALOGUE_TEMP",
    "GroupSnapshot",
    "STABLE_PREFIX",
    "VOLATILE_MARKER",
    "WHITEBOARD_MARKER",
    "make_prompt",
    "stable_prefix_bytes",
]
