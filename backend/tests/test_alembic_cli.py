"""Regression: `alembic upgrade head` must not require DEEPSEEK_API_KEY.

env.py previously instantiated the full Settings object to resolve the DB
URL, which coupled schema migrations to an unrelated LLM secret and broke
DB-only workflows.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_without_deepseek_key(tmp_path) -> None:
    db_file = tmp_path / "mig.db"
    env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    env["PLANAGENT_DB_URL"] = f"sqlite:///{db_file}"
    env["PYTHONPATH"] = str(BACKEND_DIR / "src") + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert db_file.exists()
