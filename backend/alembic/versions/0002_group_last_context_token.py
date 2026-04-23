"""add last_context_token to group_contexts

Revision ID: 0002_group_last_context_token
Revises: 0001_initial
Create Date: 2026-04-23 10:27:10.162581

"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_group_last_context_token"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("group_contexts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_context_token", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("group_contexts", schema=None) as batch_op:
        batch_op.drop_column("last_context_token")
