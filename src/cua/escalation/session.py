"""
Who is in control of the live session.

A single ledger, shared by the engine, the surface, and the escalation
handler. The surface refuses automation actions while a human holds
control, so the two can never act at the same time; every transition is
timestamped and ends up in the run result as evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Holder = Literal["automation", "human"]


class ControlEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: str
    holder: Holder
    event: Literal["paused", "ceded", "human_action", "resumed"]
    detail: str = ""


class ControlViolation(Exception):
    """Automation tried to act while a human held the session."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionControl:
    def __init__(self) -> None:
        self.holder: Holder = "automation"
        self.events: list[ControlEvent] = []

    # -------------------------------------------------------------- guards
    def assert_automation(self, action: str) -> None:
        if self.holder != "automation":
            raise ControlViolation(f"automation attempted {action!r} while a human holds the session")

    @property
    def human_in_control(self) -> bool:
        return self.holder == "human"

    # --------------------------------------------------------- transitions
    def cede_to_human(self, reason: str) -> None:
        if self.holder == "human":
            raise ControlViolation("session is already held by a human")
        self.events.append(ControlEvent(at=_now(), holder="automation", event="paused", detail=reason))
        self.holder = "human"
        self.events.append(ControlEvent(at=_now(), holder="human", event="ceded", detail="live session handed to operator"))

    def record_human_action(self, detail: str | dict[str, Any]) -> None:
        if self.holder != "human":
            raise ControlViolation("human action recorded while automation holds the session")
        text = detail if isinstance(detail, str) else _format_action(detail)
        self.events.append(ControlEvent(at=_now(), holder="human", event="human_action", detail=text))

    def return_to_automation(self, operator: str, resolution: str, notes: str = "") -> None:
        if self.holder != "human":
            raise ControlViolation("session is not held by a human")
        self.holder = "automation"
        detail = f"operator={operator} resolution={resolution}" + (f" notes={notes!r}" if notes else "")
        self.events.append(ControlEvent(at=_now(), holder="automation", event="resumed", detail=detail))


def _format_action(a: dict[str, Any]) -> str:
    kind = str(a.get("kind", "action"))
    role = a.get("role") or a.get("tag") or "element"
    name = a.get("name")
    where = f'{role} "{name}"' if name else str(role)
    if kind == "change":
        if a.get("option"):
            return f"selected {a['option']!r} in {where}"
        return f"changed {where} (value not recorded, {a.get('value_length', 0)} chars)"
    if kind == "key":
        return f"pressed {a.get('key')} in {where}"
    if kind == "submit":
        return f"submitted form {where}"
    if kind == "navigated":
        return f"navigated to {a.get('url')}"
    return f"{kind} {where}"
