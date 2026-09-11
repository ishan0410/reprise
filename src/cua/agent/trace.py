"""
The discovery trace: a faithful, typed record of what happened in a run.

This is *not* the artifact. It still contains ephemeral refs, model
metadata, and policy decisions. The artifact builder consumes it and emits
the clean capability; the trace stays in /evidence as proof and for debugging.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.actions import ActionIntent
from cua.artifact.targets import RecordedElement
from cua.escalation.session import ControlEvent
from cua.policy.gate import Decision

RunStatus = Literal["success", "stuck", "max_steps", "timeout", "error"]
StepStatus = Literal["ok", "refused", "error", "done", "stuck"]


class ObservationSummary(BaseModel):
    url: str
    title: str
    screenshot: str | None = None
    snapshot_chars: int = 0


class ModelSummary(BaseModel):
    provider: str
    model: str
    usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: int = 0
    response_id: str | None = None


class StepResult(BaseModel):
    status: StepStatus
    detail: str = ""
    extracted_name: str | None = None
    extracted_value: str | None = None


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int
    observation: ObservationSummary
    model: ModelSummary
    tool: str
    arguments: dict[str, Any]
    reason: str = ""
    intent: ActionIntent | None = None
    decision: Decision | None = None
    #: Captured just before acting on a ref-targeted element: the artifact builder's raw material.
    element: RecordedElement | None = None
    result: StepResult
    url_after: str = ""


class ProviderEvent(BaseModel):
    step: int
    from_provider: str
    to_provider: str
    reason: str


class Trace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    goal: str
    start_url: str
    capability_name: str
    #: Sensitive inputs are recorded as {{input:NAME}} references, never as values.
    inputs: dict[str, str]
    sensitive_inputs: list[str] = Field(default_factory=list)
    steps: list[TraceStep] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    sensitive_outputs: list[str] = Field(default_factory=list)
    status: RunStatus = "error"
    summary: str = ""
    final_url: str = ""
    final_title: str = ""
    started_at: str = ""
    finished_at: str = ""
    provider_events: list[ProviderEvent] = Field(default_factory=list)
    control_events: list[ControlEvent] = Field(default_factory=list)
