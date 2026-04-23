"""0004_speaker_target_and_pending_outbound

Revision ID: 0004_speaker_target_and_pending_outbound
Revises: 0003_bot_sessions
Create Date: 2026-04-23 14:30:00.000000

PR-G: add `conversation_turns.target_user_id` (speaker/target split) and the
`pending_outbound` table skeleton that PR-H will populate. Backfill
`target_user_id = user_id` for existing rows so historical data still filters
correctly under the new per-speaker history loader.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_speaker_target_and_pending_outbound"
down_revision: str | None = "0003_bot_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add target_user_id to conversation_turns.
    with op.batch_alter_table("conversation_turns", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target_user_id", sa.String(length=128), nullable=True))

    # Backfill: for rows from PR-F and earlier, the only sensible value is the
    # speaker itself (user_id). This keeps existing histories visible to the
    # speaker who made them under the new per-speaker filter.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE conversation_turns "
            "SET target_user_id = user_id "
            "WHERE target_user_id IS NULL"
        )
    )

    # 2. Create pending_outbound (PR-H storage, defined now so PR-H is schema-stable).
    op.create_table(
        "pending_outbound",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("target_user_id", sa.String(length=128), nullable=False),
        sa.Column("author_user_id", sa.String(length=128), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "delivered",
                "cancelled",
                name="pending_outbound_status",
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["group_id"], ["group_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("pending_outbound", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_pending_outbound_target_user_id"),
            ["target_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_pending_outbound_status"),
            ["status"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("pending_outbound", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pending_outbound_status"))
        batch_op.drop_index(batch_op.f("ix_pending_outbound_target_user_id"))
    op.drop_table("pending_outbound")
    # Drop the enum that the table owned (PG-only; SQLite no-ops).
    sa.Enum(name="pending_outbound_status").drop(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("conversation_turns", schema=None) as batch_op:
        batch_op.drop_column("target_user_id")
