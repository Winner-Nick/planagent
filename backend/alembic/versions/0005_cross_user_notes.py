"""0005_cross_user_notes

Revision ID: 0005_cross_user_notes
Revises: 0004_speaker_target_and_pending_outbound
Create Date: 2026-04-23 18:45:00.000000

PR-H: add the `cross_user_notes` table that backs 小计's shared whiteboard.
Rows represent a note dictated by one known human addressed to the other;
the orchestrator surfaces unconsumed rows into the audience's next volatile
prompt and stamps `consumed_at`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005_cross_user_notes"
down_revision: str | None = "0004_speaker_target_and_pending_outbound"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cross_user_notes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("group_id", sa.String(length=36), nullable=False),
        sa.Column("author_user_id", sa.String(length=128), nullable=False),
        sa.Column("audience_user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "info",
                "nudge_request",
                "appreciate",
                name="cross_user_note_kind",
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["group_contexts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cross_user_notes", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cross_user_notes_group_id"),
            ["group_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_cross_user_notes_audience_user_id"),
            ["audience_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_cross_user_notes_consumed_at"),
            ["consumed_at"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("cross_user_notes", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cross_user_notes_consumed_at"))
        batch_op.drop_index(batch_op.f("ix_cross_user_notes_audience_user_id"))
        batch_op.drop_index(batch_op.f("ix_cross_user_notes_group_id"))
    op.drop_table("cross_user_notes")
    sa.Enum(name="cross_user_note_kind").drop(op.get_bind(), checkfirst=True)
