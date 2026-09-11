"""
Deterministic replay: execute a capability artifact with no model in the loop.

Per step: render templates -> policy gate -> resolve target through the
fallback chain -> act -> post-action allowlist guard -> check declared
conditions (outcomes, recoverables, failures) -> verify checkpoint.

Control flow for the non-happy path is explicit:
    _BusinessOutcome  a declared, legitimate result: stop and report it
    _RestartPhase     a recoverable condition was handled: re-run the phase
    _Failure          a hard stop with a precise, debuggable error
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from cua.agent.loop import ConfirmationHandler, DenyAllConfirmation
from cua.artifact.actions import ActionIntent
from cua.artifact.schema import (
    BusinessOutcome,
    CapabilityArtifact,
    Checkpoint,
    Detection,
    FailurePattern,
    Phase,
    RecoverablePattern,
    Step,
)
from cua.artifact.values import UnknownInput, render_inputs
from cua.escalation.base import EscalationHandler, InterventionOutcome, InterventionRequest
from cua.evidence.recorder import RunRecorder
from cua.policy.gate import PolicyGate
from cua.policy.model import Policy, glob_to_regex
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretError, SecretStore
from cua.replay.inputs import InputValidationError, parse_output, validate_inputs
from cua.replay.result import (
    ControlEvent,
    DriftReport,
    OutcomeReport,
    RecoveryReport,
    ReplayError,
    ReplayResult,
    StepReport,
)
from cua.surface.base import Handle, Surface, SurfaceError, TargetNotFound

MAX_PHASE_RESTARTS = 3
BASE_URL_REF = "{{base_url}}"


@dataclass
class ReplayOptions:
    #: Pre-approval for risky steps on this invocation (the caller takes responsibility).
    allow_risky: bool = False
    #: Refuse artifacts that are not status "approved".
    require_approved: bool = False
    step_timeout_ms: int = 15_000
    #: Tenant binding overrides: a different base URL, and per-tenant step overrides.
    base_url: str | None = None
    tenant: str | None = None


# ----------------------------------------------------------- control flow
class _BusinessOutcome(Exception):
    def __init__(self, outcome: BusinessOutcome, step: Step) -> None:
        super().__init__(outcome.code)
        self.outcome = outcome
        self.step = step


class _RestartPhase(Exception):
    pass


class _RetryStep(Exception):
    pass


class _Failure(Exception):
    def __init__(self, error: ReplayError) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.error = error


class ReplayEngine:
    def __init__(
        self,
        *,
        surface: Surface,
        env_policy: Policy,
        secrets: SecretStore,
        recorder: RunRecorder,
        redactor: Redactor,
        confirm: ConfirmationHandler | None = None,
        escalate: EscalationHandler | None = None,
        options: ReplayOptions | None = None,
    ) -> None:
        self.surface = surface
        self.env_policy = env_policy
        self.secrets = secrets
        self.recorder = recorder
        self.redactor = redactor
        self.confirm = confirm or DenyAllConfirmation()
        self.escalate = escalate
        self.options = options or ReplayOptions()
        # per-run state
        self.gate: PolicyGate = PolicyGate(env_policy)
        self.artifact: CapabilityArtifact | None = None
        self.inputs: dict[str, str] = {}
        self.base_url = ""
        self.outputs_raw: dict[str, str] = {}
        self.steps: list[StepReport] = []
        self.recoveries: list[RecoveryReport] = []
        self.control_events: list[ControlEvent] = []
        self._recovery_attempts: Counter[str] = Counter()
        self._failure_retries: Counter[str] = Counter()
        self._interventions: Counter[str] = Counter()

    # ------------------------------------------------------------------ run
    def run(self, artifact: CapabilityArtifact, inputs: dict[str, str]) -> ReplayResult:
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        self.artifact = artifact
        self.steps, self.recoveries, self.control_events = [], [], []
        self.outputs_raw = {}
        self._recovery_attempts.clear()
        self._failure_retries.clear()
        self._interventions.clear()
        sensitive_inputs = [p.name for p in artifact.inputs if p.sensitive]
        for name in sensitive_inputs:
            if inputs.get(name):
                self.redactor.add_sensitive(name, inputs[name])
        self.recorder.event(
            "replay.start",
            capability=artifact.id,
            version=artifact.version,
            inputs={k: (f"{{{{input:{k}}}}}" if k in sensitive_inputs else v) for k, v in inputs.items()},
        )

        status: str = "failure"
        outcome: OutcomeReport | None = None
        error: ReplayError | None = None
        try:
            self._validate_artifact(artifact)
            self.inputs = self._validate_inputs(artifact, inputs)
            self.gate = PolicyGate(self.env_policy.intersect(artifact.policy))
            self.base_url = (self.options.base_url or artifact.target.base_url).rstrip("/")
            self._apply_overrides(artifact)
            for phase in ("bootstrap", "main"):
                if artifact.steps_in_phase(phase):
                    self._run_phase(phase)
            self._verify_success(artifact)
            status = "success"
        except _BusinessOutcome as bo:
            status = "business_outcome"
            outcome = OutcomeReport(code=bo.outcome.code, description=bo.outcome.description, step_id=bo.step.id)
            self.outputs_raw.update(bo.outcome.sets)
            shot = self.recorder.screenshot(self.surface.screenshot(), f"outcome-{bo.outcome.code}", force=True)
            self.recorder.event("replay.outcome", code=bo.outcome.code, step=bo.step.id, screenshot=shot)
        except _Failure as f:
            status = "failure"
            error = f.error
            if error.screenshot is None:
                try:
                    error.screenshot = self.recorder.screenshot(
                        self.surface.screenshot(), f"failure-{error.step_id or 'run'}", force=True
                    )
                except SurfaceError:
                    error.screenshot = None
            self.recorder.event("replay.failure", **error.model_dump())

        outputs = self._typed_outputs(artifact)
        result = ReplayResult(
            status=status,  # type: ignore[arg-type]
            capability_id=artifact.id,
            capability_name=artifact.name,
            capability_version=artifact.version,
            run_id=self.recorder.run_id,
            inputs={k: (f"{{{{input:{k}}}}}" if k in sensitive_inputs else v) for k, v in inputs.items()},
            outputs=outputs,
            outcome=outcome,
            error=error,
            steps=list(self.steps),
            recoveries=list(self.recoveries),
            drift=DriftReport(
                fallback_steps=[
                    {"step_id": s.step_id, "candidate_index": s.candidate_index, "locator": s.locator}
                    for s in self.steps
                    if s.used_fallback
                ]
            ),
            control_events=list(self.control_events),
            started_at=started_at,
            finished_at=datetime.now(UTC).isoformat(),
            duration_ms=int((time.monotonic() - started) * 1000),
            evidence_dir=str(self.recorder.dir),
        )
        sensitive_outputs = [o.name for o in artifact.outputs if o.sensitive]
        self.recorder.write_json("result.json", result.redacted(sensitive_outputs))
        self.recorder.event("replay.end", status=status, duration_ms=result.duration_ms, drift=result.drift.detected)
        return result

    # ------------------------------------------------------------ validation
    def _validate_artifact(self, artifact: CapabilityArtifact) -> None:
        if artifact.status == "deprecated":
            raise _Failure(
                ReplayError(code="ARTIFACT_DEPRECATED", category="artifact", message=f"{artifact.id} is deprecated")
            )
        if self.options.require_approved and artifact.status != "approved":
            raise _Failure(
                ReplayError(
                    code="ARTIFACT_NOT_APPROVED",
                    category="artifact",
                    message=f"{artifact.id} v{artifact.version} is {artifact.status}; unattended replay requires approval",
                )
            )

    def _validate_inputs(self, artifact: CapabilityArtifact, inputs: dict[str, str]) -> dict[str, str]:
        try:
            return validate_inputs(artifact, inputs)
        except InputValidationError as ex:
            raise _Failure(
                ReplayError(
                    code="INPUT_INVALID",
                    category="input",
                    message=str(ex),
                    expected="inputs matching " + ", ".join(f"{p.name}:{p.type}" for p in artifact.inputs),
                    observed=str(sorted(inputs)),
                )
            ) from ex

    def _apply_overrides(self, artifact: CapabilityArtifact) -> None:
        if not self.options.tenant:
            return
        by_step = {s.id: s for s in artifact.steps}
        for ov in artifact.overrides:
            if ov.tenant != self.options.tenant or ov.step_id not in by_step:
                continue
            step = by_step[ov.step_id]
            if ov.target is not None:
                step.target = ov.target
            if ov.value is not None:
                step.value = ov.value
            if ov.url is not None:
                step.url = ov.url
            self.recorder.event("replay.override", tenant=ov.tenant, step=ov.step_id)

    # ---------------------------------------------------------------- phases
    def _run_phase(self, phase: Phase) -> None:
        assert self.artifact is not None
        restarts = 0
        while True:
            try:
                for step in self.artifact.steps_in_phase(phase):
                    self._exec_step(step)
                return
            except _RestartPhase:
                restarts += 1
                self.recorder.event("replay.phase_restart", phase=phase, restart=restarts)
                if restarts >= MAX_PHASE_RESTARTS:
                    raise _Failure(
                        ReplayError(
                            code="RECOVERY_EXHAUSTED",
                            category="recovery",
                            message=f"phase {phase!r} restarted {restarts} times without completing",
                        )
                    ) from None

    # ----------------------------------------------------------------- steps
    def _exec_step(self, step: Step) -> None:
        for attempt in range(2):
            try:
                self._exec_step_once(step)
                return
            except _RetryStep:
                if attempt == 1:
                    raise _RestartPhase() from None
            except _Failure as f:
                if self.escalate is None or not self._escalatable(f.error):
                    raise
                outcome = self._intervene(step, f.error)
                if outcome.resolution == "retry_step":
                    continue
                if outcome.resolution == "skip_step":
                    self.steps.append(self._report(step, "skipped", detail=f"skipped by {outcome.operator}"))
                    return
                if outcome.resolution == "restart_phase":
                    raise _RestartPhase() from None
                raise

    def _exec_step_once(self, step: Step) -> None:
        t0 = time.monotonic()
        report = self._report(step, "ok")
        try:
            handle = self._perform(step, report)
            self._check_conditions(step)
            if step.checkpoint is not None:
                self._verify_checkpoint(step, step.checkpoint)
            if step.action == "extract" and step.output and handle is not None:
                self._record_output(step, handle)
        except (_Failure, _BusinessOutcome, _RestartPhase, _RetryStep) as ex:
            report.status = "failed" if isinstance(ex, _Failure) else "ok"
            report.detail = ex.error.message if isinstance(ex, _Failure) else type(ex).__name__.lstrip("_")
            if isinstance(ex, _Failure) and ex.error.step_id is None:
                ex.error.step_id = step.id
            raise
        finally:
            report.duration_ms = int((time.monotonic() - t0) * 1000)
            report.url_after = self.surface.url()
            self.steps.append(report)
            self.recorder.event(
                "step",
                step=step.id,
                action=step.action,
                status=report.status,
                candidate_index=report.candidate_index,
                used_fallback=report.used_fallback,
                detail=report.detail,
                url_after=report.url_after,
                duration_ms=report.duration_ms,
            )

    def _perform(self, step: Step, report: StepReport) -> Handle | None:
        url = self._render(step.url) if step.url else None
        value_template = self._render(step.value) if step.value else None  # secrets still references
        control_name = step.target.recorded.name if step.target and step.target.recorded else None
        control_role = step.target.recorded.role if step.target and step.target.recorded else None
        intent = ActionIntent(
            type=step.action,
            url=url,
            control_name=control_name or (step.target.description if step.target else None),
            control_role=control_role,
            value=value_template,
            key=step.key,
            declared_risk=step.risk,
        )
        decision = self.gate.check_action(intent, current_url=self.surface.url())
        if not decision.allowed:
            raise _Failure(
                ReplayError(code="POLICY_REFUSED", category="policy", message=decision.reason, step_id=step.id)
            )
        if decision.requires_confirmation:
            if self.options.allow_risky:
                self.recorder.event("policy.preapproved", step=step.id, reason=decision.reason)
            else:
                verdict = self.confirm.confirm(intent, decision, self.surface.observe())
                self.recorder.event(
                    "policy.confirmation", step=step.id, approved=verdict.approved, by=verdict.by, note=verdict.note
                )
                if not verdict.approved:
                    raise _Failure(
                        ReplayError(
                            code="CONFIRMATION_REQUIRED",
                            category="policy",
                            message=f"risky step {step.id} was not confirmed ({verdict.by}: {verdict.note}); "
                            "re-run with pre-approval or have an operator approve it",
                            step_id=step.id,
                            expected="confirmation of a risky action",
                        )
                    )

        handle: Handle | None = None
        if step.target is not None:
            handle = self._resolve(step)
            report.candidate_index = handle.candidate_index
            report.used_fallback = handle.used_fallback
            report.locator = handle.candidate.describe()
            if handle.used_fallback:
                self.recorder.event("drift.fallback", step=step.id, candidate_index=handle.candidate_index, locator=report.locator)
        try:
            if step.action == "navigate":
                self.surface.navigate(str(url))
            elif step.action == "click" and handle is not None:
                self.surface.click(handle)
            elif step.action == "type" and handle is not None:
                text, _ = self.secrets.resolve(str(value_template))
                self.surface.type_text(handle, text)
            elif step.action == "select" and handle is not None:
                self.surface.select_option(handle, str(value_template))
            elif step.action == "press":
                self.surface.press(str(step.key))
            elif step.action == "extract":
                pass  # read after conditions/checkpoint, in _record_output
            else:
                raise _Failure(
                    ReplayError(code="STEP_INVALID", category="artifact", message=f"step {step.id} is malformed", step_id=step.id)
                )
        except (SurfaceError, SecretError) as ex:
            raise _Failure(
                ReplayError(code="ACTION_FAILED", category="surface", message=str(ex), step_id=step.id, retryable=True)
            ) from ex

        after = self.gate.check_url(self.surface.url())
        if not after.allowed:
            raise _Failure(
                ReplayError(
                    code="POLICY_REFUSED",
                    category="policy",
                    message=f"step led outside the allowlist: {after.reason}",
                    step_id=step.id,
                    observed=self.surface.url(),
                )
            )
        return handle

    def _resolve(self, step: Step) -> Handle:
        assert step.target is not None
        try:
            return self.surface.resolve(step.target, timeout_ms=self.options.step_timeout_ms)
        except TargetNotFound:
            # The control may be missing *because* the app is telling us something.
            self._check_conditions(step)
            try:
                return self.surface.resolve(step.target, timeout_ms=self.options.step_timeout_ms)
            except TargetNotFound as ex:
                raise _Failure(
                    ReplayError(
                        code="TARGET_NOT_FOUND",
                        category="target",
                        message=f"could not locate {step.target.description!r}",
                        step_id=step.id,
                        expected=" | ".join(c.describe() for c in step.target.candidates),
                        observed=f"url={self.surface.url()} title={self.surface.title()!r}",
                        attempts=ex.attempts,
                        retryable=True,
                    )
                ) from ex

    def _record_output(self, step: Step, handle: Handle) -> None:
        assert self.artifact is not None and step.output is not None
        try:
            value = self.surface.read(handle)
        except SurfaceError as ex:
            raise _Failure(
                ReplayError(code="ACTION_FAILED", category="surface", message=str(ex), step_id=step.id, retryable=True)
            ) from ex
        field = self.artifact.output(step.output)
        if field is not None and field.sensitive:
            self.redactor.add_sensitive(step.output, value)
        self.outputs_raw[step.output] = value
        self.recorder.event("output", step=step.id, name=step.output, value=value)

    # ------------------------------------------------------------ conditions
    def _check_conditions(self, step: Step) -> None:
        assert self.artifact is not None
        url, title = self.surface.url(), self.surface.title()
        # Order matters: a known interstitial or expired session masks whatever is underneath it,
        # so recoverables are checked first; then legitimate business answers; then hard stops.
        for pattern in self.artifact.recoverables:
            if self._detected(pattern.detect, url, title):
                self._recover(pattern, step)
        for outcome in self.artifact.outcomes:
            if self._detected(outcome.detect, url, title):
                self.recorder.event("condition.outcome", code=outcome.code, step=step.id, terminal=outcome.terminal)
                if outcome.terminal:
                    raise _BusinessOutcome(outcome, step)
        for failure in self.artifact.failures:
            if self._detected(failure.detect, url, title):
                self._fail_on_pattern(failure, step, url, title)

    def _detected(self, detection: Detection, url: str, title: str) -> bool:
        if detection.title_contains and detection.title_contains not in title:
            return False
        if detection.url_pattern and not glob_to_regex(self._render(detection.url_pattern)).match(urlparse(url).path or "/"):
            return False
        return any(self.surface.exists(loc) for loc in detection.any_of)

    def _recover(self, pattern: RecoverablePattern, step: Step) -> None:
        attempt = self._recovery_attempts[pattern.code] + 1
        if attempt > pattern.max_attempts:
            raise _Failure(
                ReplayError(
                    code="RECOVERY_EXHAUSTED",
                    category="recovery",
                    message=f"{pattern.code} recurred after {pattern.max_attempts} recovery attempt(s)",
                    step_id=step.id,
                    observed=f"url={self.surface.url()} title={self.surface.title()!r}",
                )
            )
        self._recovery_attempts[pattern.code] = attempt
        recovery = pattern.recovery
        self.recorder.event("condition.recoverable", code=pattern.code, step=step.id, attempt=attempt, recovery=recovery.kind)
        action = ""
        try:
            if recovery.kind == "click" and recovery.target is not None:
                self.surface.click(self.surface.resolve(recovery.target, timeout_ms=self.options.step_timeout_ms))
                action = f"clicked {recovery.target.description!r}"
            elif recovery.kind == "run_phase" and recovery.phase is not None:
                if recovery.phase == step.phase and recovery.then == "restart_phase":
                    action = f"phase {recovery.phase!r} is the interrupted phase"  # the restart below re-runs it
                else:
                    self._run_phase(recovery.phase)
                    action = f"re-ran phase {recovery.phase!r}"
            elif recovery.kind == "wait":
                time.sleep((recovery.wait_ms or 1000) / 1000)
                action = f"waited {recovery.wait_ms or 1000}ms"
            else:
                raise _Failure(
                    ReplayError(code="RECOVERY_INVALID", category="artifact", message=f"{pattern.code}: malformed recovery", step_id=step.id)
                )
        except (SurfaceError, _Failure) as ex:
            self.recoveries.append(RecoveryReport(code=pattern.code, step_id=step.id, attempt=attempt, action=action or recovery.kind, succeeded=False))
            if isinstance(ex, _Failure):
                raise
            raise _Failure(
                ReplayError(code="RECOVERY_FAILED", category="recovery", message=f"{pattern.code}: {ex}", step_id=step.id)
            ) from ex
        then = f", then {recovery.then.replace('_', ' ')}"
        self.recoveries.append(RecoveryReport(code=pattern.code, step_id=step.id, attempt=attempt, action=action + then, succeeded=True))
        if recovery.then == "retry_step":
            raise _RetryStep()
        raise _RestartPhase()

    def _fail_on_pattern(self, failure: FailurePattern, step: Step, url: str, title: str) -> None:
        if failure.retryable and self._failure_retries[failure.code] < 1:
            self._failure_retries[failure.code] += 1
            self.recorder.event("condition.retryable_failure", code=failure.code, step=step.id)
            self.recoveries.append(
                RecoveryReport(code=failure.code, step_id=step.id, attempt=1, action="restart phase once", succeeded=True)
            )
            raise _RestartPhase()
        raise _Failure(
            ReplayError(
                code=failure.code,
                category=failure.category,
                message=failure.description or failure.code,
                step_id=step.id,
                expected="the step's normal result",
                observed=f"url={url} title={title!r}; matched {' | '.join(loc.describe() for loc in failure.detect.any_of)}",
                retryable=failure.retryable,
            )
        )

    # ------------------------------------------------------------ checkpoints
    def _checkpoint_holds(self, cp: Checkpoint) -> tuple[bool, str]:
        url, title = self.surface.url(), self.surface.title()
        observed = f"url={url} title={title!r}"
        if cp.url_pattern and not glob_to_regex(self._render(cp.url_pattern)).match(urlparse(url).path or "/"):
            return False, observed
        if cp.title_contains and cp.title_contains not in title:
            return False, observed
        for loc in cp.present:
            if not self.surface.exists(loc):
                return False, f"{observed}; missing {loc.describe()}"
        for loc in cp.absent:
            if self.surface.exists(loc):
                return False, f"{observed}; unexpected {loc.describe()}"
        return True, observed

    def _verify_checkpoint(self, step: Step, cp: Checkpoint, *, code: str = "CHECKPOINT_FAILED") -> None:
        deadline = time.monotonic() + cp.timeout_ms / 1000
        while True:
            ok, observed = self._checkpoint_holds(cp)
            if ok:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        # Not reached: maybe the app is saying something we know how to classify.
        self._check_conditions(step)
        raise _Failure(
            ReplayError(
                code=code,
                category="checkpoint",
                message=f"expected state after {step.id} was not reached",
                step_id=step.id,
                expected=self._describe_checkpoint(cp),
                observed=observed,
                retryable=True,
            )
        )

    def _verify_success(self, artifact: CapabilityArtifact) -> None:
        last = artifact.steps[-1]
        self._verify_checkpoint(last, artifact.success, code="SUCCESS_NOT_VERIFIED")
        missing = [n for n in artifact.success.outputs_required if n not in self.outputs_raw]
        if missing:
            raise _Failure(
                ReplayError(
                    code="SUCCESS_NOT_VERIFIED",
                    category="checkpoint",
                    message=f"required outputs were not extracted: {missing}",
                    step_id=last.id,
                    expected=f"outputs {artifact.success.outputs_required}",
                    observed=f"outputs {sorted(self.outputs_raw)}",
                )
            )

    def _describe_checkpoint(self, cp: Checkpoint) -> str:
        parts = []
        if cp.url_pattern:
            parts.append(f"path~{self._render(cp.url_pattern)}")
        if cp.title_contains:
            parts.append(f"title contains {cp.title_contains!r}")
        parts += [f"present {loc.describe()}" for loc in cp.present]
        parts += [f"absent {loc.describe()}" for loc in cp.absent]
        return "; ".join(parts) or cp.description

    # ------------------------------------------------------------ escalation
    @staticmethod
    def _escalatable(error: ReplayError) -> bool:
        return error.category not in ("input", "artifact")

    def _intervene(self, step: Step, error: ReplayError) -> InterventionOutcome:
        assert self.escalate is not None and self.artifact is not None
        if self._interventions[step.id] >= 1:
            raise _Failure(error)
        self._interventions[step.id] += 1
        shot = self.recorder.screenshot(self.surface.screenshot(), f"escalation-{step.id}", force=True)
        request = InterventionRequest(
            run_id=self.recorder.run_id,
            capability=f"{self.artifact.id} v{self.artifact.version}",
            step_id=step.id,
            step_description=step.description,
            phase=step.phase,
            reason=error.message,
            error_code=error.code,
            url=self.surface.url(),
            title=self.surface.title(),
            screenshot=shot,
            extra={"expected": error.expected, "observed": error.observed},
        )
        self._control("automation", "paused", f"{error.code} at {step.id}: {error.message}")
        self.recorder.event("escalation.requested", **request.__dict__)
        outcome = self.escalate.intervene(request)
        self._control("human", "took_control", f"operator={outcome.operator}", at=outcome.started_at)
        for entry in outcome.control_log:
            self.control_events.append(ControlEvent(at=str(entry.get("at", "")), holder="human", event="action", detail=str(entry.get("detail", entry))))
        self._control("automation", "resumed", f"resolution={outcome.resolution}; {outcome.notes}", at=outcome.ended_at)
        self.recorder.event("escalation.resolved", step=step.id, resolution=outcome.resolution, operator=outcome.operator, notes=outcome.notes)
        return outcome

    def _control(self, holder: str, event: str, detail: str, at: str | None = None) -> None:
        self.control_events.append(
            ControlEvent(at=at or datetime.now(UTC).isoformat(), holder=holder, event=event, detail=detail)  # type: ignore[arg-type]
        )

    # --------------------------------------------------------------- helpers
    def _render(self, template: str) -> str:
        try:
            return render_inputs(template.replace(BASE_URL_REF, self.base_url), self.inputs)
        except UnknownInput as ex:
            raise _Failure(
                ReplayError(code="INPUT_INVALID", category="input", message=str(ex), expected=template)
            ) from ex

    def _typed_outputs(self, artifact: CapabilityArtifact) -> dict[str, Any]:
        typed: dict[str, Any] = {}
        for name, raw in self.outputs_raw.items():
            field = artifact.output(name)
            typed[name] = parse_output(raw, field.type) if field else raw
        return typed

    @staticmethod
    def _report(step: Step, status: str, *, detail: str = "") -> StepReport:
        return StepReport(
            step_id=step.id,
            phase=step.phase,
            action=step.action,
            description=step.description,
            status=status,  # type: ignore[arg-type]
            detail=detail,
        )


ConfirmFactory = Callable[[], ConfirmationHandler]
