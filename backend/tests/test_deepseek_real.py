"""Real DeepSeek smoke tests — NO mocks.

These hit the live API. They assert structural properties (shape of the
response, presence of fields, validity of tool-call JSON), not exact strings,
because the LLM output varies run to run.
"""

from __future__ import annotations

import json

import pytest

from planagent.llm.deepseek import DeepSeekClient


@pytest.fixture(scope="module")
def client() -> DeepSeekClient:
    return DeepSeekClient()


@pytest.mark.real_api
def test_plain_chat_roundtrip(client: DeepSeekClient) -> None:
    """The simplest possible call must succeed and return non-empty text."""
    resp = client.chat(
        messages=[
            {"role": "system", "content": "Reply with a single short sentence."},
            {"role": "user", "content": "Say hi."},
        ],
        temperature=0.0,
    )
    assert resp.choices, "expected at least one choice"
    msg = resp.choices[0].message
    assert msg.role == "assistant"
    assert msg.content and len(msg.content.strip()) > 0


@pytest.mark.real_api
def test_function_calling_emits_tool_call(client: DeepSeekClient) -> None:
    """DeepSeek must be willing to emit a tool_call when an obvious tool fits.

    We do NOT assert the exact argument value — only that a tool_call is
    produced, the function name matches, and arguments parse as JSON.
    """
    tools = [
        {
            "type": "function",
            "function": {
                "name": "create_plan_draft",
                "description": "Create a draft plan from a free-form user description.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Plan title"},
                    },
                    "required": ["title"],
                },
            },
        }
    ]
    resp = client.chat(
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a plan manager. When the user describes a new plan, "
                    "you MUST call create_plan_draft with a concise title. Do not reply in text."
                ),
            },
            {
                "role": "user",
                "content": "I want to start learning Rust next week, about 30 minutes a day.",
            },
        ],
        tools=tools,
        tool_choice="auto",
        temperature=0.0,
    )
    msg = resp.choices[0].message
    assert msg.tool_calls, f"expected a tool_call, got: {msg.content!r}"
    call = msg.tool_calls[0]
    assert call.function.name == "create_plan_draft"
    args = json.loads(call.function.arguments)
    assert "title" in args
    assert isinstance(args["title"], str) and args["title"].strip()
