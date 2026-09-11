"""
Escalation contract: how automation hands a live session to a human and gets it back.

The replay engine (and, for risky actions, the discovery loop) calls an
EscalationHandler when it cannot safely proceed. The handler owns the pause,
the operator-facing intervention request, the transfer of control, and the
resume signal. The engine only sees the outcome and what to do next.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

Resolution = Literal["retry_step", "skip_step", "restart_phase", "abort"]


@dataclass
class InterventionRequest:
    run_id: str
    capability: str
    step_id: str
    step_description: str
    phase: str
    reason: str
    error_code: str
    url: str
    title: str
    screenshot: str | None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InterventionOutcome:
    resolution: Resolution
    operator: str
    notes: str = ""
    #: What the human did while in control, as recorded by the handler.
    control_log: list[dict[str, Any]] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""


class EscalationHandler(ABC):
    @abstractmethod
    def intervene(self, request: InterventionRequest) -> InterventionOutcome: ...
