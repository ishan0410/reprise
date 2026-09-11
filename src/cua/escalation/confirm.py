"""Route risky-action confirmations through the human handoff."""

from __future__ import annotations

from cua.agent.loop import ConfirmationHandler, ConfirmationResult
from cua.artifact.actions import ActionIntent
from cua.escalation.base import EscalationHandler, InterventionRequest
from cua.evidence.recorder import RunRecorder
from cua.policy.gate import Decision
from cua.surface.base import Observation


class ConfirmViaHandoff(ConfirmationHandler):
    """
    Resolutions map onto a yes/no question:
        retry_step    -> approved: automation performs the risky action itself
        skip_step     -> the operator performed it by hand; automation must not repeat it
        restart_phase / abort -> denied
    """

    def __init__(self, handoff: EscalationHandler, recorder: RunRecorder, *, capability: str, run_label: str) -> None:
        self.handoff = handoff
        self.recorder = recorder
        self.capability = capability
        self.run_label = run_label
        self._count = 0

    def confirm(self, intent: ActionIntent, decision: Decision, observation: Observation) -> ConfirmationResult:
        self._count += 1
        control = f'{intent.control_role or "control"} "{intent.control_name}"' if intent.control_name else intent.type
        shot = self.recorder.screenshot(observation.screenshot_png, f"confirm-{self._count:02d}", force=True)
        request = InterventionRequest(
            run_id=self.recorder.run_id,
            capability=self.capability,
            step_id=f"{self.run_label}-confirm-{self._count}",
            step_description=f"{intent.type} {control}",
            phase="main",
            reason=decision.reason,
            error_code="CONFIRMATION_REQUIRED",
            url=observation.url,
            title=observation.title,
            screenshot=shot,
            extra={"intent": intent.model_dump(exclude_none=True)},
        )
        outcome = self.handoff.intervene(request)
        if outcome.resolution == "retry_step":
            return ConfirmationResult(approved=True, by=outcome.operator, note=outcome.notes or "approved")
        if outcome.resolution == "skip_step":
            return ConfirmationResult(approved=False, by=outcome.operator, note=outcome.notes or "performed manually", performed_by_human=True)
        return ConfirmationResult(approved=False, by=outcome.operator, note=outcome.notes or f"denied ({outcome.resolution})")
