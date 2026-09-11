"""
Discovery loop tests with a scripted model against the real surface and mock app.

They exercise the orchestration the real LLM run relies on: ref-based
actions, secret/input references, policy refusal fed back to the model,
risky-action confirmation, outcome containment, stopping conditions, and
the evidence written (including redaction).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cua.agent.loop import DiscoveryAgent, DiscoveryRequest
from cua.agent.providers.scripted import ScriptedProvider
from cua.agent.trace import Trace
from cua.evidence.recorder import RunRecorder
from cua.policy.gate import PolicyGate
from cua.policy.model import AllowlistPolicy, Policy, RiskPolicy, SecretsPolicy
from cua.policy.redaction import Redactor
from cua.policy.secrets import SecretStore
from cua.surface.playwright_surface import PlaywrightSurface

pytestmark = pytest.mark.browser

ENV = {"TARGET_USERNAME": "teller1", "TARGET_PASSWORD": "demo-pass-2024"}


@pytest.fixture(scope="module")
def surface() -> Iterator[PlaywrightSurface]:
    s = PlaywrightSurface(headless=True, default_timeout_ms=5_000)
    s.open()
    try:
        yield s
    finally:
        s.close()


def step(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def el(role: str, name: str | None = None) -> dict[str, str | None]:
    return {"role": role, "name": name}


LOGIN = [
    step("type", element=el("textbox", "Username"), text="{{secret:TARGET_USERNAME}}"),
    step("type", element=el("textbox", "Password"), text="{{secret:TARGET_PASSWORD}}"),
    step("click", element=el("button", "Sign In")),
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
            capability_name="test_cap",
            inputs=inputs or {},
            sensitive_inputs=sensitive_inputs,
            max_steps=max_steps,
        )
    )
    recorder.close()
    return trace, recorder.dir


# ------------------------------------------------------------------ happy path


def test_lookup_flow_extracts_balance_and_records_evidence(
    surface: PlaywrightSurface, mock_app: str, tmp_path: Path
) -> None:
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="{{input:member_id}}"),
        step("click", element=el("button", "Search")),
        step("extract", element=el("cell", "$4,210.55"), name="savings_balance", sensitive=True),
        step("done", summary="Read the savings balance for member 10001."),
    ]
    trace, run_dir = run_script(surface, mock_app, tmp_path, script, inputs={"member_id": "10001"})

    assert trace.status == "success"
    assert trace.outputs == {"savings_balance": "$4,210.55"}
    assert trace.sensitive_outputs == ["savings_balance"]
    assert [s.tool for s in trace.steps] == ["type", "type", "click", "type", "click", "extract", "done"]
    assert all(s.result.status == "ok" for s in trace.steps[:-1]) and trace.steps[-1].result.status == "done"

    # Element metadata captured for the artifact builder, before the action changed the page.
    username = trace.steps[0].element
    assert username and username.role == "textbox" and username.name == "Username" and username.css
    balance = trace.steps[5].element
    assert balance and balance.row_cells == ["Savings", "S-10001-01", "Primary Savings", "$4,210.55"]
    assert balance.cell_index == 3

    # Evidence files exist and are redacted.
    assert (run_dir / "run.jsonl").exists() and (run_dir / "trace.json").exists()
    transcript = (run_dir / "transcript.jsonl").read_text()
    assert transcript.count("\n") == 7
    assert "demo-pass-2024" not in transcript and "[REDACTED:secret:TARGET_PASSWORD]" in transcript
    assert "$4,210.55" not in transcript and "[REDACTED:savings_balance]" in transcript
    trace_json = (run_dir / "trace.json").read_text()
    assert "demo-pass-2024" not in trace_json and "$4,210.55" not in trace_json
    assert len(list((run_dir / "screenshots").glob("step-*.png"))) == 7


def test_sensitive_input_is_never_shown_to_the_model(surface: PlaywrightSurface, mock_app: str, tmp_path: Path) -> None:
    script = [*LOGIN, step("type", element=el("textbox", "Member ID"), text="{{input:member_id}}"), step("done", summary="x")]
    trace, run_dir = run_script(
        surface, mock_app, tmp_path, script, inputs={"member_id": "10002"}, sensitive_inputs=frozenset({"member_id"})
    )
    assert trace.status == "success"
    assert trace.inputs == {"member_id": "{{input:member_id}}"}
    transcript = (run_dir / "transcript.jsonl").read_text()
    assert "{{input:member_id}}" in transcript
    assert "10002" not in json.loads(transcript.splitlines()[3])["request"]["user_text"].split("Accessibility snapshot")[0]


# ---------------------------------------------------------------------- policy


def test_policy_refusal_is_fed_back_and_run_continues(surface: PlaywrightSurface, mock_app: str, tmp_path: Path) -> None:
    script = [*LOGIN, step("navigate", url=f"{mock_app}/__admin/inject"), step("done", summary="gave up on admin")]
    trace, run_dir = run_script(surface, mock_app, tmp_path, script)
    refused = trace.steps[3]
    assert refused.tool == "navigate" and refused.result.status == "refused"
    assert refused.decision and not refused.decision.allowed and "denied" in refused.decision.reason
    assert trace.status == "success"
    # The model saw the refusal in its next prompt.
    last_request = json.loads((run_dir / "transcript.jsonl").read_text().splitlines()[-1])["request"]["user_text"]
    assert "REFUSED by policy" in last_request
    assert any(json.loads(line)["kind"] == "policy.refused" for line in (run_dir / "run.jsonl").read_text().splitlines())


def test_risky_action_requires_confirmation_and_is_refused_without_a_channel(
    surface: PlaywrightSurface, mock_app: str, tmp_path: Path
) -> None:
    script = [
        *LOGIN,
        step("type", element=el("textbox", "Member ID"), text="10002"),
        step("click", element=el("button", "Search")),
        step("click", element=el("link", "Open Sub-Account")),
        step("type", element=el("textbox", "Nickname"), text="Bills"),
        step("type", element=el("textbox", "Initial Deposit"), text="25"),
        step("click", element=el("button", "Review")),
        step("click", element=el("button", "Confirm & Open Account")),
        step("done", summary="Reached the review screen; confirmation not permitted."),
    ]
    trace, _ = run_script(surface, mock_app, tmp_path, script)
    confirm = trace.steps[9]
    assert confirm.tool == "click" and confirm.result.status == "refused"
    assert confirm.decision and confirm.decision.risk == "risky" and confirm.decision.requires_confirmation
    assert "not confirmed" in confirm.result.detail
    assert trace.status == "success"
    assert surface.url().endswith("/members/10002/sub-accounts/review")  # nothing was committed


def test_action_leaving_the_allowlist_is_contained(surface: PlaywrightSurface, mock_app: str, tmp_path: Path) -> None:
    narrow = make_policy(mock_app, paths=["/login"])
    script = [*LOGIN, step("done", summary="x")]
    trace, _ = run_script(surface, mock_app, tmp_path, script, policy=narrow)
    sign_in = trace.steps[2]
    assert sign_in.result.status == "refused" and "outside the allowlist" in sign_in.result.detail
    assert sign_in.url_after.endswith("/login")


# ------------------------------------------------------------ stopping rules


def test_stuck_ends_the_run(surface: PlaywrightSurface, mock_app: str, tmp_path: Path) -> None:
    trace, _ = run_script(surface, mock_app, tmp_path, [{"tool": "stuck", "reason": "no way forward"}])
    assert trace.status == "stuck" and trace.summary == "no way forward"


def test_max_steps_ends_the_run(surface: PlaywrightSurface, mock_app: str, tmp_path: Path) -> None:
    script = [step("navigate", url=f"{mock_app}/login")] * 5
    trace, _ = run_script(surface, mock_app, tmp_path, script, max_steps=3)
    assert trace.status == "max_steps" and len(trace.steps) == 3


def test_unknown_ref_and_missing_element_are_reported_not_fatal(
    surface: PlaywrightSurface, mock_app: str, tmp_path: Path
) -> None:
    script = [step("click", ref="e999"), step("click", element=el("button", "Does Not Exist")), step("done", summary="x")]
    trace, _ = run_script(surface, mock_app, tmp_path, script)
    assert trace.steps[0].result.status == "error" and "e999" in trace.steps[0].result.detail
    assert trace.steps[1].tool == "stuck"  # the scripted model declares stuck when the element is absent
    assert trace.status == "stuck"
