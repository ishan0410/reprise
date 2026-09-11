"""Google Gemini provider (google-genai SDK). Vision + forced function calling."""

from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import errors, types

from cua.agent.llm import LLMProvider, ModelRequest, ModelTurn, ProviderBusy, ProviderError, ToolCall

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, *, supports_vision: bool = True) -> None:
        self.model = model
        self._supports_vision = supports_vision
        self._client = genai.Client(api_key=api_key)

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    def decide(self, request: ModelRequest) -> ModelTurn:
        parts: list[types.Part] = [types.Part.from_text(text=request.user_text)]
        if request.screenshot_png and self._supports_vision:
            parts.append(types.Part.from_bytes(data=request.screenshot_png, mime_type="image/png"))
        declarations = [
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in request.tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=request.system,
            tools=[types.Tool(function_declarations=declarations)],
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.ANY)
            ),
            temperature=0,
        )
        t0 = time.monotonic()
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=[types.Content(role="user", parts=parts)], config=config
            )
        except errors.APIError as ex:
            if ex.code in (429, 503):
                raise ProviderBusy(f"gemini {ex.code}: {ex.message}") from ex
            raise ProviderError(f"gemini {ex.code}: {ex.message}") from ex
        latency_ms = int((time.monotonic() - t0) * 1000)

        calls = [ToolCall(name=fc.name or "", arguments=dict(fc.args or {})) for fc in (resp.function_calls or [])]
        usage: dict[str, int] = {}
        meta = resp.usage_metadata
        if meta is not None:
            usage = {
                "input_tokens": int(meta.prompt_token_count or 0),
                "output_tokens": int(meta.candidates_token_count or 0),
            }
        text: str | None = None
        if not calls:
            text = _safe_text(resp)
        return ModelTurn(
            tool_calls=calls,
            provider=self.name,
            model=self.model,
            text=text,
            usage=usage,
            latency_ms=latency_ms,
            response_id=getattr(resp, "response_id", None),
        )


def _safe_text(resp: Any) -> str | None:
    try:
        value = resp.text
    except (ValueError, AttributeError):
        return None
    return str(value) if value else None
