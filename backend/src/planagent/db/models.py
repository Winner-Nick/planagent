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


class PendingOutboundStatus(str, enum.Enum):  # noqa: UP042
    pending = "pending"
    delivered = "delivered"
    cancelled = "cancelled"


class GroupContext(Base):
    __tablename__ = "group_contexts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    wechat_group_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Kept for backward compatibility (pre-PR-F scheduler sends). PR-F moves the
    # authoritative per-user token onto BotSession.last_context_token.
    last_context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    members: Mapped[list[GroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    plans: Mapped[list[Plan]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    turns: Mapped[list[ConversationTurn]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    bot_sessions: Mapped[list[BotSession]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupMember(Base):
    __tablename__ = "group_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    wechat_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    group: Mapped[GroupContext] = relationship(back_populates="members")


class BotSession(Base):
    """One logical 1:1 ClawBot chat, emulating a member of the fake group.

    The ClawBot platform doesn't support real group chat on personal WeChat, so
    we fan out a "group" as N independent 1:1 sessions that share a logical
    `group_id`. Each BotSession has its own bot_token (one ClawBot per user),
    its own last_inbound_at / last_outbound_at (for the 24h keep-alive window),
    and its own `last_context_token` — the ClawBot protocol threads replies on
    that token, and it's scoped per user, NOT per group.
    """

    __tablename__ = "bot_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Credential-file friendly label (e.g. "peng", "chenchen"). Unique so the
    # bootstrap loader can upsert by filename.
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Starts NULL — we don't know the user's wechat_user_id until their first
    # inbound message arrives. Runtime fills it in, and it becomes unique.
    wechat_user_id: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True, index=True
    )
    bot_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bot_token: Mapped[str] = mapped_column(String(512), nullable=False)
    baseurl: Mapped[str | None] = mapped_column(String(256), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_wakeup_ping_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Context token from this user's most recent inbound — required for any
    # scheduler-originated (no-live-message) send. Per-session because ClawBot
    # scopes context tokens to the conversation, not to our logical group.
    last_context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="bot_sessions")


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
    """Single row in the per-group conversation log.

    PR-G split: `user_id` semantically means **speaker_user_id** (who said this
    turn / whose inbound triggered this assistant-or-tool row). `target_user_id`
    is the user the orchestrator was replying TO on this turn. For user rows
    those two are identical; for assistant/tool rows generated while handling
    speaker X, both are X. This lets history loading filter by the
    current speaker and avoid cross-user contamination.
    """

    __tablename__ = "conversation_turns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[ConversationRole] = mapped_column(
        Enum(ConversationRole, name="conversation_role"), nullable=False
    )
    # Semantically "speaker_user_id" — keeping the physical name stable so
    # existing callers / queries don't break. Use the `speaker_user_id`
    # property below for readability.
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # PR-G: added. For role=user, mirrors `user_id`. For assistant/tool,
    # identifies the user this reply is addressed to (i.e. the speaker of
    # the inbound that this handler invocation is processing).
    target_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    group: Mapped[GroupContext] = relationship(back_populates="turns")

    @property
    def speaker_user_id(self) -> str | None:
        """Readability alias for `user_id` — the speaker on this turn."""
        return self.user_id


class PendingOutbound(Base):
    """Queued outbound text awaiting a user's wake-up window.

    Skeleton in PR-G: PR-H will route cross-user notifications through this
    table (e.g. Peng tells 小计 "告诉辰辰晚上一起复盘一下" and it lands here
    until 辰辰 next pings the bot). Defined now so PR-H doesn't need another
    migration.
    """

    __tablename__ = "pending_outbound"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(
        ForeignKey("group_contexts.id", ondelete="CASCADE"), nullable=False
    )
    target_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[PendingOutboundStatus] = mapped_column(
        Enum(PendingOutboundStatus, name="pending_outbound_status"),
        default=PendingOutboundStatus.pending,
        nullable=False,
    )
