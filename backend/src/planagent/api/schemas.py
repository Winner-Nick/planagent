from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from planagent.db.models import ConversationRole, PlanStatus, ReminderStatus


class PlanBase(BaseModel):
    title: str
    description: str | None = None
    status: PlanStatus = PlanStatus.draft
    start_at: datetime | None = None
    due_at: datetime | None = None
    expected_duration_per_session_min: int | None = None
    recurrence_cron: str | None = None
    priority: int = 0
    owner_user_id: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class PlanCreate(PlanBase):
    group_id: str


class PlanUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: PlanStatus | None = None
    start_at: datetime | None = None
    due_at: datetime | None = None
    expected_duration_per_session_min: int | None = None
    recurrence_cron: str | None = None
    priority: int | None = None
    owner_user_id: str | None = None
    metadata_json: dict[str, Any] | None = None


class PlanRead(PlanBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    group_id: str
    created_at: datetime
    updated_at: datetime


class ReminderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    plan_id: str
    fire_at: datetime
    fired_at: datetime | None
    message: str
    status: ReminderStatus
    created_at: datetime


class GroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    wechat_group_id: str
    name: str | None
    created_at: datetime
    last_seen_at: datetime | None


class ConversationTurnRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    group_id: str
    role: ConversationRole
    user_id: str | None
    content: str | None
    tool_calls_json: dict[str, Any] | None
    tool_call_id: str | None
    context_token: str | None
    created_at: datetime
