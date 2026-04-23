"""Load and persist bot credentials to `~/.planagent/credentials.json`."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

CRED_DIR = Path.home() / ".planagent"
CRED_PATH = CRED_DIR / "credentials.json"


def load_credentials() -> dict[str, Any] | None:
    if not CRED_PATH.is_file():
        return None
    try:
        with CRED_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_credentials(data: dict[str, Any]) -> None:
    CRED_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(CRED_DIR, 0o700)
    tmp = CRED_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.replace(tmp, CRED_PATH)
    with contextlib.suppress(OSError):
        os.chmod(CRED_PATH, 0o600)
