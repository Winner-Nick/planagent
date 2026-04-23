"""Verify migration 0002 adds last_context_token column with NULL default."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config

from alembic import command

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _cfg(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_migration_adds_column_and_existing_row_defaults_null(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    url = f"sqlite:///{db_file}"

    # Upgrade only to 0001_initial, then seed a GroupContext row that pre-dates 0002.
    command.upgrade(_cfg(url), "0001_initial")
    engine = sa.create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO group_contexts (id, wechat_group_id, created_at) "
                "VALUES ('g1', 'wx-grp', '2026-04-23T00:00:00+00:00')"
            )
        )

    # Now upgrade to head → column is added; pre-existing row gets NULL.
    command.upgrade(_cfg(url), "head")
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(group_contexts)"))}
        assert "last_context_token" in cols
        row = conn.execute(
            sa.text("SELECT last_context_token FROM group_contexts WHERE id='g1'")
        ).one()
        assert row[0] is None

        # Column is writable.
        conn.execute(
            sa.text("UPDATE group_contexts SET last_context_token='tok-42' WHERE id='g1'")
        )
        conn.commit()
        row = conn.execute(
            sa.text("SELECT last_context_token FROM group_contexts WHERE id='g1'")
        ).one()
        assert row[0] == "tok-42"
    engine.dispose()
