"""Structured JSON logging helpers.

We capture stderr via a fresh `StreamHandler` bound to a StringIO buffer
and then call each public log helper. Every line must be valid JSON with
the documented event name + fields.
"""

from __future__ import annotations

import io
import json
import logging

from planagent.logutil import (
    JSONFormatter,
    log_event,
    log_handler_failed,
    log_inbound_received,
    log_outbound_sent,
    log_pending_outbound_flushed,
    log_reminder_fired,
    log_wakeup_decision,
)


def _install_capture() -> tuple[io.StringIO, logging.Handler, logging.Handler | None]:
    """Attach a JSON-formatted StreamHandler capturing into an io.StringIO.

    Returns (buffer, installed_handler, previously_installed_default) so the
    caller can restore the logging state.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    # Preserve the existing configured level so pytest-caplog keeps working
    # in sibling tests.
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    return buf, handler, prev_level  # type: ignore[return-value]


def _drain(buf: io.StringIO) -> list[dict]:
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def test_log_event_emits_one_line_of_json():
    buf, handler, prev_level = _install_capture()
    try:
        log_event("custom_event", foo="bar", count=3)
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "custom_event"
    assert evt["foo"] == "bar"
    assert evt["count"] == 3
    assert evt["level"] == "INFO"
    assert "ts" in evt


def test_inbound_received_shape():
    buf, handler, prev_level = _install_capture()
    try:
        log_inbound_received(
            session_name="peng",
            wechat_user_id="u_123",
            text="hello world",
            context_token="ctx_abc",
        )
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "inbound_received"
    assert evt["session_name"] == "peng"
    assert evt["wechat_user_id"] == "u_123"
    assert evt["text_preview"] == "hello world"
    assert evt["context_token"] == "ctx_abc"


def test_outbound_sent_shape_and_truncation():
    long_text = "x" * 500
    buf, handler, prev_level = _install_capture()
    try:
        log_outbound_sent(
            session_name="peng",
            target_user_id="u_321",
            text=long_text,
            client_id="planagent-deadbeef",
        )
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "outbound_sent"
    assert evt["session_name"] == "peng"
    assert evt["target_user_id"] == "u_321"
    assert evt["client_id"] == "planagent-deadbeef"
    # preview capped
    assert len(evt["text_preview"]) <= 80
    assert evt["text_preview"].endswith("…") or evt["text_preview"] == long_text


def test_reminder_fired_shape():
    buf, handler, prev_level = _install_capture()
    try:
        log_reminder_fired(
            plan_id="plan_1",
            owner="peng",
            fire_at="2026-04-23T10:00:00+00:00",
            message="time to start",
        )
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "reminder_fired"
    assert evt["plan_id"] == "plan_1"
    assert evt["owner"] == "peng"
    assert evt["fire_at"].startswith("2026-04-23")
    assert evt["message_preview"] == "time to start"


def test_wakeup_decision_shape():
    buf, handler, prev_level = _install_capture()
    try:
        log_wakeup_decision(
            session_name="peng",
            should_ping=True,
            reason="24h_window_closing",
        )
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "wakeup_decision"
    assert evt["session_name"] == "peng"
    assert evt["should_ping"] is True
    assert evt["reason"] == "24h_window_closing"


def test_pending_outbound_flushed_shape():
    buf, handler, prev_level = _install_capture()
    try:
        log_pending_outbound_flushed(pending_id="po_1", target_user_id="u_42")
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "pending_outbound_flushed"
    assert evt["pending_id"] == "po_1"
    assert evt["target_user_id"] == "u_42"


def test_handler_failed_shape_and_level():
    buf, handler, prev_level = _install_capture()
    try:
        log_handler_failed(
            session_name="peng",
            error="connection refused",
            exc_type="ConnectionError",
        )
    finally:
        logging.getLogger().removeHandler(handler)
        logging.getLogger().setLevel(prev_level)

    events = _drain(buf)
    assert len(events) == 1
    evt = events[0]
    assert evt["event"] == "handler_failed"
    assert evt["level"] == "ERROR"
    assert evt["exc_type"] == "ConnectionError"
    assert "connection refused" in evt["error"]
