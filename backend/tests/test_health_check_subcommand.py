"""End-to-end test for the `--health-check` subcommand.

Spawns the real bridge CLI (no mocks) against a throwaway SQLite DB and a
fake PID file. We:

  * stage a PID file containing our own test-process PID (definitely
    alive) → expect exit 0 and JSON with `alive: true`
  * stage a PID file containing a guaranteed-dead PID (2**31 - 1) →
    expect exit 1 and JSON with `alive: false`
  * omit the PID file entirely → expect exit 1 with `pid: null`

The subprocess runs with a one-off DB so it doesn't interact with a
developer's real state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


@pytest.fixture()
def fresh_env(tmp_path: Path) -> dict[str, str]:
    db_path = tmp_path / f"test-{uuid.uuid4().hex}.db"
    env = os.environ.copy()
    env["PLANAGENT_DB_URL"] = f"sqlite+aiosqlite:///{db_path}"
    env["DEEPSEEK_API_KEY"] = env.get("DEEPSEEK_API_KEY", "test-key")
    env["WECHAT_BOT_TOKEN"] = env.get("WECHAT_BOT_TOKEN", "test-token")
    return env


def _run_health(pid_file: Path, env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "planagent.wechat.bridge",
            "--health-check",
            "--pid-file",
            str(pid_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout


def test_health_check_exits_zero_when_pid_alive(tmp_path: Path, fresh_env):
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text(str(os.getpid()))

    code, out = _run_health(pid_file, fresh_env)
    payload = json.loads(out)

    assert code == 0
    assert payload["alive"] is True
    assert payload["pid"] == os.getpid()
    # Structure sanity.
    assert "uptime_seconds" in payload
    assert "num_sessions" in payload
    assert "open_pending_outbounds" in payload
    assert payload["scheduler_alive"] is True


def test_health_check_exits_nonzero_when_pid_dead(tmp_path: Path, fresh_env):
    pid_file = tmp_path / "bridge.pid"
    dead_pid = 2**31 - 1  # max PID on most kernels; overwhelmingly unlikely to be live
    pid_file.write_text(str(dead_pid))

    code, out = _run_health(pid_file, fresh_env)
    payload = json.loads(out)

    assert code == 1
    assert payload["alive"] is False
    assert payload["pid"] == dead_pid
    assert payload["scheduler_alive"] is False


def test_health_check_exits_nonzero_when_pid_file_missing(tmp_path: Path, fresh_env):
    pid_file = tmp_path / "does-not-exist.pid"

    code, out = _run_health(pid_file, fresh_env)
    payload = json.loads(out)

    assert code == 1
    assert payload["alive"] is False
    assert payload["pid"] is None


def test_health_check_handles_garbage_pid_file(tmp_path: Path, fresh_env):
    pid_file = tmp_path / "bridge.pid"
    pid_file.write_text("not a number\n")

    code, out = _run_health(pid_file, fresh_env)
    payload = json.loads(out)

    assert code == 1
    assert payload["alive"] is False
    assert payload["pid"] is None
