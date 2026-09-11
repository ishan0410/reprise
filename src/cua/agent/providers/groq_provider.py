"""Groq provider (OpenAI-compatible chat completions). Vision is optional per model."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, cast

from groq import APIConnectionError, APIStatusError, Groq, RateLimitError

from cua.agent.llm import LLMProvider, ModelRequest, ModelTurn, ProviderBusy, ProviderError, ToolCall

DEFAULT_MODEL = "llama-3.3-70b-versatile"


class GroqProvider(LLMProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, supports_vision: bool = False) -> None:
        self.model = model
        self._supports_vision = supports_vision
        self._client = Groq(api_key=api_key)

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    def decide(self, request: ModelRequest) -> ModelTurn:
        content: Any = request.user_text
        if request.screenshot_png and self._supports_vision:
            b64 = base64.b64encode(request.screenshot_png).decode()
            content = [
                {"type": "text", "text": request.user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]
        tools = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in request.tools
        ]
        t0 = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=cast(Any, [{"role": "system", "content": request.system}, {"role": "user", "content": content}]),
                tools=cast(Any, tools),
                tool_choice="required",
                temperature=0,
            )
        except RateLimitError as ex:
            raise ProviderBusy(f"groq 429: {ex}") from ex
        except APIStatusError as ex:
            if ex.status_code in (502, 503, 529):
                raise ProviderBusy(f"groq {ex.status_code}: {ex}") from ex
            raise ProviderError(f"groq {ex.status_code}: {ex}") from ex
        except APIConnectionError as ex:
            raise ProviderError(f"groq connection error: {ex}") from ex
        latency_ms = int((time.monotonic() - t0) * 1000)

        message = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in message.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(name=tc.function.name, arguments=args if isinstance(args, dict) else {}))
        usage: dict[str, int] = {}
        if resp.usage is not None:
            usage = {"input_tokens": int(resp.usage.prompt_tokens), "output_tokens": int(resp.usage.completion_tokens)}
        return ModelTurn(
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            text=None if calls else (message.content or None),
            usage=usage,
            latency_ms=latency_ms,
            response_id=resp.id,
        )
