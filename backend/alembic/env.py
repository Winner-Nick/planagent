from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from planagent.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return url
    # Read the DB URL from env directly — instantiating Settings would pull
    # DEEPSEEK_API_KEY and couple schema migrations to unrelated LLM secrets.
    raw = os.environ.get("PLANAGENT_DB_URL", "sqlite:///./planagent.db")
    # strip async driver if present so alembic can run synchronously
    if raw.startswith("sqlite+aiosqlite:///"):
        return "sqlite:///" + raw[len("sqlite+aiosqlite:///") :]
    return raw


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = _resolve_url()
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=url.startswith("sqlite"),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
