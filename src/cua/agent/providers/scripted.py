"""
Deterministic stand-in for a model: replays a script of tool calls.

Used by the test suite and by the offline demo (no API keys). A script
entry may name its element by {"role": ..., "name": ...}; it is resolved
to the ref present in the current observation, exactly as a model would
pick one from the snapshot. If the element is not on screen the script
declares itself stuck, which is the honest failure mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cua.agent.llm import LLMProvider, ModelRequest, ModelTurn, ToolCall
from cua.surface.snapshot import parse_refs


class ScriptedProvider(LLMProvider):
    name = "scripted"

    def __init__(self, steps: Sequence[dict[str, Any]], model: str = "scripted-v1") -> None:
        self.model = model
        self._steps = list(steps)
        self._index = 0

    @property
    def supports_vision(self) -> bool:
        return False

    def decide(self, request: ModelRequest) -> ModelTurn:
        if self._index >= len(self._steps):
            return self._turn(ToolCall("stuck", {"reason": "script exhausted before the goal was declared done"}))
        step = self._steps[self._index]
        self._index += 1
        args: dict[str, Any] = dict(step.get("arguments", {}))
        element = args.pop("element", None)
        if element is not None:
            ref = self._find_ref(request.user_text, element)
            if ref is None:
                return self._turn(
                    ToolCall("stuck", {"reason": f"scripted element {element} is not in the current observation"})
                )
            args["ref"] = ref
        args.setdefault("reason", step.get("reason", "scripted"))
        return self._turn(ToolCall(str(step["tool"]), args))

    def _turn(self, call: ToolCall) -> ModelTurn:
        return ModelTurn(tool_calls=[call], provider=self.name, model=self.model)

    @staticmethod
    def _find_ref(user_text: str, element: dict[str, Any]) -> str | None:
        role = element.get("role")
        name = element.get("name")
        for info in parse_refs(user_text).values():
            if info.role == role and (name is None or info.name == name):
                return info.ref
        return None
