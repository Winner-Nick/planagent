"""0006_plan_status_overdue_cancelled

Revision ID: 0006_plan_status_overdue_cancelled
Revises: 0005_cross_user_notes
Create Date: 2026-04-23 22:30:00.000000

PR-I: extend `PlanStatus` with two new values:

- `overdue`: auto-assigned by the scheduler when a non-recurring active plan's
  `due_at` falls more than 10 minutes (`OVERDUE_GRACE_S`) into the past and no
  reminder fired/closed it out. Reflects reality on the whiteboard.
- `cancelled`: user-intent. Invoked via the new `cancel_plan` tool when the
  user explicitly says "算了 / 取消吧 / 不做了". Prefer over `delete_plan`
  so the row survives for audit + "辰辰那 2 个取消的计划" queries.

Schema story:

- SQLite: the Enum column is physically a VARCHAR — no ALTER needed at the
  DB layer. The authoritative change is the Python enum definition in
  `planagent.db.models.PlanStatus`. This migration is a documented no-op
  on SQLite so `alembic upgrade head` on an existing 0005 database still
  advances the `alembic_version` pointer cleanly.
- PostgreSQL: real ENUM type, would need `ALTER TYPE ... ADD VALUE`. Guarded
  below behind `bind.dialect.name == "postgresql"` so the SQLite-only dev
  path stays untouched. Kept non-transactional via a connection-level
  AUTOCOMMIT since `ALTER TYPE ... ADD VALUE` can't run inside a txn on PG.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006_plan_status_overdue_cancelled"
down_revision: str | None = "0005_cross_user_notes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_NEW_VALUES: tuple[str, ...] = ("overdue", "cancelled")


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        # No-op: Enum stored as VARCHAR, new values already accepted.
        return
    if dialect == "postgresql":
        # `ALTER TYPE ... ADD VALUE` cannot run inside a transaction on PG.
        # Pop out to autocommit for the duration of these two statements.
        raw = bind.connection  # type: ignore[attr-defined]
        with raw.execution_options(isolation_level="AUTOCOMMIT"):
            for val in _NEW_VALUES:
                raw.exec_driver_sql(
                    f"ALTER TYPE plan_status ADD VALUE IF NOT EXISTS '{val}'"
                )
        return
    # Other dialects (MySQL, etc.): safest to no-op; the ORM will still
    # reject unknown values at the Python layer if the column is tightened
    # by hand.


def downgrade() -> None:
    # PG can't drop enum values without rewriting dependent columns; SQLite
    # has nothing to undo. Leave downgrade as a documented no-op.
    return
