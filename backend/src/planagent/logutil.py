"""Structured (one-JSON-line-per-event) logging for the bridge runtime.

This module is intentionally free of project-internal imports so it can be
pulled into the bridge's `main()` before anything heavy is touched.

Design:
  - `setup_json_logging(level)` installs a JSON formatter on the root
    logger. Idempotent; safe to call more than once.
  - `log_event(name, **fields)` emits a single event with a stable shape:
    `{"ts": "...", "level": "INFO", "event": <name>, ...fields}`.
  - Named helpers (`log_inbound_received`, `log_outbound_sent`, ...) wrap
    `log_event` with the event name + field contract the bridge promises.

All helpers no-op on failure so a broken formatter cannot take down the
bridge. Previews are truncated to 80 chars to keep log lines bounded.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_EVENT_LOGGER_NAME = "planagent.events"
_PREVIEW_MAX = 80


def _preview(text: str | None, limit: int = _PREVIEW_MAX) -> str:
    if not text:
        return ""
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


class JSONFormatter(logging.Formatter):
    """One-line JSON per record.

    Structured events (emitted via `log_event`) arrive with a `payload`
    dict attached to the record; we merge it into the top-level object.
    Plain `log.info("...")` calls still serialize to JSON, with `event`
    defaulting to `"log"` and the formatted message under `message`.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        base: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
        }
        payload = getattr(record, "payload", None)
        if isinstance(payload, dict):
            base["event"] = payload.pop("event", "log")
            # Event fields win over any accidental collisions in base.
            base.update(payload)
        else:
            base["event"] = "log"
            base["message"] = record.getMessage()
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        try:
            return json.dumps(base, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            # Fallback: dump a minimal line rather than crash the handler.
            return json.dumps(
                {
                    "ts": ts,
                    "level": record.levelname,
                    "event": "log_serialize_error",
                    "logger": record.name,
                }
            )


def _default_log_dir() -> Path:
    return Path(os.path.expanduser("~/.planagent/logs"))


def setup_json_logging(
    level: int = logging.INFO,
    *,
    logfile: str | os.PathLike[str] | None = None,
    enable_file_rotation: bool = False,
) -> None:
    """Install the JSON formatter on the root logger.

    When `enable_file_rotation=True`, also attach a `TimedRotatingFileHandler`
    rolling daily at midnight, keeping 14 days of history. Default path is
    `~/.planagent/logs/bridge.log`. The file handler is best-effort: if the
    directory cannot be created we fall back to stderr only.
    """
    formatter = JSONFormatter()
    root = logging.getLogger()
    root.setLevel(level)

    # Replace any existing stderr stream handler's formatter; install one
    # if none exists. We don't wipe handlers so test harnesses (pytest,
    # caplog) keep their own.
    has_stream = False
    for h in root.handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.FileHandler
        ):
            h.setFormatter(formatter)
            has_stream = True
    if not has_stream:
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        root.addHandler(sh)

    if enable_file_rotation:
        try:
            path = Path(logfile) if logfile else (_default_log_dir() / "bridge.log")
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.TimedRotatingFileHandler(
                str(path), when="midnight", backupCount=14, encoding="utf-8"
            )
            fh.setFormatter(formatter)
            # Only add once — identify by the underlying file.
            already = any(
                isinstance(h, logging.handlers.TimedRotatingFileHandler)
                and getattr(h, "baseFilename", None) == fh.baseFilename
                for h in root.handlers
            )
            if already:
                fh.close()
            else:
                root.addHandler(fh)
        except OSError:
            logging.getLogger(__name__).warning(
                "file log rotation disabled: cannot open log dir"
            )


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured event line.

    Unknown values (datetimes, Paths, etc.) are coerced via `str(...)` in
    the formatter; callers should stick to primitives for readability.

    Defensive: `logging.config.fileConfig` defaults to
    `disable_existing_loggers=True`, which alembic's `env.py` triggers
    every time migrations run. That would otherwise silence our event
    logger partway through a test session. We undo the flag on every
    emit so the stream stays live no matter who reinitialized logging.
    """
    logger = logging.getLogger(_EVENT_LOGGER_NAME)
    if logger.disabled:
        logger.disabled = False
    payload = {"event": event, **fields}
    logger.log(level, event, extra={"payload": payload})


# --- Named helpers (the bridge's event contract) -----------------------------


def log_inbound_received(
    *,
    session_name: str,
    wechat_user_id: str | None,
    text: str | None,
    context_token: str | None,
) -> None:
    log_event(
        "inbound_received",
        session_name=session_name,
        wechat_user_id=wechat_user_id or "",
        text_preview=_preview(text),
        context_token=context_token or "",
    )


def log_outbound_sent(
    *,
    session_name: str,
    target_user_id: str,
    text: str,
    client_id: str | None,
) -> None:
    log_event(
        "outbound_sent",
        session_name=session_name,
        target_user_id=target_user_id,
        text_preview=_preview(text),
        client_id=client_id or "",
    )


def log_reminder_fired(
    *,
    plan_id: str,
    owner: str | None,
    fire_at: Any,
    message: str,
) -> None:
    log_event(
        "reminder_fired",
        plan_id=plan_id,
        owner=owner or "",
        fire_at=str(fire_at) if fire_at is not None else "",
        message_preview=_preview(message),
    )


def log_wakeup_decision(
    *,
    session_name: str,
    should_ping: bool,
    reason: str | None,
) -> None:
    log_event(
        "wakeup_decision",
        session_name=session_name,
        should_ping=bool(should_ping),
        reason=reason or "",
    )


def log_pending_outbound_flushed(
    *,
    pending_id: str,
    target_user_id: str,
) -> None:
    log_event(
        "pending_outbound_flushed",
        pending_id=pending_id,
        target_user_id=target_user_id,
    )


def log_handler_failed(
    *,
    session_name: str,
    error: str,
    exc_type: str,
) -> None:
    log_event(
        "handler_failed",
        level=logging.ERROR,
        session_name=session_name,
        error=_preview(error, limit=200),
        exc_type=exc_type,
    )


__all__ = [
    "JSONFormatter",
    "log_event",
    "log_handler_failed",
    "log_inbound_received",
    "log_outbound_sent",
    "log_pending_outbound_flushed",
    "log_reminder_fired",
    "log_wakeup_decision",
    "setup_json_logging",
]
