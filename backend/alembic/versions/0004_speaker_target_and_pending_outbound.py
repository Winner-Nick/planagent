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

    # Backfill target_user_id:
    # - user rows (role='user') — trivially copy user_id.
    # - assistant/tool rows — pre-PR-G these were stored with user_id=NULL
    #   because only speakers carry an id. To keep them visible under the
    #   new per-speaker history filter, inherit the target_user_id from the
    #   *most recent preceding user row in the same group* (the speaker this
    #   assistant/tool reply was addressed to). Without this step, legacy
    #   assistant turns would silently disappear from prompts on upgrade.
    bind = op.get_bind()

    # Step 1: user rows — trivial copy.
    bind.execute(
        sa.text(
            "UPDATE conversation_turns "
            "SET target_user_id = user_id "
            "WHERE target_user_id IS NULL AND role = 'user'"
        )
    )

    # Step 2: assistant/tool rows — walk per group in creation order and
    # attribute each to the last seen speaker. Done row-by-row here because
    # SQLite (dev DB) lacks window functions in older versions and this
    # migration needs to be portable.
    result = bind.execute(
        sa.text(
            "SELECT id, group_id, role, user_id, target_user_id, created_at "
            "FROM conversation_turns "
            "ORDER BY group_id, created_at, id"
        )
    )
    last_speaker_by_group: dict[str, str] = {}
    assign: list[tuple[str, str]] = []
    for row in result:
        row_id, group_id, role, user_id, target_user_id, _ = row
        if role == "user" and user_id:
            last_speaker_by_group[group_id] = user_id
            continue
        if target_user_id is not None:
            continue
        speaker = last_speaker_by_group.get(group_id)
        if speaker:
            assign.append((speaker, row_id))
    for speaker, row_id in assign:
        bind.execute(
            sa.text(
                "UPDATE conversation_turns SET target_user_id = :sp WHERE id = :rid"
            ),
            {"sp": speaker, "rid": row_id},
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
