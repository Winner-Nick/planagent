"""0007_scheduled_messages

Revision ID: 0007_scheduled_messages
Revises: 0006_plan_status_overdue_cancelled
Create Date: 2026-04-23 20:00:00.000000

PR-L: add the `scheduled_messages` table that backs `schedule_message_to_peer`.

A ScheduledMessage is a one-off fire-and-forget push addressed to a specific
user at a specific wall-clock time — the "三分钟后告诉辰辰 X" flow. Before
PR-L that intent was modeled as `create_plan_draft(owner=peer) +
schedule_reminder`, which polluted the plan board with throwaway titles.
Splitting the concept keeps Plan rows meaningful.

Chained onto 0006_plan_status_overdue_cancelled (PR-I) — this PR was
originally authored against 0005 but rebased on top of PR-I before merge.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_scheduled_messages"
down_revision: str | None = "0006_plan_status_overdue_cancelled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("author_user_id", sa.String(length=128), nullable=True),
        sa.Column("target_user_id", sa.String(length=128), nullable=False),
        sa.Column("fire_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "cancelled",
                name="scheduled_message_status",
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["group_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("scheduled_messages", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_scheduled_messages_group_id"),
            ["group_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_messages_target_user_id"),
            ["target_user_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_messages", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scheduled_messages_target_user_id"))
        batch_op.drop_index(batch_op.f("ix_scheduled_messages_group_id"))
    op.drop_table("scheduled_messages")
    sa.Enum(name="scheduled_message_status").drop(op.get_bind(), checkfirst=True)
