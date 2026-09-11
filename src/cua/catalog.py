"""
Agent-facing capability catalog (stretch goal).

Saved artifacts, presented the way an AI agent's tool-calling layer expects
them: a name, a description, a JSON Schema for the arguments, and a
description of what comes back. Invoking a tool runs the deterministic
replay engine; the agent never sees a browser, a locator, or a model.

Only the latest version of each capability is listed, and by default only
approved ones are invocable, which is the approval gate from the brief.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cua.artifact.schema import CapabilityArtifact, InputParam, OutputField
from cua.artifact.store import existing_versions, load_artifact
from cua.replay.engine import ReplayEngine
from cua.replay.result import ReplayResult

_INPUT_JSON_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean", "enum": "string"}
_OUTPUT_JSON_TYPES = {"string": "string", "money": "number", "number": "number", "integer": "integer", "boolean": "boolean", "enum": "string"}


class CapabilityNotFound(LookupError):
    pass


class CapabilityNotInvocable(PermissionError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    """The function-calling view of a capability. Serialises to the OpenAI/Gemini/Anthropic tool shape."""

    name: str
    description: str
    parameters: dict[str, Any]
    returns: dict[str, Any]
    version: str
    status: str
    outcomes: list[str]

    def as_tool(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


def _param_schema(p: InputParam) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _INPUT_JSON_TYPES[p.type]}
    if p.description:
        schema["description"] = p.description
    if p.pattern:
        schema["pattern"] = p.pattern
    if p.enum:
        schema["enum"] = p.enum
    if p.example is not None:
        schema["examples"] = [p.example]
    if p.sensitive:
        schema["x-sensitive"] = True
    return schema


def _output_schema(o: OutputField) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": _OUTPUT_JSON_TYPES[o.type]}
    if o.description:
        schema["description"] = o.description
    if o.enum:
        schema["enum"] = o.enum
    if o.sensitive:
        schema["x-sensitive"] = True
    return schema


def tool_definition(artifact: CapabilityArtifact) -> ToolDefinition:
    outcomes = [o.code for o in artifact.outcomes if o.terminal]
    outcome_note = f" Possible business outcomes instead of a result: {', '.join(outcomes)}." if outcomes else ""
    return ToolDefinition(
        name=artifact.name,
        description=artifact.description.rstrip(".") + "." + outcome_note,
        parameters={
            "type": "object",
            "properties": {p.name: _param_schema(p) for p in artifact.inputs},
            "required": [p.name for p in artifact.inputs if p.required],
            "additionalProperties": False,
        },
        returns={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success", "business_outcome", "failure"]},
                "outputs": {"type": "object", "properties": {o.name: _output_schema(o) for o in artifact.outputs}},
                "outcome": {"type": ["object", "null"], "description": "code + description when status is business_outcome"},
                "error": {"type": ["object", "null"], "description": "code, category, step, expected, observed when status is failure"},
            },
        },
        version=artifact.version,
        status=artifact.status,
        outcomes=outcomes,
    )


class Catalog:
    def __init__(self, root: Path, *, require_approved: bool = True) -> None:
        self.root = root
        self.require_approved = require_approved

    def names(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(d.name for d in self.root.iterdir() if d.is_dir() and existing_versions(self.root, d.name))

    def latest(self, name: str) -> CapabilityArtifact:
        versions = existing_versions(self.root, name)
        if not versions:
            raise CapabilityNotFound(name)
        major, minor, patch = versions[-1]
        return load_artifact(self.root / name / f"v{major}.{minor}.{patch}.json")

    def tools(self) -> list[ToolDefinition]:
        return [tool_definition(self.latest(n)) for n in self.names()]

    def describe(self, name: str) -> ToolDefinition:
        return tool_definition(self.latest(name))

    def invoke(self, name: str, arguments: dict[str, Any], engine: ReplayEngine) -> ReplayResult:
        artifact = self.latest(name)
        if self.require_approved and artifact.status != "approved":
            raise CapabilityNotInvocable(f"{name} v{artifact.version} is {artifact.status}; approve it before agents may invoke it")
        return engine.run(artifact, {k: str(v) for k, v in arguments.items()})


def result_for_agent(result: ReplayResult, artifact: CapabilityArtifact) -> dict[str, Any]:
    """The compact, typed payload an agent gets back. Sensitive outputs are included: the agent is the caller."""
    payload: dict[str, Any] = {"status": result.status, "outputs": result.outputs}
    if result.outcome:
        payload["outcome"] = {"code": result.outcome.code, "description": result.outcome.description}
    if result.error:
        e = result.error
        payload["error"] = {"code": e.code, "category": e.category, "message": e.message, "step": e.step_id,
                            "expected": e.expected, "observed": e.observed, "retryable": e.retryable}
    payload["capability"] = f"{artifact.name}@{artifact.version}"
    payload["run_id"] = result.run_id
    return payload
