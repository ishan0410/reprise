"""
The capability artifact: a typed, versioned, reviewable description of a
recorded flow that an AI agent can invoke and the replay engine can execute
without a model in the loop.

Reading order mirrors how a reviewer thinks about it:
    identity  -> contract (inputs, outputs) -> behaviour (steps, success)
    -> declared conditions (outcomes, recoverables, failures)
    -> policy (what it may touch) -> provenance (where it came from)

Nothing in here is model-specific. Ephemeral refs, prompts, and model
reasoning stay in the discovery trace under /evidence; the artifact holds
only what replay needs plus what a human needs to review it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.actions import ActionType, RiskLevel
from cua.artifact.targets import Locator, TargetDescriptor
from cua.policy.model import Policy

SCHEMA_VERSION = "1.0"

Phase = Literal["bootstrap", "main"]
ArtifactStatus = Literal["draft", "approved", "deprecated"]
InputType = Literal["string", "integer", "number", "boolean", "enum"]
OutputType = Literal["string", "money", "number", "integer", "boolean", "enum"]


# ------------------------------------------------------------------ identity
class TargetSurface(BaseModel):
    """Which application, on which kind of surface, in which environment."""

    model_config = ConfigDict(extra="forbid")

    #: Selects the surface adapter. Only "web" is implemented; "legacy_web" / "desktop" are the extension points.
    kind: Literal["web", "legacy_web", "desktop"] = "web"
    #: Logical application id shared by every tenant running the same vendor product.
    app: str
    app_version: str | None = None
    #: Environment binding. Steps refer to it as {{base_url}}; a tenant supplies its own.
    base_url: str
    #: bbox locator candidates are only meaningful at the recorded viewport.
    viewport: dict[str, int] = Field(default_factory=lambda: {"width": 1280, "height": 900})


# ------------------------------------------------------------------ contract
class InputParam(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: InputType = "string"
    description: str = ""
    required: bool = True
    #: Never shown to a model, never persisted in logs; passed as {{input:NAME}} and substituted at act time.
    sensitive: bool = False
    pattern: str | None = None
    enum: list[str] | None = None
    example: str | None = None


class OutputField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: OutputType = "string"
    description: str = ""
    #: Redacted in evidence; returned to the caller, whose job it is to handle it appropriately.
    sensitive: bool = False
    enum: list[str] | None = None
    #: The step that extracts it, or None when a declared outcome sets it.
    extracted_by: str | None = None


# ---------------------------------------------------------------- behaviour
class Checkpoint(BaseModel):
    """A state assertion. Replay verifies it rather than assuming an action worked."""

    model_config = ConfigDict(extra="forbid")

    description: str = ""
    #: Glob on the URL path, after {{input:...}} substitution, e.g. "/members/{{input:member_id}}".
    url_pattern: str | None = None
    title_contains: str | None = None
    #: All must be visible.
    present: list[Locator] = Field(default_factory=list)
    #: None may be visible.
    absent: list[Locator] = Field(default_factory=list)
    timeout_ms: int = 10_000


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    phase: Phase = "main"
    action: ActionType
    #: Why this step exists, in the operator's words. For reviewers.
    description: str = ""
    target: TargetDescriptor | None = None
    #: type/select: a template; may contain {{input:NAME}} and {{secret:NAME}} references.
    value: str | None = None
    #: navigate: a template, normally "{{base_url}}/path".
    url: str | None = None
    #: press
    key: str | None = None
    #: extract: the output field this step fills.
    output: str | None = None
    risk: RiskLevel = "safe"
    #: Expected state after the step. Absent for steps that do not change the page.
    checkpoint: Checkpoint | None = None


class SuccessCondition(Checkpoint):
    #: Outputs that must have been extracted for the run to count as a success.
    outputs_required: list[str] = Field(default_factory=list)


# ------------------------------------------------------- declared conditions
class Detection(BaseModel):
    """How replay recognises a condition on screen. Checked after every step."""

    model_config = ConfigDict(extra="forbid")

    #: Any one visible means detected.
    any_of: list[Locator] = Field(min_length=1)
    url_pattern: str | None = None
    title_contains: str | None = None


class BusinessOutcome(BaseModel):
    """A legitimate answer the caller needs to hear, not a crash. 'No such member' lives here."""

    model_config = ConfigDict(extra="forbid")

    code: str
    description: str = ""
    detect: Detection
    #: Output values to report alongside the outcome, e.g. {"found": "false"}.
    sets: dict[str, str] = Field(default_factory=dict)
    #: True: the run ends here with status "business_outcome". False: note it and continue.
    terminal: bool = True


class Recovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["click", "run_phase", "wait"]
    #: click: what to click (e.g. a "Continue" button on a known interstitial).
    target: TargetDescriptor | None = None
    #: run_phase: which phase to re-run (e.g. "bootstrap" to sign in again).
    phase: Phase | None = None
    wait_ms: int | None = None
    #: What to do afterwards. restart_phase re-runs the interrupted phase from its first step,
    #: which is the only safe choice when a server-rendered form has lost its state.
    then: Literal["retry_step", "restart_phase"] = "restart_phase"


class RecoverablePattern(BaseModel):
    """A known interruption replay can handle by itself, a bounded number of times."""

    model_config = ConfigDict(extra="forbid")

    code: str
    description: str = ""
    detect: Detection
    recovery: Recovery
    max_attempts: int = 2


class FailurePattern(BaseModel):
    """A known hard stop, given a precise code so the caller can act on it."""

    model_config = ConfigDict(extra="forbid")

    code: str
    category: Literal["authorization", "authentication", "application", "validation", "other"] = "other"
    description: str = ""
    detect: Detection
    retryable: bool = False


class DeclaredConditions(BaseModel):
    """The reviewable, per-application list of non-happy-path states. Merged into artifacts by the builder."""

    model_config = ConfigDict(extra="forbid")

    outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoverables: list[RecoverablePattern] = Field(default_factory=list)
    failures: list[FailurePattern] = Field(default_factory=list)


# --------------------------------------------------------------- provenance
class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discovery_run_id: str
    goal: str
    #: provider/model pairs that made decisions during discovery.
    models: list[str] = Field(default_factory=list)
    created_at: str
    created_by: str = "cua-discover"
    evidence_dir: str | None = None
    reviewed_by: str | None = None
    review_notes: str | None = None


class TenantOverride(BaseModel):
    """Per-tenant specialisation of one step, applied at replay for that tenant only."""

    model_config = ConfigDict(extra="forbid")

    tenant: str
    step_id: str
    target: TargetDescriptor | None = None
    value: str | None = None
    url: str | None = None


# ------------------------------------------------------------------ artifact
class CapabilityArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    id: str
    name: str
    version: str
    description: str
    #: Unattended replay should require "approved". Discovery always emits "draft".
    status: ArtifactStatus = "draft"
    target: TargetSurface

    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)

    steps: list[Step] = Field(min_length=1)
    success: SuccessCondition

    outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recoverables: list[RecoverablePattern] = Field(default_factory=list)
    failures: list[FailurePattern] = Field(default_factory=list)

    #: What this capability needs. Intersected with the environment policy at replay; can only narrow it.
    policy: Policy
    overrides: list[TenantOverride] = Field(default_factory=list)
    provenance: Provenance

    # ------------------------------------------------------------- helpers
    def steps_in_phase(self, phase: Phase) -> list[Step]:
        return [s for s in self.steps if s.phase == phase]

    def input(self, name: str) -> InputParam | None:
        return next((i for i in self.inputs if i.name == name), None)

    def output(self, name: str) -> OutputField | None:
        return next((o for o in self.outputs if o.name == name), None)
