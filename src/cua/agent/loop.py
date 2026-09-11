"""
The discovery orchestrator: observe -> decide -> act until done, stuck, or a limit.

The model only ever proposes. Every proposal goes through the PolicyGate,
then the Surface; refusals are fed back to the model as the result of its
action. Risky actions in "confirm" mode go to a ConfirmationHandler, which
is the seam the human handoff plugs into.
"""

from __future__ import annotations

import hashlib
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cua.agent.llm import LLMProvider, ModelRequest, ProviderBusy, ProviderError, ToolCall
from cua.agent.prompts import SYSTEM_PROMPT, build_user_message
from cua.agent.tools import ACTION_TOOLS, TOOL_SPECS, intent_from_call
from cua.agent.trace import (
    ModelSummary,
    ObservationSummary,
    ProviderEvent,
    StepResult,
    Trace,
    TraceStep,
)
from cua.artifact.actions import ActionIntent
from cua.artifact.targets import RecordedElement
from cua.artifact.values import UnknownInput, render_inputs
from cua.evidence.recorder import RunRecorder
from cua.policy.gate import Decision, PolicyGate
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretError, SecretStore
from cua.surface.base import Handle, Observation, Surface, SurfaceError

#: Output names that are sensitive regardless of what the model says.
_SENSITIVE_NAME = re.compile(r"balance|ssn|social|account|card|routing|dob|birth|address|phone|email|salary|income", re.IGNORECASE)


@dataclass(frozen=True)
class ConfirmationResult:
    approved: bool
    by: str
    note: str = ""


class ConfirmationHandler(ABC):
    """Decides whether a risky action may proceed. The human handoff implements this."""

    @abstractmethod
    def confirm(self, intent: ActionIntent, decision: Decision, observation: Observation) -> ConfirmationResult: ...


class DenyAllConfirmation(ConfirmationHandler):
    def confirm(self, intent: ActionIntent, decision: Decision, observation: Observation) -> ConfirmationResult:
        return ConfirmationResult(approved=False, by="system", note="no confirmation channel configured")


@dataclass
class DiscoveryRequest:
    goal: str
    start_url: str
    capability_name: str = "capability"
    inputs: dict[str, str] = field(default_factory=dict)
    sensitive_inputs: frozenset[str] = frozenset()
    max_steps: int = 25
    timeout_s: float = 300.0


class DiscoveryAgent:
    def __init__(
        self,
        *,
        surface: Surface,
        llm: LLMProvider,
        gate: PolicyGate,
        secrets: SecretStore,
        recorder: RunRecorder,
        redactor: Redactor,
        confirm: ConfirmationHandler | None = None,
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.gate = gate
        self.secrets = secrets
        self.recorder = recorder
        self.redactor = redactor
        self.confirm = confirm or DenyAllConfirmation()
        self._provider_events: list[ProviderEvent] = []
        self._step_index = 0

    # ------------------------------------------------------------------ run
    def run(self, request: DiscoveryRequest) -> Trace:
        trace = Trace(
            run_id=self.recorder.run_id,
            goal=request.goal,
            start_url=request.start_url,
            capability_name=request.capability_name,
            inputs={n: (f"{{{{input:{n}}}}}" if n in request.sensitive_inputs else v) for n, v in request.inputs.items()},
            sensitive_inputs=sorted(request.sensitive_inputs),
            started_at=datetime.now(UTC).isoformat(),
        )
        self.recorder.event(
            "discovery.start", goal=request.goal, start_url=request.start_url, capability=request.capability_name
        )
        history: list[str] = []
        deadline = time.monotonic() + request.timeout_s

        start_intent = ActionIntent(type="navigate", url=request.start_url)
        start_decision = self.gate.check_action(start_intent, current_url="about:blank")
        if not start_decision.allowed:
            trace.status = "error"
            trace.summary = f"start URL refused by policy: {start_decision.reason}"
            return self._finish(trace)
        self.surface.navigate(request.start_url)

        for i in range(request.max_steps):
            self._step_index = i
            if time.monotonic() > deadline:
                trace.status = "timeout"
                trace.summary = f"timed out after {request.timeout_s:.0f}s"
                break
            observation = self.surface.observe()
            shot = self.recorder.screenshot(observation.screenshot_png, f"step-{i:02d}")
            user_text = build_user_message(
                goal=request.goal,
                inputs=request.inputs,
                sensitive_inputs=request.sensitive_inputs,
                secret_names=self.gate.policy.secrets.allowed,
                history=history,
                observation=observation,
                step_index=i,
                max_steps=request.max_steps,
            )
            model_request = ModelRequest(
                system=SYSTEM_PROMPT,
                user_text=user_text,
                screenshot_png=observation.screenshot_png if self.llm.supports_vision else None,
                tools=TOOL_SPECS,
            )
            try:
                turn = self.llm.decide(model_request)
            except (ProviderBusy, ProviderError) as ex:
                self.recorder.event("llm.error", step=i, error=str(ex))
                trace.status = "error"
                trace.summary = f"model unavailable: {ex}"
                break

            transcript_entry: dict[str, Any] = {
                "step": i,
                "provider": turn.provider,
                "model": turn.model,
                "request": {
                    "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
                    "user_text": user_text,
                    "screenshot": shot,
                    "screenshot_sent_to_model": model_request.screenshot_png is not None,
                    "tools": [t.name for t in TOOL_SPECS],
                },
                "response": {
                    "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in turn.tool_calls],
                    "text": turn.text,
                    "usage": turn.usage,
                    "latency_ms": turn.latency_ms,
                    "response_id": turn.response_id,
                },
            }

            step = TraceStep(
                index=i,
                observation=ObservationSummary(
                    url=observation.url, title=observation.title, screenshot=shot, snapshot_chars=len(observation.snapshot)
                ),
                model=ModelSummary(
                    provider=turn.provider,
                    model=turn.model,
                    usage=turn.usage,
                    latency_ms=turn.latency_ms,
                    response_id=turn.response_id,
                ),
                tool="(none)",
                arguments={},
                result=StepResult(status="error"),
            )
            try:
                terminal = self._handle_turn(turn.tool_calls, observation, request, trace, step, history)
            finally:
                # Written after the action so values learned sensitive during this step are redacted too.
                self.recorder.append_jsonl("transcript.jsonl", transcript_entry)
                step.url_after = self.surface.url()
                trace.steps.append(step)
                self.recorder.event(
                    "step",
                    index=i,
                    tool=step.tool,
                    status=step.result.status,
                    detail=step.result.detail,
                    url_after=step.url_after,
                )
            if terminal:
                break
        else:
            trace.status = "max_steps"
            trace.summary = f"stopped after {request.max_steps} steps without the goal being declared done"
        return self._finish(trace)

    # ----------------------------------------------------------------- steps
    def _handle_turn(
        self,
        calls: list[ToolCall],
        observation: Observation,
        request: DiscoveryRequest,
        trace: Trace,
        step: TraceStep,
        history: list[str],
    ) -> bool:
        """Apply the model's first tool call. Returns True when the run should stop."""
        n = len(history) + 1
        if not calls:
            step.result = StepResult(status="error", detail="model returned no tool call")
            history.append(f"{n}. (no action returned; respond with exactly one tool call)")
            return False
        call = calls[0]
        args = dict(call.arguments)
        step.tool = call.name
        step.reason = str(args.pop("reason", "") or "")
        step.arguments = args

        if call.name == "done":
            trace.status = "success"
            trace.summary = str(args.get("summary", "")) or "goal declared done"
            step.result = StepResult(status="done", detail=trace.summary)
            return True
        if call.name == "stuck":
            trace.status = "stuck"
            trace.summary = step.reason or "model declared itself stuck"
            step.result = StepResult(status="stuck", detail=trace.summary)
            return True
        if call.name not in ACTION_TOOLS:
            step.result = StepResult(status="error", detail=f"unknown tool {call.name!r}")
            history.append(f"{n}. {call.name}: unknown tool")
            return False

        try:
            intent = intent_from_call(call, observation)
        except SurfaceError as ex:
            step.result = StepResult(status="error", detail=str(ex))
            history.append(f"{n}. {self._describe_call(call)} -> ERROR: {ex}")
            return False
        step.intent = intent

        decision = self.gate.check_action(intent, current_url=observation.url)
        step.decision = decision
        if not decision.allowed:
            self.recorder.event("policy.refused", step=step.index, intent=intent, reason=decision.reason)
            step.result = StepResult(status="refused", detail=decision.reason)
            history.append(f"{n}. {self._describe_call(call)} -> REFUSED by policy: {decision.reason}")
            return False
        if decision.requires_confirmation:
            outcome = self.confirm.confirm(intent, decision, observation)
            self.recorder.event(
                "policy.confirmation", step=step.index, intent=intent, approved=outcome.approved, by=outcome.by, note=outcome.note
            )
            if not outcome.approved:
                detail = f"risky action not confirmed ({outcome.by}: {outcome.note})"
                step.result = StepResult(status="refused", detail=detail)
                history.append(f"{n}. {self._describe_call(call)} -> REFUSED: {detail}")
                return False

        try:
            handle: Handle | None = None
            element: RecordedElement | None = None
            ref = args.get("ref")
            if ref:
                handle = self.surface.handle_for_ref(str(ref))
                element = self.surface.describe(handle)
                step.element = element
            detail = self._execute(call, intent, handle, request, trace, step)
        except (SurfaceError, SecretError, UnknownInput, ValueError) as ex:
            step.result = StepResult(status="error", detail=str(ex))
            history.append(f"{n}. {self._describe_call(call)} -> ERROR: {ex}")
            return False

        after = self.gate.check_url(self.surface.url())
        if not after.allowed:
            # A click can leave the allowlist just as a navigate can. Contain it and tell the model.
            left = self.surface.url()
            self.recorder.event("policy.contained", step=step.index, url=left, reason=after.reason)
            self.surface.navigate(observation.url)
            step.result = StepResult(status="refused", detail=f"action led outside the allowlist ({after.reason}); returned")
            history.append(f"{n}. {self._describe_call(call)} -> REFUSED: led to {left}, which is outside the allowlist")
            return False

        step.result.status = "ok"
        step.result.detail = detail
        history.append(f"{n}. {self._describe_call(call)} -> {detail}")
        return False

    def _execute(
        self,
        call: ToolCall,
        intent: ActionIntent,
        handle: Handle | None,
        request: DiscoveryRequest,
        trace: Trace,
        step: TraceStep,
    ) -> str:
        args = call.arguments
        control = f"{intent.control_role} \"{intent.control_name}\"" if intent.control_name else (intent.control_role or "")
        if call.name == "navigate":
            self.surface.navigate(str(intent.url))
            return f"navigated to {intent.url}"
        if handle is None:
            if call.name == "press":
                self.surface.press(str(intent.key))
                return f"pressed {intent.key}"
            raise ValueError(f"{call.name} requires a ref")
        if call.name == "click":
            self.surface.click(handle)
            return f"clicked {control}"
        if call.name == "type":
            raw = str(args.get("text", ""))
            text = render_inputs(raw, request.inputs)
            text, used_secret = self.secrets.resolve(text)
            self.surface.type_text(handle, text)
            return f"typed {'a secret' if used_secret else 'the value'} into {control}"
        if call.name == "select":
            self.surface.select_option(handle, str(args.get("option", "")))
            return f"selected {args.get('option')!r} in {control}"
        if call.name == "extract":
            name = str(args.get("name", "")).strip() or f"output_{step.index}"
            value = self.surface.read(handle)
            sensitive = bool(args.get("sensitive")) or bool(_SENSITIVE_NAME.search(name))
            if sensitive:
                self.redactor.add_sensitive(name, value)
                if name not in trace.sensitive_outputs:
                    trace.sensitive_outputs.append(name)
            trace.outputs[name] = value
            step.result = StepResult(status="ok", extracted_name=name, extracted_value=value)
            return f"extracted {name} = {value!r} from {control}"
        if call.name == "press":
            self.surface.press(str(intent.key))
            return f"pressed {intent.key}"
        raise ValueError(f"unhandled action {call.name}")

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _describe_call(call: ToolCall) -> str:
        shown = {k: v for k, v in call.arguments.items() if k != "reason"}
        inner = ", ".join(f"{k}={v!r}" for k, v in shown.items())
        return f"{call.name}({inner})"

    def on_provider_fallback(self, from_provider: str, to_provider: str, reason: str) -> None:
        event = ProviderEvent(step=self._step_index, from_provider=from_provider, to_provider=to_provider, reason=reason)
        self._provider_events.append(event)
        self.recorder.event("llm.fallback", **event.model_dump())

    def _finish(self, trace: Trace) -> Trace:
        trace.provider_events = list(self._provider_events)
        trace.final_url = self.surface.url()
        try:
            trace.final_title = self.surface.observe().title
        except SurfaceError:
            trace.final_title = ""
        trace.finished_at = datetime.now(UTC).isoformat()
        self.recorder.write_json("trace.json", trace)
        self.recorder.event("discovery.end", status=trace.status, summary=trace.summary, steps=len(trace.steps))
        return trace
