"""
The policy gate: the single choke point every action passes through.

Discovery calls it with the model's proposed action; replay calls it with
the artifact step. Neither the model nor the artifact can reach the
surface without a Decision from here, which is why safety does not depend
on the prompt.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from cua.artifact.actions import ActionIntent, RiskLevel

from .model import Policy, glob_to_regex
from .secrets import secret_names_in


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    risk: RiskLevel = "safe"
    #: True when the action is risky and policy mode is "confirm": the executor must obtain
    #: confirmation (a human, or an explicit pre-approval for this replay) before acting.
    requires_confirmation: bool = False


class PolicyViolation(Exception):
    def __init__(self, decision: Decision, intent: ActionIntent) -> None:
        super().__init__(f"policy refused {intent.type}: {decision.reason}")
        self.decision = decision
        self.intent = intent


class PolicyGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._risky_controls = [re.compile(p, re.IGNORECASE) for p in policy.risk.risky_control_patterns]
        self._risky_urls = [glob_to_regex(p) for p in policy.risk.risky_url_patterns]

    # ------------------------------------------------------------- decisions
    def check_action(self, intent: ActionIntent, *, current_url: str) -> Decision:
        allowlist = self.policy.allowlist
        if intent.type not in allowlist.action_types:
            return Decision(allowed=False, reason=f"action type {intent.type!r} is not allowed")

        if intent.type == "navigate":
            if not intent.url:
                return Decision(allowed=False, reason="navigate requires a url")
            if secret_names_in(intent.url):
                return Decision(allowed=False, reason="secret references are not permitted in URLs")
            ok, reason = self.policy.allows_url(intent.url)
            if not ok:
                return Decision(allowed=False, reason=reason)

        if intent.value:
            for name in secret_names_in(intent.value):
                if name not in self.policy.secrets.allowed:
                    return Decision(allowed=False, reason=f"secret {name!r} is not allowed for this capability")

        risk = self.classify_risk(intent, current_url=current_url)
        if risk == "safe":
            return Decision(allowed=True, reason="safe action within allowlist", risk="safe")
        mode = self.policy.risk.mode
        if mode == "block":
            return Decision(allowed=False, reason="risky action blocked by policy (mode=block)", risk="risky")
        if mode == "confirm":
            return Decision(
                allowed=True,
                reason="risky action requires confirmation (mode=confirm)",
                risk="risky",
                requires_confirmation=True,
            )
        return Decision(allowed=True, reason="risky action allowed by policy (mode=allow)", risk="risky")

    def check_url(self, url: str) -> Decision:
        """Post-action guard: a click can navigate just as a navigate can."""
        ok, reason = self.policy.allows_url(url)
        return Decision(allowed=ok, reason=reason)

    def classify_risk(self, intent: ActionIntent, *, current_url: str) -> RiskLevel:
        """Policy rules can raise risk; a proposer's declared_risk can raise it too, never lower it."""
        if intent.declared_risk == "risky":
            return "risky"
        if (
            intent.type in ("click", "press", "select", "type")
            and intent.control_name
            and any(p.search(intent.control_name) for p in self._risky_controls)
        ):
            return "risky"
        url_for_rule = intent.url if intent.type == "navigate" else current_url
        if url_for_rule and intent.type in ("navigate", "click", "press"):
            path = urlparse(url_for_rule).path or "/"
            if any(p.match(path) for p in self._risky_urls):
                return "risky"
        return "safe"

    # ------------------------------------------------------------ enforcement
    def enforce(self, intent: ActionIntent, *, current_url: str) -> Decision:
        decision = self.check_action(intent, current_url=current_url)
        if not decision.allowed:
            raise PolicyViolation(decision, intent)
        return decision

    def enforce_url(self, url: str) -> Decision:
        decision = self.check_url(url)
        if not decision.allowed:
            raise PolicyViolation(decision, ActionIntent(type="navigate", url=url))
        return decision
