"""
Headed-browser handoff: the minimal, real operator surface.

Protocol (all files live in the run's evidence directory):
    intervention.json   written by automation: what stopped, where, why, screenshot
    resume.json         written by the operator (via `cua-resume`): resolution, operator, notes

While waiting, automation holds no commands against the browser; the
operator works in the very same window. A banner is injected into the page
so it is obvious who is in control, and a DOM-level recorder captures the
operator's actions (which control, what kind of action; never typed values).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from cua.escalation.base import EscalationHandler, InterventionOutcome, InterventionRequest, Resolution
from cua.escalation.session import SessionControl
from cua.evidence.recorder import RunRecorder
from cua.surface.base import Surface, SurfaceError

INTERVENTION_FILE = "intervention.json"
RESUME_FILE = "resume.json"


class ResumeSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Resolution
    operator: str
    notes: str = ""
    signalled_at: str | None = None


WaitHook = Callable[[float], None]  # called each poll with seconds elapsed; tests simulate the operator here


class HeadedBrowserHandoff(EscalationHandler):
    def __init__(
        self,
        *,
        surface: Surface,
        control: SessionControl,
        recorder: RunRecorder,
        timeout_s: float = 600.0,
        poll_ms: int = 500,
        on_wait: WaitHook | None = None,
        announce: Callable[[str], None] = print,
    ) -> None:
        self.surface = surface
        self.control = control
        self.recorder = recorder
        self.timeout_s = timeout_s
        self.poll_ms = poll_ms
        self.on_wait = on_wait
        self.announce = announce

    # ------------------------------------------------------------ protocol
    def intervene(self, request: InterventionRequest) -> InterventionOutcome:
        run_dir = self.recorder.dir
        resume_path = run_dir / RESUME_FILE
        if resume_path.exists():
            resume_path.unlink()  # a stale signal from an earlier intervention in this run
        self.recorder.write_json(INTERVENTION_FILE, {**asdict(request), "requested_at": _now(), "resume_file": str(resume_path)})

        self.control.cede_to_human(f"{request.error_code} at {request.step_id}: {request.reason}")
        started_at = _now()
        self.surface.start_human_recording()
        self._banner(f"HUMAN CONTROL — {request.capability} · step {request.step_id} · {request.error_code}. "
                     f"Fix the problem here, then run cua-resume.")
        self._announce(request, resume_path)

        signal = self._wait_for_resume(resume_path)
        actions = self.surface.stop_human_recording()
        self._banner(None)
        for action in actions:
            self.control.record_human_action(action)
        self.control.return_to_automation(signal.operator, signal.resolution, signal.notes)
        ended_at = _now()
        self.recorder.event(
            "escalation.human_actions", step=request.step_id, count=len(actions), actions=actions
        )
        return InterventionOutcome(
            resolution=signal.resolution,
            operator=signal.operator,
            notes=signal.notes,
            control_log=[{"at": started_at, "detail": _describe(a)} for a in actions],
            started_at=started_at,
            ended_at=ended_at,
        )

    def _wait_for_resume(self, resume_path: Path) -> ResumeSignal:
        t0 = time.monotonic()
        while True:
            elapsed = time.monotonic() - t0
            if resume_path.exists():
                try:
                    return ResumeSignal.model_validate_json(resume_path.read_text(encoding="utf-8"))
                except (ValidationError, OSError, ValueError) as ex:
                    self.recorder.event("escalation.bad_resume_signal", error=str(ex))
                    resume_path.unlink(missing_ok=True)
            if elapsed >= self.timeout_s:
                self.recorder.event("escalation.timeout", after_s=int(elapsed))
                return ResumeSignal(resolution="abort", operator="timeout", notes=f"no operator response within {int(self.timeout_s)}s")
            if self.on_wait is not None:
                self.on_wait(elapsed)
            # Yields to the driver's event loop, so the human-action recorder keeps receiving events.
            self.surface.wait_idle(self.poll_ms)

    # ------------------------------------------------------------- helpers
    def _banner(self, text: str | None) -> None:
        try:
            self.surface.set_banner(text)
        except SurfaceError:
            pass  # cosmetic; a page mid-navigation can refuse it

    def _announce(self, request: InterventionRequest, resume_path: Path) -> None:
        lines = [
            "",
            "=" * 78,
            "  HUMAN INTERVENTION REQUIRED — automation is paused; the browser window is yours.",
            f"  capability : {request.capability}",
            f"  step       : {request.step_id} ({request.phase}) {request.step_description}",
            f"  stopped on : {request.error_code} — {request.reason}",
            f"  page       : {request.title!r} {request.url}",
            f"  screenshot : {self.recorder.dir / request.screenshot}" if request.screenshot else "  screenshot : (none)",
            f"  details    : {self.recorder.dir / INTERVENTION_FILE}",
            "",
            "  When done, hand control back with ONE of:",
            f"    cua-resume --run {self.recorder.dir} --resolution retry_step    --operator <you>   # re-run the step",
            f"    cua-resume --run {self.recorder.dir} --resolution skip_step     --operator <you>   # you completed it",
            f"    cua-resume --run {self.recorder.dir} --resolution restart_phase --operator <you>   # re-run the phase",
            f"    cua-resume --run {self.recorder.dir} --resolution abort         --operator <you>   # give up",
            f"  (automation resumes automatically as abort after {int(self.timeout_s)}s)",
            "=" * 78,
            "",
        ]
        self.announce("\n".join(lines))


def write_resume_signal(run_dir: Path, resolution: Resolution, operator: str, notes: str = "") -> Path:
    """What `cua-resume` does: the operator's side of the protocol."""
    if not (run_dir / INTERVENTION_FILE).exists():
        raise FileNotFoundError(f"{run_dir} has no pending intervention ({INTERVENTION_FILE} not found)")
    path = run_dir / RESUME_FILE
    signal = ResumeSignal(resolution=resolution, operator=operator, notes=notes, signalled_at=_now())
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(signal.model_dump_json(), encoding="utf-8")
    tmp.replace(path)  # atomic: the waiter never sees a half-written file
    return path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _describe(action: dict[str, Any]) -> str:
    from cua.escalation.session import _format_action

    return _format_action(action)
