"""Migration 0003 round-trip: add `bot_sessions`, relax group_members."""

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


def test_migration_creates_bot_sessions_and_relaxes_group_members(tmp_path) -> None:
    db_file = tmp_path / "mig3.db"
    url = f"sqlite:///{db_file}"

    # Pre-seed at 0002: group + member with wechat_user_id NOT NULL.
    command.upgrade(_cfg(url), "0002_group_last_context_token")
    engine = sa.create_engine(url, future=True)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO group_contexts (id, wechat_group_id, created_at) "
                "VALUES ('g1', 'wx-grp', '2026-04-23T00:00:00+00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO group_members (id, group_id, wechat_user_id, is_bot) "
                "VALUES ('m1', 'g1', 'wx-user-legacy', 0)"
            )
        )

    # Upgrade to head.
    command.upgrade(_cfg(url), "head")

    with engine.connect() as conn:
        # bot_sessions table + all expected columns.
        cols = {row[1] for row in conn.execute(sa.text("PRAGMA table_info(bot_sessions)"))}
        expected = {
            "id",
            "group_id",
            "name",
            "wechat_user_id",
            "bot_user_id",
            "bot_token",
            "baseurl",
            "display_name",
            "last_inbound_at",
            "last_outbound_at",
            "last_wakeup_ping_at",
            "last_context_token",
            "created_at",
            "updated_at",
        }
        assert expected.issubset(cols), f"missing columns: {expected - cols}"

        # Can insert a BotSession with NULL wechat_user_id (pre-first-inbound).
        conn.execute(
            sa.text(
                "INSERT INTO bot_sessions "
                "(id, group_id, name, bot_token, created_at, updated_at) "
                "VALUES ('bs1', 'g1', 'peng', 'tok-123', "
                "'2026-04-23T00:00:00+00:00', '2026-04-23T00:00:00+00:00')"
            )
        )
        conn.commit()
        row = conn.execute(
            sa.text("SELECT wechat_user_id, bot_token FROM bot_sessions WHERE id='bs1'")
        ).one()
        assert row[0] is None
        assert row[1] == "tok-123"

        # Later we can stamp wechat_user_id.
        conn.execute(
            sa.text(
                "UPDATE bot_sessions SET wechat_user_id='wx-user-peng' WHERE id='bs1'"
            )
        )
        conn.commit()

        # group_members.wechat_user_id is now nullable — can insert NULL.
        conn.execute(
            sa.text(
                "INSERT INTO group_members (id, group_id, wechat_user_id, is_bot) "
                "VALUES ('m2', 'g1', NULL, 0)"
            )
        )
        conn.commit()
        # Pre-existing row must still be there and addressable.
        row = conn.execute(
            sa.text("SELECT wechat_user_id FROM group_members WHERE id='m1'")
        ).one()
        assert row[0] == "wx-user-legacy"

    engine.dispose()
