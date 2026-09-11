"""Tools the model can call, and the mapping from a call to a policy-checkable ActionIntent."""

from __future__ import annotations

from typing import Any

from cua.agent.llm import ToolCall, ToolSpec
from cua.artifact.actions import ActionIntent
from cua.surface.base import Observation, UnknownRef

_REASON = {"type": "string", "description": "One sentence: why this action moves toward the goal."}
_REF = {"type": "string", "description": "The [ref=...] id of the element, copied from the snapshot."}


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": {**props, "reason": _REASON}, "required": [*required, "reason"]}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("navigate", "Go to a URL inside the application.", _schema({"url": {"type": "string"}}, ["url"])),
    ToolSpec("click", "Click a link, button, or other control.", _schema({"ref": _REF}, ["ref"])),
    ToolSpec(
        "type",
        "Replace the contents of a text field. When the task provides a {{secret:NAME}} or "
        "{{input:NAME}} reference for a value, pass that reference verbatim; the system substitutes it.",
        _schema({"ref": _REF, "text": {"type": "string"}}, ["ref", "text"]),
    ),
    ToolSpec(
        "select",
        "Choose an option in a dropdown by its visible label.",
        _schema({"ref": _REF, "option": {"type": "string"}}, ["ref", "option"]),
    ),
    ToolSpec("press", "Press a keyboard key, e.g. Enter.", _schema({"key": {"type": "string"}}, ["key"])),
    ToolSpec(
        "extract",
        "Read a value shown on screen and record it as a named output of the task.",
        _schema(
            {
                "ref": _REF,
                "name": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
                "sensitive": {
                    "type": "boolean",
                    "description": "true if the value is financial data or personal information",
                },
            },
            ["ref", "name", "sensitive"],
        ),
    ),
    ToolSpec(
        "done",
        "Declare the goal fully achieved. Only after every required value has been extracted.",
        _schema({"summary": {"type": "string", "description": "What was accomplished, in one or two sentences."}}, ["summary"]),
    ),
    ToolSpec(
        "stuck",
        "Declare that you cannot make safe progress and a human operator should take over.",
        _schema({}, []),
    ),
)

ACTION_TOOLS = frozenset({"navigate", "click", "type", "select", "press", "extract"})
TERMINAL_TOOLS = frozenset({"done", "stuck"})


def intent_from_call(call: ToolCall, observation: Observation) -> ActionIntent:
    args = call.arguments
    if call.name == "navigate":
        return ActionIntent(type="navigate", url=str(args.get("url", "")))
    if call.name == "press":
        return ActionIntent(type="press", key=str(args.get("key", "")))
    ref = str(args.get("ref", ""))
    element = observation.elements.get(ref)
    if element is None:
        raise UnknownRef(ref)
    value: str | None = None
    if call.name == "type":
        value = str(args.get("text", ""))
    elif call.name == "select":
        value = str(args.get("option", ""))
    return ActionIntent(
        type=call.name,  # type: ignore[arg-type]
        control_name=element.name,
        control_role=element.role,
        value=value,
    )
