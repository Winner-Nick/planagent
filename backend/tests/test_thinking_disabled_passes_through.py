"""PR-G bug #2 support: DeepSeekClient passes thinking={"type":"disabled"}.

Unit test. We intercept the outbound HTTP request via a stub httpx transport
and inspect the JSON body. No real API key required.
"""

from __future__ import annotations

import json

import httpx

from planagent.config import Settings
from planagent.llm.deepseek import DeepSeekClient

_FAKE_RESPONSE_BODY = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek-chat",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _CaptureTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.last_body: dict | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.last_body = json.loads(request.content)
        return httpx.Response(
            200,
            json=_FAKE_RESPONSE_BODY,
            headers={"content-type": "application/json"},
        )


def _client_with_transport(transport: _CaptureTransport) -> DeepSeekClient:
    # Build a DeepSeekClient with a fake settings object and monkey in a
    # transport-capturing httpx.Client. OpenAI SDK accepts `http_client`.
    # Settings uses pydantic aliases on its env vars; the populate-by-field
    # constructor still works if we pass the alias names as kwargs when
    # populate_by_name is off. Safer path: set env vars before constructing.
    import os

    os.environ["DEEPSEEK_API_KEY"] = "sk-dummy"
    os.environ["DEEPSEEK_BASE_URL"] = "https://example.invalid/v1"
    os.environ["DEEPSEEK_MODEL"] = "deepseek-chat"
    settings = Settings()  # type: ignore[call-arg]
    dc = DeepSeekClient(settings=settings)
    # Swap the underlying OpenAI client's http transport. The OpenAI SDK
    # exposes `_client` (httpx.Client) — we replace it with one using our
    # capture transport.
    dc._client._client = httpx.Client(  # type: ignore[attr-defined]
        transport=transport,
        base_url="https://example.invalid/v1",
    )
    return dc


def test_thinking_disabled_by_default() -> None:
    transport = _CaptureTransport()
    dc = _client_with_transport(transport)
    dc.chat(messages=[{"role": "user", "content": "hi"}])
    assert transport.last_body is not None
    # `extra_body` in the SDK is merged into the JSON body directly, so the
    # payload on the wire should carry `thinking` at the top level.
    assert transport.last_body.get("thinking") == {"type": "disabled"}


def test_thinking_explicit_override() -> None:
    transport = _CaptureTransport()
    dc = _client_with_transport(transport)
    dc.chat(
        messages=[{"role": "user", "content": "hi"}],
        thinking={"type": "enabled"},
    )
    assert transport.last_body is not None
    assert transport.last_body.get("thinking") == {"type": "enabled"}
