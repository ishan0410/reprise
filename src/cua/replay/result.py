"""
The replay result contract: what an AI agent gets back from invoking a capability.

Three top-level statuses, deliberately distinct:
    success           the flow completed and the success condition was verified; outputs are populated
    business_outcome  the application gave a legitimate non-happy answer ("no such member");
                      the caller needs it, nothing is broken
    failure           the flow could not complete; `error` says what step, what was expected,
                      what was observed, and whether retrying could help
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.escalation.session import ControlEvent

ReplayStatus = Literal["success", "business_outcome", "failure"]
ErrorCategory = Literal[
    "input",  # caller supplied bad parameters
    "artifact",  # the capability itself is unusable (deprecated, malformed)
    "policy",  # refused by the allowlist or awaiting confirmation of a risky step
    "target",  # a control could not be located
    "checkpoint",  # the expected state was not reached
    "authorization",  # declared failure pattern: permission denied
    "authentication",  # declared failure pattern: credentials rejected
    "application",  # declared failure pattern: app error page
    "validation",  # declared failure pattern: validation the caller cannot fix by retrying
    "recovery",  # a recoverable condition recurred beyond its budget or its recovery failed
    "surface",  # the driver could not perform an action
    "internal",
]


class StepReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    phase: str
    action: str
    description: str = ""
    status: Literal["ok", "failed", "skipped"]
    #: Which locator candidate resolved the target (0 = primary). Anything above 0 is drift.
    candidate_index: int | None = None
    used_fallback: bool = False
    locator: str | None = None
    duration_ms: int = 0
    detail: str = ""
    url_after: str = ""


class RecoveryReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    step_id: str
    attempt: int
    action: str
    succeeded: bool


class OutcomeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    description: str = ""
    step_id: str


class ReplayError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    category: ErrorCategory
    message: str
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    retryable: bool = False
    screenshot: str | None = None
    #: Locator attempts, when a target could not be resolved.
    attempts: list[str] = Field(default_factory=list)


class DriftReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Steps whose primary locator failed and a fallback resolved the control.
    fallback_steps: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def detected(self) -> bool:
        return bool(self.fallback_steps)


class ReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ReplayStatus
    capability_id: str
    capability_name: str
    capability_version: str
    run_id: str
    #: Sensitive inputs appear as {{input:NAME}}.
    inputs: dict[str, str] = Field(default_factory=dict)
    #: Typed per the artifact's output schema (money -> float, integer -> int, ...).
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: OutcomeReport | None = None
    error: ReplayError | None = None
    steps: list[StepReport] = Field(default_factory=list)
    recoveries: list[RecoveryReport] = Field(default_factory=list)
    drift: DriftReport = Field(default_factory=DriftReport)
    #: Who held the live session, and when. Non-empty only if a human was brought in.
    control_events: list[ControlEvent] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    evidence_dir: str | None = None

    def redacted(self, sensitive_outputs: list[str]) -> ReplayResult:
        """A copy safe to persist: sensitive outputs replaced by placeholders."""
        outputs = {k: (f"[REDACTED:{k}]" if k in sensitive_outputs else v) for k, v in self.outputs.items()}
        return self.model_copy(update={"outputs": outputs})
