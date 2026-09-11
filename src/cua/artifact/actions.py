"""Action vocabulary shared by discovery, the artifact schema, policy, and replay."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ActionType = Literal["navigate", "click", "type", "select", "press", "extract"]
ACTION_TYPES: tuple[ActionType, ...] = ("navigate", "click", "type", "select", "press", "extract")

RiskLevel = Literal["safe", "risky"]


class ActionIntent(BaseModel):
    """
    An action about to be executed, in the form the policy gate evaluates.

    Produced by the discovery loop from a model tool call, and by the replay
    engine from an artifact step, so both paths are gated identically.
    """

    model_config = ConfigDict(extra="forbid")

    type: ActionType
    #: navigate: destination.
    url: str | None = None
    #: click/type/select/extract: the control's accessible name (or text) and role.
    control_name: str | None = None
    control_role: str | None = None
    #: type/select: a literal, or a {{secret:NAME}} / {{input:NAME}} reference.
    value: str | None = None
    #: press: key name, e.g. "Enter".
    key: str | None = None
    #: What the proposer believes. Can raise, never lower, the effective risk.
    declared_risk: RiskLevel | None = None
