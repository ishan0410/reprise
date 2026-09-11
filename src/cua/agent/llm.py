"""
Provider-agnostic model interface for the discovery loop.

One decide() call per step: the model gets the system prompt, a single
user message (goal, history, current observation), optionally a
screenshot, and the tool definitions; it returns tool calls. Keeping each
step stateless avoids juggling provider-specific multi-turn tool-result
formats and keeps token cost bounded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelRequest:
    system: str
    user_text: str
    screenshot_png: bytes | None
    tools: Sequence[ToolSpec]


@dataclass
class ModelTurn:
    tool_calls: list[ToolCall]
    provider: str
    model: str
    text: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    #: Provider-assigned response id, when available. Part of the "this really happened" evidence.
    response_id: str | None = None


class ProviderBusy(Exception):
    """Rate limited (429) or over capacity (503). Triggers fallback to the next provider."""


class ProviderError(Exception):
    """Any other provider failure."""


class LLMProvider(ABC):
    name: str
    model: str

    @property
    @abstractmethod
    def supports_vision(self) -> bool: ...

    @abstractmethod
    def decide(self, request: ModelRequest) -> ModelTurn: ...


FallbackListener = Callable[[str, str, str], None]  # (from_provider, to_provider, reason)


class FallbackProvider(LLMProvider):
    """Tries providers in order; a ProviderBusy moves permanently to the next one for this run."""

    name = "fallback"

    def __init__(self, providers: Sequence[LLMProvider], on_fallback: FallbackListener | None = None) -> None:
        if not providers:
            raise ValueError("at least one provider is required")
        self._providers = list(providers)
        self._index = 0
        self._on_fallback = on_fallback

    @property
    def current(self) -> LLMProvider:
        return self._providers[self._index]

    @property
    def model(self) -> str:  # type: ignore[override]
        return self.current.model

    @property
    def supports_vision(self) -> bool:
        return self.current.supports_vision

    def decide(self, request: ModelRequest) -> ModelTurn:
        while True:
            current = self.current
            try:
                return current.decide(request)
            except ProviderBusy as ex:
                if self._index + 1 >= len(self._providers):
                    raise
                self._index += 1
                if self._on_fallback:
                    self._on_fallback(current.name, self.current.name, str(ex))
