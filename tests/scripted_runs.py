"""Shared helpers: run a scripted discovery against the mock app through the real surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cua.agent.loop import DiscoveryAgent, DiscoveryRequest
from cua.agent.providers.scripted import ScriptedProvider
from cua.agent.trace import Trace
from cua.evidence.recorder import RunRecorder
from cua.policy.gate import PolicyGate
from cua.policy.model import AllowlistPolicy, Policy, RiskPolicy, SecretsPolicy
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.surface.playwright_surface import PlaywrightSurface

ENV = {"TARGET_USERNAME": "teller1", "TARGET_PASSWORD": "demo-pass-2024"}


def step(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def el(role: str, name: str | None = None) -> dict[str, str | None]:
    return {"role": role, "name": name}


LOGIN = [
    step("type", element=el("textbox", "Username"), text="{{secret:TARGET_USERNAME}}", reason="Enter the staff username"),
    step("type", element=el("textbox", "Password"), text="{{secret:TARGET_PASSWORD}}", reason="Enter the staff password"),
    step("click", element=el("button", "Sign In"), reason="Sign in to the console"),
]


def make_policy(base: str, paths: list[str] | None = None, mode: str = "confirm") -> Policy:
    return Policy(
        name="test",
        allowlist=AllowlistPolicy(
            origins=[base],
            path_patterns=paths or ["/", "/login", "/logout", "/members/**", "/notice/**"],
            denied_path_patterns=["/__admin/**"],
        ),
        risk=RiskPolicy(mode=mode),  # type: ignore[arg-type]
        secrets=SecretsPolicy(allowed=list(ENV)),
    )


def run_script(
    surface: PlaywrightSurface,
    base: str,
    tmp_path: Path,
    script: list[dict[str, Any]],
    *,
    goal: str = "test goal",
    inputs: dict[str, str] | None = None,
    sensitive_inputs: frozenset[str] = frozenset(),
    policy: Policy | None = None,
    max_steps: int = 20,
    capability_name: str = "test_cap",
) -> tuple[Trace, Path]:
    policy = policy or make_policy(base)
    secrets = SecretStore(policy.secrets.allowed, env=ENV)
    redactor = Redactor(policy.redaction.patterns, secrets=secrets.known_values())
    recorder = RunRecorder(tmp_path, "discovery", redactor=redactor)
    agent = DiscoveryAgent(
        surface=surface,
        llm=ScriptedProvider(script),
        gate=PolicyGate(policy),
        secrets=secrets,
        recorder=recorder,
        redactor=redactor,
    )
    trace = agent.run(
        DiscoveryRequest(
            goal=goal,
            start_url=f"{base}/login",
            capability_name=capability_name,
            inputs=inputs or {},
            sensitive_inputs=sensitive_inputs,
            max_steps=max_steps,
        )
    )
    recorder.close()
    return trace, recorder.dir
