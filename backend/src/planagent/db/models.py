from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class PlanStatus(str, enum.Enum):  # noqa: UP042
    draft = "draft"
    active = "active"
    completed = "completed"
    paused = "paused"


class ReminderStatus(str, enum.Enum):  # noqa: UP042
    pending = "pending"
    sent = "sent"
    skipped = "skipped"
    cancelled = "cancelled"


class ConversationRole(str, enum.Enum):  # noqa: UP042
    user = "user"
    assistant = "assistant"
    tool = "tool"
    system = "system"


class GroupContext(Base):
    __tablename__ = "group_contexts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    wechat_group_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    members: Mapped[list[GroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    plans: Mapped[list[Plan]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    turns: Mapped[list[ConversationTurn]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    wechat_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    group: Mapped[GroupContext] = relationship(back_populates="members")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, name="plan_status"), default=PlanStatus.draft, nullable=False
    )
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_duration_per_session_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recurrence_cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    owner_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="plans")
    reminders: Mapped[list[Reminder]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"), nullable=False
    )
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ReminderStatus] = mapped_column(
        Enum(ReminderStatus, name="reminder_status"),
        default=ReminderStatus.pending,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    plan: Mapped[Plan] = relationship(back_populates="reminders")


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ConversationRole] = mapped_column(
        Enum(ConversationRole, name="conversation_role"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="turns")
