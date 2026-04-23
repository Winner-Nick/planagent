from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from openai import OpenAI

from planagent.config import Settings, get_settings

# PR-G: DeepSeek's `thinking` API accepts {"type": "enabled"|"disabled"}.
# Docs: https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
# We default to DISABLED because the reasoning channel otherwise gets written
# into `content`, which leaks chain-of-thought into the user-visible reply —
# see bug #2 of PR-G. Callers that explicitly want reasoning can override.
_DEFAULT_THINKING: dict[str, Any] = {"type": "disabled"}


class DeepSeekClient:
    """Thin wrapper over the OpenAI SDK pointed at DeepSeek.

    Exposes only what the agent actually needs: a single `chat` call that
    accepts tools and returns the raw ChatCompletion object. The caller is
    responsible for interpreting tool_calls — this layer does not decide
    anything on the LLM's behalf.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = OpenAI(
            api_key=self._settings.deepseek_api_key,
            base_url=self._settings.deepseek_base_url,
        )

    @property
    def model(self) -> str:
        return self._settings.deepseek_model

    def chat(
        self,
        messages: Iterable[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        thinking: dict[str, Any] | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if response_format is not None:
            kwargs["response_format"] = response_format

        # `thinking` rides in `extra_body` because the OpenAI SDK doesn't
        # surface DeepSeek-specific fields. Always pass a default of
        # `disabled` so chain-of-thought never leaks into `content`; callers
        # can pass `thinking={"type": "enabled"}` to opt back in.
        effective_thinking = thinking if thinking is not None else _DEFAULT_THINKING
        kwargs["extra_body"] = {"thinking": effective_thinking}
        return self._client.chat.completions.create(**kwargs)
