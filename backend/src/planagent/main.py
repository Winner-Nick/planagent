from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from alembic import command
from planagent.api import groups as groups_api
from planagent.api import plans as plans_api
from planagent.config import get_settings
from planagent.db import dispose_engine, init_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # Alembic migrations run with the sync driver.
    sync_url = db_url
    if sync_url.startswith("sqlite+aiosqlite:///"):
        sync_url = "sqlite:///" + sync_url[len("sqlite+aiosqlite:///") :]
    cfg.set_main_option("sqlalchemy.url", sync_url)
    return cfg


def run_migrations(db_url: str) -> None:
    command.upgrade(_alembic_config(db_url), "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    run_migrations(settings.db_url)
    init_engine(settings.db_url)
    try:
        yield
    finally:
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="planagent", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(plans_api.router, prefix="/api/v1")
    app.include_router(groups_api.router, prefix="/api/v1")
    return app


app = create_app()
